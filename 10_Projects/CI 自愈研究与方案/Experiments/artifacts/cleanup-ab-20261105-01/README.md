---
ai_generated: true
reviewed: false
---

# 清理简化 A/B 结果归档

这是 `cleanup-ab-20261105-01` 的精简发布副本，包含原始/离线复核双口径指标、59 对配对数据、影响分类、离线审计结论和来源哈希。完整结论见 [实验报告](../../2026-11-05-Skill清理简化A-B实测.md)。本轮没有达到完整验收门槛，不提升 Skill 的验证状态。

原始 118 次执行、118 次评审、模型会话、逐项证据和文件清单继续保留在本地 `evals/ci-self-healing/runs/cleanup-ab-20261105-01/`，该目录按现有规则不进入 Git。本归档不包含模型凭据、运行时配置、缓存或完整会话。原始记录未被覆盖。

- `metrics.json`、`paired-comparison.csv`：成本、性能及原始/离线复核的结果口径。
- `finding-classification.json`、`offline-audit.json`：实际影响分类与可追溯的技术偏差复核。
- `integrity-summary.json`：模型身份、并发、时限、历史保留和阶段记录检查摘要。
- `plan.json`、两个 `*-contract/policy.json` 文件：预先冻结的场景、模型、预算和分析条件。
- `provenance.json`：原始来源字节哈希及导出时的路径替换说明。`<REPO>`、`<HOME>` 只用于发布展示，不是新执行轨迹。
- `methods/`：本轮实际执行的离线审计源文件快照；要重做离线审计，须将其置于上述原始运行目录的 `analysis/` 布局并使用匹配的评测引擎。这里的脚本不会重新调用模型。

仓库版报告的本地原始证据链接仅在原始运行目录可用时有效；远端可直接查看本目录中的归档文件。既有历史 A/B 与 E1–E3 原始记录仍保留在各自实验目录。
