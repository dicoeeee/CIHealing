---
title: "Nx Self-Healing CI：SELF_HEALING.md 配置"
type: evergreen
subtype: mechanism
domain: ci-self-healing
status: draft
created: 2026-08-11
updated: 2026-08-17
topics:
  - repository-instructions
  - self-healing-md
  - claude-md
  - configuration-status
tags:
  - ci-cd
  - ci-self-healing
  - nx
ai_access: true
ai_generated: true
reviewed: false
source_notes:
  - "[[Nx - AI-Powered Self-Healing CI]]"
content_policy: source-bound
inference_allowed: false
design_content_allowed: false
claim_citation_required: true
authority_scope: public-primary-sources
---

# Nx Self-Healing CI：SELF_HEALING.md 配置

## 1. 文件位置与格式

| 直接事实 | 原文证据 | 适用条件 | 核验日期 |
| --- | --- | --- | --- |
| 文件路径是仓库内的 `.nx/SELF_HEALING.md`。 | [Configuration with SELF_HEALING.md](https://nx.dev/docs/features/ci-features/self-healing-ci#configuration-with-self_healingmd) | Nx Cloud Self-Healing CI | 2026-08-12 |
| 该文件包含 freeform Markdown，AI agent 自然读取和解释。 | [Configuration with SELF_HEALING.md](https://nx.dev/docs/features/ci-features/self-healing-ci#configuration-with-self_healingmd) | 文件存在 | 2026-08-12 |
| 官方将使用专用文件的原因描述为：把 CI-specific instructions 与 local development context 分开；文件位于 `.nx` 目录，与其他 Nx Cloud configuration 相邻。 | [Why a dedicated file?](https://nx.dev/docs/features/ci-features/self-healing-ci#configuration-with-self_healingmd) | 文件组织说明 | 2026-08-12 |

## 2. 官方列出的内容类型

| 判断类型 | 内容类型 | 官方说明 | 官方示例 | 原文证据 | 核验日期 |
| --- | --- | --- | --- | --- | --- |
| 直接事实 | Confidence Rules | Override how the AI categorizes failure severity | migrations 路径中的 failure 应分类为 `environment_state` | [What to include](https://nx.dev/docs/features/ci-features/self-healing-ci#what-to-include) | 2026-08-12 |
| 直接事实 | Off-Limits Areas | agent 不应修改的目录或文件 | `/src/generated/` 是生成代码 | [What to include](https://nx.dev/docs/features/ci-features/self-healing-ci#what-to-include) | 2026-08-12 |
| 直接事实 | Fix Preferences | 引导 agent 处理常见问题的方法 | 优先更新 ESLint rules，而不是添加 disable comments | [What to include](https://nx.dev/docs/features/ci-features/self-healing-ci#what-to-include) | 2026-08-12 |
| 直接事实 | Predefined Fixes | 为已知 failure 指定 deterministic solution | lint failure 先尝试 `nx lint --fix` | [What to include](https://nx.dev/docs/features/ci-features/self-healing-ci#what-to-include) | 2026-08-12 |
| 直接事实 | Context | 引用 agent 应读取的其他文档 | 参考 `ARCHITECTURE.md` 了解 module boundaries | [What to include](https://nx.dev/docs/features/ci-features/self-healing-ci#what-to-include) | 2026-08-12 |

### Example SELF_HEALING.md 中的指令

| 判断类型 | 官方示例中的指令 | 原文证据 | 适用条件 | 核验日期 |
| --- | --- | --- | --- | --- |
| 直接事实 | test target 的 fix 需要 high confidence。 | [Example SELF_HEALING.md](https://nx.dev/docs/features/ci-features/self-healing-ci#example-self_healingmd) | 官方示例文件 | 2026-08-12 |
| 直接事实 | formatting fix 可以使用 medium confidence。 | [Example SELF_HEALING.md](https://nx.dev/docs/features/ci-features/self-healing-ci#example-self_healingmd) | 官方示例文件 | 2026-08-12 |
| 直接事实 | `/legacy/` 需要 manual review。 | [Example SELF_HEALING.md](https://nx.dev/docs/features/ci-features/self-healing-ci#example-self_healingmd) | 官方示例文件 | 2026-08-12 |
| 直接事实 | type error 优先显式类型，不使用 `any`。 | [Example SELF_HEALING.md](https://nx.dev/docs/features/ci-features/self-healing-ci#example-self_healingmd) | 官方示例文件 | 2026-08-12 |

## 3. 与 CLAUDE.md 的公开关系

| 直接事实 | 原文证据 | 适用条件 | 核验日期 |
| --- | --- | --- | --- |
| 如果仓库根目录已存在 `CLAUDE.md`，Self-Healing CI agent 会读取它以获得额外上下文。 | [Using CLAUDE.md](https://nx.dev/docs/features/ci-features/self-healing-ci#using-claudemd) | 根目录存在该文件 | 2026-08-12 |
| 两个文件都存在时，冲突指令由 `SELF_HEALING.md` 优先。 | [Using CLAUDE.md](https://nx.dev/docs/features/ci-features/self-healing-ci#using-claudemd) | 指令发生冲突 | 2026-08-12 |
| 两个文件都会被读取；`CLAUDE.md` 的 general context 仍可使用。 | [Using CLAUDE.md](https://nx.dev/docs/features/ci-features/self-healing-ci#using-claudemd) | 两个文件都存在 | 2026-08-12 |
| 官方建议将 CI-specific instructions 放在 `SELF_HEALING.md`。 | [Using CLAUDE.md](https://nx.dev/docs/features/ci-features/self-healing-ci#using-claudemd) | 文件职责建议 | 2026-08-12 |

## 4. 配置状态入口

| 直接事实 | 原文证据 | 适用条件 | 核验日期 |
| --- | --- | --- | --- |
| CI run 之后，可以在 Nx Cloud pipeline execution 的 Configurations tab 查看 `SELF_HEALING.md` 是否被 detected and applied。 | [Viewing configuration status](https://nx.dev/docs/features/ci-features/self-healing-ci#viewing-configuration-status) | CI run 已完成并可在 Nx Cloud 查看 | 2026-08-12 |
| 同一 Configurations tab 可以显示当次 CI pipeline execution 相关的 workspace settings、CLI overrides 和 final effective configuration。 | [Viewing applied configuration](https://nx.dev/docs/features/ci-features/self-healing-ci#viewing-applied-configuration) | 使用相应 workspace settings 或 CLI overrides | 2026-08-12 |

## 5. 核验范围

- 文档版本：Nx v23 页面，核验日期 2026-08-12。
- 文档源码 commit：`da694024c34c988893a99209a1ce21d25a46de06`。

## 相关笔记

- [[Nx - AI-Powered Self-Healing CI]]
- [[CI 自愈控制机制 - 修复作用域与否决门禁|Nx Self-Healing CI：Workspace settings 与 CLI overrides]]
- [[仓库级语义指令作为修复上下文]]
