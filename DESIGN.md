# 反馈分析 — 完整产品设计

目标版本：本地工作台 v1。展示名“反馈分析 / Feedback Analysis”，内部仍 signalbench。本文是待实现设计；现有 CLI、规则分类和静态报告可复用，交互应用尚未完成。

## 1. 用户与完成场景

面向整理应用评论、客服反馈的产品/运营人员。用户导入自己的表格，确认正文列与工作表，处理重复/无效内容，得到能回到原文的分析，再导出报告。首版只做表格反馈，不加 OCR、文档写作、聊天机器人、在线 LLM 接口。
必须支持样例与真实本地文件；演示不能绕过字段映射或只读预生成 report.html。

## 2. 现状与必须修正的接口

- io.py/load_source_records 自动遍历非空 sheet、按别名映射；新增逐表显式选择和列映射，复用解析，不另写第二套 CSV 引擎。
- SourceRecord 包含 record_id、source_file/sheet/row、platform、author_hash、text_raw。作者 hash 不使原文匿名。
- prepare_records 标 valid/invalid/display_issue，重复只标记。read_prepared_csv 会重跑规则，所以人工处理不能靠直接改 prepared.csv。
- DeterministicClassifier 仅中英关键词情感、pricing/reliability/support/safety/general 主题。confidence 是启发式，不能写成“83%可信”。replay 只是重放已有决定。
- classify_records 是串行批处理与 digest 绑定 checkpoint；没有暂停接口、实时事件、用户复核。
- DataSentinel 的 validate_snapshot/profile_drift 可复用，evaluate_gate 的一律阻断不能搬入产品。

## 3. 页面与视觉

暖白 #f5f3ee，白色表格面，正文 #292c29，陶土橙 #b65c32 表达主要操作，错误用暗红。顶部导航“批次 / 导入”，面包屑显示批次名和版本；不做四个大统计卡占满首屏。
桌面1440：左侧批次列表208px，中间主表至少600px，右侧证据抽屉360px可收起；表头固定、行高40px、长文换行在抽屉。768以下批次导航变抽屉，证据全屏页；390保留筛选与逐行复核，宽表局部滚动。

### 导入与映射 /imports/:id
拖入文件或选样例 → 文件列表及错误 → XLSX工作表勾选 → 每表映射 → 前20行预览 → “创建分析批次”。
正文必选；platform/record_id/author/created_at可选。来源不同的表分别映射，映射可应用到同结构其他表但需预览确认。显示有效/空白预估与被跳过sheet；没有正文列阻止提交并定位字段。
继承原上限：100文件、单文件16MiB、128列、合计10000行、正文8192字符。初次打开给清楚的限制摘要。部分文件失败时保留成功文件，不悄悄忽略失败项。

### 批次质量 /batches/:id/quality
顶部“总行数 / 纳入 / 排除 / 待处理”四个紧凑数字，下面是问题筛选chips和表格：选择框、原文摘要、来源、质量原因、重复组、是否纳入。
点行打开原文与规则说明；可搜索、按问题/来源筛选、批量排除/恢复。重复组提供“每组保留首条”明确操作，默认保留全部。
空正文/解析损坏是硬错误，不能强行纳入；短文本、链接等软规则可用“仍纳入”覆盖并写原因。不改原文，纠正原文须创建修订批次。
所有筛选与行数即时一致。筛选不自动改变纳入集合。处理后“开始分析”冻结该批次 revision；全排除时说明如何恢复，不生成空的成功报告。

### 分析与结果 /runs/:id
运行中显示真实批次进度、已完成数、失败原因、暂停/取消；切页后能回来，断线重连不重复计数。失败提供针对当前输入的恢复，不把 retry 当重新导入。
成功页左侧筛选，中央情感/主题分布与变化，右侧对应原文列表。点图表柱同时筛选明细，显示“当前筛选 / 全批次”。表格排序、分页、复核状态保持在URL。
指标分母始终是已纳入且已有最终标签的记录，另显示未处理数；待复核记录仍计入但明确其数量。未完成任务带“部分结果”，不能以完整批次标题导出。
“人工复核”保留算法原判、改后标签、原因、版本。修改建立 review revision，重新聚合而非重跑模型。主题允许首版五类，其他/不确定需保留，不强塞四类。
规则依据展示命中表达与冲突标记（需扩展 classifier 返回 rule_hits），不用伪概率。时间筛选仅有明确导入日期列且成功解析时出现。

### 批次比较 /compare
用户显式选参考批次；展示样本数、过滤条件、平台/情感/主题/正文长度分布变化。复用 PSI/JS/缺失变化，不对自由正文做高基数JS。
样本少于30、空数值分布、来源/映射差异显示“不可可靠比较”及理由；空分布不是0漂移。漂移不阻断查看或分析。默认仅展示距离/差异，业务阈值须显式设置并随结果保存，不用合成校准阈值当通用标准。

## 4. 数据和服务契约（拟建）

保留源数据不可变；人工动作独立储存：
~~~json
{"batch_id":"batch_01","revision":2,"sources":[{"asset_id":"asset_1","sheet":"评论","mapping":{"text":"评价","platform":"渠道","record_id":"编号"}}],"include_policy":{"default":"valid_and_reviewable","duplicate_policy":"keep_all"}}
~~~
review表：record_id、batch_revision、review_revision、include_override(null/include/exclude)、sentiment_override、topic_override、reason。展示质量原因与最终纳入状态分别保存，不能回写原 prepared 行质量标签。正文或映射改变产生新 record/preparation digest，使旧checkpoint不兼容。
日期映射另存 batch_record_metadata(batch_id,record_id,occurred_at,date_raw,date_parse_status)，不改变现有 SourceRecord 构造与 CLI 契约。用户逐表选 ISO8601（时间必须带时区）或 YYYY-MM-DD（日粒度，不推断时刻）；混用格式及无时区时间标解析问题，保留原文并允许忽略日期或修改映射。日期错误不排除有效正文；日期筛选明确显示未解析记录数量，未映射日期时隐藏时间筛选。批次冻结时冻结日期元数据。
SourceRecord适配保持源file/sheet/row可追溯；DataSentinel只对选定结构列显式类型转换，不能直接把CSV数字字符串塞给 numeric contract。

| API | 输入/结果 |
|---|---|
| POST /imports | multipart文件 → import_id，assets、sheets、columns、preview_rows、errors |
| PATCH /imports/{id}/mapping | 每表选择与映射，If-Match → 新revision及字段问题 |
| POST /batches | import_id/revision → batch_id/revision、质量统计 |
| GET /batches/{id}/records | quality/source/search/include、sort/order、cursor、limit≤200 → rows、filtered_total、batch/include/exclude/pending totals |
| PATCH /batches/{id}/reviews | record_ids≤500、override/reason、If-Match → 新review revision |
| POST /batches/{id}/reviews/keep-first-duplicates | If-Match → 对当前纳入的重复副本一次性追加排除决定；返回更新数与新review revision |
| POST /analysis-runs | batch_id/revision、review_revision、classifier=deterministic、batch_size默认100 → job |
| GET /analysis-runs/{id}/summary | 已完成 run 的全批次 totals、sentiment、topics 与复核统计；图表保留全批次分母，筛选行数由 records API 返回。进行中/中断状态由 jobs API 提供 |
| GET /analysis-runs/{id}/records | sentiment/topic/date/search/review_status、sort/order、cursor、limit≤200 → 当前筛选的明细页与总数 |
| GET /analysis-runs/{id}/records/{record_id} | 原文、来源、原判、规则依据、复核记录 |
| POST /comparisons | current_run、reference_run、各filter → 分布/限制/配置 |
| POST /analysis-runs/{id}/exports | format=html/json/csv/xlsx，scope=summary/detail，filter → artifact |

暂停在批次边界落checkpoint；恢复绑定 preparation、classifier/config digest，不兼容返回409并可复制为新任务。人工标签修订不冒充新模型输出。共享汇总不含原文/文件名；明细下载按钮写“含原文与来源”，按用户选定范围导出。

## 5. 模块安排与实施顺序

A. adapters/imports.py、review_store.py：显式映射、原文与review分离、DataSentinel适配；用现有样例和中文混合列名表完成质量页面。
B. app API+worker：真实分类进度、批后暂停恢复、checkpoint兼容；先跑一个用户文件到报告。
C. web 导入/质量/结果三页、证据抽屉、图表和筛选统一查询；算法输出加 rule_hits。
D. 人工复核、参考批次比较、共享/明细导出、历史恢复，完成全部关键状态。
E. 公开标注数据的小型评估与错误例集：至少含否定、反讽、正负冲突和未知主题。展示规则局限；不以合成效果宣称通用理解。

DataSentinel作为本地依赖的契约/统计模块接入，不复制整份项目；保留独立算法测试。首版不复用其 admission decision。PaperForge留作以后独立设计，不阻塞本版。

## 6. 实物完成要求

- 多sheet文件，其中一表缺正文列：能修映射，成功表保留；不生成空“成功”。
- 重复+短文本+正常记录混合：保留/排除改变分母、图表、明细、导出且可追溯。
- 分析中暂停，刷新后继续；换输入不能套旧checkpoint。取消保留部分结果但显式标记。
- 点负向主题图看到对应原文；人工改判后原判仍可见，汇总同步。
- 空参考分布显示不可比较，不显示无漂移；显著变化仍允许分析。
- 用全新目录按README运行；中文字段、长原文、小窗口、导出再次读取均实测。

最终交付：可用工作台、两个开箱样例（干净/需处理）、一次真实流程短演示、用户教程、架构与规则局限、准确的当前运行证据。
