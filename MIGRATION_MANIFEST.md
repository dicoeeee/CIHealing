---
type: migration-record
domain: ci-self-healing
status: completed
created: 2026-08-28
updated: 2026-08-28
ai_access: true
ai_generated: true
reviewed: false
---

# CI 自愈内容迁移记录

## 来源快照

- 来源知识库：`/Users/zhujiayi/personal/00_KB`
- 来源分支：`main`
- 来源 HEAD：`220a6d831372c3b114085ea1bc464ce4f73ce71d`
- 快照日期：`2026-08-28`
- 快照口径：复制当时的工作区实际内容，包括 CI 自愈范围内尚未提交的修改与草稿。

## 纳入范围

1. CI 自愈专题目录：
   - `00_Inbox/CI 自愈候选/`
   - `10_Projects/CI 自愈研究与方案/`
   - `30_Resources/CI-CD/CI 自愈/`
   - `99_Attachments/CI 自愈/`
2. 所有 frontmatter 为 `domain: ci-self-healing` 的可访问笔记，其中包括 `30_Resources/CI-CD/Harness CI.md`。
3. 专题 MOC、Obsidian Base、模板和工作流。
4. 专题工作流直接依赖的治理文件：`AI 使用边界.md`、`来源约束知识规则.md`、`知识库工作流.md`，以及该通用工作流引用的 5 个基础模板。

共复制 57 个来源文件，大小约 3.6 MB。仓库根目录的 `README.md`、`AGENTS.md`、`MIGRATION_MANIFEST.md` 和 `.gitignore` 为迁移时新增的仓库级文件，不计入上述 57 个来源文件。

## 排除与保护

- 未读取或复制 `90_Private/`。
- 未复制 frontmatter 含 `ai_access: false` 的笔记。
- 未从原知识库删除、移动、暂存或提交任何文件。
- 未把与 CI 自愈无关的普通知识库内容纳入仓库。
