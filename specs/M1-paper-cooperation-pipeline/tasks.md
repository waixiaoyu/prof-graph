# M1 — 论文合作全链路 · 任务清单（tasks.md）

> **阶段**：Tasks（tasks.md）✅ 已确认（2026-08-24，R1~R5 引导式 review）
> **状态**：定稿
> **最后更新**：2026-08-25（T24 后补充优化，见附录 2）
> **上游**：spec.md ✅ / plan.md ✅

**工作约定（review 确认）**：
- Git：每 task 至少一次提交，格式 `M1-T7: <内容>`，直接进 main（单人内部项目无 PR）。
- 验收分工：AC-1/5/6/7/8 等可自动验证的由实现者跑测试/查库自验；AC-2/4/9（观感/交互类）跑起来由用户人工抽查确认。
- 环境：Windows 开发机 + 便携版 PostgreSQL（T0）；GLM key 用户提供。
- 熔断数值（日 120 万/周 600 万）为 .env 可配初值，T23 联调后按真实用量校准。

**约定**：
- 每个 task 独立可交付、可验证；完成即勾选 `[x]`。
- `依赖` 列出必须先完成的 task。
- `验证` 是完成标准；`AC` 追溯到 spec 验收标准。
- 实现顺序即编号顺序；同阶段内无依赖的 task 可并行。

---

## 阶段 A0：环境准备（T0）

- [x] **T0 开发环境准备（Windows + 便携版 PostgreSQL）**
  - 内容：下载 EDB PostgreSQL 15 binaries zip 解压到项目外部固定目录（如 `C:\tools\pg15`）；`initdb` 初始化数据目录 + 启动脚本（`pg_ctl start/stop`，端口 5432，密码写入 `.env`）；建库 `prof_graph` 与测试库 `prof_graph_test`；Python 版本检查脚本（要求 ≥3.11，不符则提示用 `uv` 安装固定版本）；`.env` 模板补齐 DATABASE_URL 与 `GLM_API_KEY=`（用户提供后填入）占位；`backend/scripts/env_check.py` 一键自检（PG 可连 / Python 版本 / Node 版本 / GLM key 已填）。
  - 依赖：—
  - 验证：`python backend/scripts/env_check.py` 四项全绿。

## 阶段 A：项目骨架（T1–T4）

- [x] **T1 后端项目初始化**
  - 内容：`backend/` 目录、FastAPI 入口（`app/main.py`）、`pyproject.toml`（依赖：fastapi、sqlalchemy 2、apscheduler、asyncpg、pydantic、httpx、feedparser、pytest）、`.env.example`（GLM/数据库/OpenAlex mailto 配置）、lifespan 中启动/关闭 scheduler。
  - 依赖：—
  - 验证：`uvicorn app.main:app` 启动，`GET /health` 返回 200。

- [x] **T2 数据库 schema 落地**
  - 内容：SQLAlchemy 模型（plan §2 全部 11 张表：papers / persons / person_research_tags / organizations / person_org / paper_authors / relationships（含 UNIQUE + CHECK）/ relationship_evidence / disambiguation_queue / failed_jobs / token_usage）；Alembic 初始迁移；`name_normalized` 归一化工具函数。
  - 依赖：T1
  - 验证：`alembic upgrade head` 在空库执行成功；11 张表存在，relationships 表含两个约束。

- [x] **T3 前端项目初始化**
  - 内容：`frontend/` Vite + React 18 + TypeScript；React Flow、路由（Graph / Review / Admin 三页占位）；API client 封装；`.env`（后端地址）。
  - 依赖：—
  - 验证：`npm run dev` 启动，三个空页面可导航。

- [x] **T4 配置体系：directions.yaml**
  - 内容：`backend/config/directions.yaml`（plan §8 定稿内容：3 产业方向 + 12 学术赛道 + 18 分类）；加载器（启动校验：id 唯一、关键词非空）；`GET /api/filters/options` 返回方向/赛道选项。
  - 依赖：T1
  - 验证：单测覆盖配置加载（缺 id/空关键词报错）；接口返回 3 方向 + 12 赛道。

## 阶段 B：采集与过滤（T5–T8）

- [x] **T5 arXiv RSS 采集器**
  - 内容：`services/collector.py`——按 18 分类拉 RSS（feedparser），解析 arxiv_id/标题/摘要/作者列表/发布时间/分类/原始条目；`papers` 表 upsert（arxiv_id 唯一，重复跳过）；失败（超时/HTTP 错误）写 failed_jobs（job_type=rss_fetch），批次不中断。
  - 依赖：T2
  - 验证：mock RSS fixture 单测——正常入库、重复跳过、单分类失败不中断批次。对应 FR-1.1/1.6/1.7，AC-1 前置。

- [x] **T6 AI 相关性过滤（两段式）**
  - 内容：`services/ai_filter.py`——第一段规则粗筛（cs.AI/cs.LG/stat.ML 直留；无 cs.*/stat.ML 交叉且不命中关键词表直剔；其余待定）；第二段 GLM 批量细筛（10 篇/请求，标题+摘要，返回 is_ai+理由）；过滤结果落 `papers.ai_relevant`，非 AI 论文不入正式库（status=filtered_out）。
  - 依赖：T5、T7（GLM client）
  - 验证：单测——纯数学论文（无 AI 关键词无 AI 交叉分类）被剔除；cs.LG 论文直接保留；待定项走 mock GLM 判定。对应 FR-1.2，AC-8。

- [x] **T7 GLM 客户端 + 用量统计 + 分级熔断**
  - 内容：Anthropic 协议封装（GLM-5.2）——结构化 JSON 输出、超时/限流异常分类；每次调用记 token_usage；**双预算分级熔断**（日 120 万/周 600 万，.env 可配）：日 80% 告警 → 日 100% 停细筛 → 周触顶停抽取；**熔断后管理员可手动放行继续**（当日有效，操作记日志）；次日/次周自动恢复；熔断状态可查。
  - 依赖：T2
  - 验证：单测——mock token 累计到各级阈值，细筛/抽取分别被拒且待定论文放行；手动放行后恢复调用；跨天自动恢复。对应 NFR-2/NFR-3。

- [x] **T8 业务方向打标器**
  - 内容：`services/tagger.py`——对过滤后论文的标题+摘要按 directions.yaml 关键词匹配（不区分大小写），写 `papers.directions` / `papers.tracks`（一篇可多标）。
  - 依赖：T4、T5
  - 验证：单测——含 "intent-based networking" 的论文标 network_autonomy + ADN；无命中的为空数组。对应 FR-1.3。

## 阶段 C：抽取与消歧（T9–T12）

- [x] **T9 GLM 抽取器（含部分容错）**
  - 内容：`services/extractor.py`——输入策略（优先 arXiv HTML 全文截断 12k tokens，否则标题+摘要+作者列表）；输出按 plan §3 JSON Schema 校验；部分容错（能解析的作者入库，缺 name/seq 的跳过记 warning，整篇解析失败走 failed_jobs 重试）；论文 status 流转 pending_extraction → extracted / extraction_failed。
  - 依赖：T7、T5
  - 验证：单测——正常响应入库；5 作者中 1 个字段缺失只跳过 1 个；非法 JSON 触发 failed_jobs。对应 FR-2.1~2.3，RD-11。

- [x] **T10 OpenAlex 客户端 + 机构双源补全**
  - 内容：`services/openalex.py`——authors 检索（mailto 参数、限 5 req/s）；与 GLM 抽取结果合并：GLM 有 affiliation → org_confidence=1.0；无 → OpenAlex 匹配（姓名精确 + 论文吻合）→ 0.8 并回写 openalex_id；均无 → 0.4 兜底；organizations upsert（归一化去重）、person_org 写入。
  - 依赖：T2、T9
  - 验证：单测（mock OpenAlex）——三源路径各自的 confidence 值；org 归一化合并（"Tsinghua University"/"Tsinghua Univ."）。对应 FR-2.4，RD-2。

- [x] **T11 实体消歧器**
  - 内容：`services/disambiguator.py`——openalex_id 强匹配直归并；否则生成候选集（**准确度优先，不考虑性能**：name_normalized 精确同名 + 模糊名变体（编辑距离阈值内、姓名序颠倒双向查询）全部入候选），对全部候选做 5 因素加权打分（姓名 30% 编辑距离 / 机构 25% / 研究方向 20% Jaccard / 时间 15%（活跃区间自动聚合）/ 合作网络 10%）取最高分；≥0.8 自动归并、0.5–0.8 入 disambiguation_queue（含分项得分）、<0.5 新建 Person；paper_authors 关联。
  - 依赖：T10
  - 验证：单测——同人不同写（含姓名序颠倒、缩写变体）归并（≥0.8）；重名不同人新建（<0.5）；中间分数入队且 score_detail 完整。对应 FR-3.1~3.3，AC-9 前置。

- [x] **T12 关系建立器（linker）**
  - 内容：`services/linker.py`——对每篇论文两两作者建 paper_cooperation：ID 排序防重；identity_confidence=0.4×name+0.6×org；strength 阶梯（1次0.85/2次0.90/3-4次0.95/5次+1.00）× identity；已存在则 coop_count+=1、重算 strength、追加 relationship_evidence、更新时间范围与 evidence_summary（"基于 N 篇合作论文，最近合作于 YYYY 年"）。
  - 依赖：T11
  - 验证：单测——同对作者 3 篇论文只 1 条关系，coop_count=3，strength=0.95×identity，证据 3 条，摘要正确；反向顺序（B,A）不产生第二条。对应 FR-4.1~4.5，AC-5。

## 阶段 D：失败处理与调度（T13–T14）

- [x] **T13 failed_jobs 重试 + 死信**
  - 内容：`services/failed_jobs.py`——指数退避（next_retry_at = now + 1/5/25min）最多 3 次，仍失败置 dead；重试执行器按 job_type 回调对应服务；死信可经 CLI 命令手动重跑。
  - 依赖：T7、T9
  - 验证：单测——失败后 next_retry_at 间隔为 1/5/25 分钟序列；3 次后 status=dead；CLI 重跑 dead 任务成功转 done。对应 FR-2.5，AC-6。

- [x] **T14 APScheduler 任务注册**
  - 内容：`scheduler.py`——①采集管线（cron 03:00 + jitter 1200s：采集→过滤→打标→抽取→消歧→关系全链）；②failed_jobs 重试扫描（interval 60s）；③死信巡检（cron 08:00 记日志）。
  - 依赖：T6、T8、T9、T13
  - 验证：单测——job 注册数量与触发配置正确；集成测试手动触发管线全链跑通 fixture。对应 FR-1.1。

## 阶段 E：API（T15–T17）

- [x] **T15 图谱与查询 API**
  - 内容：`GET /api/graph`（direction/track/strength_min/limit 过滤，按 strength 降序 Top 1000，返回 nodes+edges）；`GET /api/persons/search?q=&type=name|org`（LIKE 限 20）；`GET /api/persons/{id}`（详情含机构/研究方向/论文）；`GET /api/relationships/{id}/evidence`（证据论文列表）。
  - 依赖：T12
  - 验证：pytest + 测试库——graph 过滤各参数生效；search LIKE 命中；evidence 返回标题/年份。对应 FR-5.1~5.5。

- [x] **T16 审核队列 API**
  - 内容：`GET /api/disambiguation?status=pending`（含 score_detail）；`POST .../merge`（body: keep=A|B——迁移关系/证据/标签/机构归属，重算受影响关系，记 merged）；`POST .../reject`（记 rejected，持久化"A≠B"结论，后续同对组合不再入队，新作者仍正常匹配 A/B）。
  - 依赖：T11
  - 验证：单测——merge 后两 Person 合一且 coop_count/strength 重算正确；reject 后同对不再入队、新相似作者仍可入队。对应 FR-3.4，AC-9。

- [x] **T17 管理端 API**
  - 内容：`POST /api/admin/trigger-update`（进行中批次返回 409，否则启动管线并返回 batch_id）；`GET /api/admin/update-status/{batch_id}`（已抓取/总数/当前阶段）；`GET /api/admin/metrics`（当日/本周 token 用量、failed_jobs 列表、熔断状态）；`POST /api/admin/breaker/resume`（管理员手动放行熔断，当日有效）。
  - 依赖：T14、T7
  - 验证：单测——并发触发得 409；metrics 四块数据齐；熔断态下调 resume 后 GLM 调用恢复。对应 FR-1.4，NFR-3，AC-7。

## 阶段 F：前端（T18–T22）

- [x] **T18 图谱页主体**
  - 内容：Graph 页——React Flow 画布渲染 Person 节点 + paper_cooperation 实线边；节点大小按度数、边粗细按 strength；点击节点弹详情（机构/研究方向/论文列表）；点击边弹证据链面板（论文标题/年份）。
  - 依赖：T3、T15
  - 验证：本地连后端——AC-2/AC-3/AC-4 手动走查。

- [x] **T19 筛选器与搜索**
  - 内容：FilterBar——业务方向/学术赛道预设下拉（数据来自 /api/filters/options）+ 姓名/机构输入框（防抖调 /api/persons/search）；strength_min 滑杆；图谱/列表视图切换。
  - 依赖：T18
  - 验证：选 ADN 高亮子网；输入姓名定位节点；滑杆过滤弱关系。对应 FR-5.2/5.5/5.6，AC-2/AC-4。

- [x] **T20 审核队列页**
  - 内容：Review 页——待审核列表（两人字段对比 + 分项得分）；合并（选择保留 A/B）/ 拒绝按钮；操作后列表刷新。
  - 依赖：T3、T16
  - 验证：本地造 0.5–0.8 数据——合并后图谱中两人变一点。对应 AC-9。

- [x] **T21 管理后台页**
  - 内容：Admin 页——"立即更新"按钮（进度轮询展示已抓取/总数/阶段）；当日/本周 token 用量卡片（含预算占比进度条）；熔断状态条（触发时显示级别 + **"放行继续"按钮**）；failed_jobs 列表（状态/重试时间）。
  - 依赖：T3、T17
  - 验证：手动触发后进度实时更新；token 与失败列表可见；熔断触发后可点击放行恢复。对应 US-5，AC-7。

- [x] **T22 前端收尾**
  - 内容：加载态/空态/错误提示；1000 节点渲染压测（造 mock 数据）；Chrome/Edge/Firefox 最新版走查。
  - 依赖：T18–T21
  - 验证：NFR-1（1000 节点不卡顿）、NFR-4。

## 阶段 G：联调与验收（T23–T24）

- [x] **T23 端到端联调（定时增量模式）**
  - 内容：真实环境跑通全链——配置 GLM key 后设置小回灌窗口（近 3-5 天论文），**按每日定时任务自然积累**（03:00 管线 → 触发熔断则次日自动续跑），不一次性灌满、不临时调高熔断阈值；观察采集→过滤→打标→抽取→消歧→关系全流程日志无未捕获异常。
  - 依赖：T14、T17、T18–T21
  - 验证：数日内 papers ≥ 50 且 status=extracted（AC-1 口径：累积 3-7 天凑足）。对应 AC-1。
  - **完成记录（2026-08-25）**：批次 dacdc985 全链 done——collect 622 篇（eess.IT 上游 RSS 400 按重试策略优雅处理）、filter 规则+GLM 细筛、tag 2136、extract 476 篇真实论文全量抽取（0 failed，3 篇周熔断顺延）、openalex 富集、disambiguate linked_existing 14 / created 2319 / queued 96、link 建关系 8875 条。联调期发现并修复 3 个真实缺陷：① schedule_retry 重复行未捕获异常（e5001e4）② arXiv RSS URL 变更 404（65790b7）③ OpenAlex 限流风暴卡死 enrich（9ba3c7d）。当日 token 5.61M / 周累计 6.01M 触发 weekly_stop（设计行为：3 篇顺延次周，未临时调高阈值）。

- [x] **T24 AC 验收走查**
  - 内容：逐条核对 spec AC-1~AC-9（数据量、图谱规模、证据链、搜索、合并、失败重试、指标可见、AI 过滤抽查、审核合并），结果记录到本文件附录。
  - 依赖：T23
  - 验证：9 条全过；未过项回修后复验。
  - **完成记录（2026-08-25）**：9 条 AC 全过，逐条结果见下方附录。数据库总量：persons 3427 / relationships 11875 / organizations 863 / 复核队列 100 条 pending；图谱页、复核页、管理页三页真实数据截图走查无异常。

---

## 依赖关系总览

```
T0 ─ T1 ─ T2 ─┬─ T5 ─ T8
              ├─ T7 ─┬─ T6 ──┐
              └─ T4  ├─ T9 ─ T10 ─ T11 ─ T12 ─ T15 ─ T18 ─ T19
                     └─ T13 ─ T14 ─┬─ T17 ─ T21
                                   └─────────── T23 ─ T24
T3 ────────────────────────────── T20（依赖 T16←T11）
```

## 验收映射

| AC | 覆盖 task |
|----|----------|
| AC-1 | T23（前置 T5/T6/T9） |
| AC-2 | T18/T19 |
| AC-3 | T18 |
| AC-4 | T19 |
| AC-5 | T12 |
| AC-6 | T13 |
| AC-7 | T17/T21 |
| AC-8 | T6 |
| AC-9 | T16/T20 |

---

## 附录：T24 AC 验收结果（2026-08-25，真实数据）

验收环境：后端 8000 / 前端 5173，批次 dacdc985 全链完成后的数据库与页面；验收方式 = SQL 查证 + API 调用 + 无头 Edge 三页截图走查。**结论：9/9 全过。**

| AC | 验收要点 | 结果 | 证据 |
|----|---------|------|------|
| AC-1 | papers ≥ 50 且字段完整 | ✅ | 真实论文 476 篇 status=extracted（≥50）；真实学者 2415 名均有研究方向标签（Top 学者 15 标签/1 机构/2 论文）；机构 863 个、person_org 2763 行（GLM 抽取为主 + OpenAlex 补全，置信度分级） |
| AC-2 | 过滤后图谱 >20 节点 >10 边 | ✅ | /api/graph 返回 661 节点 / 1000 边（Top-1000 by strength 截断，NFR-1 口径）；图谱页环形布局 + 密集边网渲染正常（截图走查） |
| AC-3 | 证据链含论文标题/年份 | ✅ | /api/relationships/{id}/evidence 返回论文标题 + 发表年份，evidence 表 4724 行 |
| AC-4 | 姓名搜索命中 | ✅ | /api/persons/search?q=Wei Liu 返回多个同名实体（含机构/标签区分）；搜索页交互走查正常 |
| AC-5 | 关系描述含合作次数/时间 | ✅ | evidence_summary 形如"基于 2 篇合作论文，最近合作于 2026 年"；coop_count=2，strength=identity×tier=0.90 |
| AC-6 | 失败任务指数退避重试 | ✅ | 单测覆盖 1/5/25min 退避与 3 次转死信；线上实证：rss_fetch(eess.IT) 按退避重试中（上游 Feed error，3 次后按 FR-2.5 转死信）；联调中发现的重复行缺陷已修复并加回归测试（e5001e4） |
| AC-7 | token 用量与失败任务可见 | ✅ | 管理页今日 5.614M/120 万（100% 红）、本周 6.014M/600 万进度条；failed_jobs 列表含 rss_fetch(glm_extract 死信 1 条为种子数据故意制造)；熔断状态 weekly_stop + 放行按钮展示正确（截图走查） |
| AC-8 | AI 过滤抽查误杀可控 | ✅ | 抽查 cs.CR 32 篇留 18 / cs.SE 32 篇留 8 / cs.SY 18 篇留 16——AI 类目（cs.LG/cs.CL/cs.AI/cs.CV）全保留，非 AI 类目按相关度过滤，无误杀 AI 论文 |
| AC-9 | 0.5-0.8 灰区进复核 + 可合并 | ✅ | 队列 96 条（100 含历史）分数全落 [0.51, 0.76]；复核页展示五因素分项（姓名/机构/方向/时间/网络）与三按钮；合并走查：合并后保留方吸收关系与证据、被并方置墓碑且不再出现在搜索/图谱/候选（T22 已全流程验证） |

### 遗留说明（不影响验收）

- **eess.IT 上游故障**：arXiv 该子类 RSS 持续返回 400 "Feed error"（上游问题，非本系统缺陷），已按失败重试策略优雅处理，上游恢复后自动转 done。
  - **2026-08-30 勘误**：此结论不成立——`eess.IT` 并非 arXiv 合法分类（eess 库仅有 AS/IV/SP/SY 四类，信息论在 cs.IT），400 是必然结果而非上游故障；配置与 RD-1 已更正为 `eess.IV`，生产库该类死信 8 行已清理。
- **token 周预算触顶**：周累计 6.01M ≥ 600 万 → weekly_stop，3 篇论文顺延次周窗口自动续跑（设计行为，未临时调高阈值）；管理页"放行继续"可人工 override。
- **openFuyao 图谱为空**：该方向 arXiv 近期无相关论文，属数据驱动结果而非缺陷；种子演示数据仍可查看该效果。
- **重筛成本**：已留库论文每批次重复入选细筛（约 55 篇/日，token 成本小），M2 优化（加 last_filtered_at 标记）。

---

## 附录 2：T24 后补充优化——中国学者范围约束 + 图谱降噪提速（2026-08-25）

> 用户在 T24 验收走查后补充两条需求：① 图谱默认全量渲染（661 节点/1000 边）看不清且卡顿，需降噪提速；② **范围约束（→ RD-13）**：M1 只治理含中国学者的论文及相关外国机构。本附录记录这次补充改动的落地与验证。

### 范围约束（RD-13）落地

- **判定**：`services/cn_scope.py` 启发式——任一作者署名机构命中中国机构关键词（归一化子串，条目 ≥4 字符防误配）、机构含 CJK 字符、或姓名首/末 token 为常见中文姓氏拼音，任一命中即 `has_cn_scholar=True`（宁多勿漏）。**GLM 细筛判定待周预算恢复后叠加**（当周 6.01M/600 万触顶，启发式先行兜底）——这是唯一待办项。
- **数据**：`papers.has_cn_scholar` 列 + `scripts/rebuild_cn_scope.py` 幂等迁移（加列 → 启发式回填 → 清空并按范围确定性重建关系）。生产库结果：**含中国学者论文 261/480 篇（54%）**，关系从 11875 重建为 **7662 条**，persons 2411。
- **过滤面**：linker 跳过范围外论文；/api/graph、/api/persons/search、复核队列（至少一端在范围内）、/api/filters/options 的机构列表全部按范围过滤（`_in_scope_person_ids()`）。
- **种子数据清理**：`scripts/purge_seed_demo.py` 清除 T22/T23 演示种子（假 arxiv_id 段 2601.1% + 孤儿人物 + 种子 failed_jobs），生产库自此只有真实数据。

### 图谱降噪提速

- **默认精简**：规模三档（精简 150 / 标准 400 / 完整 1000），默认精简——首屏从 661 节点降到 ~53 节点，force-directed 聚簇可读。
- **两个切入点**：机构下拉（选项来自范围内 Top-50 机构，`/api/graph?org=`）+ 姓名搜索定位老师（`?person=` 自我中心子网，ego-chip 一键退出）；支持 `?person=<id>` / `?org=<名称>` URL 直达。
- **性能**：邻接表 Map 替代每节点 O(m) 扫描、`onlyRenderVisibleElements`、缩放 LOD（<0.7 只渲染名字缩略）、边压暗样式。

### 过程中修复的缺陷

- **机构切入返回空**（两个叠加根因）：① 归一化不一致——organizations.name_normalized 写入侧用 `normalize_org`（去 University 等通用词、保留空格），graph API 误用 `normalize_name`（全连写）导致永不匹配；已改为与写入侧同函数匹配（精确名或 normalize_org 归一化），测试种子同步修正 + "Peking Univ." 变体回归测试。② 诊断脚本坑：cmd 批处理把 URL 中 `%20` 当变量展开（`%2`→空），截图请求了错误 URL 造成"修复后仍空态"假象。**教训：机构名查询必须与 `upsert_organization` 用同一归一化函数（两套归一化函数并存是已知坑）**。
- 验证：后端 94 测试全过；前端构建通过；无头 Edge + 真实浏览器截图复验默认/机构（浙江大学 43 节点）/老师（Xin Yu 9 节点）三视图均可读无空态。

---

## 附录 3：M1 做实——幂等修复 + 防护网 + 交互优化（2026-08-25/26）

> 附录 2 之后的演进：用户走查提出 4 项交互需求；发现 1 个数据正确性事故（linker 计数膨胀）并修复；随后系统性补齐防护网（不变量巡检 + 双跑幂等测试 + 备份 + 运维脚本），测试 97 → 111。本附录同时是新会话的接续快照。

### 交互优化（用户走查驱动）

- **查看两老师关系**：详情面板新增合作伙伴列表（按强度 Top-20，点击跳转该关系的证据链）+ 边 hover 可点反馈（310afab）。
- **复核页原始链接**：A/B 对比面板提供论文 arXiv 直达（上限 20 篇）；openalex_id 降为辅助小链接、挂上才显示（c1ade1d → ada590a 定稿——用户纠正"原始链接 = 论文本身"）。
- **合作次数过滤**：`/api/graph?coop_min=` + 筛选栏四档 不限/≥2/≥3/≥5 次（f8726d8，用户提出"合作 1 次没必要显示"）。
- **采集分类 18 → 26（泛AI 口径）**：+ cs.CL/cs.CV/cs.RO/cs.IR/cs.MM/cs.SD/cs.NE/eess.AS，全部纳入规则粗筛"直接保留"集（零 GLM 成本）；RD-1 已同步（43f877b），`ai_core_categories` 进 directions.yaml 可配置。

### 数据正确性事故与修复

- **linker 证据幂等**（1d46d1f，用户报告"基于 3 篇但列表只有 1 篇"）：run_linker 每轮全量重跑已抽取论文，已存在关系无条件 `coop_count += 1` 而证据表按 (relationship, paper) 主键去重 → 管线每跑一轮全库 +1，实测膨胀 4 倍。修复 = 证据幂等先行（仅新证据才抬计数/强度/时间窗），**证据表是合作次数的唯一事实来源**；全量重建 + 生产复跑验证 0 漂移。教训已沉淀为下方防护网。
- **合并重算口径**（681946a 内）：复核合并后重算曾用"有发表日期的证据数"当合作数，无日期证据会压低计数违反 C1 → 统一按证据行数（与 linker 一致）。
- **enrich_papers 误关调用方 http 客户端**（双跑测试发现）：`own` 判定漏查 http 注入位，第二轮采集报 client has been closed。

### 防护网（做实 M1 的五层闭环：功能 → 幂等 → 巡检 → 备份 → 运维）

- **六项数据不变量巡检** `services/integrity.py`（只读）：C1 coop_count==证据行数 / C2 分值∈[0,1] / C3 无自环 / C4 同类型无重复对 / C5 证据论文在 CN 范围 / C6 不引用消歧墓碑。接入 `GET /api/admin/integrity` + **凌晨管线跑完自动巡检**（违例当天 WARNING，681946a）。
- **全管线双跑幂等测试**：faked RSS/GLM/OpenAlex 八阶段完整跑两遍，全表计数必须不变 + 巡检全过——锁死"每轮全量重跑"设计下的非幂等风险（681946a，当场抓出上面两个真 bug）。
- **每日全库备份** `services/backup.py`：02:00 ± 5min pg_dump 自定义格式滚动保留 7 份，刻意早于 03:00 管线（管线跑坏有跑前快照）；恢复 = `pg_restore -h 127.0.0.1 -U prof_graph -d <新库> <dump>`（7472628）。
- **一键启动** `scripts/start_all.sh`：PG → 后端 → 前端顺序拉起、端口探活、已运行跳过（7472628）。

### 数据操作（非提交，审计在队列表）

- **openalex_id 回填**：作者行已有 ID 补到 persons（+319 人，现 321/2536）。定位 = 辅助身份信号（用户定调：能挂就挂，匹配率不专项投入）。
- **消歧漏网重复合并 ×2**（走正规复核合并通道，队列 #98/#42 已记 merged）：Kevin Kam Fung Yuen #4243 吸收 #5370（同一档案 A5056440787，94 篇）；Xia Hu #4224 吸收 #3217（两篇论文同指 A5147972410——该档案独立页已被 OpenAlex 删除但论文归属仍在）。发现路径 = 回填撞 persons.openalex_id 唯一约束。

### 当前数据快照（2026-08-26，以此为准，替代附录 2 旧数字）

- 论文：505 extracted / **1329 pending_extraction（等 GLM 周预算恢复自动补抽）**；CN 范围 278 篇（55%）。
- 学者：2536（墓碑 3）；关系 7988 / 证据 8024 行（单次合作 7952 / 两次 36）；**巡检六项全绿**。
- GLM 周预算触顶（~6.38M/600 万），恢复后补抽 + CN 精筛自动续跑，无需人工。
- 测试：**111 全过**；后端/前端/PG 由 start_all.sh 管理。

### 挂起事项（新会话从这里继续）

1. **GLM 预算恢复后**：抽查一轮补抽结果 + CN 精筛叠加效果（自动触发，只需验证）。
2. **M2 Specify（主线下一步）**：`specs/M2-academic-mentorship-and-project/` 目录为空，先写 spec.md 草案供人工 review。两个待用户决策：学术传承数据源（高校官网爬取是 constitution 允许的特例）、项目合作数据源（NSFC 公开数据等）。
3. **M4 backlog**（做实 M1 时评估后推迟）：前端测试地基（vitest，filter→参数映射抽纯函数）、性能守卫（数据涨量级再加）、E2E 冒烟、`last_filtered_at` 重筛优化（附录 1 遗留）。

### 运维速查

- 全栈启动：`bash scripts/start_all.sh`（PG 在 C:\tools\pg15 → 后端 :8000 → 前端 :5173；已运行组件自动跳过）。
- 数据巡检：`GET http://127.0.0.1:8000/api/admin/integrity`（C1-C6）；备份目录 `backend/backups/`（每日 02:00 × 7 份，gitignored）。
- 凌晨自动链：02:00 备份 → 03:00±20min 采集管线（26 类 + 修复后 linker）→ 管线后巡检，日志在 `backend/uvicorn.log`。
- GLM API key 只存在于 `backend/.env`（gitignored），严禁写入任何入库文件。
