# Specs — Spec-Driven Development

本目录是 prof-graph 项目的**唯一事实来源**（single source of truth）。代码从 spec 派生，需求变更先改 spec 再改代码。

> 参考实践：[Spec-Driven Development](https://www.thebcms.com/blog/spec-driven-development/)

---

## 工作流：四阶段闭环

每个里程碑（feature）严格按以下顺序推进，**每阶段结束必须人工 review，未确认不进入下一阶段**：

```
1. Specify  →  spec.md    做什么、为什么（EARS 语法，含 out-of-scope 和验收标准）
     ↓ [人工 review]
2. Plan     →  plan.md    怎么做（架构、数据模型、API 契约）
     ↓ [人工 review]
3. Tasks    →  tasks.md   按什么顺序（原子化、可独立交付的任务清单）
     ↓ [人工 review]
4. Implement → 代码        agent 按 tasks 逐步实现，每个 task 跑测试、提交
```

## 目录结构

```
specs/
├── README.md                          ← 本文件（全局导航）
├── .specify/
│   └── memory/
│       └── constitution.md            ← 项目级硬约束（所有产物必须遵守）
├── M1-paper-cooperation-pipeline/     ← 里程碑 1
│   ├── spec.md
│   ├── plan.md
│   └── tasks.md
├── M2-academic-mentorship-and-project/← 里程碑 2
├── M2.5-manual-editing/               ← 里程碑 2.5：后台手动编辑（治理闭环）
├── M3-potential-relationships/        ← 里程碑 3
└── M4-stability-and-polish/           ← 里程碑 4
```

## 里程碑总览

| 里程碑 | 主题 | 目标 | 依赖 |
|--------|------|------|------|
| **M1** | 论文合作全链路 | arXiv RSS → GLM 抽取 → 论文合作关系入库 → 图谱展示 | 无 |
| **M2** | 学术传承 + 项目合作 | 补全另 2 种直接关系的数据源和抽取链路 | M1 |
| **M2.5** | 后台手动编辑 | 人工治理闭环：改人/改关系/按人删除/操作日志（constitution §3.1 承诺欠账） | M2 |
| **M3** | 潜在关系挖掘 | 基于已有网络跑通 3 种潜在关系计算 | M2 |
| **M4** | 稳定性与体验打磨 | 失败处理、成本熔断、合规、数据监控 | M3 |

## 里程碑进度

| 里程碑 | spec | plan | tasks | implement | 状态 |
|--------|------|------|-------|-----------|------|
| M1 | ✅ 已确认 2026-08-19 | ✅ 已确认 2026-08-24 | ✅ 已确认 2026-08-24 | ✅ 完成 2026-08-26 | T0~T24 全部实现并验证；采集 26 类（泛AI）；CN 范围约束、linker 证据幂等修复；防护网齐备：六项不变量巡检 + 全管线双跑幂等测试 + 每日备份 + 一键启动（111 测试）。**2026-08-30 勘误**：eess.IT 为幻影分类更正 eess.IV（RD-1）+ 全文输入加固/残页拒收（12 篇必然死信救回，spec §9 附录） |
| M2 | ✅ 已确认 2026-08-26 | ✅ 已确认 2026-08-27 | ✅ 已确认 2026-08-27（R1–R5） | ✅ 完成 2026-08-27 | T0~T16 全部实现并验收（216 测试）：学术传承链（NISL/IPADS 实跑 170 快照 / 775 传承关系全带证据，AC-1/2/9）+ 项目合作链（3 源实跑，AC-3 按降级口径跑通）+ API/前端（rel_types 筛选、混合证据、职位主页）+ 防护网（C1–C10 全绿、双跑幂等覆盖新链路、D2 重筛优化）；遗留观察项见 spec §9 附录 |
| M2.5 | ✅ 已确认 2026-08-28 | ✅ 已确认 2026-08-28 | ✅ 已确认 2026-08-28 | ✅ 完成 2026-08-28 | T1~T6 全部实现并验收（288 测试）：字段编辑（改名重算归一）/机构归属（仅选已有机构）/标签整组替换 + 关系墓碑删除（linker 不复活）+ 按人合规级联删除（全可见面消失）+ admin_edits 审计成对 + 前端编辑表单/边操作/日志卡片；墓碑库 C1-C10 全绿 |
| M3 | ✅ 已确认 2026-08-31 | ✅ 已确认 2026-08-31 | ✅ 已确认 2026-08-31 | ⏳ 进行中 | spec 定稿：两方法 common_network（≥2 共同合作者）/ research_similarity（J≥0.3+双向 top-5），spatiotemporal 移出待数据复盘（宪法 §3.1 同步修订）；实现 T1-T7 进行中 |
| M4 | — | — | — | — | 未开始 |

图例：✅ 已确认 / ⏳ 进行中 / — 未开始

## 全局规则速查（详见 `constitution.md`）

- 技术栈不可漂移：PostgreSQL + FastAPI + React + GLM-5.2 + APScheduler
- 第一阶段 3 种直接关系（论文/项目/学术传承）、2 种潜在关系方法（spatiotemporal 2026-08-31 移出待复盘），均人↔人
- 顾问关系、role_match、企业实体 → 第二阶段
- 措辞约定：RSS = 一阶段，爬虫 = 二阶段（高校官网是特例）
- `docs/` 仅为参考，实现以 `specs/` 为准
