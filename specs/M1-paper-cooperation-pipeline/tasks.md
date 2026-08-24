# M1 — 论文合作全链路 · 任务清单（tasks.md）

> **阶段**：Tasks（tasks.md）✅ 已确认（2026-08-24，R1~R5 引导式 review）
> **状态**：定稿
> **最后更新**：2026-08-24
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

- [ ] **T0 开发环境准备（Windows + 便携版 PostgreSQL）**
  - 内容：下载 EDB PostgreSQL 15 binaries zip 解压到项目外部固定目录（如 `C:\tools\pg15`）；`initdb` 初始化数据目录 + 启动脚本（`pg_ctl start/stop`，端口 5432，密码写入 `.env`）；建库 `prof_graph` 与测试库 `prof_graph_test`；Python 版本检查脚本（要求 ≥3.11，不符则提示用 `uv` 安装固定版本）；`.env` 模板补齐 DATABASE_URL 与 `GLM_API_KEY=`（用户提供后填入）占位；`backend/scripts/env_check.py` 一键自检（PG 可连 / Python 版本 / Node 版本 / GLM key 已填）。
  - 依赖：—
  - 验证：`python backend/scripts/env_check.py` 四项全绿。

## 阶段 A：项目骨架（T1–T4）

- [ ] **T1 后端项目初始化**
  - 内容：`backend/` 目录、FastAPI 入口（`app/main.py`）、`pyproject.toml`（依赖：fastapi、sqlalchemy 2、apscheduler、asyncpg、pydantic、httpx、feedparser、pytest）、`.env.example`（GLM/数据库/OpenAlex mailto 配置）、lifespan 中启动/关闭 scheduler。
  - 依赖：—
  - 验证：`uvicorn app.main:app` 启动，`GET /health` 返回 200。

- [ ] **T2 数据库 schema 落地**
  - 内容：SQLAlchemy 模型（plan §2 全部 11 张表：papers / persons / person_research_tags / organizations / person_org / paper_authors / relationships（含 UNIQUE + CHECK）/ relationship_evidence / disambiguation_queue / failed_jobs / token_usage）；Alembic 初始迁移；`name_normalized` 归一化工具函数。
  - 依赖：T1
  - 验证：`alembic upgrade head` 在空库执行成功；11 张表存在，relationships 表含两个约束。

- [ ] **T3 前端项目初始化**
  - 内容：`frontend/` Vite + React 18 + TypeScript；React Flow、路由（Graph / Review / Admin 三页占位）；API client 封装；`.env`（后端地址）。
  - 依赖：—
  - 验证：`npm run dev` 启动，三个空页面可导航。

- [ ] **T4 配置体系：directions.yaml**
  - 内容：`backend/config/directions.yaml`（plan §8 定稿内容：3 产业方向 + 12 学术赛道 + 18 分类）；加载器（启动校验：id 唯一、关键词非空）；`GET /api/filters/options` 返回方向/赛道选项。
  - 依赖：T1
  - 验证：单测覆盖配置加载（缺 id/空关键词报错）；接口返回 3 方向 + 12 赛道。

## 阶段 B：采集与过滤（T5–T8）

- [ ] **T5 arXiv RSS 采集器**
  - 内容：`services/collector.py`——按 18 分类拉 RSS（feedparser），解析 arxiv_id/标题/摘要/作者列表/发布时间/分类/原始条目；`papers` 表 upsert（arxiv_id 唯一，重复跳过）；失败（超时/HTTP 错误）写 failed_jobs（job_type=rss_fetch），批次不中断。
  - 依赖：T2
  - 验证：mock RSS fixture 单测——正常入库、重复跳过、单分类失败不中断批次。对应 FR-1.1/1.6/1.7，AC-1 前置。

- [ ] **T6 AI 相关性过滤（两段式）**
  - 内容：`services/ai_filter.py`——第一段规则粗筛（cs.AI/cs.LG/stat.ML 直留；无 cs.*/stat.ML 交叉且不命中关键词表直剔；其余待定）；第二段 GLM 批量细筛（10 篇/请求，标题+摘要，返回 is_ai+理由）；过滤结果落 `papers.ai_relevant`，非 AI 论文不入正式库（status=filtered_out）。
  - 依赖：T5、T7（GLM client）
  - 验证：单测——纯数学论文（无 AI 关键词无 AI 交叉分类）被剔除；cs.LG 论文直接保留；待定项走 mock GLM 判定。对应 FR-1.2，AC-8。

- [ ] **T7 GLM 客户端 + 用量统计 + 分级熔断**
  - 内容：Anthropic 协议封装（GLM-5.2）——结构化 JSON 输出、超时/限流异常分类；每次调用记 token_usage；**双预算分级熔断**（日 120 万/周 600 万，.env 可配）：日 80% 告警 → 日 100% 停细筛 → 周触顶停抽取；**熔断后管理员可手动放行继续**（当日有效，操作记日志）；次日/次周自动恢复；熔断状态可查。
  - 依赖：T2
  - 验证：单测——mock token 累计到各级阈值，细筛/抽取分别被拒且待定论文放行；手动放行后恢复调用；跨天自动恢复。对应 NFR-2/NFR-3。

- [ ] **T8 业务方向打标器**
  - 内容：`services/tagger.py`——对过滤后论文的标题+摘要按 directions.yaml 关键词匹配（不区分大小写），写 `papers.directions` / `papers.tracks`（一篇可多标）。
  - 依赖：T4、T5
  - 验证：单测——含 "intent-based networking" 的论文标 network_autonomy + ADN；无命中的为空数组。对应 FR-1.3。

## 阶段 C：抽取与消歧（T9–T12）

- [ ] **T9 GLM 抽取器（含部分容错）**
  - 内容：`services/extractor.py`——输入策略（优先 arXiv HTML 全文截断 12k tokens，否则标题+摘要+作者列表）；输出按 plan §3 JSON Schema 校验；部分容错（能解析的作者入库，缺 name/seq 的跳过记 warning，整篇解析失败走 failed_jobs 重试）；论文 status 流转 pending_extraction → extracted / extraction_failed。
  - 依赖：T7、T5
  - 验证：单测——正常响应入库；5 作者中 1 个字段缺失只跳过 1 个；非法 JSON 触发 failed_jobs。对应 FR-2.1~2.3，RD-11。

- [ ] **T10 OpenAlex 客户端 + 机构双源补全**
  - 内容：`services/openalex.py`——authors 检索（mailto 参数、限 5 req/s）；与 GLM 抽取结果合并：GLM 有 affiliation → org_confidence=1.0；无 → OpenAlex 匹配（姓名精确 + 论文吻合）→ 0.8 并回写 openalex_id；均无 → 0.4 兜底；organizations upsert（归一化去重）、person_org 写入。
  - 依赖：T2、T9
  - 验证：单测（mock OpenAlex）——三源路径各自的 confidence 值；org 归一化合并（"Tsinghua University"/"Tsinghua Univ."）。对应 FR-2.4，RD-2。

- [ ] **T11 实体消歧器**
  - 内容：`services/disambiguator.py`——openalex_id 强匹配直归并；否则生成候选集（**准确度优先，不考虑性能**：name_normalized 精确同名 + 模糊名变体（编辑距离阈值内、姓名序颠倒双向查询）全部入候选），对全部候选做 5 因素加权打分（姓名 30% 编辑距离 / 机构 25% / 研究方向 20% Jaccard / 时间 15%（活跃区间自动聚合）/ 合作网络 10%）取最高分；≥0.8 自动归并、0.5–0.8 入 disambiguation_queue（含分项得分）、<0.5 新建 Person；paper_authors 关联。
  - 依赖：T10
  - 验证：单测——同人不同写（含姓名序颠倒、缩写变体）归并（≥0.8）；重名不同人新建（<0.5）；中间分数入队且 score_detail 完整。对应 FR-3.1~3.3，AC-9 前置。

- [ ] **T12 关系建立器（linker）**
  - 内容：`services/linker.py`——对每篇论文两两作者建 paper_cooperation：ID 排序防重；identity_confidence=0.4×name+0.6×org；strength 阶梯（1次0.85/2次0.90/3-4次0.95/5次+1.00）× identity；已存在则 coop_count+=1、重算 strength、追加 relationship_evidence、更新时间范围与 evidence_summary（"基于 N 篇合作论文，最近合作于 YYYY 年"）。
  - 依赖：T11
  - 验证：单测——同对作者 3 篇论文只 1 条关系，coop_count=3，strength=0.95×identity，证据 3 条，摘要正确；反向顺序（B,A）不产生第二条。对应 FR-4.1~4.5，AC-5。

## 阶段 D：失败处理与调度（T13–T14）

- [ ] **T13 failed_jobs 重试 + 死信**
  - 内容：`services/failed_jobs.py`——指数退避（next_retry_at = now + 1/5/25min）最多 3 次，仍失败置 dead；重试执行器按 job_type 回调对应服务；死信可经 CLI 命令手动重跑。
  - 依赖：T7、T9
  - 验证：单测——失败后 next_retry_at 间隔为 1/5/25 分钟序列；3 次后 status=dead；CLI 重跑 dead 任务成功转 done。对应 FR-2.5，AC-6。

- [ ] **T14 APScheduler 任务注册**
  - 内容：`scheduler.py`——①采集管线（cron 03:00 + jitter 1200s：采集→过滤→打标→抽取→消歧→关系全链）；②failed_jobs 重试扫描（interval 60s）；③死信巡检（cron 08:00 记日志）。
  - 依赖：T6、T8、T9、T13
  - 验证：单测——job 注册数量与触发配置正确；集成测试手动触发管线全链跑通 fixture。对应 FR-1.1。

## 阶段 E：API（T15–T17）

- [ ] **T15 图谱与查询 API**
  - 内容：`GET /api/graph`（direction/track/strength_min/limit 过滤，按 strength 降序 Top 1000，返回 nodes+edges）；`GET /api/persons/search?q=&type=name|org`（LIKE 限 20）；`GET /api/persons/{id}`（详情含机构/研究方向/论文）；`GET /api/relationships/{id}/evidence`（证据论文列表）。
  - 依赖：T12
  - 验证：pytest + 测试库——graph 过滤各参数生效；search LIKE 命中；evidence 返回标题/年份。对应 FR-5.1~5.5。

- [ ] **T16 审核队列 API**
  - 内容：`GET /api/disambiguation?status=pending`（含 score_detail）；`POST .../merge`（body: keep=A|B——迁移关系/证据/标签/机构归属，重算受影响关系，记 merged）；`POST .../reject`（记 rejected，持久化"A≠B"结论，后续同对组合不再入队，新作者仍正常匹配 A/B）。
  - 依赖：T11
  - 验证：单测——merge 后两 Person 合一且 coop_count/strength 重算正确；reject 后同对不再入队、新相似作者仍可入队。对应 FR-3.4，AC-9。

- [ ] **T17 管理端 API**
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

- [ ] **T23 端到端联调（定时增量模式）**
  - 内容：真实环境跑通全链——配置 GLM key 后设置小回灌窗口（近 3-5 天论文），**按每日定时任务自然积累**（03:00 管线 → 触发熔断则次日自动续跑），不一次性灌满、不临时调高熔断阈值；观察采集→过滤→打标→抽取→消歧→关系全流程日志无未捕获异常。
  - 依赖：T14、T17、T18–T21
  - 验证：数日内 papers ≥ 50 且 status=extracted（AC-1 口径：累积 3-7 天凑足）。对应 AC-1。

- [ ] **T24 AC 验收走查**
  - 内容：逐条核对 spec AC-1~AC-9（数据量、图谱规模、证据链、搜索、合并、失败重试、指标可见、AI 过滤抽查、审核合并），结果记录到本文件附录。
  - 依赖：T23
  - 验证：9 条全过；未过项回修后复验。

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
