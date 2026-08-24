# M1 — 论文合作全链路 · 技术方案（plan.md）

> **阶段**：Plan（plan.md）
> **状态**：⏳ 起草中，待人工 review
> **最后更新**：2026-08-19
> **上游**：spec.md ✅ 已确认（RD-1~RD-12，AC×9）

---

## 1. 总体架构

```
┌─────────────────────────── 后端 (FastAPI, Python 3.11) ───────────────────────────┐
│                                                                                    │
│  APScheduler ──→ Collector ──→ AI Filter ──→ Tagger ──→ papers 表                  │
│  (每日 03:00±20min)  arXiv RSS     两段式过滤     业务方向打标                        │
│                          │                                            │            │
│                          ↓                                            ↓            │
│                    Extractor(GLM) ──→ persons / organizations / person_org          │
│                          │               + 消歧 (Disambiguator)                    │
│                          ↓                → disambiguation_queue (0.5–0.8)         │
│                    Linker ──→ relationships + relationship_evidence                 │
│                          (paper_cooperation, 身份置信度 + 关系强度)                  │
│                                                                                    │
│  OpenAlex Client (机构补全, mailto=prof-graph@internal)                             │
│  FailedJob Manager (指数退避 1/5/25min → 死信)    Token Meter (用量统计/熔断)        │
│                                                                                    │
│  REST API (/api/...) ──→ 图谱数据 / 搜索 / 筛选 / 审核队列 / 手动更新 / 后台指标      │
└────────────────────────────────────────────────────────────────────────────────────┘
                                        │
┌─────────────────────── 前端 (React 18 + Vite + TypeScript) ────────────────────────┐
│  图谱页(React Flow) │ 搜索/筛选区 │ 学者详情 │ 证据链 │ 审核队列 │ 后台仪表盘         │
└────────────────────────────────────────────────────────────────────────────────────┘
```

**处理管线**（每篇论文的固定流水）：

```
arXiv RSS 抓取 → 去重(arxiv_id) → AI 相关性过滤 → 业务方向打标 → 入库(papers)
  → GLM 抽取(作者/机构/研究方向) → 机构双源补全(GLM+OpenAlex) → 实体消歧
  → 建 Person/Organization → 建 paper_cooperation 关系(+证据)
```

**技术栈锁定**（constitution）：PostgreSQL 15 + FastAPI + SQLAlchemy 2 + APScheduler + React 18 + React Flow + GLM-5.2（Anthropic 协议）。

---

## 2. 数据模型（PostgreSQL DDL）

```sql
-- 论文（采集+过滤后入库）
CREATE TABLE papers (
    id              BIGSERIAL PRIMARY KEY,
    arxiv_id        VARCHAR(20) UNIQUE NOT NULL,     -- 如 "2401.12345v1"
    title           TEXT NOT NULL,
    abstract        TEXT,
    authors_raw     JSONB NOT NULL,                  -- 原始作者名单（顺序保留）
    published_at    TIMESTAMPTZ,
    categories      TEXT[] NOT NULL,                 -- arXiv 分类，如 {cs.AI,cs.NI}
    rss_entry       JSONB,                           -- 原始 RSS 条目（审计用）
    ai_relevant     BOOLEAN NOT NULL DEFAULT TRUE,   -- AI 过滤结果（false 的不入库，此字段留审计位）
    directions      TEXT[] DEFAULT '{}',             -- 业务方向标签：{ADN, openFuyao}
    tracks          TEXT[] DEFAULT '{}',             -- 学术赛道标签：{network_automation, distributed_training}
    status          VARCHAR(20) NOT NULL DEFAULT 'pending_extraction',
                    -- pending_extraction / extracted / extraction_failed
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 人员
CREATE TABLE persons (
    id              BIGSERIAL PRIMARY KEY,
    name            VARCHAR(200) NOT NULL,
    name_normalized VARCHAR(200) NOT NULL,           -- 小写+去音标+去空格，索引用
    openalex_id     VARCHAR(50) UNIQUE,              -- 命中 OpenAlex 时写入（强消歧键）
    -- RD-8: M1 无职位/联系方式/主页
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_persons_name ON persons (name_normalized);

-- 人员研究方向（多值，从 GLM 抽取聚合）
CREATE TABLE person_research_tags (
    person_id       BIGINT REFERENCES persons(id) ON DELETE CASCADE,
    tag             VARCHAR(100) NOT NULL,
    PRIMARY KEY (person_id, tag)
);

-- 机构
CREATE TABLE organizations (
    id              BIGSERIAL PRIMARY KEY,
    name            VARCHAR(300) NOT NULL,
    name_normalized VARCHAR(300) NOT NULL,
    level           VARCHAR(20),                     -- university / institute / company / lab
    website         TEXT,
    location        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name_normalized)
);

-- 人员-机构归属（含机构确信度，支持一人多机构）
CREATE TABLE person_org (
    person_id       BIGINT REFERENCES persons(id) ON DELETE CASCADE,
    org_id          BIGINT REFERENCES organizations(id) ON DELETE CASCADE,
    org_confidence  NUMERIC(3,2) NOT NULL DEFAULT 0.4,  -- RD-2: 双源均无则 0.4 兜底
    source          VARCHAR(20) NOT NULL,               -- glm / openalex / merged
    paper_id        BIGINT REFERENCES papers(id),       -- 证据论文
    PRIMARY KEY (person_id, org_id)
);

-- 论文-作者关联（抽取结果，作者顺序保留）
CREATE TABLE paper_authors (
    paper_id        BIGINT REFERENCES papers(id) ON DELETE CASCADE,
    author_seq      INT NOT NULL,                     -- 0-based 作者顺序
    person_id       BIGINT REFERENCES persons(id),
    raw_name        VARCHAR(200) NOT NULL,
    name_confidence NUMERIC(3,2) NOT NULL DEFAULT 1.0,
    PRIMARY KEY (paper_id, author_seq)
);

-- 关系（只存人↔人，constitution 硬约束）
CREATE TABLE relationships (
    id                  BIGSERIAL PRIMARY KEY,
    person_a_id         BIGINT NOT NULL REFERENCES persons(id),
    person_b_id         BIGINT NOT NULL REFERENCES persons(id),   -- 恒定 a_id < b_id，防反向重复
    type                VARCHAR(40) NOT NULL,                     -- M1 仅 'paper_cooperation'
    identity_confidence NUMERIC(3,2) NOT NULL,      -- 身份置信度 = 0.4×姓名 + 0.6×机构
    strength            NUMERIC(3,2) NOT NULL,      -- 关系强度（见 §5 阶梯公式）
    coop_count          INT NOT NULL DEFAULT 0,     -- 合作论文数（阶梯自变量）
    time_start          DATE,                       -- 最早合作时间
    time_end            DATE,                       -- 最近合作时间
    evidence_summary    TEXT,                       -- "基于 3 篇合作论文，最近合作于 2024 年"
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (person_a_id, person_b_id, type),
    CHECK (person_a_id < person_b_id)      -- 防反向重复三保险之一（FR-4.4：不得新建重复关系）
);

-- 关系证据（多对多：关系 ↔ 支撑论文）
CREATE TABLE relationship_evidence (
    relationship_id  BIGINT REFERENCES relationships(id) ON DELETE CASCADE,
    paper_id         BIGINT REFERENCES papers(id) ON DELETE CASCADE,
    PRIMARY KEY (relationship_id, paper_id)
);

-- 消歧审核队列（0.5–0.8 区间，RD-9）
CREATE TABLE disambiguation_queue (
    id              BIGSERIAL PRIMARY KEY,
    person_a_id     BIGINT NOT NULL REFERENCES persons(id),
    person_b_id     BIGINT NOT NULL REFERENCES persons(id),
    score           NUMERIC(3,2) NOT NULL,
    score_detail    JSONB,                           -- 5 因素分项得分
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending / merged / rejected
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ
);

-- 失败任务（采集/抽取/补全各类失败统一记录）
CREATE TABLE failed_jobs (
    id              BIGSERIAL PRIMARY KEY,
    job_type        VARCHAR(40) NOT NULL,            -- rss_fetch / glm_extract / openalex_lookup
    target          TEXT NOT NULL,                   -- arxiv_id 或描述
    attempt         INT NOT NULL DEFAULT 0,          -- 已重试次数
    next_retry_at   TIMESTAMPTZ,                     -- 下次重试时间（1/5/25min 退避）
    error           TEXT,
    status          VARCHAR(20) NOT NULL DEFAULT 'retrying',  -- retrying / dead / done
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- GLM 用量统计（NFR-3 可观测 + NFR-2 熔断依据）
CREATE TABLE token_usage (
    id              BIGSERIAL PRIMARY KEY,
    day             DATE NOT NULL,
    job_type        VARCHAR(40) NOT NULL,
    input_tokens    INT NOT NULL DEFAULT 0,
    output_tokens   INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_token_usage_day ON token_usage (day);
```

---

## 3. GLM 抽取 · JSON Schema（RD-5 落实）

**输入策略**（FR-2.2）：优先全文（arXiv HTML/LaTeX 源，截断至 12k tokens）；不可得则 标题+摘要+作者列表（约 500-1500 tokens）。

**输出 Schema**（结构化输出，严格校验；部分容错见解析规则）：

```json
{
  "type": "object",
  "required": ["authors", "research_tags"],
  "properties": {
    "authors": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["name", "seq"],
        "properties": {
          "name":        { "type": "string" },
          "seq":         { "type": "integer" },
          "affiliation": { "type": ["string", "null"] },
          "affiliation_source": { "enum": ["paper", "null"] },
          "is_corresponding": { "type": "boolean" }
        }
      }
    },
    "research_tags": {
      "type": "array",
      "items": { "type": "string" },
      "maxItems": 8
    }
  }
}
```

**解析规则**（RD-11 部分容错）：
- 顶层 JSON 可解析 → 逐个作者校验：`name`+`seq` 完整者入库；`affiliation` 为 null 的走机构双源补全（§6）。
- 个别作者对象缺 `name` 或 `seq` → 跳过该作者（记 warning 日志），其余照常入库。
- 顶层 JSON 解析失败 → 整篇走 FR-2.5 重试（1/5/25min 指数退避，3 次后死信）。

---

## 4. AI 相关性过滤（FR-1.2 落实）· 两段式

**目标**：16 类采集面里剔除非 AI 论文，同时控制 token 成本（不能每篇都全量过 GLM）。

**第一段 · 规则粗筛（零成本）**：基于 arXiv 分类 + 关键词的启发式：

| 判定 | 规则 |
|------|------|
| 直接保留 | 主分类 ∈ {cs.AI, cs.LG, stat.ML}（这三个类基本必是 AI） |
| 直接剔除 | 无任何 cs.* / stat.ML 交叉分类，且标题+摘要不命中关键词表 |
| 待定 → 第二段 | 其余（eess/math 主分类、cs.NI/cs.DC 等非 AI 核心类） |

关键词表（配置文件维护，节选）：`machine learning, deep learning, neural network, reinforcement learning, transformer, LLM, GNN, learning-based, trained, model inference...`

**第二段 · GLM 细筛（只处理待定项）**：批量判定（一次请求带 10 篇的标题+摘要，返回每篇 `is_ai: bool` + 一句话理由），输入约 800 tokens/篇，远低于全量抽取。判定 prompt 要求："论文使用了 AI/ML 方法（而非仅提及）才算 AI 相关"。

**成本估算**：16 类日增约 400-600 篇；规则粗筛后待定约 30%；GLM 细筛约 120-180 篇/日 ≈ 15 万 tokens/日；抽取（AI 相关约 200 篇 × 2500 tokens）≈ 50 万 tokens/日。**熔断阈值（NFR-2 落实）：日预算 80 万 tokens，达 80% 告警（后台横幅），达 100% 暂停 GLM 细筛与抽取（规则粗筛和采集继续），次日自动恢复。**

---

## 5. 置信度与关系强度（RD-10 落实）· 数值定稿

**身份置信度**：

```
identity_confidence = 0.4 × name_confidence + 0.6 × org_confidence
```

- `name_confidence`：GLM 抽取名与原始作者名完全一致 1.0；归并/消歧后的历史 Person 取其消歧得分。
- `org_confidence`：paper 显式署名 1.0 / OpenAlex 补全 0.8 / 双源均无兜底 0.4（person_org.org_confidence）。

**关系强度 · 阶梯参数**（按 RD-10 四原则取值，N=5）：

```
strength = identity_confidence × tier(coop_count)

tier:  1 次   → 0.85        （单次不压低）
       2 次   → 0.90        （稳定，+0.05）
       3–4 次 → 0.95        （强，+0.10）
       5 次+  → 1.00        （确凿，封顶）
```

| 合作次数 | tier | identity=1.0 时 | identity=0.7 时 |
|---------|------|----------------|-----------------|
| 1 | 0.85 | 0.85 | 0.60 |
| 2 | 0.90 | 0.90 | 0.63 |
| 3–4 | 0.95 | 0.95 | 0.67 |
| 5+ | 1.00 | 1.00 | 0.70 |

- 冷启动（普遍 1 次）身份确信的关系 0.85，不被压低 ✔
- 合作 5 次触顶，未采用 2-3 次的快速触顶（review 中已否决）✔
- 阶梯跃升、非线性 ✔
- FR-4.4 合并时：`coop_count += 1` 后重查 tier 即得新 strength。

---

## 6. 机构双源补全 + 消歧（FR-2.4 / FR-3 落实）

**双源补全流程**：
1. GLM 抽取的 `affiliation`（非 null）→ 归一化后 upsert organizations，`org_confidence=1.0, source=glm`。
2. null → 调 OpenAlex `GET /authors?search=<name>&filter=last_known_institutions.id:<org>`（带 `mailto=prof-graph@internal`，礼貌池限 5 req/s）：
   - 作者名精确匹配 + 最近论文与本文 arxiv_id 吻合 → 采用其 institution，`org_confidence=0.8, source=openalex`，同时回写 `persons.openalex_id`。
   - 无匹配 → 不建归属，`org_confidence=0.4` 兜底记入该作者所在关系计算。

**消歧流程**（FR-3.2/3.3）：
```
新作者 → openalex_id 命中已有 Person？ → 是：归并
              ↓ 否
        5 因素打分：
          姓名 30%（编辑距离 ≥0.95 → 1.0；0.85–0.95 → 0.7；否则 0.2）
          机构 25%（同 org_id 1.0 / 同名不同写 0.7 / 无机构 0.4）
          研究方向 20%（Jaccard(person_research_tags) ）
          时间 15%（论文时间落在该 Person 已知活跃区间 1.0 / 相邻 0.6 / 更远 0.3；
                  活跃区间 = 该 Person 已参与论文 published_at 的最早~最近，自动聚合，无需人工维护）
          合作网络 10%（共享 ≥2 个合作者 1.0 / 1 个 0.6 / 0 个 0.2）
              ↓ 取最高分
        ≥0.8 自动归并 / 0.5–0.8 入审核队列 / <0.5 新建 Person
```

**审核合并 API**（RD-9 最简入口）：`POST /api/disambiguation/{id}/merge` 将 B 并入 A（B 的关系/证据/标签迁移至 A，重算受影响关系的 coop_count 与 strength），队列记录 `merged`。

---

## 7. API 契约（REST，/api 前缀）

| Method | Path | 用途 | 对应 |
|--------|------|------|------|
| GET | `/api/graph?direction=&track=&strength_min=&limit=` | 图谱数据（nodes+edges，默认 Top 1000 按关系强度） | FR-5.1/5.2/5.6 |
| GET | `/api/persons/search?q=&type=name\|org` | 姓名/机构输入框匹配（LIKE，限 20 条） | FR-5.5 |
| GET | `/api/persons/{id}` | 学者详情（机构/研究方向/论文） | FR-5.3, US-4 |
| GET | `/api/relationships/{id}/evidence` | 证据链（支撑论文列表） | FR-5.4, US-2 |
| GET | `/api/filters/options` | 筛选器选项（业务方向+学术赛道，读配置） | FR-5.2 |
| GET | `/api/disambiguation?status=pending` | 审核队列列表（含分项得分） | FR-3.4 |
| POST | `/api/disambiguation/{id}/merge` | 合并（body: keep=A\|B） | FR-3.4 |
| POST | `/api/disambiguation/{id}/reject` | 判定非同人，移出队列 | FR-3.4 |
| POST | `/api/admin/trigger-update` | 手动触发采集（返回 batch_id） | FR-1.4, US-5 |
| GET | `/api/admin/update-status/{batch_id}` | 批次进度（已抓取/总数/阶段） | FR-1.4, US-5 |
| GET | `/api/admin/metrics` | 当日 token 用量 + failed_jobs 列表 + 熔断状态 | NFR-3, AC-7 |

分页/列表接口统一 `?limit=&offset=`；错误统一 `{ "error": { "code", "message" } }`。

---

## 8. 调度与配置

**APScheduler（应用内）**：

| 任务 | 触发 | 说明 |
|------|------|------|
| arXiv 采集管线 | cron `03:00` + `jitter=1200s` | 每日（FR-1.1，时间此处定稿） |
| failed_jobs 重试 | interval `60s` | 扫 `next_retry_at` 到期且 `retrying` 的任务 |
| 死信巡检 | cron `08:00` | 汇总昨日死信数入日志/后台 |

**业务方向配置文件** `config/directions.yaml`（RD-6 落实，结构示例）：

```yaml
directions:                       # 产业方向
  - id: ADN
    name_cn: 自动驾驶网络
    keywords: [autonomous driving network, ADN, intent-based networking,
               network automation, self-healing network, O-RAN]
  - id: openFuyao
    name_cn: 扶摇算力集群
    keywords: [heterogeneous computing scheduling, GPU cluster scheduling,
               distributed training, kubernetes scheduling, workload colocation,
               inference serving]
tracks:                           # 学术赛道（筛选项 + 打标规则）
  - id: network_automation
    name: Network Automation
    keywords: [network automation, SDN, intent-based networking]
  - id: fault_analysis
    name: Fault Root Cause Analysis
    keywords: [root cause analysis, anomaly detection, network fault]
  - id: distributed_training
    name: Distributed Training
    keywords: [distributed training, data parallelism, model parallelism, allreduce]
  - id: gpu_scheduling
    name: GPU Cluster Scheduling
    keywords: [GPU scheduling, cluster scheduling, resource pooling, colocation]
  # ... plan 实现期补全至 8-12 个赛道
arxiv_categories: [cs.AI, cs.LG, cs.NI, cs.DC, cs.OS, cs.AR, cs.PF, cs.SE, cs.DB,
                   cs.CR, eess.SP, eess.SY, eess.IT, math.OC, math.PR, stat.ML]
```

打标规则：论文标题+摘要命中任一 keyword（不区分大小写）→ 打对应 direction/track 标签；一篇可多标。

---

## 9. 项目结构

```
prof-graph/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口 + APScheduler 启动
│   │   ├── config.py            # 配置加载（含 directions.yaml）
│   │   ├── models/              # SQLAlchemy 模型（§2）
│   │   ├── api/                 # 路由（§7）
│   │   ├── services/
│   │   │   ├── collector.py     # arXiv RSS 抓取
│   │   │   ├── ai_filter.py     # 两段式 AI 过滤（§4）
│   │   │   ├── tagger.py        # 业务方向打标
│   │   │   ├── extractor.py     # GLM 抽取 + JSON 解析容错
│   │   │   ├── openalex.py      # OpenAlex 客户端
│   │   │   ├── disambiguator.py # 消歧 + 审核合并
│   │   │   ├── linker.py        # 关系建立/合并（§5 公式）
│   │   │   └── failed_jobs.py   # 失败管理 + 重试
│   │   └── scheduler.py         # 任务注册（§8）
│   ├── config/directions.yaml
│   └── tests/                   # pytest（公式/解析容错/消歧打分单测）
├── frontend/
│   └── src/
│       ├── pages/               # Graph / Admin / Review
│       ├── components/          # GraphCanvas(React Flow) / FilterBar / EvidencePanel ...
│       └── api/                 # 请求封装
├── specs/                       # SDD 产物（本目录）
└── docs/                        # 参考文档（只读）
```

---

## 10. 验收标准 → 实现映射

| AC | 验证方式 | 涉及模块 |
|----|---------|---------|
| AC-1 | 采集运行 3-7 天或手动回灌，`SELECT count(*) FROM papers` ≥ 50 且 status='extracted' | collector→linker 全链 |
| AC-2 | `GET /api/graph?direction=ADN`（或 track）节点>20、关系>10 | linker, api |
| AC-3 | `GET /api/relationships/{id}/evidence` 返回≥1 篇（标题/年份） | api |
| AC-4 | `GET /api/persons/search?q=<姓名>` 命中并在前端定位 | api, Graph 页 |
| AC-5 | 同对作者多篇 → relationships 仅 1 条，coop_count 正确，evidence_summary 含"基于 N 篇" | linker 单测 |
| AC-6 | mock GLM 500，failed_jobs 记录，next_retry_at 间隔 1/5/25min | failed_jobs 单测 |
| AC-7 | `GET /api/admin/metrics` 返回 token 用量与 failed_jobs | api |
| AC-8 | 投放 math.OC 纯数学论文样本，断言未入 papers（或 ai_relevant=false） | ai_filter 单测 |
| AC-9 | 构造 0.5–0.8 样本入队列，POST merge 后两 Person 合一且关系迁移正确 | disambiguator 单测 |

---

## 11. Open Questions（plan 阶段）

- 无。RD-1~12 与本 plan 已覆盖 spec 全部待定项（调度时间 03:00、熔断阈值 80万 tokens/日、阶梯参数 N=5、赛道清单初版 8-12 个）。赛道清单在 tasks 实现阶段补全，不阻塞架构。
