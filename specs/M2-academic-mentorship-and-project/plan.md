# M2 — 学术传承 + 项目合作 · 技术方案（plan.md）

> **阶段**：Plan（plan.md）⏳ 草案待评审（2026-08-26）
> **状态**：草案
> **上游**：spec.md ✅ 已确认（RD-M2-1~12；项目合作降级 RD-M2-7，学术传承为主线）

---

## 1. 总体架构（M1 基础上的增量）

```
┌────────────────────────── 后端 (FastAPI, Python 3.11) ─────────────────────────┐
│                                                                                │
│  【主线：学术传承】                                                              │
│  APScheduler ─→ Crawler ─→ web_pages 表 ─→ PageExtractor(GLM) ──┐              │
│  (每日检查到期种子)  种子配置 sources.yaml    （成员/师生/分组信号）  │              │
│                                                                ↓              │
│  M1 论文管线（每日 03:00）─→ Extractor 顺带抽致谢信号 ──→ MentorLinker         │
│                                              （RD-M2-8 二级来源）  │              │
│                                                                    ↓          │
│                    消歧扩展（中文↔拼音归一 + 强归并 RD-M2-12）→ relationships    │
│                                                                    (academic_  │
│  【降级链路：项目合作】                                               mentorship) │
│  APScheduler ─→ NewsCollector ─→ news_items ─→ 信号预筛 ─→ NewsExtractor(GLM)  │
│  (每日 04:00)     RSS 源配置        （规则粗筛）    （人员/项目/参与事实）        │
│                                                                    ↓          │
│                                        ProjectLinker → projects + relationships │
│                                                                    (project_   │
│                                                                     cooperation)
│  复用不动：failed_jobs 重试 / token 双预算熔断 / 审核队列 / 备份 / 不变量巡检     │
└────────────────────────────────────────────────────────────────────────────────┘
                                        │
┌─────────────────────── 前端 (React 18 + React Flow) ───────────────────────────┐
│  图谱页：三类型边（实线=论文合作 / 虚线=学术传承 / 点线=项目合作）+ 类型筛选器      │
│  证据面板：论文/网页快照/资讯 三类证据混合列表（均可点击跳原文）                    │
│  学者详情：新增职位/主页字段                                                      │
└────────────────────────────────────────────────────────────────────────────────┘
```

**处理管线**（新增两条，与 M1 管线并行编排进 `run_pipeline`）：

```
学术传承：种子到期 → 定向爬取(robots/限速/深度限制) → 内容指纹比对 → 新/变化页面
  → GLM 抽取(成员名单/师生信号/分组信号) → 中文消歧 → 建 academic_mentorship(+subtype, +网页证据)
论文致谢：M1 extract 阶段顺带输出 mentorship_signals → 消歧 → 同上(+论文证据, 可信度 0.6 低档)
项目合作：RSS 拉取 → 去重(url) → 信号预筛(规则) → GLM 抽取(人员/项目/参与)
  → 消歧 → 建/更 projects 实体 → 建 project_cooperation(+资讯或网页证据)
```

**技术栈增量**（不违反 constitution 锁定）：`beautifulsoup4`（HTML 解析）、`pypinyin`（中文姓名转拼音）。

---

## 2. 数据模型（DDL 增量）

### 2.1 现有表变更

```sql
-- persons：RD-8 遗留字段落地（FR-3.6，仅教师主页抽取，缺失留 NULL）
ALTER TABLE persons
    ADD COLUMN title    VARCHAR(100),   -- 职位/职称：教授 / 长聘副教授 / ...
    ADD COLUMN homepage TEXT,           -- 个人主页 URL
    ADD COLUMN email    VARCHAR(200);   -- 电子邮箱

-- relationships：type 扩展 + subtype（RD-M2-2 四子类型）
-- 同一对人可同时存在 same_lab 与 mentor_student → 唯一键从 (a,b,type) 改为 (a,b,type,subtype)
ALTER TABLE relationships
    ADD COLUMN subtype VARCHAR(30) NOT NULL DEFAULT '',  -- ''(论文/项目) / mentor_student /
        same_lab / same_advisor / same_cohort            -- 学术传承子类型
    DROP CONSTRAINT relationships_person_a_id_person_b_id_type_key,
    ADD CONSTRAINT uq_rel_pair_type_subtype UNIQUE (person_a_id, person_b_id, type, subtype);
-- 存量 paper_cooperation 行 subtype='' 即满足新唯一键，无需数据迁移
```

`relationships.coop_count` 语义按 type 区分（列复用，不改结构）：论文合作=合作论文数（M1 不变）、项目合作=共同项目数、学术传承=独立证据条数（只作展示，不进强度公式）。

### 2.2 新表

```sql
-- 爬取页面快照（学术传承主线证据 + 高校新闻公示页）
CREATE TABLE web_pages (
    id           BIGSERIAL PRIMARY KEY,
    url          TEXT UNIQUE NOT NULL,
    seed_id      VARCHAR(100) NOT NULL,          -- 来源种子标识（sources.yaml 的 seed.id）
    page_type    VARCHAR(30) NOT NULL,           -- faculty / lab_members / grad_list / news
    title        TEXT,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_text TEXT,                           -- 去导航/脚本后的正文
    content_hash VARCHAR(64),                    -- SHA-256，增量重爬跳过用
    status       VARCHAR(20) NOT NULL DEFAULT 'pending_extraction',
                 -- pending_extraction / extracted / no_signal / extraction_failed
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_extracted_hash VARCHAR(64)              -- 上次已抽取内容的指纹（区分"变了"与"没变"）
);

-- 资讯条目（RSS 源，项目合作证据）
CREATE TABLE news_items (
    id           BIGSERIAL PRIMARY KEY,
    source_id    VARCHAR(50) NOT NULL,           -- sources.yaml 的 rss.id
    url          TEXT UNIQUE NOT NULL,           -- 去重键（link/guid 归一）
    title        TEXT NOT NULL,
    summary      TEXT,
    published_at TIMESTAMPTZ,                    -- 条目缺 pubDate 时用抓取时间
    rss_entry    JSONB,                          -- 原始条目（审计）
    status       VARCHAR(20) NOT NULL DEFAULT 'pending_screen',
                 -- pending_screen / screened_no_signal / extracted / no_signal / extraction_failed
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 项目轻量实体（RD-M2-6）
CREATE TABLE projects (
    id             BIGSERIAL PRIMARY KEY,
    name           VARCHAR(300) NOT NULL,
    name_normalized VARCHAR(300) NOT NULL,
    project_type   VARCHAR(50),                  -- 国家重点研发 / 省市科技项目 / 企业合作 / 联合实验室 / other
    time_start     DATE,
    time_end       DATE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name_normalized)
);

-- 证据表扩展：分类型证据表（RD-M2-1 落实，沿用 M1 模式，避免泛化改造）
CREATE TABLE relationship_evidence_pages (
    relationship_id BIGINT REFERENCES relationships(id) ON DELETE CASCADE,
    web_page_id     BIGINT REFERENCES web_pages(id) ON DELETE CASCADE,
    PRIMARY KEY (relationship_id, web_page_id)
);
CREATE TABLE relationship_evidence_news (
    relationship_id BIGINT REFERENCES relationships(id) ON DELETE CASCADE,
    news_item_id    BIGINT REFERENCES news_items(id) ON DELETE CASCADE,
    PRIMARY KEY (relationship_id, news_item_id)
);
-- 论文证据沿用 relationship_evidence(paper_id)（含致谢信号证据，RD-M2-8）
```

**机构粒度**（FR-4.3）：复用 `organizations.level`（university / department / lab 已有值域），页面抽取出的"学校/院系/实验室"三级分别 upsert，`person_org.source` 扩展 `'webpage'` 取值。

---

## 3. GLM 抽取 · JSON Schema（RD-M2-1/8 落实）

### 3.1 网页抽取（教师主页 / 实验室成员页 / 毕业生名单页）

输入：`content_text` 截断 12k tokens（约 24k 汉字/字符，中文按 ~2 chars/token 估）。

```json
{
  "type": "object", "required": ["members"],
  "properties": {
    "lab_name":        { "type": ["string", "null"] },
    "org_school":      { "type": ["string", "null"] },   // 学校
    "org_department":  { "type": ["string", "null"] },   // 院系
    "members": { "type": "array", "items": {
      "type": "object", "required": ["name"],
      "properties": {
        "name":  { "type": "string" },
        "role":  { "enum": ["professor", "associate_professor",
                            "assistant_professor", "phd", "master", "alumni", "unknown"] },
        "advisor":     { "type": ["string", "null"] },   // 该成员的导师（页面明示时）
        "grad_year":   { "type": ["integer", "null"] },
        "title":       { "type": ["string", "null"] },   // Person 扩展字段（教师主页）
        "homepage":    { "type": ["string", "null"] },
        "email":       { "type": ["string", "null"] }
      }}},
    "page_context": { "enum": ["official_lab", "department_site", "grad_list", "unclear"] }
  }
}
```

**关系推导规则**（代码完成，不让 GLM 配对）：
- 成员有 `advisor` → 与导师（若导师也在 members 或可消歧到 Person）建 `mentor_student`；导师不在页面时仅当导师可消歧成功才建。
- 同页面 members 且 `page_context=official_lab` → 两两建 `same_lab`（同页成员数上限截断：>30 人时只建 advisor 关系 + 同届/同门分组，防组合爆炸，N² 上限 400 对）。
- `grad_list` 页 + 同 `grad_year` → 建 `same_cohort`。
- 导师相同的不同成员 → `same_advisor`（优先于 same_lab，两者可并存——subtype 不同行）。

**维度分档映射**（FR-5.2 输入，GLM 输出枚举 → 代码查表）：

| 维度 | 取值来源 | 分档 |
|------|---------|------|
| 数据源可信度 | `page_context` + 页面类型种子 | official_lab 1.0 / department_site·grad_list 0.8 / unclear 0.6（致谢来源恒 0.6，RD-M2-8） |
| 推断确定性 | 推导规则本身 | advisor 明示 1.0 / 同页同类目 0.9 / 同页同列表 0.8 / 同 grad_year 0.7 |
| 信息明确性 | role 完整度 | role≠unknown 1.0 / role=unknown 0.6 |
| 时间吻合度 | grad_year/页面时间 | 明确年份 1.0 / 无年份 0.5 |

### 3.2 论文致谢信号（M1 extractor 扩展，RD-M2-8）

M1 `EXTRACT_SYSTEM` 输出 schema **追加可选字段**（向后兼容，老 prompt 结果无此字段照常）：

```json
"mentorship_signals": [{ "advisor": "姓名", "student": "姓名或null(泛指)",
                          "lab": "实验室名或null", "hint": "acknowledgement 原文片段" }]
```

仅在全文输入时抽取（摘要无致谢）。student 为 null 的泛致谢（"感谢导师X"且作者唯一可对应）由代码尝试作者↔advisor 配对，配不上则丢弃。

### 3.3 资讯抽取（RSS 资讯 + 高校新闻公示页共用）

输入：标题+摘要（RSS 条目通常无全文；新闻公示页用 content_text）。

```json
{
  "type": "object", "required": ["no_signal"],
  "properties": {
    "no_signal":  { "type": "boolean" },          // true = 无人员/项目参与信号
    "persons":  { "type": "array", "items": { "type": "object",
      "required": ["name"], "properties": {
        "name": {}, "org": { "type": ["string","null"] }, "role": { "type": ["string","null"] } }}},
    "projects": { "type": "array", "items": { "type": "object",
      "required": ["name"], "properties": {
        "name": {}, "project_type": { "type": ["string","null"] },
        "time_start": { "type": ["string","null"] }, "time_end": { "type": ["string","null"] } }}},
    "participations": { "type": "array", "items": { "type": "object",
      "required": ["person_name", "project_name"],
      "properties": {
        "person_name": {}, "project_name": {},
        "explicitness": { "enum": ["listed_members", "stated_participation",
                                    "implied", "vague"] },
        "sufficiency":  { "enum": ["detailed_role", "role_stated",
                                    "mentioned", "minimal"] } }}}
  }
}
```

**置信度分档映射**（FR-6.2）：explicitness → 1.0/0.8/0.5/0.3；sufficiency → 1.0/0.8/0.5/0.3；数据源可信度 → 高校官网新闻 1.0 / 知名 AI 媒体（配置标 vip）0.8 / 其他 0.6；可访问性 → 网页全文 0.8 / 仅摘要 0.5（资讯场景固定这两档）。

**信号预筛**（FR-1.4，规则零成本）：标题+摘要不含任何中文姓名模式（2-4 字中文名 + 教授/博士/老师/院士 等称谓词）且不含"项目/课题/联合实验室/合作签约"关键词 → `screened_no_signal`，不进 GLM。

---

## 4. 置信度与强度数值定稿（RD-M2-3/4 落实）

### 4.1 学术传承 `academic_mentorship`

```
confidence = 0.4×src + 0.3×infer + 0.2×clarity + 0.1×time   （分档见 §3.1）
           + same_org_bonus                                  （同组+0.10/同系+0.05/同院+0.03/同校+0.01，cap 1.0）
identity_confidence = 强归并 1.0 / 打分归并取消歧得分 / 新建 Person 0.9（中文名新建略降）
strength = identity × subtype_base × evidence_boost
  subtype_base：mentor_student 0.95 / same_advisor 0.90 / same_lab 0.85 / same_cohort 0.75
  evidence_boost：独立证据 ≥2 来源 → ×1.05 封顶 1.0；单来源 ×1.0
```

示例：官方实验室页（src 1.0）、advisor 明示（infer 1.0）、role 明确（clarity 1.0）、无年份（time 0.5）→ confidence = 0.4+0.3+0.2+0.05 = 0.95；同实验室 +0.10 → cap 1.0。identity 1.0 → strength = 1.0×0.95×1.0 = 0.95。
致谢来源（src 0.6、infer 0.7、clarity 0.6、time 0.5）→ 0.24+0.21+0.12+0.05 = 0.62，无同机构加分 → 0.62；identity 0.9 → strength 0.9×0.85(same_lab) ≈ 0.77 的中档弱证据，符合设计原则。

### 4.2 项目合作 `project_cooperation`

```
confidence = 0.3×explicitness + 0.3×sufficiency + 0.2×src + 0.2×accessibility
identity_confidence 同上
strength = identity × tier(共同项目数)：1 个 0.90 / 2 个 0.95 / 3+ 1.00
```

### 4.3 合并语义（FR-5.5 / FR-6.4）

沿用 M1 linker 的**证据幂等模式**（证据表是事实来源）：
- `(relationship, page/news/paper)` 证据主键已存在 → 不重算、不重复计。
- 新增证据 → 按最新证据重算 confidence（取各维度历史最好分档），strength 按公式重查；学术传承 `evidence_boost` 数独立来源（web_pages 种子数 + 论文数）。
- 时间范围取证据时间的并集。

---

## 5. 中文人名消歧（RD-M2-12 落实）

```
新中文名 → normalize_cn(name)：中文转拼音（pypinyin，取常用音）拼接小写
    例："张三" → "zhangsan"；"王小明" → "wangxiaoming"
  ↓ 与现有 normalize_name 体系合并：persons.name_normalized 写拼音归一值，
    "Zhang San" 归一后同为 "zhangsan"（swap_name_order 已处理"San Zhang"）
  ↓
① 强归并：姓名归一命中（含颠倒）且 机构为同一 organizations 实体（同校或同院系）
    → 直接归并，identity=1.0，不进队列
② 其余走 M1 五因素打分（机构/研究方向/时间/合作网络因素照旧；
    中文重名高发，阈值不动：≥0.8 自动 / 0.5–0.8 入队 / <0.5 新建）
```

**多音字策略**：pypinyin 默认常用音；归一失配的落入打分或队列，由审核兜底，不做姓名字典。`find_candidates` 候选集扩展：拼音归一精确 + 现有编辑距离模糊。

---

## 6. 爬虫设计（FR-2 / NFR-1 落实）

| 项 | 设计 |
|----|------|
| 调度 | 每日 05:00 扫 `web_pages`：新种子全爬、老页面按 `recrawl_days`（默认 7）到期重爬 |
| robots.txt | `urllib.robotparser` 每 host 缓存 1 天；禁止则跳过该 URL，记 warning 日志 |
| 限速 | 每 host 相邻请求 ≥ `rate_limit_seconds`（默认 2s），进程内 per-host 时间戳 |
| 深度 | 种子入口页 depth=0，仅跟进指向**同 host** 且命中成员/列表链接模式的 <a>，depth ≤ `depth_limit`（默认 1，即入口页+一层） |
| 解析 | BeautifulSoup：去 nav/script/footer，取正文文本；提取 <a> 文本含 中文姓名+称谓 或 "成员/师资/毕业生/团队" 的链接 |
| UA | `prof-graph/0.2 (academic-network-governance; internal)`（NFR-1） |
| 失败 | 复用 failed_jobs（job_type=`web_crawl`），退避重试 3 次后死信 |

**新闻公示页**（RD-M2-11）：作为 `page_type=news` 的种子，爬取后走 §3.3 资讯抽取路径，数据源可信度记 1.0 档。

---

## 7. API 契约扩展

| Method | Path | 变更 | 对应 |
|--------|------|------|------|
| GET | `/api/graph` | 新增 `rel_types=paper_cooperation,academic_mentorship,project_cooperation`（逗号分隔，默认全三；subtype 不入筛选） | FR-7.1/7.2 |
| GET | `/api/relationships/{id}/evidence` | 返回混合证据：`papers[] / web_pages[](title,url,fetched_at) / news_items[](title,url,source,published_at)` | FR-7.3 |
| GET | `/api/persons/{id}` | 详情加 `title / homepage`（email 后台可见，图谱页不展示） | FR-7.4 |
| GET | `/api/filters/options` | 加 `relationship_types`（固定三项，前端筛选器选项） | FR-7.2 |
| POST | `/api/admin/trigger-update` | 加 body `scope: "arxiv"(默认) \| "news" \| "crawl" \| "all"` | FR-1.6/2.6 |
| GET | `/api/admin/metrics` | 加 rss 各源最近拉取状态、crawl 各种子状态、新 job_type 的 token 用量 | NFR-3 |
| GET | `/api/admin/integrity` | 不变量巡检扩展项（见 §10） | NFR-5 |

---

## 8. 调度与配置

**APScheduler 新增任务**：

| 任务 | 触发 | 说明 |
|------|------|------|
| RSS 资讯采集 | cron `04:00` + jitter 600s | 拉取 → 预筛 → GLM 抽取 → 项目关系（降级链路） |
| 高校爬取 | cron `05:00` + jitter 600s | 到期种子重爬 → 变化页面抽取 → 传承关系（主线） |
| failed_jobs 重试 / 死信巡检 / 备份 | 不变 | job_type 自然扩展覆盖新链路 |

**配置文件 `backend/config/sources.yaml`**（结构定稿，加载器仿 directions.yaml：启动校验 + 缓存）：

```yaml
rss:
  - id: qbitai            # 量子位（官方 feed，2026-08-26 实测可用）
    url: https://www.qbitai.com/feed
    tier: known_media     # 数据源可信度档：0.8
    enabled: true
  - id: jiqizhixin        # 机器之心（官方 /rss 已失效；rssbox 微信源，公益源稳定性待观察）
    url: https://decemberpei.cyou/rssbox/wechat-jiqizhixin.xml
    tier: known_media
    enabled: true
  - id: xinzhiyuan        # 新智元（同上 rssbox）
    url: https://decemberpei.cyou/rssbox/wechat-xinzhiyuan.xml
    tier: known_media
    enabled: true
  # 实测不可用、注释保留：
  # - id: jiqizhixin_official  (https://www.jiqizhixin.com/rss → 返回 HTML，非 feed)
  # - id: rsshub_demo          (https://rsshub.app/... → 403)

crawl:
  rate_limit_seconds: 2
  depth_limit: 1
  recrawl_days: 7
  seeds:
    # 首批 2-3 所 AI 强校 CS/EE 院系 + 重点实验室（RD-M2-9）
    # 具体入口 URL 于 tasks 实现前由用户提供定稿，以下为占位结构示例：
    - id: tsinghua-cs-nisi
      school: 清华大学
      org_path: 计算机系 / 网络与信息安全研究室
      url: https://www.cs.tsinghua.edu.cn/...(待用户提供)
      page_type: lab_members
    - id: pku-ee-xxx
      school: 北京大学
      org_path: 电子学院 / ...(待用户提供)
      url: ...(待用户提供)
      page_type: faculty
```

**成本预算**（NFR-2）：沿用日 120 万 / 周 600 万双预算。常规增量估算：资讯约 60 条/日（3 源 ×20），预筛后约 30% 进抽取 ×800 tokens ≈ 1.5 万/日；爬取首批约 50-100 页一次性 + 每周增量，单页约 3k tokens ≈ 首批 30 万一次性 + 日常 <2 万/日；致谢追加输出 <200 tokens/篇 ×250 篇 ≈ 5 万/日。合计日常增量 <10 万/日，远低于现有余量，**无需调阈值**。

---

## 9. 项目结构增量

```
backend/app/
├── services/
│   ├── crawler.py          # 高校官网定向爬取（§6）
│   ├── news_collector.py   # RSS 拉取 + 去重 + 信号预筛
│   ├── page_extractor.py   # 网页 GLM 抽取（§3.1）+ Person 字段补全
│   ├── news_extractor.py   # 资讯/新闻页 GLM 抽取（§3.3）+ projects upsert
│   ├── mentor_linker.py    # academic_mentorship 建立/合并（§4.1）
│   ├── project_linker.py   # project_cooperation 建立/合并（§4.2）
│   └── （extractor.py 扩展致谢信号；disambiguator.py 扩展拼音归一 + 强归并）
├── config/sources.yaml     # §8
└── alembic/versions/xxxx_m2.py   # persons 加列 / relationships.subtype + 唯一键改造 / 4 张新表

frontend/src/
├── components/GraphCanvas  # 三类型边样式 + 图例
├── components/FilterBar    # 关系类型多选筛选器
└── components/EvidencePanel # 混合证据列表（论文/网页/资讯）
```

`pipeline.py` STAGES 扩展：`[collect, filter, tag, extract, openalex, cn_scope, disambiguate, link, crawl, mentor_link, news_collect, news_extract, project_link]`——爬取/资讯排在论文链路后，共享同一批次的熔断与进度上报；`trigger-update` 的 scope 参数决定跑全链还是子集。

---

## 10. 数据不变量扩展（NFR-5）

M1 六项（C1-C6）基础上新增：

- **C7** 关系唯一性（含 subtype）：`relationships` 无 (a,b,type,subtype) 重复（唯一键保证，巡检确认无 CHECK 违例）。
- **C8** 新类型证据非空：每条 `academic_mentorship` 至少 1 条 pages/paper 证据；每条 `project_cooperation` 至少 1 条 news 证据且关联 project 存在。
- **C9** 置信度/强度域：两类新关系 confidence、strength ∈ [0,1]，subtype ∈ 枚举。
- **C10** 证据幂等：pages/news 证据表无重复主键；（relationship, paper) 语义沿用 C 系列。

全管线双跑幂等测试扩展：`crawl → mentor_link` 与 `news_collect → project_link` 各双跑一次，断言关系数/强度/证据数不变。

---

## 11. 验收标准 → 实现映射

| AC | 验证方式 | 涉及模块 |
|----|---------|---------|
| AC-1 | 配置 2 校种子后手动触发 crawl，`web_pages` ≥20 条且 content_hash 非空 | crawler 单测 + 实跑 |
| AC-2 | `mentor_linker` 实跑产出 ≥10 条 academic_mentorship，`GET /api/relationships/{id}/evidence` 返回可点击网页/论文证据 | mentor_linker, api |
| AC-3 | mock RSS fixture：预筛、no_signal 短路、含 participations 的条目建关系；对实跑不设数量指标 | news_collector/extractor/project_linker 单测 |
| AC-4 | `GET /api/graph?rel_types=academic_mentorship` 仅返回传承边；前端三线型渲染 + 图例 | api/graph, GraphCanvas |
| AC-5 | 构造 Person("Zhang San")+机构 与网页"张三"同机构，断言强归并同一 id；中英不同机构场景入队 | disambiguator 单测 |
| AC-6 | 教师主页 fixture 抽取 title/homepage 回填 persons | page_extractor 单测 |
| AC-7 | mock robots.txt 禁止路径未抓取；限速时间戳间隔 ≥2s | crawler 单测 |
| AC-8 | mock HTTP 500 / GLM 失败 → failed_jobs(job_type=web_crawl/news_fetch/glm_extract)，退避间隔 1/5/25min | failed_jobs 单测 |
| AC-9 | 同 URL 重爬 hash 不变 → 不调 GLM、关系不变；hash 变化 → 重抽取且 (rel,page) 证据幂等 | crawler + mentor_linker 双跑单测 |
| AC-10 | integrity 巡检 C1-C10 通过；扩展双跑幂等测试绿 | integrity + pipeline 测试 |

---

## 12. Open Questions（plan 阶段）

1. **高校种子入口 URL**（RD-M2-9 遗留）：结构已定（§8），具体 2-3 所学校的入口 URL 需要**用户提供**——建议 tasks 清单把它列为 T0 前置输入，评审 plan 时若能直接给出更好。
2. **rssbox 公益源长期稳定性**：机器之心/新智元走个人公益 rssbox，存在失效风险。方案：`news_collector` 对连续失败源自动置 `enabled: false` 并在 metrics 报警，管理员换源改配置即可。是否需要备用源清单第二梯队（AI科技评论/智源等）→ 实现阶段顺手实测补充，不阻塞。
3. **同页大实验室组合爆炸截断阈值**（§3.1 的 400 对上限）：首个真实实验室页爬取后校验是否合理，tasks 中留调参项。
