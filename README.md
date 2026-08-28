# CI Self-Healing

本仓库是 CI 自愈研究、方案文档、实验与后续项目代码的统一工作目录。

当前内容从个人知识库 `/Users/zhujiayi/personal/00_KB` 复制而来，保留了原有 PARA 路径、Obsidian 双链、frontmatter、来源快照与可视化附件。原知识库中的文件未删除、未移动。

## 导航

- 项目主页：[`10_Projects/CI 自愈研究与方案/CI 自愈研究与方案.md`](10_Projects/CI%20自愈研究与方案/CI%20自愈研究与方案.md)
- 主题地图：[`50_MOCs/CI 自愈 MOC.md`](50_MOCs/CI%20自愈%20MOC.md)
- 研究工作台：[`50_MOCs/CI 自愈工作台.base`](50_MOCs/CI%20自愈工作台.base)
- 专题工作流：[`80_Admin/CI 自愈专题工作流.md`](80_Admin/CI%20自愈专题工作流.md)
- 迁移记录：[`MIGRATION_MANIFEST.md`](MIGRATION_MANIFEST.md)

## 内容分层

- `00_Inbox/`：待判断、待准入的候选资料。
- `30_Resources/**/Sources/`：公开一手来源及其可追溯快照。
- `30_Resources/**/Knowledge/`：公开一手来源能够共同确认的机制事实。
- `10_Projects/**/Candidates/`：可讨论、可验证、可放弃的方案候选。
- `10_Projects/**/Architecture/`：本地架构推理与设计方案。
- `10_Projects/**/ADR/`：需要明确取舍的决策记录。
- `10_Projects/**/Experiments/`：需要实验证明的假设与结果。
- `99_Attachments/`：网页快照、图表、HTML 与验证附件。

后续代码可按实际实现逐步建立模块目录；研究事实、设计判断和实验结果仍应保持上述边界，不混写为同一种证据。

## 治理约定

`Sources/` 与 `Knowledge/` 遵循 [`80_Admin/来源约束知识规则.md`](80_Admin/来源约束知识规则.md)：只保存公开一手来源直接支持的内容。推理、设计和实验分别进入 `Architecture/`、`ADR/` 或 `Experiments/`。`reviewed: true` 只表示经过人工确认，AI 修改正文后应恢复为 `reviewed: false`。
