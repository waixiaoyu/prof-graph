# M2.5 — 后台手动编辑 · 实现计划（plan.md）

> **阶段**：Plan ⏳（随开发一并确认，2026-08-29）
> **上游**：spec.md ✅（RD-1~8 已确认）
> 范围小（无新数据源/无管线），plan 聚焦 DDL、API 契约、墓碑拦截点与测试四块。

---

## 1. 数据模型（T1，单次迁移）

```
persons        + deleted_at TIMESTAMPTZ NULL                      -- 合规删除墓碑
relationships  + deleted_at TIMESTAMPTZ NULL
               + deleted_reason TEXT NULL
admin_edits    （新表，FR-6）
  id BIGSERIAL PK
  action        VARCHAR(50)   -- update_person / set_orgs / set_research_tags /
                              -- delete_relationship / adjust_strength / delete_person
  entity_type   VARCHAR(20)   -- person / relationship
  entity_id     BIGINT
  before        JSONB NULL
  after         JSONB NULL
  reason        TEXT NULL
  created_at    TIMESTAMPTZ DEFAULT now()
  INDEX (entity_type, entity_id), INDEX (created_at)
```

迁移文件 `m25_t1_tombstones_admin_edits.py`（down_revision=d4e8a2c6f307）。存量数据零迁移（新列全可空）。

## 2. 服务层 admin_edits.py（T2）

统一入口模式：每个操作 = 校验 → 快照 before → 变更 → 快照 after → 写日志行（同一事务，NFR-4 成对保证）。

- `update_person_fields(session, person_id, fields, reason)`：可改 name/title/homepage/email；改 name 重算 `name_normalized`（`normalize_person_name`）；墓碑（merged_into 或 deleted）拒改抛 404 语义异常。
- `set_person_orgs(session, person_id, org_ids, reason)`：全量替换 person_org；org_ids 必须已存在于 organizations（不存在即 4xx）；行写 source='admin'、confidence=1.0。
- `set_person_research_tags(session, person_id, tags, reason)`：全量替换 person_research_tags。
- `delete_relationship(session, rel_id, reason)`：置 deleted_at/deleted_reason，证据保留。
- `adjust_relationship_strength(session, rel_id, strength, reason)`：strength ∈ [0,1] 校验。
- `delete_person(session, person_id, reason)`：合规级联——
  person.deleted_at=now；其关系（作任一端）置墓碑 + **物理删三张证据表中这些关系的证据行**；删 person_org / person_research_tags；paper_authors.person_id 置 NULL（行保留 raw_name，论文作者名单不缺位）；其 pending 消歧条目置 status='cancelled'、resolved_at=now。
- `list_edits(session, entity_type?, entity_id?, limit, offset)`。
- 共用 `record_edit(session, action, entity_type, entity_id, before, after, reason)`。

异常约定：`AdminEditError(message, status_code)`，API 层统一捕获转 HTTPException。

## 3. API（T3，挂 app/api/admin.py，RD-6）

| 方法 | 路径 | body | 返回 |
|---|---|---|---|
| PATCH | /api/admin/persons/{id} | {name?, title?, homepage?, email?, reason} | 更新后 person |
| PUT | /api/admin/persons/{id}/orgs | {org_ids: [int], reason} | 更新后 org 列表 |
| PUT | /api/admin/persons/{id}/research-tags | {tags: [str], reason} | 更新后 tags |
| DELETE | /api/admin/persons/{id} | {reason} | {ok, cascaded:{relations, evidence, orgs, tags, paper_links, queue}} |
| DELETE | /api/admin/relationships/{id} | {reason} | {ok} |
| PATCH | /api/admin/relationships/{id} | {strength, reason} | 更新后关系 |
| GET | /api/admin/edits | ?entity_type&entity_id&limit&offset | {items, total} |

reason 必填（min_length=1）；org 搜索复用现有 `GET /api/persons/search?type=org` 供前端选机构。

## 4. 墓碑拦截点（T4，FR-4.2 / FR-5.3 / NFR-2）

**linker 复活拦截**（三处同一模式：查关系时**不过滤**墓碑——否则会撞唯一键新建重复行；查到 `deleted_at` 非空 → 跳过该对）：
- `linker.py` 论文合作对循环（:79 查询后）
- `mentor_linker.py`（:290 同）
- `project_linker.py`（:108 同）

**删除人不进新关系**：三个 linker 取 person_ids 后过滤已删除 person（一次性 `SELECT id FROM persons WHERE deleted_at IS NOT NULL`，量小）。disambiguator 同理跳过（见下）。

**可见面过滤**（persons 墓碑与 merged_into 同口径，`Person.deleted_at.is_(None)`）：
- `graph.py` 图谱/搜索两处（:258/:269）
- `disambiguator.py` 候选三处（:213/:221/:322）

**巡检跳过**（integrity.py，NFR-2）：C1–C4、C7–C9 全部加 `Relationship.deleted_at.is_(None)`；C6 墓碑定义扩为 `merged_into_id IS NOT NULL OR deleted_at IS NOT NULL`（仅对未删关系计违例）。C5/C10 按证据行口径不变。

## 5. 前端（T5）

- `DetailPanel.tsx`：学者详情加"编辑"按钮 → 表单态（name/title/homepage/email 输入 + 机构多选（搜索已有机构）+ 标签编辑）→ 提交调对应端点 → 成功后刷新详情；"删除此人（合规）"放表单底部，需确认+原因。
- 边详情（DetailPanel 关系段）：加"删除关系"（原因必填）与"调整强度"（数字输入）。
- `AdminPage.tsx`：加"操作日志"卡片（最近 20 条：action/实体/时间/reason，按实体过滤入参留 UI 外）。
- `api/client.ts`（或等价）：补 7 个调用。

## 6. 测试（T6）

新增 `tests/test_admin_edits.py`（服务层 + API 合一，用测试库）：
- 每操作：before/after 落库正确 + admin_edits 成对行（NFR-4）
- 改名重算 name_normalized（含中文"张三"）
- orgs 传不存在 id → 4xx；墓碑 person 拒改
- 删关系后：linker 重跑不复活（对既有双跑幂等测试扩展：先删一条再跑断言仍墓碑）
- 按人删除后：全可见面消失、证据行清空、papers 不动、C1–C10 全绿（AC-4）
- 墓碑库上巡检全绿（造 1 个 FR-4 墓碑 + 1 个 FR-5 级联样本跑 check_integrity）
- strength 越界 4xx；重复删除 4xx（FR-4.4）

既有 277 测试零回归（NFR-3）。

## 7. Open Questions

无。
