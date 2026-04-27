import { ChangeEvent, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type Sheet = { asset_id: string; source_sha256: string; sheet: string; columns: string[]; preview_rows: Record<string, string>[] };
type Imported = { import_id: string; revision: number; sheets: Sheet[]; errors?: Array<{ display_name: string; message: string }> };
type Mapping = { text: string; platform: string; author: string; record_id: string; created_at: string; date_format: string };
type RecordRow = { record_id: string; text_raw: string; platform: string; source_sheet: string; source_row: number; quality_label: string; quality_reason: string; duplicate_group: string; duplicate_rank: number; duplicate_count: number; effective_include: boolean; review_revision: number; include_override: string | null; review_reason: string; occurred_at: string | null; date_parse_status: string };
type Job = { job_id: string; status: string; capabilities: { pause: boolean; resume: boolean }; progress: { done: number; total: number; unit: string }; error?: string };
type ResultRow = { record_id: string; text_raw: string; platform: string; source_sheet: string; source_row: number; algorithm_sentiment: string; algorithm_topic: string; sentiment: string; topic: string; decision_note: string; rule_hits: string[]; review_reason: string; review_revision: number; occurred_at: string | null; date_parse_status: string };
type RunItem = { job_id: string; batch_id: string; status: string; done: number; total: number };
type Direction = "asc" | "desc";
type QualityFilter = { quality: string; include: string; source?: string; search?: string; sort: string; order: Direction; limit: number; cursor?: string };
type ResultFilter = { topic?: string; sentiment?: string; review_status?: string; date_from?: string; date_to?: string; search?: string; sort: string; order: Direction; limit: number; cursor?: string };
type PageInfo = { filtered_total: number; offset: number; limit: number; page_number: number; page_count: number; cursor: string | null; previous_cursor: string | null; next_cursor: string | null };
type QualityResponse = PageInfo & { rows: RecordRow[]; batch_total: number; included_total: number; excluded_total: number; pending_total: number; review_revision: number };
type ResultResponse = PageInfo & { rows: ResultRow[]; review_revision: number; date_filter_available: boolean; unparsed_date_count: number };
type Distribution = { count: number; shares: Record<string, number> | null };
type Comparison = {
  reference_count: number;
  current_count: number;
  reliable: boolean;
  limitations: string[];
  sentiment: { reference: Distribution; current: Distribution };
  topics: { reference: Distribution; current: Distribution };
  structure: { metrics: Array<{ column: string; kind: string; distribution_score: number | null; missingness_delta: number; comparable: boolean; reason: string | null }> };
};

const labels: Record<keyof Mapping, string> = { text: "反馈正文", platform: "来源", author: "作者关联", record_id: "原始编号", created_at: "日期", date_format: "日期格式" };
const limitationLabels: Record<string, string> = { sample_lt_30: "任一批次少于 30 条，仅展示差异，不判断趋势", empty_analysis_distribution: "至少一个批次没有可比较的分析记录" };

function DistributionComparison({ title, value }: { title: string; value: { reference: Distribution; current: Distribution } }) {
  const keys = [...new Set([...Object.keys(value.reference.shares ?? {}), ...Object.keys(value.current.shares ?? {})])].sort();
  return <section className="distribution"><h4>{title}</h4>{keys.length ? keys.map((key) => <div key={key}><span>{key}</span><span>参考 {Math.round((value.reference.shares?.[key] ?? 0) * 100)}%</span><span>当前 {Math.round((value.current.shares?.[key] ?? 0) * 100)}%</span></div>) : <p>没有可比较的分布。</p>}</section>;
}

function Pagination({ page, onMove }: { page: PageInfo; onMove: (cursor: string | undefined) => void }) {
  return <div className="pagination" aria-label="分页">
    <button disabled={page.offset === 0} onClick={() => onMove(page.previous_cursor ?? undefined)}>上一页</button>
    <span>第 {page.page_number} / {page.page_count} 页 · 共 {page.filtered_total.toLocaleString()} 条</span>
    <button disabled={!page.next_cursor} onClick={() => onMove(page.next_cursor ?? undefined)}>下一页</button>
  </div>;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, init);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload?.detail?.error?.message || payload?.error?.message || "请求失败");
  return payload as T;
}

function emptyMapping(): Mapping { return { text: "", platform: "", author: "", record_id: "", created_at: "", date_format: "yyyy-mm-dd" }; }
function sheetKey(sheet: Sheet): string { return `${sheet.asset_id}:${sheet.sheet}`; }

const DEFAULT_PAGE_SIZE = 50;

function readQualityFilter(params = new URLSearchParams(window.location.search)): QualityFilter {
  const limit = Number(params.get("limit"));
  return {
    quality: params.get("quality") ?? "all",
    include: params.get("include") ?? "all",
    source: params.get("source") ?? undefined,
    search: params.get("q") ?? undefined,
    sort: params.get("qsort") ?? "position",
    order: params.get("qorder") === "desc" ? "desc" : "asc",
    limit: Number.isInteger(limit) && limit >= 1 && limit <= 200 ? limit : DEFAULT_PAGE_SIZE,
    cursor: params.get("qcursor") ?? undefined,
  };
}

function readResultFilter(params = new URLSearchParams(window.location.search)): ResultFilter {
  const limit = Number(params.get("limit"));
  return {
    topic: params.get("topic") ?? undefined,
    sentiment: params.get("sentiment") ?? undefined,
    review_status: params.get("review_status") ?? undefined,
    date_from: params.get("date_from") ?? undefined,
    date_to: params.get("date_to") ?? undefined,
    search: params.get("search") ?? undefined,
    sort: params.get("sort") ?? "source",
    order: params.get("order") === "desc" ? "desc" : "asc",
    limit: Number.isInteger(limit) && limit >= 1 && limit <= 200 ? limit : DEFAULT_PAGE_SIZE,
    cursor: params.get("cursor") ?? undefined,
  };
}

function qualityApiQuery(filter: QualityFilter): string {
  const params = new URLSearchParams({ sort: filter.sort, order: filter.order, limit: String(filter.limit) });
  if (filter.quality !== "all") params.set("quality", filter.quality);
  if (filter.include !== "all") params.set("include", filter.include);
  if (filter.source?.trim()) params.set("source", filter.source.trim());
  if (filter.search?.trim()) params.set("search", filter.search.trim());
  if (filter.cursor) params.set("cursor", filter.cursor);
  return params.toString();
}

function resultApiQuery(filter: ResultFilter): string {
  const params = new URLSearchParams({ sort: filter.sort, order: filter.order, limit: String(filter.limit) });
  for (const key of ["topic", "sentiment", "review_status", "date_from", "date_to", "search", "cursor"] as const) {
    const value = filter[key];
    if (value?.trim()) params.set(key, value.trim());
  }
  return params.toString();
}

function qualityUrlParams(batchId: string, filter: QualityFilter, jobId?: string): URLSearchParams {
  const params = new URLSearchParams({ view: "quality", batch: batchId });
  if (jobId) params.set("job", jobId);
  if (filter.quality !== "all") params.set("quality", filter.quality);
  if (filter.include !== "all") params.set("include", filter.include);
  if (filter.source?.trim()) params.set("source", filter.source.trim());
  if (filter.search?.trim()) params.set("q", filter.search.trim());
  if (filter.sort !== "position") params.set("qsort", filter.sort);
  if (filter.order !== "asc") params.set("qorder", filter.order);
  if (filter.limit !== DEFAULT_PAGE_SIZE) params.set("limit", String(filter.limit));
  if (filter.cursor) params.set("qcursor", filter.cursor);
  return params;
}

function resultUrlParams(runId: string, batchId: string | undefined, filter: ResultFilter): URLSearchParams {
  const params = new URLSearchParams({ view: "results", run: runId });
  if (batchId) params.set("batch", batchId);
  if (filter.topic) params.set("topic", filter.topic);
  if (filter.sentiment) params.set("sentiment", filter.sentiment);
  if (filter.review_status) params.set("review_status", filter.review_status);
  if (filter.date_from) params.set("date_from", filter.date_from);
  if (filter.date_to) params.set("date_to", filter.date_to);
  if (filter.search?.trim()) params.set("search", filter.search.trim());
  if (filter.sort !== "source") params.set("sort", filter.sort);
  if (filter.order !== "asc") params.set("order", filter.order);
  if (filter.limit !== DEFAULT_PAGE_SIZE) params.set("limit", String(filter.limit));
  if (filter.cursor) params.set("cursor", filter.cursor);
  return params;
}

function writeLocation(params: URLSearchParams, mode: "push" | "replace" = "replace") {
  const query = params.toString();
  const next = `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`;
  const method = mode === "push" ? "pushState" : "replaceState";
  window.history[method]({}, "", next);
}

const EMPTY_PAGE: PageInfo = { filtered_total: 0, offset: 0, limit: DEFAULT_PAGE_SIZE, page_number: 1, page_count: 1, cursor: null, previous_cursor: null, next_cursor: null };

function App() {
  const [imported, setImported] = useState<Imported>();
  const [selected, setSelected] = useState<Sheet>();
  const [sheetMappings, setSheetMappings] = useState<Record<string, Mapping>>({});
  const [includedSheets, setIncludedSheets] = useState<string[]>([]);
  const [batchId, setBatchId] = useState<string | undefined>(() => new URLSearchParams(window.location.search).get("batch") ?? undefined);
  const [records, setRecords] = useState<RecordRow[]>([]);
  const [qualityPage, setQualityPage] = useState<PageInfo>(EMPTY_PAGE);
  const [qualityTotals, setQualityTotals] = useState({ batch_total: 0, included_total: 0, excluded_total: 0, pending_total: 0 });
  const [reviewRevision, setReviewRevision] = useState(0);
  const [reviewTarget, setReviewTarget] = useState<RecordRow>();
  const [reviewReason, setReviewReason] = useState("");
  const [qualityQuery, setQualityQuery] = useState<QualityFilter>(() => readQualityFilter());
  const [job, setJob] = useState<Job>();
  const [autoOpenRunId, setAutoOpenRunId] = useState<string>();
  const [summary, setSummary] = useState<Array<Record<string, unknown>>>([]);
  const [resultRows, setResultRows] = useState<ResultRow[]>([]);
  const [resultPage, setResultPage] = useState<PageInfo>(EMPTY_PAGE);
  const [resultFilter, setResultFilter] = useState<ResultFilter>(() => readResultFilter());
  const [dateFilterAvailable, setDateFilterAvailable] = useState(false);
  const [unparsedDateCount, setUnparsedDateCount] = useState(0);
  const [runs, setRuns] = useState<RunItem[]>([]);
  const [referenceRun, setReferenceRun] = useState("");
  const [comparison, setComparison] = useState<Comparison>();
  const [labelTarget, setLabelTarget] = useState<ResultRow>();
  const [labelSentiment, setLabelSentiment] = useState("");
  const [labelTopic, setLabelTopic] = useState("");
  const [labelReason, setLabelReason] = useState("");
  const [runReviewRevision, setRunReviewRevision] = useState(0);
  const [message, setMessage] = useState("选择一个本地表格，先确认每一列的用途。");
  // Upload and explicit field mapping are one product step; a file has not
  // become a quality batch merely because its sheet names are visible.
  const stage = summary.length ? 3 : job || batchId ? 2 : 0;

  const mapping = selected ? sheetMappings[sheetKey(selected)] ?? emptyMapping() : emptyMapping();
  const update = (field: keyof Mapping, value: string) => {
    if (!selected) return;
    const key = sheetKey(selected);
    setSheetMappings((current) => ({ ...current, [key]: { ...(current[key] ?? emptyMapping()), [field]: value } }));
  };
  const columns = selected?.columns ?? [];
  const sourcePreview = selected?.preview_rows.slice(0, 20) ?? [];
  const canMap = Boolean(includedSheets.length && imported && includedSheets.every((key) => sheetMappings[key]?.text));

  async function loadQuality(targetBatchId: string, filter: QualityFilter) {
    const query = qualityApiQuery(filter);
    const value = await api<QualityResponse>(`/batches/${targetBatchId}/records?${query}`);
    setRecords(value.rows);
    setQualityPage(value);
    setQualityTotals({ batch_total: value.batch_total, included_total: value.included_total, excluded_total: value.excluded_total, pending_total: value.pending_total });
    setReviewRevision(value.review_revision);
    return value;
  }

  async function loadResultPage(runId: string, filter: ResultFilter) {
    const query = resultApiQuery(filter);
    const value = await api<ResultResponse>(`/analysis-runs/${runId}/records?${query}`);
    setResultRows(value.rows);
    setResultPage(value);
    setRunReviewRevision(value.review_revision);
    setDateFilterAvailable(value.date_filter_available);
    setUnparsedDateCount(value.unparsed_date_count);
    return value;
  }

  async function restoreRun(runId: string, knownBatchId?: string, filter: ResultFilter = resultFilter) {
    try {
      setAutoOpenRunId(undefined);
      const [nextJob, runList] = await Promise.all([
        api<Job>(`/jobs/${runId}`),
        api<{ runs: RunItem[] }>("/analysis-runs"),
      ]);
      const resolvedBatchId = knownBatchId ?? runList.runs.find((item) => item.job_id === runId)?.batch_id;
      if (!resolvedBatchId) throw new Error("找不到这次分析对应的批次。");
      setBatchId(resolvedBatchId);
      setRuns(runList.runs);
      await loadQuality(resolvedBatchId, qualityQuery);
      if (nextJob.status === "succeeded") {
        const [summaryValue, rowsValue] = await Promise.all([
          api<{ summary: Array<Record<string, unknown>> }>(`/analysis-runs/${runId}/summary`),
          loadResultPage(runId, filter),
        ]);
        setSummary(summaryValue.summary);
        const restoredFilter = { ...filter, cursor: rowsValue.cursor ?? undefined };
        setResultFilter(restoredFilter);
        setJob(nextJob);
        writeLocation(resultUrlParams(runId, resolvedBatchId, restoredFilter));
        setMessage("已恢复分析结果；筛选、排序和页码来自当前地址。");
      } else {
        setSummary([]);
        setJob(nextJob);
        writeLocation(qualityUrlParams(resolvedBatchId, qualityQuery, runId));
        setMessage("已恢复批次与任务状态；分析尚未完成。");
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "无法恢复运行");
    }
  }

  async function restoreJob(jobId: string, targetBatchId: string, filter: QualityFilter = qualityQuery) {
    try {
      const nextJob = await api<Job>(`/jobs/${jobId}`);
      setBatchId(targetBatchId);
      setSummary([]);
      setAutoOpenRunId(undefined);
      setJob(nextJob);
      await loadQuality(targetBatchId, filter);
      writeLocation(qualityUrlParams(targetBatchId, filter, jobId));
      setMessage(nextJob.status === "succeeded" ? "已恢复批次与完成任务；结果可从上方进入。" : "已恢复任务状态；可继续、取消或查看当前批次。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "无法恢复任务");
    }
  }

  useEffect(() => {
    api<{ runs: RunItem[] }>("/analysis-runs").then((value) => setRuns(value.runs)).catch(() => undefined);
    const params = new URLSearchParams(window.location.search);
    const initialRunId = params.get("run");
    const initialJobId = params.get("job");
    const initialBatchId = params.get("batch");
    if (initialRunId) void restoreRun(initialRunId, initialBatchId ?? undefined, readResultFilter(params));
    else if (initialJobId && initialBatchId) {
      void restoreJob(initialJobId, initialBatchId, readQualityFilter(params));
    } else if (initialBatchId) {
      void loadQuality(initialBatchId, readQualityFilter(params)).catch((error) => setMessage(error instanceof Error ? error.message : "无法恢复质量批次"));
    }
    const onPopState = () => {
      const current = new URLSearchParams(window.location.search);
      const runId = current.get("run");
      const jobId = current.get("job");
      const batch = current.get("batch");
      if (runId) {
        const filter = readResultFilter(current);
        setSummary([]);
        setResultFilter(filter);
        void restoreRun(runId, batch ?? undefined, filter);
      } else if (batch) {
        const filter = readQualityFilter(current);
        setQualityQuery(filter);
        if (jobId) void restoreJob(jobId, batch, filter);
        else {
          setSummary([]);
          setBatchId(batch);
          void loadQuality(batch, filter).catch((error) => setMessage(error instanceof Error ? error.message : "无法恢复质量批次"));
        }
      } else {
        setSummary([]);
        setBatchId(undefined);
        setJob(undefined);
        setMessage("选择一个本地表格，先确认每一列的用途。");
      }
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  async function openRun(run: RunItem) {
    const filter: ResultFilter = { sort: "source", order: "asc", limit: DEFAULT_PAGE_SIZE };
    setSummary([]); setResultFilter(filter); setComparison(undefined);
    await restoreRun(run.job_id, run.batch_id, filter);
  }

  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const files = [...(event.target.files ?? [])];
    if (!files.length) return;
    setMessage("正在读取文件和工作表结构…");
    try {
      const form = new FormData(); files.forEach((file) => form.append("files", file));
      const next = await api<Imported>("/imports", { method: "POST", body: form });
      setImported(next); setSelected(next.sheets[0]); setSheetMappings({}); setIncludedSheets([]); setBatchId(undefined); setRecords([]); setSummary([]);
      setQualityQuery({ quality: "all", include: "all", sort: "position", order: "asc", limit: DEFAULT_PAGE_SIZE });
      setResultFilter({ sort: "source", order: "asc", limit: DEFAULT_PAGE_SIZE });
      writeLocation(new URLSearchParams({ view: "import" }));
      setMessage(`已读取 ${next.sheets.length} 个工作表${next.errors?.length ? `，${next.errors.length} 个文件失败` : ""}。请选择真正含反馈正文的表。`);
    } catch (error) { setMessage(error instanceof Error ? error.message : "导入失败"); }
  }

  async function createBatch() {
    if (!imported || !canMap) return;
    setMessage("正在保存映射并生成质量批次…");
    try {
      const selectedSheets = imported.sheets.filter((sheet) => includedSheets.includes(sheetKey(sheet)));
      const sheets = selectedSheets.map((sheet) => {
        const selectedMapping = sheetMappings[sheetKey(sheet)];
        const columnsPayload = Object.fromEntries(Object.entries(selectedMapping).filter(([field, value]) => field !== "date_format" && value).map(([field, value]) => [field, value]));
        return { asset_id: sheet.asset_id, source_sha256: sheet.source_sha256, sheet: sheet.sheet, columns: columnsPayload, date_format: selectedMapping.created_at ? selectedMapping.date_format : null };
      });
      const mapped = await api<{ revision: number }>(`/imports/${imported.import_id}/mapping`, { method: "PATCH", headers: { "Content-Type": "application/json", "If-Match": String(imported.revision) }, body: JSON.stringify({ sheets }) });
      const batch = await api<{ batch_id: string }>("/batches", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ import_id: imported.import_id, revision: mapped.revision }) });
      const filter: QualityFilter = { quality: "all", include: "all", sort: "position", order: "asc", limit: DEFAULT_PAGE_SIZE };
      setBatchId(batch.batch_id); setQualityQuery(filter);
      const quality = await loadQuality(batch.batch_id, filter);
      writeLocation(qualityUrlParams(batch.batch_id, { ...filter, cursor: quality.cursor ?? undefined }));
      setMessage("质量批次已创建。先检查原文、重复和排除原因，再开始分析。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "创建批次失败"); }
  }

  async function runAnalysis() {
    if (!batchId) return;
    try {
      const initialFilter: ResultFilter = { sort: "source", order: "asc", limit: DEFAULT_PAGE_SIZE };
      setResultFilter(initialFilter);
      const next = await api<Job>("/analysis-runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ batch_id: batchId }) });
      setJob(next);
      setAutoOpenRunId(next.job_id);
      writeLocation(qualityUrlParams(batchId, qualityQuery, next.job_id));
      setMessage("真实规则分类已入队；进度只在批次结束后更新。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "无法开始分析"); }
  }

  function resetToImport() {
    setImported(undefined); setSelected(undefined); setIncludedSheets([]); setSheetMappings({});
    setBatchId(undefined); setRecords([]); setJob(undefined); setAutoOpenRunId(undefined); setSummary([]); setResultRows([]);
    setQualityQuery({ quality: "all", include: "all", sort: "position", order: "asc", limit: DEFAULT_PAGE_SIZE });
    setResultFilter({ sort: "source", order: "asc", limit: DEFAULT_PAGE_SIZE });
    setQualityPage(EMPTY_PAGE); setResultPage(EMPTY_PAGE); setComparison(undefined);
    writeLocation(new URLSearchParams({ view: "import" }));
    setMessage("选择下一份本地表格，完成后可与已有 run 比较。");
  }

  function changeInclusion(row: RecordRow) {
    setReviewTarget(row); setReviewReason("");
  }

  async function saveInclusion() {
    if (!batchId || !reviewTarget || !reviewReason.trim()) return;
    const row = reviewTarget;
    const action = row.effective_include ? "exclude" : "include";
    try {
      const result = await api<{ review_revision: number }>(`/batches/${batchId}/reviews`, { method: "PATCH", headers: { "Content-Type": "application/json", "If-Match": String(reviewRevision) }, body: JSON.stringify({ updates: [{ record_id: row.record_id, include_override: action, reason: reviewReason }] }) });
      await loadQuality(batchId, qualityQuery);
      setReviewRevision(result.review_revision); setReviewTarget(undefined); setReviewReason(""); setMessage(action === "exclude" ? "已排除该记录；原文仍可查看。" : "已纳入该记录；原因已随复核版本保存。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "复核未保存"); }
  }

  async function keepFirstDuplicates() {
    if (!batchId) return;
    try {
      const result = await api<{ review_revision: number; updated_count: number }>(`/batches/${batchId}/reviews/keep-first-duplicates`, { method: "POST", headers: { "If-Match": String(reviewRevision) } });
      await loadQuality(batchId, qualityQuery);
      setReviewRevision(result.review_revision);
      setMessage(result.updated_count ? `已排除 ${result.updated_count} 条重复副本，原文与分组仍保留。` : "当前没有需要排除的重复副本。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "批量复核失败"); }
  }

  useEffect(() => {
    if (!job || ["succeeded", "failed", "cancelled"].includes(job.status)) return;
    const events = new EventSource(`/api/v1/jobs/${job.job_id}/events`);
    const refresh = async () => {
      try { setJob(await api<Job>(`/jobs/${job.job_id}`)); }
      catch { /* EventSource will retry with Last-Event-ID. */ }
    };
    ["queued", "running", "pausing", "paused", "cancelling", "cancelled", "succeeded", "failed", "interrupted"].forEach((kind) => events.addEventListener(kind, refresh));
    return () => events.close();
  }, [job?.job_id]);

  useEffect(() => {
    if (job?.status !== "succeeded" || autoOpenRunId !== job.job_id) return;
    Promise.all([
      api<{ summary: Array<Record<string, unknown>> }>(`/analysis-runs/${job.job_id}/summary`),
      loadResultPage(job.job_id, resultFilter),
      api<{ runs: RunItem[] }>("/analysis-runs"),
    ]).then(([summaryValue, rowsValue, runValue]) => {
      const restoredFilter = { ...resultFilter, cursor: rowsValue.cursor ?? undefined };
      setSummary(summaryValue.summary); setResultFilter(restoredFilter); setRuns(runValue.runs);
      setAutoOpenRunId(undefined);
      writeLocation(resultUrlParams(job.job_id, batchId, restoredFilter));
      setMessage("分析完成。每个数字和原文都来自当前批次。");
    }).catch((error) => setMessage(error.message));
  }, [job?.status, autoOpenRunId]);

  async function restoreResultPage() {
    if (!job || job.status !== "succeeded") return;
    try {
      const [summaryValue, rowsValue] = await Promise.all([
        api<{ summary: Array<Record<string, unknown>> }>(`/analysis-runs/${job.job_id}/summary`),
        loadResultPage(job.job_id, resultFilter),
      ]);
      const restoredFilter = { ...resultFilter, cursor: rowsValue.cursor ?? undefined };
      setSummary(summaryValue.summary); setResultFilter(restoredFilter);
      writeLocation(resultUrlParams(job.job_id, batchId, restoredFilter));
      setMessage("已返回这次分析的结果与当前筛选。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "无法恢复结果"); }
  }

  function showQualityPage() {
    if (!batchId) return;
    setSummary([]);
    writeLocation(qualityUrlParams(batchId, qualityQuery, job?.job_id));
    void loadQuality(batchId, qualityQuery).catch((error) => setMessage(error instanceof Error ? error.message : "无法恢复质量批次"));
    setMessage("返回质量批次；可继续复核或再次运行。");
  }

  async function filterResults(next: ResultFilter, mode: "push" | "replace" = "replace") {
    if (!job) return;
    const normalized: ResultFilter = { ...next };
    if (!("cursor" in next)) normalized.cursor = undefined;
    setResultFilter(normalized);
    writeLocation(resultUrlParams(job.job_id, batchId, normalized), mode);
    try {
      const value = await loadResultPage(job.job_id, normalized);
      const corrected = { ...normalized, cursor: value.cursor ?? undefined };
      setResultFilter(corrected);
      writeLocation(resultUrlParams(job.job_id, batchId, corrected));
    } catch (error) { setMessage(error instanceof Error ? error.message : "筛选失败"); }
  }

  async function filterQuality(next: QualityFilter, mode: "push" | "replace" = "replace") {
    if (!batchId) return;
    const normalized: QualityFilter = { ...next };
    if (!("cursor" in next)) normalized.cursor = undefined;
    setQualityQuery(normalized);
    writeLocation(qualityUrlParams(batchId, normalized, job?.job_id), mode);
    try {
      const value = await loadQuality(batchId, normalized);
      const corrected = { ...normalized, cursor: value.cursor ?? undefined };
      setQualityQuery(corrected);
      writeLocation(qualityUrlParams(batchId, corrected, job?.job_id));
    } catch (error) { setMessage(error instanceof Error ? error.message : "筛选失败"); }
  }

  async function downloadExport(scope: "summary" | "detail", format: "html" | "csv") {
    if (!job) return;
    try {
      const artifact = await api<{ artifact_id: string }>(`/analysis-runs/${job.job_id}/exports`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scope, format, ...resultFilter }) });
      const link = document.createElement("a");
      link.href = `/api/v1/artifacts/${artifact.artifact_id}/download`;
      link.download = "";
      document.body.appendChild(link);
      link.click();
      link.remove();
      setMessage(`${scope === "summary" ? "共享汇总" : "含原文明细"}已按当前筛选生成。`);
    } catch (error) { setMessage(error instanceof Error ? error.message : "导出失败"); }
  }

  async function controlJob(action: "pause" | "resume" | "cancel") {
    if (!job) return;
    try { setJob(await api<Job>(`/jobs/${job.job_id}/${action}`, { method: "POST" })); }
    catch (error) { setMessage(error instanceof Error ? error.message : "任务状态未改变"); }
  }

  async function compareWithReference() {
    if (!job || !referenceRun) return;
    try {
      const value = await api<Comparison>("/comparisons", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reference_run: referenceRun, current_run: job.job_id }) });
      setComparison(value); setMessage(value.reliable ? "批次比较已完成。" : "比较已完成，但样本或分布不足，不能给出可靠变化结论。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "比较失败"); }
  }

  function openLabelReview(row: ResultRow) {
    setLabelTarget(row); setLabelSentiment(row.sentiment); setLabelTopic(row.topic); setLabelReason("");
  }

  async function saveLabelReview() {
    if (!job || !labelTarget || !labelReason.trim()) return;
    try {
      await api(`/analysis-runs/${job.job_id}/reviews`, { method: "PATCH", headers: { "Content-Type": "application/json", "If-Match": String(runReviewRevision) }, body: JSON.stringify({ updates: [{ record_id: labelTarget.record_id, sentiment_override: labelSentiment, topic_override: labelTopic, reason: labelReason }] }) });
      const [summaryValue, rowsValue] = await Promise.all([
        api<{ summary: Array<Record<string, unknown>> }>(`/analysis-runs/${job.job_id}/summary`),
        loadResultPage(job.job_id, resultFilter),
      ]);
      const corrected = { ...resultFilter, cursor: rowsValue.cursor ?? undefined };
      setSummary(summaryValue.summary); setResultFilter(corrected); setLabelTarget(undefined);
      writeLocation(resultUrlParams(job.job_id, batchId, corrected));
      setMessage("人工标签已保存；汇总已更新，算法原判仍保留。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "人工标签未保存"); }
  }

  const overall = summary.find((row) => row.scope === "overall");
  const topicCounts = (overall?.topic_counts ?? {}) as Record<string, number>;
  const qualityStart = qualityPage.filtered_total ? qualityPage.offset + 1 : 0;
  const qualityEnd = qualityPage.offset + records.length;
  const resultStart = resultPage.filtered_total ? resultPage.offset + 1 : 0;
  const resultEnd = resultPage.offset + resultRows.length;

  return <main><aside><p className="brand">SIGNALBENCH / LOCAL</p><h1>反馈分析</h1><p>从一份反馈表，走到有原文依据的结论。</p><nav><button className={stage === 0 ? "active" : ""} onClick={resetToImport}>01　上传与映射</button><button className={stage === 2 ? "active" : ""} disabled={!batchId} onClick={showQualityPage}>02　输入质量与分析</button><button className={stage === 3 ? "active" : ""} disabled={job?.status !== "succeeded"} onClick={restoreResultPage}>03　结果</button></nav><small>本地文件 · 离线规则 · 不发送到外部服务</small></aside><section className="shell"><header><span>批次 / 导入</span><span>{message}</span></header><div className="heading"><div><p className="eyebrow">DATA WORKSPACE</p><h2>{stage === 0 ? "导入与字段映射" : stage === 2 ? "输入质量与分析" : "分析结果"}</h2></div><b>0{stage === 0 ? 1 : stage === 2 ? 2 : 3}</b></div>
    {stage === 0 && !imported && <article className="landing"><div className="card upload"><h3>先选择本地 CSV、TSV 或 XLSX</h3><p>一次最多 100 个文件；单文件 16 MiB、每表 128 列、合计 10,000 行。上传后先显示工作表和前 20 行，尚不会创建分析。</p><label className="file"><input type="file" accept=".csv,.tsv,.xlsx" multiple onChange={upload}/><span>选择本地文件</span></label></div>{runs.some((run) => run.status === "succeeded") && <div className="card recent"><h3>最近完成的本地分析</h3>{runs.filter((run) => run.status === "succeeded").slice(0, 5).map((run) => <button key={run.job_id} onClick={() => openRun(run)}><span>{run.job_id.slice(-8)}</span><small>{run.done} / {run.total} 条 · 打开结果</small></button>)}</div>}</article>}
    {stage === 0 && imported && selected && <article className="grid"><div className="card"><h3>选择工作表</h3>{Boolean(imported.errors?.length) && <div className="notice error-list"><b>{imported.errors!.length} 个文件未导入</b>{imported.errors!.map((item) => <p key={`${item.display_name}-${item.message}`}>{item.display_name}：{item.message}</p>)}</div>}<div className="sheets">{imported.sheets.map((sheet) => <button key={`${sheet.asset_id}-${sheet.sheet}`} className={sheet === selected ? "selected" : includedSheets.includes(sheetKey(sheet)) ? "included" : ""} onClick={() => setSelected(sheet)}>{sheet.sheet}<small>{sheet.columns.length} 列 · {sheet.preview_rows.length} 行预览</small></button>)}</div><button className="sheet-toggle" onClick={() => { const key = sheetKey(selected); setIncludedSheets((current) => current.includes(key) ? current.filter((item) => item !== key) : [...current, key]); }}>{includedSheets.includes(sheetKey(selected)) ? "✓ 此工作表已纳入" : "+ 纳入此工作表"}</button><h3>告诉我们每一列是什么</h3>{(["text", "platform", "author", "record_id", "created_at"] as Array<keyof Mapping>).map((field) => <label key={field}>{labels[field]}{field === "text" && "（必选）"}<select value={mapping[field]} onChange={(event) => update(field, event.target.value)}><option value="">不使用此列</option>{columns.map((column) => <option key={column} value={column}>{column}</option>)}</select></label>)}{mapping.created_at && <label>日期格式<select value={mapping.date_format} onChange={(event) => update("date_format", event.target.value)}><option value="yyyy-mm-dd">YYYY-MM-DD</option><option value="iso8601">ISO8601（含时区）</option></select></label>}<button className="primary" disabled={!canMap} onClick={createBatch}>创建质量批次 →</button></div><div className="card"><h3>源数据预览</h3><table><thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{sourcePreview.map((row, index) => <tr key={index}>{columns.map((column) => <td key={column}>{row[column]}</td>)}</tr>)}</tbody></table></div></article>}
    {stage === 2 && <article>
      <div className="stats">
        <span>总行 <b>{qualityTotals.batch_total.toLocaleString()}</b></span>
        <span>纳入 <b>{qualityTotals.included_total.toLocaleString()}</b></span>
        <span>排除 <b>{qualityTotals.excluded_total.toLocaleString()}</b></span>
        <span>待处理 <b>{qualityTotals.pending_total.toLocaleString()}</b></span>
      </div>
      <div className="card">
        <div className="row">
          <div><h3>质量明细</h3><p>只加载当前页；搜索、来源、质量和纳入状态由服务端筛选。重复不会被默认删除。</p></div>
          <button className="primary" disabled={!batchId || Boolean(job && !["succeeded", "failed", "cancelled", "interrupted"].includes(job.status))} onClick={runAnalysis}>开始真实分析 →</button>
        </div>
        <div className="quality-tools">
          <input aria-label="搜索质量明细" placeholder="搜索原文" value={qualityQuery.search ?? ""} onChange={(event) => void filterQuality({ ...qualityQuery, search: event.target.value, cursor: undefined })}/>
          <input aria-label="筛选来源" placeholder="筛选来源或工作表" value={qualityQuery.source ?? ""} onChange={(event) => void filterQuality({ ...qualityQuery, source: event.target.value, cursor: undefined })}/>
          <select aria-label="筛选质量状态" value={qualityQuery.quality} onChange={(event) => void filterQuality({ ...qualityQuery, quality: event.target.value, cursor: undefined })}>
            <option value="all">全部质量</option><option value="valid">有效</option><option value="invalid">无效</option><option value="display_issue">展示问题</option>
          </select>
          <select aria-label="筛选分析范围" value={qualityQuery.include} onChange={(event) => void filterQuality({ ...qualityQuery, include: event.target.value, cursor: undefined })}>
            <option value="all">全部范围</option><option value="included">已纳入</option><option value="excluded">已排除</option><option value="pending">待处理</option>
          </select>
          <select aria-label="质量排序" value={qualityQuery.sort} onChange={(event) => void filterQuality({ ...qualityQuery, sort: event.target.value, cursor: undefined })}>
            <option value="position">原始顺序</option><option value="source">来源</option><option value="quality">质量状态</option><option value="duplicate">重复组</option><option value="text">原文</option><option value="include">分析范围</option>
          </select>
          <button onClick={() => void filterQuality({ ...qualityQuery, order: qualityQuery.order === "asc" ? "desc" : "asc", cursor: undefined })}>{qualityQuery.order === "asc" ? "升序" : "降序"}</button>
          <select aria-label="每页记录数" value={qualityQuery.limit} onChange={(event) => void filterQuality({ ...qualityQuery, limit: Number(event.target.value), cursor: undefined })}>
            <option value={25}>25 条 / 页</option><option value={50}>50 条 / 页</option><option value={100}>100 条 / 页</option>
          </select>
          <button onClick={keepFirstDuplicates}>重复组仅保留首条</button>
          <span>{qualityStart.toLocaleString()}–{qualityEnd.toLocaleString()} / {qualityPage.filtered_total.toLocaleString()} 条匹配</span>
        </div>
        <table><thead><tr><th>原文</th><th>来源</th><th>质量</th><th>重复</th><th>分析范围</th></tr></thead>
          <tbody>{records.map((row) => <tr key={row.record_id}>
            <td>{row.text_raw}</td><td>{row.platform} / {row.source_sheet}:{row.source_row}</td>
            <td><span className={`tag ${row.quality_label}`}>{row.quality_label}{row.quality_reason ? ` · ${row.quality_reason}` : ""}</span></td>
            <td>{row.duplicate_count > 1 ? `${row.duplicate_rank} / ${row.duplicate_count}` : "—"}</td>
            <td><button disabled={row.quality_reason === "blank" || row.quality_reason === "encoding_marker" || row.quality_reason === "control_character"} onClick={() => changeInclusion(row)}>{row.effective_include ? "纳入 · 排除" : "排除 · 纳入"}</button>{row.review_reason && <small>{row.review_reason}</small>}</td>
          </tr>)}</tbody>
        </table>
        {records.length === 0 && <p className="empty-state">没有符合条件的记录。清除筛选或放宽搜索词后再试。</p>}
        <Pagination page={qualityPage} onMove={(cursor) => void filterQuality({ ...qualityQuery, cursor }, "push")}/>
      </div>
      {job && <div className="card progress"><h3>{job.status}</h3><p>{job.progress.done} / {job.progress.total} 条记录已在真实批次边界完成</p><div><i style={{ width: `${job.progress.total ? job.progress.done / job.progress.total * 100 : 0}%` }}/></div><div className="filter-row">{job.capabilities.pause && <button onClick={() => controlJob("pause")}>批次结束后暂停</button>}{job.capabilities.resume && <button onClick={() => controlJob("resume")}>继续</button>}{!["succeeded", "failed", "cancelled"].includes(job.status) && <button onClick={() => controlJob("cancel")}>取消任务</button>}</div>{job.error && <p className="error">{job.error}</p>}</div>}
    </article>}
    {stage === 3 && <article className="grid results">
      <div className="card"><h3>当前批次的情感分布</h3><p className="number">{String(overall?.classified_total ?? "—")}</p>
        <div className="filter-row">
          <button className={!resultFilter.sentiment && !resultFilter.topic ? "active-filter" : ""} onClick={() => void filterResults({ ...resultFilter, sentiment: undefined, topic: undefined, cursor: undefined })}>全部 {String(overall?.classified_total ?? 0)}</button>
          <button className={resultFilter.sentiment === "positive" ? "active-filter" : ""} onClick={() => void filterResults({ ...resultFilter, sentiment: "positive", topic: undefined, cursor: undefined })}>正向 {String(overall?.positive_total ?? 0)}</button>
          <button className={resultFilter.sentiment === "neutral" ? "active-filter" : ""} onClick={() => void filterResults({ ...resultFilter, sentiment: "neutral", topic: undefined, cursor: undefined })}>中性 {String(overall?.neutral_total ?? 0)}</button>
          <button className={resultFilter.sentiment === "negative" ? "active-filter" : ""} onClick={() => void filterResults({ ...resultFilter, sentiment: "negative", topic: undefined, cursor: undefined })}>负向 {String(overall?.negative_total ?? 0)}</button>
        </div>
        <p>规则分类的置信度是启发式提示，不是校准概率。</p>
      </div>
      <div className="card"><h3>主题分布</h3>{Object.entries(topicCounts).map(([topic, count]) => <button className={`topic ${resultFilter.topic === topic ? "active-filter" : ""}`} key={topic} onClick={() => void filterResults({ ...resultFilter, topic, sentiment: undefined, cursor: undefined })}><span>{topic}</span><b>{count}</b><i style={{ width: `${Number(overall?.classified_total) ? count / Number(overall?.classified_total) * 100 : 0}%` }}/></button>)}</div>
      <div className="card full">
        <div className="row"><div><h3>结论旁边就是原文</h3><p>当前筛选：{resultFilter.topic ?? resultFilter.sentiment ?? (resultFilter.review_status === "reviewed" ? "人工已复核" : resultFilter.review_status === "unreviewed" ? "待人工复核" : "全部")} · {resultStart.toLocaleString()}–{resultEnd.toLocaleString()} / {resultPage.filtered_total.toLocaleString()} 条</p>
          <div className="quality-tools result-tools">
            <input aria-label="搜索结果原文" placeholder="搜索原文或来源" value={resultFilter.search ?? ""} onChange={(event) => void filterResults({ ...resultFilter, search: event.target.value, cursor: undefined })}/>
            <select aria-label="结果排序" value={resultFilter.sort} onChange={(event) => void filterResults({ ...resultFilter, sort: event.target.value, cursor: undefined })}><option value="source">来源</option><option value="sentiment">情感</option><option value="topic">主题</option><option value="date">日期</option><option value="text">原文</option></select>
            <select aria-label="人工复核状态" value={resultFilter.review_status ?? "all"} onChange={(event) => void filterResults({ ...resultFilter, review_status: event.target.value || undefined, cursor: undefined })}><option value="all">全部复核状态</option><option value="reviewed">人工已复核</option><option value="unreviewed">待人工复核</option></select>
            <button onClick={() => void filterResults({ ...resultFilter, order: resultFilter.order === "asc" ? "desc" : "asc", cursor: undefined })}>{resultFilter.order === "asc" ? "升序" : "降序"}</button>
            <select aria-label="每页结果数" value={resultFilter.limit} onChange={(event) => void filterResults({ ...resultFilter, limit: Number(event.target.value), cursor: undefined })}><option value={25}>25 条 / 页</option><option value={50}>50 条 / 页</option><option value={100}>100 条 / 页</option></select>
            <button onClick={() => void filterResults({ sort: "source", order: "asc", limit: DEFAULT_PAGE_SIZE })}>清除筛选</button>
          </div>
          {dateFilterAvailable && <div className="date-tools"><label>从<input type="date" value={resultFilter.date_from ?? ""} onChange={(event) => void filterResults({ ...resultFilter, date_from: event.target.value || undefined, cursor: undefined })}/></label><label>到<input type="date" value={resultFilter.date_to ?? ""} onChange={(event) => void filterResults({ ...resultFilter, date_to: event.target.value || undefined, cursor: undefined })}/></label>{unparsedDateCount > 0 && <span>{unparsedDateCount} 条日期未解析，不进入时间筛选</span>}</div>}
        </div><div className="filter-row"><button onClick={() => downloadExport("summary", "html")}>共享汇总 HTML</button><button onClick={() => downloadExport("detail", "csv")}>含原文明细 CSV</button><button onClick={showQualityPage}>返回质量页</button></div></div>
        <Pagination page={resultPage} onMove={(cursor) => void filterResults({ ...resultFilter, cursor }, "push")}/>
        <div className="evidence-list">{resultRows.map((row) => <section key={row.record_id}><span className="tag">{row.topic} · {row.sentiment}</span><blockquote>“{row.text_raw}”</blockquote><p>{row.platform} / {row.source_sheet}:{row.source_row} · {row.occurred_at ?? "无可用日期"} · {row.decision_note}</p><p className="rule-hits">{row.rule_hits.length ? `命中：${row.rule_hits.join(" · ")}` : "未命中强规则，归入 general / neutral"}</p>{(row.algorithm_sentiment !== row.sentiment || row.algorithm_topic !== row.topic) && <p className="reviewed">算法原判 {row.algorithm_topic} / {row.algorithm_sentiment} → 人工复核 {row.topic} / {row.sentiment}<br/>{row.review_reason}</p>}<button onClick={() => openLabelReview(row)}>复核标签</button></section>)}</div>
        {resultRows.length === 0 && <p className="empty-state">没有符合当前筛选的分析结果。</p>}
      </div>
      <div className="card full"><h3>与已完成批次比较</h3><div className="filter-row"><select value={referenceRun} onChange={(event) => setReferenceRun(event.target.value)}><option value="">选择参考 run</option>{runs.filter((item) => item.status === "succeeded" && item.job_id !== job?.job_id).map((item) => <option key={item.job_id} value={item.job_id}>{item.job_id.slice(-8)} · {item.total} 条</option>)}</select><button disabled={!referenceRun} onClick={compareWithReference}>比较</button><button onClick={resetToImport}>新建另一批次</button></div>{comparison && <div className={`notice ${comparison.reliable ? "" : "warning"}`}><b>{comparison.reliable ? "可以比较" : "仅展示差异"}</b><p>参考 {comparison.reference_count} 条，当前 {comparison.current_count} 条。</p>{comparison.limitations.map((item) => <p key={item}>{limitationLabels[item] ?? item}</p>)}<div className="comparison-grid"><DistributionComparison title="情感占比" value={comparison.sentiment}/><DistributionComparison title="主题占比" value={comparison.topics}/></div><h4>输入结构距离</h4><div className="metric-table">{comparison.structure.metrics.map((metric) => <div key={metric.column}><span>{metric.column}</span><span>{metric.comparable && metric.distribution_score !== null ? metric.distribution_score.toFixed(3) : "不可比较"}</span><span>缺失变化 {Math.round(metric.missingness_delta * 100)}%</span>{metric.reason && <small>{metric.reason}</small>}</div>)}</div></div>}</div>
    </article>}
    {labelTarget && <div className="modal" role="dialog" aria-modal="true" aria-labelledby="label-review-title"><div className="modal-card"><p className="eyebrow">结果复核</p><h3 id="label-review-title">修订情感与主题</h3><blockquote>{labelTarget.text_raw}</blockquote><div className="grid"><label>情感<select value={labelSentiment} onChange={(event) => setLabelSentiment(event.target.value)}><option value="positive">positive</option><option value="neutral">neutral</option><option value="negative">negative</option></select></label><label>主题<select value={labelTopic} onChange={(event) => setLabelTopic(event.target.value)}><option value="pricing">pricing</option><option value="reliability">reliability</option><option value="support">support</option><option value="safety">safety</option><option value="general">general</option></select></label></div><p className="rule-hits">算法原判：{labelTarget.algorithm_topic} / {labelTarget.algorithm_sentiment}<br/>命中：{labelTarget.rule_hits.join(" · ") || "无"}</p><label>复核原因<textarea autoFocus value={labelReason} onChange={(event) => setLabelReason(event.target.value)} placeholder="说明人工判断依据"/></label><div className="modal-actions"><button onClick={() => setLabelTarget(undefined)}>取消</button><button className="primary" disabled={!labelReason.trim()} onClick={saveLabelReview}>保存并重新聚合</button></div></div></div>}
    {reviewTarget && <div className="modal" role="dialog" aria-modal="true" aria-labelledby="review-title"><div className="modal-card"><p className="eyebrow">人工复核</p><h3 id="review-title">{reviewTarget.effective_include ? "从分析范围排除" : "纳入分析范围"}</h3><blockquote>{reviewTarget.text_raw}</blockquote><label>复核原因<textarea autoFocus value={reviewReason} onChange={(event) => setReviewReason(event.target.value)} placeholder="写明可复查的业务原因"/></label><div className="modal-actions"><button onClick={() => setReviewTarget(undefined)}>取消</button><button className="primary" disabled={!reviewReason.trim()} onClick={saveInclusion}>保存复核</button></div></div></div>}
  </section></main>;
}

createRoot(document.getElementById("root")!).render(<App/>);
