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
├── M3-potential-relationships/        ← 里程碑 3
└── M4-stability-and-polish/           ← 里程碑 4
```

## 里程碑总览

| 里程碑 | 主题 | 目标 | 依赖 |
|--------|------|------|------|
| **M1** | 论文合作全链路 | arXiv RSS → GLM 抽取 → 论文合作关系入库 → 图谱展示 | 无 |
| **M2** | 学术传承 + 项目合作 | 补全另 2 种直接关系的数据源和抽取链路 | M1 |
| **M3** | 潜在关系挖掘 | 基于已有网络跑通 3 种潜在关系计算 | M2 |
| **M4** | 稳定性与体验打磨 | 失败处理、成本熔断、合规、数据监控 | M3 |

## 里程碑进度

| 里程碑 | spec | plan | tasks | implement | 状态 |
|--------|------|------|-------|-----------|------|
| M1 | ✅ 待最终确认 | — | — | — | spec 已定稿（含 RD-1~5），待 review 签字进 plan |
| M2 | — | — | — | — | 未开始 |
| M3 | — | — | — | — | 未开始 |
| M4 | — | — | — | — | 未开始 |

图例：✅ 已确认 / ⏳ 进行中 / — 未开始

## 全局规则速查（详见 `constitution.md`）

- 技术栈不可漂移：PostgreSQL + FastAPI + React + GLM-5.2 + APScheduler
- 第一阶段 3 种直接关系（论文/项目/学术传承）、3 种潜在关系方法，均人↔人
- 顾问关系、role_match、企业实体 → 第二阶段
- 措辞约定：RSS = 一阶段，爬虫 = 二阶段（高校官网是特例）
- `docs/` 仅为参考，实现以 `specs/` 为准
