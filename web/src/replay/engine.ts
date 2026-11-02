// Replay layer: answers /api/v1 requests and job event streams from
// recording.json, which scripts/record_replay.py captured from the real backend.
// Nothing here computes analysis results; anything that was not recorded is
// refused with an explicit message instead of being invented.
import recording from "./recording.json";

type Json = Record<string, any>;
type Sample = (typeof recording.samples)[number];

const pick = (): Sample[] => recording.samples as Sample[];
// PURE markers let a normal (non-replay) build drop the recording entirely.
const samples: Sample[] = /* @__PURE__ */ pick();
export const replaySamples = /* @__PURE__ */ samples.map((s) => ({ id: s.id, label: s.label, filename: s.filename, text: s.text, ui_mapping: s.ui_mapping }));

const STARTED_KEY = "signalbench-replay-runs";
const lastEvent = /* @__PURE__ */ new Map<string, Json>();
const playing = /* @__PURE__ */ new Set<string>();

function startedRuns(): string[] {
  try { return JSON.parse(sessionStorage.getItem(STARTED_KEY) ?? "[]"); } catch { return []; }
}
function rememberRun(jobId: string) {
  try { sessionStorage.setItem(STARTED_KEY, JSON.stringify([jobId, ...startedRuns().filter((id) => id !== jobId)])); } catch { /* storage may be blocked */ }
}

class Unavailable {
  constructor(public message: string) {}
}

const NOT_RECORDED = "回放版只提供录制时走过的操作；这个筛选、排序或分页组合没有录制，请换一个条件，或在本地运行后端自行分析。";
const NO_WRITE = "回放版是只读的：不能保存复核、纳入/排除或批量处理。请在本地运行后端使用这个功能。";

const canonical = (params: URLSearchParams) =>
  new URLSearchParams([...params.entries()].filter(([k, v]) => k !== "cursor" && v !== "").sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))).toString();

const sameJson = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

function sampleBy(pick: (s: Sample) => boolean, what: string): Sample {
  const found = samples.find(pick);
  if (!found) throw new Unavailable(`回放版里没有这个${what}；它只包含示例文件的录制结果。`);
  return found;
}

function lookup(table: Record<string, unknown>, params: URLSearchParams) {
  if (params.get("search")?.trim()) throw new Unavailable("回放版不能搜索文本：搜索需要现场查询后端，在线版没有后端。");
  if (params.get("source")?.trim()) throw new Unavailable("回放版不能按来源文本筛选：需要现场查询后端，在线版没有后端。");
  if (params.get("date_from") || params.get("date_to")) throw new Unavailable("回放版不能按任意日期范围筛选：只录制了固定条件的结果。");
  const hit = table[canonical(params)];
  if (!hit) throw new Unavailable(NOT_RECORDED);
  return hit;
}

async function textOf(files: File[]) {
  return Promise.all(files.map(async (file) => ({ name: file.name, text: await file.text() })));
}

async function route(method: string, url: URL, init?: RequestInit): Promise<{ status: number; body: unknown }> {
  const path = url.pathname.replace(/^.*\/api\/v1/, "");
  const params = url.searchParams;
  const headers = new Headers(init?.headers);
  const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
  let m: RegExpMatchArray | null;

  if (method === "POST" && path === "/imports") {
    const files = (init!.body as FormData).getAll("files") as File[];
    const given = await textOf(files);
    const sample = given.length === 1 ? samples.find((s) => s.filename === given[0].name && s.text === given[0].text) : undefined;
    if (!sample) throw new Unavailable("回放版不能处理你选择的文件：在线版没有后端，只内置了示例文件的录制结果。请点“使用示例数据”，或在本地运行后端上传自己的文件。");
    return { status: 201, body: sample.import };
  }
  if (method === "PATCH" && (m = path.match(/^\/imports\/([^/]+)\/mapping$/))) {
    const sample = sampleBy((s) => s.import.import_id === m![1], "导入");
    if (!sameJson(body, sample.mapping.request) || headers.get("If-Match") !== sample.mapping.if_match) throw new Unavailable("回放版只提供录制时使用的字段映射。请使用示例数据预填好的映射，不要修改。");
    return { status: 200, body: sample.mapping.response };
  }
  if (method === "POST" && path === "/batches") {
    const sample = sampleBy((s) => s.batch.request.import_id === body?.import_id, "导入");
    if (!sameJson(body, sample.batch.request)) throw new Unavailable("回放版里的导入版本与录制不一致，请重新载入示例数据。");
    return { status: 201, body: sample.batch.response };
  }
  if (method === "POST" && path === "/analysis-runs") {
    const sample = sampleBy((s) => s.run.request.batch_id === body?.batch_id, "批次");
    rememberRun(sample.run.response.job_id);
    playing.add(sample.run.response.job_id);
    lastEvent.delete(sample.run.response.job_id);
    return { status: 202, body: sample.run.response };
  }
  if (method === "GET" && path === "/analysis-runs") {
    const started = startedRuns();
    return { status: 200, body: { runs: started.map((id) => recording.runs.find((r) => r.job_id === id)).filter(Boolean) } };
  }
  if (method === "GET" && (m = path.match(/^\/batches\/([^/]+)\/records$/))) {
    const sample = sampleBy((s) => s.batch.response.batch_id === m![1], "批次");
    return { status: 200, body: lookup(sample.quality, params) };
  }
  if (path.startsWith("/batches/") && path.includes("/reviews")) throw new Unavailable(NO_WRITE);
  if (method === "GET" && (m = path.match(/^\/jobs\/([^/]+)$/))) {
    const sample = sampleBy((s) => s.run.response.job_id === m![1], "任务");
    const event = lastEvent.get(sample.run.response.job_id);
    if (!playing.has(sample.run.response.job_id)) return { status: 200, body: sample.run.final_job };
    if (!event) return { status: 200, body: sample.run.response };
    const payload = event.payload as Json;
    return { status: 200, body: { ...sample.run.response, status: event.kind, progress: { ...sample.run.response.progress, done: payload.done ?? 0, total: payload.total ?? sample.run.response.progress.total }, error: payload.error ?? null } };
  }
  if (method === "POST" && path.match(/^\/jobs\/[^/]+\/(pause|resume|cancel)$/)) throw new Unavailable("回放版里的任务已经结束，不能暂停、继续或取消。");
  if (method === "GET" && (m = path.match(/^\/analysis-runs\/([^/]+)\/summary$/))) {
    return { status: 200, body: sampleBy((s) => s.run.response.job_id === m![1], "任务").run.summary };
  }
  if (method === "GET" && (m = path.match(/^\/analysis-runs\/([^/]+)\/records$/))) {
    const sample = sampleBy((s) => s.run.response.job_id === m![1], "任务");
    return { status: 200, body: lookup(sample.run.results, params) };
  }
  if (method === "PATCH" && path.match(/^\/analysis-runs\/[^/]+\/reviews$/)) throw new Unavailable(NO_WRITE);
  if (method === "POST" && (m = path.match(/^\/analysis-runs\/([^/]+)\/exports$/))) {
    const sample = sampleBy((s) => s.run.response.job_id === m![1], "任务");
    const filtered = ["topic", "sentiment", "review_status", "search", "date_from", "date_to"].some((k) => body?.[k]);
    const hit = (sample.run.exports as Record<string, any>)[`${body?.scope}:${body?.format}`];
    if (filtered || !hit || body?.sort !== "source" || body?.order !== "asc") throw new Unavailable("回放版只提供未筛选、按来源排序的两份导出：共享汇总 HTML 与含原文明细 CSV。请先点“清除筛选”。");
    return { status: 201, body: hit.artifact };
  }
  if (method === "POST" && path === "/comparisons") {
    const hit = (recording.comparisons as Record<string, unknown>)[`${body?.reference_run}|${body?.current_run}`];
    if (!hit) throw new Unavailable("回放版只录制了两份示例数据互为参考的比较。请先分别分析两份示例数据，再选择另一份作参考。");
    return { status: 200, body: hit };
  }
  throw new Unavailable("回放版不支持这个操作：在线版没有后端。");
}

function install() {
  const realFetch = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(raw, window.location.href);
    if (!url.pathname.includes("/api/v1/")) return realFetch(input, init);
    const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
    try {
      const { status, body } = await route(method, url, init);
      return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    } catch (error) {
      if (!(error instanceof Unavailable)) throw error;
      return new Response(JSON.stringify({ detail: { error: { code: "REPLAY_UNAVAILABLE", message: error.message, retryable: false } } }), { status: 501, headers: { "Content-Type": "application/json" } });
    }
  };

  class ReplayEventSource extends EventTarget {
    static readonly CONNECTING = 0; static readonly OPEN = 1; static readonly CLOSED = 2;
    readonly url: string; readonly withCredentials = false;
    readyState = 1; onopen: ((e: Event) => void) | null = null; onmessage: ((e: MessageEvent) => void) | null = null; onerror: ((e: Event) => void) | null = null;
    private timers: number[] = [];
    constructor(url: string | URL) {
      super();
      this.url = String(url);
      const jobId = new URL(this.url, window.location.href).pathname.split("/").slice(-2)[0];
      const sample = samples.find((s) => s.run.response.job_id === jobId);
      sample?.run.events.forEach((entry, index) => {
        this.timers.push(window.setTimeout(() => {
          if (this.readyState === 2) return;
          const data = entry.data as Json;
          lastEvent.set(jobId, data);
          if (index === sample.run.events.length - 1) playing.delete(jobId);
          this.dispatchEvent(new MessageEvent(entry.event ?? "message", { data: JSON.stringify(data), lastEventId: String(data.seq) }));
        }, 180 * (index + 1)));
      });
    }
    close() { this.readyState = 2; this.timers.forEach(clearTimeout); }
  }
  (window as any).EventSource = ReplayEventSource;

  // Recorded artifacts download as blobs instead of navigating to /api/v1.
  document.addEventListener("click", (event) => {
    const link = (event.target as Element | null)?.closest?.("a");
    const m = link?.getAttribute("href")?.match(/\/api\/v1\/artifacts\/([^/]+)\/download$/);
    if (!m) return;
    event.preventDefault();
    for (const sample of samples) {
      for (const entry of Object.values(sample.run.exports as Record<string, any>)) {
        if (entry.artifact.artifact_id !== m[1]) continue;
        const bytes = Uint8Array.from(atob(entry.base64), (c) => c.charCodeAt(0));
        const href = URL.createObjectURL(new Blob([bytes], { type: entry.media_type }));
        const anchor = document.createElement("a");
        anchor.href = href; anchor.download = entry.artifact.filename;
        document.body.appendChild(anchor); anchor.click(); anchor.remove();
        setTimeout(() => URL.revokeObjectURL(href), 1000);
      }
    }
  }, true);
}

export const installReplay = install;
