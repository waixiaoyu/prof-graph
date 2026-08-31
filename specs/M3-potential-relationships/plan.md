# M3 — 潜在关系挖掘 · 实现计划（plan.md）

> **阶段**：Plan ✅ 已确认（2026-08-31，含置信度公式热度因子近似、视图内端点约束、默认关开关、周日 06:00 重算、top 10 详情等实现裁量）
> **上游**：spec.md ✅ 2026-08-31（RD-1~RD-11；spatiotemporal 已移出）
> 形态：一个纯计算服务 + 一张表 + 周调度 + API/前端展示层。**零 GLM、零外部请求**（NFR-1）。全量重算 = 内存计算 + 单事务全量替换。

---

## 1. 数据模型（T1，单次迁移）

```
potential_relationships（新表，FR-1.1）
  id               BIGSERIAL PK
  person_a_id      BIGINT NOT NULL REFERENCES persons(id)
  person_b_id      BIGINT NOT NULL REFERENCES persons(id)   -- a < b（ck 约束）
  discovery_method VARCHAR(30) NOT NULL                    -- common_network / research_similarity
  confidence       NUMERIC(3,2) NOT NULL                   -- [0.10, 0.70]（ck 约束）
  reason           TEXT                                    -- 人类可读发现原因（重算时生成）
  supporting_signals JSONB                                 -- 见 §2 各方法
  created_at       TIMESTAMPTZ DEFAULT now()
  updated_at       TIMESTAMPTZ DEFAULT now()
  UNIQUE (person_a_id, person_b_id, discovery_method)
  CHECK (person_a_id < person_b_id)
  CHECK (confidence BETWEEN 0.10 AND 0.70)
  INDEX (person_a_id), INDEX (person_b_id)
```

ORM：`models/__init__.py` 加 `PotentialRelationship`。迁移 `m3_t1_potential_relationships.py`（down_revision=`a1f3c8d20b95`），存量零迁移。

## 2. 计算服务 `services/potential.py`（T2/T3）

模块顶部常量（测试可覆盖）：`COMMON_MIN_COLLABORATORS = 2`、`RS_JACCARD_MIN = 0.3`、`RS_TOP_K = 5`。

### 2.1 common_network（FR-2.1）

```
adj = 活跃直接关系(两端活跃人)构建的邻接表；direct = 活跃直接关系对集合
for 每个候选对 (a<b)：common = adj[a] ∩ adj[b]
  保留条件：|common| >= 2 且 (a,b) ∉ direct
signals = {"common_collaborators": [pid...], "common_collaborator_names": [姓名...], "count": n}
reason = "共同合作者 {n} 人：{姓名、姓名…}"
```

置信度（docs/02 公式沿用，网络距离项在 |common|≥2 时恒为 1/3）：
`conf = clamp(0.5×min(n/5, 1.0) + 0.3×(1/3) + 0.2×org_sim, 0.1, 0.7)`，`org_sim` = 双方 person_org 存在共同机构 1.0 / 否则 0.5（docs 口径）。

### 2.2 research_similarity（FR-2.2）

```
tags[p] = person_research_tags 小写集合（双方非空才参与）
合格对：Jaccard(tags[a], tags[b]) >= 0.3
互认 top-5：每人将合格对端按 (jaccard 降序, 对方 id 升序) 取前 5；
           对 (a,b) 保留当且仅当 a 选了 b 且 b 选了 a（RD-11）
signals = {"overlap_tags": [...], "jaccard": j, "tags_a": n1, "tags_b": n2}
reason = "研究方向相似（{j:.2f}）：{重叠标签…}"
```

置信度：`conf = clamp(0.7×jaccard + 0.3×min(|overlap|/5, 1.0), 0.1, 0.7)`。docs 公式的"研究热度因子"（论文数量/活跃度）不可稳定计算，以重叠标签数归一近似实现——**与 docs 的偏差在此记录**。

### 2.3 重算编排 `recompute_potential(session)`（FR-4.3/4.4）

1. 载入：活跃 person id 集（deleted_at/merged_into_id 均 NULL）、活跃 relationships、direct 对集、tags、person_org（org_sim 用）；
2. 纯内存计算两组行；
3. **单事务**：`DELETE FROM potential_relationships` → bulk INSERT。异常整体回滚（旧全量保留）；不写 failed_jobs（无外部依赖）。
4. 返回 report：`{common_network: n, research_similarity: n, duration_s: x}`（记日志）。

幂等：全量替换 + reason 不含时间戳 → 同输入双跑同结果。失效行（墓碑人/新直接关系/合并）随 DELETE 自然清除（FR-3.3）。

CLI：`scripts/run_potential.py`（沿用 retry_failed.py 的参数与日志模式，供验收与补跑）。

## 3. C11 巡检（T4，FR-3.4）

`integrity.py` 新增 C11「潜在关系不变量」：每行 `a < b`、method ∈ 枚举、confidence 界内、**两端均为活跃人**、**两端无活跃直接关系**。违例计数 + samples，与 C1–C10 同结构。

## 4. 调度（T4，FR-4.1）

`scheduler.py`：`CronTrigger(day_of_week="sun", hour=6, minute=0, jitter=600, timezone=TZ)`，job 注册名 `potential_recompute`；包装 try/except 仅记日志。

## 5. API（T5，FR-5.1/5.3）

| 端点 | 变更 | 说明 |
|---|---|---|
| GET /api/graph | 增参 `include_potential: bool = False` | 响应增 `potential_edges: [{a, b, methods: [{method, confidence, reason, signals}]}]`（pair 聚合，RD-8）。**潜在边两端必须已在本次视图 nodes 集内**（与 person/org/direction 筛选口径一致），否则不返回该边——视图一致性。潜在边不参与 rel_types/strength_min/coop_min（RD-7 独立维度） |
| GET /api/persons/{id} | 响应增 `potential_connections` | `[{person_id, name, orgs, methods: [...]}]`，confidence 降序 top 10。墓碑人本身 404，天然排除 |

## 6. 前端（T6，FR-5.2/5.3）

- `FilterBar.tsx`：与 rel_types 并列加"潜在关系"复选框（默认关）→ `include_potential`。
- `GraphPage.tsx`：潜在边用 React Flow `style: {strokeDasharray: "6 4"}` + 灰色弱化；点击潜在边 → 详情显示方法/置信度/reason/signals（共同合作者名单或重叠标签）。
- `DetailPanel.tsx`：学者详情加"潜在连接"段（top 10，含方法与原因，点击跳转对方）。
- `api/client.ts`：参数与两个新字段类型。

## 7. 测试策略（目标 ~18 例，NFR-3）

- `test_potential.py`（计算）：两方法产出与 signals/reason 断言；common 排除三分支（单共同者 / 已有直接关系 / 墓碑端点）；RS 排除（阈值 / 单向 top-5 不互认 / 无标签）；双跑幂等；预置脏行（失效对）重算后被清除；confidence 界内含 clamp。
- `test_integrity.py`：C11 违例样本（直接关系复活对 / 墓碑端点 / 越界置信度）。
- `test_api_graph.py`：include_potential 关=无字段、开=聚合结构、视图外端点不返回、人详情 potential_connections、墓碑人不可见。
- 前端走查（AC-4）：开关、虚线渲染、边详情、详情列表。

## 8. 任务拆分（tasks.md 预览）

| # | 任务 | 依赖 | 关键验收 |
|---|---|---|---|
| T1 | 表 + ORM + 迁移 | — | 迁移可升可降，模型测试 |
| T2 | common_network 计算 | T1 | AC-1（方法一） |
| T3 | research_similarity + 互认 top-5 | T1 | AC-1（方法二） |
| T4 | 重算编排 + 回滚 + CLI + 周调度 + C11 | T2/T3 | AC-2/3/6（部分） |
| T5 | API 两端点 | T4 | AC-4（API 半） |
| T6 | 前端展示 | T5 | AC-4（前端半） |
| T7 | 验收：生产实跑 + 抽查 5 条 + 计时 + 文档 + 进度板 | T6 | AC-5/6/7 |

## 9. 开放问题

无——spec OQ-1（RS 阈值）已由探针实证 + 互认封顶机制消化，上线后按观感调常量即可；OQ-2 已随 spatiotemporal 移出关闭。
