# M2 — 学术传承 + 项目合作 · 任务清单（tasks.md）

> **阶段**：Tasks（tasks.md）⏳ 草案待评审（2026-08-27）
> **状态**：草案
> **上游**：spec.md ✅（RD-M2-1~12）/ plan.md ✅（2026-08-27 引导式 review，OQ-1~3 决议）

**工作约定（沿用 M1）**：
- Git：每 task 至少一次提交，格式 `M2-T{n}: <内容>`，直接进 main（单人内部项目无 PR）。
- 验收分工：AC-1/2/3/7/8/9/10 可自动验证的由实现者跑测试/查库自验；AC-4/5/6（观感/交互类）跑起来由用户人工抽查确认。
- 环境：Windows 开发机 + 便携版 PostgreSQL（M1 已就绪）；GLM key 沿用 `.env`。
- 新依赖仅 `beautifulsoup4`、`pypinyin`（plan §1 锁定，不得追加）。

**约定**：
- 每个 task 独立可交付、可验证；完成即勾选 `[x]`。
- `依赖` 列出必须先完成的 task；`验证` 是完成标准；`AC` 追溯到 spec 验收标准。
- 实现顺序即编号顺序；同阶段内无依赖的 task 可并行。
- 学术传承为主线（RD-M2-7），阶段 B1 优先于 B2；B2 内部可穿插。

---

## 阶段 B0：数据模型与配置（T0–T2）

- [ ] **T0 配置体系：sources.yaml + 加载器**
  - 内容：`backend/config/sources.yaml`（plan §8 定稿内容：RSS 3 源 + NISL/IPADS 两种子 + crawl 参数 rate_limit_seconds/depth_limit/recrawl_days）；加载器仿 directions.yaml（启动校验：seed.id 唯一、url 合法、page_type 枚举、rss.tier 枚举；进程内缓存）；`GET /api/filters/options` 增补 `relationship_types` 固定三项。
  - 依赖：—
  - 验证：单测覆盖配置加载（重复 id / 非法 tier / 空 seeds 报错；合法配置解析出 3 源 2 种子）。对应 FR-7.2 前置。

- [ ] **T1 数据库迁移：M2 DDL 增量**
  - 内容：SQLAlchemy 模型扩展 + Alembic 迁移（plan §2）：`persons` 加 title/homepage/email；`relationships` 加 subtype + 唯一键改造 `(a,b,type,subtype)`（存量 paper_cooperation 行 subtype='' 零迁移）；新表 web_pages / news_items / projects / relationship_evidence_pages / relationship_evidence_news；`person_org.source` 值域注释扩展 `'webpage'`。
  - 依赖：—
  - 验证：`alembic upgrade head` 在 M1 库上执行成功且存量数据完好（关系数/证据数不变）；空库执行成功；同对同人同 subtype 触发唯一冲突（单测）。
  - AC：AC-10 前置。

- [ ] **T2 不变量巡检扩展 C7–C10**
  - 内容：`integrity.py` 新增四项检查（plan §10）：C7 关系唯一含 subtype；C8 新类型证据非空（mentorship ≥1 条 pages/paper 证据，project ≥1 条 news 证据且 project 存在）；C9 新关系 confidence/strength ∈ [0,1]、subtype 枚举；C10 证据表无重复主键。`GET /api/admin/integrity` 输出兼容扩展。
  - 依赖：T1
  - 验证：构造违例 fixture 单测逐项报违例；干净库全绿。对应 NFR-5，AC-10。

## 阶段 B1：学术传承主线（T3–T8）

- [ ] **T3 定向爬虫 crawler.py**
  - 内容：plan §6 全项——种子到期计算（recrawl_days、首爬全量）；robots.txt（per-host 缓存 1 天，禁止则跳过记 warning）；per-host 限速 ≥rate_limit_seconds；深度 ≤depth_limit 且仅同 host、仅命中"成员/师资/毕业生/团队 | 中文姓名+称谓"链接模式的 <a> 跟进；BeautifulSoup 去导航/脚本取正文；SHA-256 内容指纹；快照 upsert web_pages（url 唯一，变化才置 pending_extraction）；失败写 failed_jobs（job_type=web_crawl），批次不中断；UA 按 NFR-1。
  - 依赖：T1
  - 验证：mock server 单测——正常入库含指纹；重复抓取内容不变不重置状态；robots 禁止路径未抓取；同 host 请求间隔 ≥2s；单页失败不中断批次。对应 FR-2.1~2.5、AC-1/7 前置。

- [ ] **T4 中文拼音归一 + 消歧强归并**
  - 内容：`utils/names.py` 扩展 normalize_cn（pypinyin 常用音拼接，中文姓名→拼音归一，与现有 normalize_name 体系合并；"张三"→"zhangsan" 与 "Zhang San" 归一一致）；`disambiguator.py` 扩展：find_candidates 候选集加拼音归一精确匹配；强归并规则（姓名归一命中含颠倒 + 机构为同一 organizations 实体 → 直接归并 identity=1.0，不进队列）；新中文名新建 Person 的 identity 基准 0.9（plan §4.1）。
  - 依赖：T1
  - 验证：单测——"张三"+"清华大学" vs Person("Zhang San", 清华) 强归并同 id；同名不同机构走打分入队；"San Zhang" 颠倒命中；多音字（"单"姓）不崩溃取默认音。对应 FR-4.1/4.2、AC-5。

- [ ] **T5 网页抽取 page_extractor.py**
  - 内容：plan §3.1——输入 content_text 截断（中文 24k chars）；GLM schema（lab_name/org_school/org_department/members[name,role,advisor,grad_year,title,homepage,email]/page_context）；维度分档映射表（page_context+页面类型→src；推导规则→infer；role 完整度→clarity；年份→time）；机构三级 upsert（university/department/lab，person_org.source='webpage'）；教师页 Person 字段回填（仅非空覆盖）；无信号 → status=no_signal；失败走 failed_jobs 退避重试；部分容错（无效成员跳过记 warning）；同页成员 >30 只产 advisor/同届/同门信号（400 对截断）。
  - 依赖：T3、T4（抽取后即消歧入库）
  - 验证：mock GLM 单测——正常名单入库 persons/person_org；个别成员缺 name 跳过；无信号短路；title/homepage 回填 persons；机构三级正确。对应 FR-3.2/3.6/3.7、AC-6。

- [ ] **T6 传承关系建立 mentor_linker.py**
  - 内容：plan §3.1 推导规则 + §4.1 公式——子类型四类推导（advisor 明示→mentor_student；同页 official_lab→same_lab；同 grad_year→same_cohort；同 advisor 不同成员→same_advisor，可与 same_lab 并存不同行）；confidence=0.4src+0.3infer+0.2clarity+0.1time + 同机构粒度加分（同组+0.10/系+0.05/院+0.03/校+0.01，cap 1.0）；identity（强归并 1.0/打分取分/新建 0.9）；strength=identity×subtype_base(0.95/0.90/0.85/0.75)×evidence_boost(≥2 独立来源 ×1.05 cap 1.0)；证据幂等合并（(rel,page) 主键已存在不重算；新证据按各维度历史最好分档重算，时间范围取并集）；evidence_summary 中文描述（如"基于实验室官网成员页，同实验室"）。
  - 依赖：T5
  - 验证：单测——四子类型各一例分档数值断言（对齐 plan §4.1 算例 0.95/0.62）；同页重抽证据幂等；同对人同 subtype 合并不重复；不同 subtype 并存两行。对应 FR-5.1~5.6、AC-2/9 前置。

- [ ] **T7 论文致谢信号扩展（RD-M2-8）**
  - 内容：`extractor.py` EXTRACT_SYSTEM 追加可选输出 mentorship_signals[{advisor,student,lab,hint}]（向后兼容）；仅全文输入时要求；student 为 null 的泛致谢由代码配对（作者唯一可对应才建，否则丢弃）；产出走 mentor_linker 建关系，证据挂 relationship_evidence(paper_id)，src 恒 0.6。
  - 依赖：T6
  - 验证：单测——mock 带 mentorship_signals 的响应建 mentor_student 且证据为论文；无该字段的老响应不受影响；泛致谢配不上作者时丢弃。对应 FR-3.3、RD-M2-8。

- [ ] **T8 爬取链路集成 + 实跑验收（NISL + IPADS）**
  - 内容：pipeline.py STAGES 增 `crawl → mentor_link`（排论文链路后，同批次进度/熔断）；scheduler 增爬取任务 cron 05:00+jitter600s；trigger-update 支持 scope="crawl"；对 NISL/IPADS 真实种子实跑一轮：校验页面数/抽取质量/子类型分布/组合截断阈值（plan OQ-3 调参项），IPADS 作为大页压力样本。
  - 依赖：T3~T7
  - 验证：实跑 `web_pages` ≥20（AC-1）、academic_mentorship ≥10 且证据可点击（AC-2）；重爬内容不变不重抽（AC-9）；不变量 C1–C10 全绿。对应 FR-2.6、AC-1/2/9。

## 阶段 B2：项目合作降级链路（T9–T12）

- [ ] **T9 RSS 采集 news_collector.py**
  - 内容：feedparser 拉取 sources.yaml 启用源；url 归一去重（条目 link/guid）；入库 news_items（缺 pubDate 用抓取时间）；信号预筛规则（中文姓名+称谓模式 或 "项目/课题/联合实验室/合作签约"关键词，否则 screened_no_signal 不进 GLM）；失败写 failed_jobs（job_type=news_fetch）不中断其他源；**连续 3 次拉取失败的源自动置内存态停用并在 metrics 报警**（plan OQ-2：不换配置文件本体，重启后按配置重试）。
  - 依赖：T0、T1
  - 验证：mock RSS fixture 单测——正常入库；重复条目跳过；预筛无信号短路；单源失败不中断批次；连续失败自动停用。对应 FR-1.1~1.5、AC-3 前置。

- [ ] **T10 资讯抽取 + projects 实体 news_extractor.py**
  - 内容：plan §3.3——输入标题+摘要（新闻公示页走 content_text，由 crawler 产出 page_type=news 页面路由到此）；GLM schema（no_signal/persons/projects/participations 含 explicitness、sufficiency 枚举）；分档映射；projects upsert（name_normalized 归一，同名项目归并）；无信号短路；失败退避重试。
  - 依赖：T9
  - 验证：mock GLM 单测——含 participations 条目正确建 projects 并关联；no_signal 短路；同名项目两篇报道归并同一 project id。对应 FR-3.1、RD-M2-6。

- [ ] **T11 项目关系建立 project_linker.py**
  - 内容：plan §4.2——同项目两两参与者建 project_cooperation；confidence=0.3explicit+0.3suff+0.2src+0.2access（高校新闻 src=1.0 / 配置 tier known_media=0.8；网页全文 0.8 / 仅摘要 0.5）；strength=identity×tier(1:0.90/2:0.95/3+:1.00)；证据幂等（(rel,news) 主键）；evidence_summary。
  - 依赖：T10、T4
  - 验证：单测——分档数值断言；同项目重抽取幂等；不同项目 A、B 合并证据 coop_count=2 strength 重查。对应 FR-6.1~6.5。

- [ ] **T12 资讯链路集成**
  - 内容：pipeline STAGES 增 `news_collect → news_extract → project_link`；scheduler 增 cron 04:00+jitter600s；trigger-update scope="news"；admin metrics 增各源最近拉取状态/停用标记。
  - 依赖：T9~T11
  - 验证：mock 全链单测（AC-3 自动验证部分）；实跑 1 个源拉取入库（不设数量指标）。对应 FR-1.6、AC-3/8。

## 阶段 B3：API 与前端（T13–T15）

- [ ] **T13 API 扩展**
  - 内容：plan §7——`GET /api/graph` 增 rel_types 参数（默认三类型全开，边载荷带 type/subtype）；`GET /api/relationships/{id}/evidence` 返回 papers[]/web_pages[]/news_items[] 混合（各含标题+URL+时间）；`GET /api/persons/{id}` 增 title/homepage（email 仅后台）；`GET /api/admin/metrics` 增 rss 源状态、crawl 种子状态、新 job_type token；`POST /api/admin/trigger-update` 增 scope。
  - 依赖：T8、T12
  - 验证：单测——rel_types 过滤正确；混合证据结构断言；scope 参数路由正确。对应 FR-7.1~7.4。

- [ ] **T14 前端：三类型边 + 筛选器 + 证据面板**
  - 内容：GraphCanvas 三线型（实线=论文合作 / 虚线=学术传承 / 点线=项目合作）+ 图例；FilterBar 增关系类型多选（默认全选）；EvidencePanel 混合证据列表（论文/网页快照/资讯三段，均可点击跳原文，网页证据展示快照时间）；学者详情增职位/主页字段；颜色/线型方案实现时以观感调优为准。
  - 依赖：T13
  - 验证：开发服起来人工核对三类型可区分、筛选生效、证据链接可打开、详情字段显示。对应 AC-4/6（用户抽查）。

- [ ] **T15 防护网扩展：双跑幂等 + 巡检验证**
  - 内容：M1 的全管线双跑幂等测试扩展覆盖新链路（crawl→mentor_link、news_collect→project_link 各双跑，断言关系数/强度/证据数不变）；C1–C10 全量巡检接入每日管线后日志（沿用 M1 模式）；启动脚本/README 快速开始补 M2 说明。
  - 依赖：T8、T12
  - 验证：CI 全绿（M1 111 测试 + 新增无回归）；双跑幂等断言通过。对应 NFR-5、AC-10。

## 阶段 B4：验收与归档（T16）

- [ ] **T16 AC-1~AC-10 验收走查 + 文档对齐**
  - 内容：逐条 AC 收集证据（自动项跑测试/查库截图，观感项请用户抽查确认）；specs/README 进度板更新；spec.md/tasks.md 勾选与附录记录遗留事项（如 rssbox 源稳定性观察、截断阈值实跑结论）。
  - 依赖：T8、T12~T15
  - 验证：AC 全过；进度板 implement ✅。对应全部 AC。

---

## 任务依赖总览

```
T0 ──────────────┐
T1 ── T2 ────────┤
 │               │
 ├─ T3 ─ T5 ─ T6 ─ T7 ─┐
 ├─ T4 ──┘      │      ├─ T8 ──┐
 │              │      │       │
 └─ T9 ─ T10 ─ T11 ── T12 ────┤
                             ├─ T13 ─ T14
                             └─ T15 ─ T16
```

## 实现规模预估

- 后端新增 ~6 个服务模块 + 3 处扩展，前端 3 个组件改造；预计 17 个 task，与 M1（T0–T24）量级相当或略小（基础设施全复用）。
- GLM 实跑成本：首批爬取一次性约 30 万 tokens（plan §8 估算），日常增量 <10 万/日，熔断无需调参。
