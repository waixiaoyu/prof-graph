# M3 — 潜在关系挖掘 · 任务清单（tasks.md）

> **阶段**：Tasks ✅ 已确认（2026-08-31）/ Implement ✅ 完成（2026-08-29，T1–T7 全绿）
> **上游**：spec.md ✅ 2026-08-31 / plan.md ✅ 2026-08-31
> 工作约定沿用 M1/M2/M2.5：每 task 至少一次提交 `M3-T{n}: <内容>`；全部完成后 spec/tasks 勾选归档。

| # | 任务 | 依赖 | 验证 | AC |
|---|------|------|------|----|
| T1 ✅ | 数据模型 + 迁移：potential_relationships 表（三保险 + confidence CHECK）+ ORM | — | alembic upgrade head 空库/生产库双态成功；模型与迁移一致；294 全绿零回归 | 前置 |
| T2 ✅ | common_network 计算（≥2 共同合作者 + 排除分支 + signals/reason/置信度） | T1 | 单测 7 例（产出断言/单共同者排除/已有直接关系排除/deleted 人排除/merged 人排除/墓碑关系排除/clamp 上限）全绿；全量 301（294 零回归） | AC-1（方法一）、AC-2（部分） |
| T3 ✅ | research_similarity 计算（Jaccard≥0.3 + 双向互认 top-5 + 排除分支） | T1 | 单测 7 例（产出断言/阈值排除/单向不互认剔除/无标签排除/大小写归一+clamp/已有直接关系排除/墓碑人排除）全绿；全量 308（294 零回归） | AC-1（方法二） |
| T4 ✅ | 重算编排（单事务全量替换/回滚/幂等/失效行清除）+ CLI + 周日 06:00 调度 + C11 巡检 | T2/T3 | 单测：双跑幂等（快照相等）/预置脏行+端点墓碑重算全清/回滚保旧全量（撞 CHECK 验证）；C11 三违例（复活对/墓碑端点/未知 method）+ 墓碑关系非复活负例；调度注册+周日 06:00 触发器断言；全量 313（294 零回归） | AC-2/3/6（部分） |
| T5 ✅ | API：/graph include_potential（pair 聚合 + 视图内端点约束）+ 人详情 potential_connections top10 | T4 | API 单测 4 例（关=无字段/pair 聚合两法一列表+结构字段/视图外端点不返回且不受 strength_min 筛/人详情 top10 降序含 name+orgs+methods）全绿；全量 317（294 零回归） | AC-4（API 半） |
| T6 ✅ | 前端：FilterBar 开关（默认关）+ React Flow 虚线灰边 + 边详情 + 人详情潜在连接段 | T5 | `npm run build`（tsc+vite）过；开发服浏览器走查全绿：默认关（请求无 include_potential、0 虚线边）→ 勾选后 1001 边中恰 1 条虚线灰边（#94a3b8/dasharray 6 4）→ 点击弹潜在关系面板（共同合作者·置信度 0.70·6 人名单）→ 人详情"潜在连接（1）"段（姓名/机构/方法/置信度）→ 点条目切换到对方详情且对称回链；期间"重载后边消失"经 stash 对照+干净服务器证为长跑 HMR 假象，非代码缺陷（观感留用户抽查） | AC-4（前端半） |
| T7 ✅ | 全量验收：生产实跑一轮 + 人工抽 5 条核验 + 计时 ≤5min + 零 token 确认 + C1-C11 全绿 + 全量测试零回归 + 文档勾选归档 | T1–T6 | 生产库官方重算 report {common_network:43, research_similarity:1111, duration_s:1.37}（与首跑 1.15s 幂等一致，≪5min）；抽 5 条独立 SQL 核验（3045↔3265 共同合作者 6 人=signals 记录；3284 四条 RS jaccard=1.0 双向互认 top-5、无活跃直接关系、端点活跃）；零 token 确认（potential.py 仅 stdlib+sqlalchemy，无 LLM 客户端，NFR-1）；生产 C1-C11 十一项全绿；全量 317 passed 零回归；specs/README.md M3 行 implement ✅ | AC-5/6/7 |
