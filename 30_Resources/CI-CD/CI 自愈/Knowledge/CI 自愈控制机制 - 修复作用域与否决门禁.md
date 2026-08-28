---
title: "Nx Self-Healing CI：Workspace settings 与 CLI overrides"
type: evergreen
subtype: mechanism
domain: ci-self-healing
status: draft
created: 2026-08-11
updated: 2026-08-12
topics:
  - repair-scope
  - task-eligibility
  - workspace-settings
  - auto-apply
  - cli-override
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

# Nx Self-Healing CI：Workspace settings 与 CLI overrides

## 1. Workspace settings

### General settings

| 判断类型 | 配置 | 官方公开行为 | 原文证据 | 核验日期 |
| --- | --- | --- | --- | --- |
| 直接事实 | Enable Self-Healing CI | 对当前 workspace 的 PR 启用 Self-Healing CI | [General settings](https://nx.dev/docs/features/ci-features/self-healing-ci#general-settings) | 2026-08-12 |
| 直接事实 | GitHub PR comments | 在 GitHub PR comments 中显示 Self-Healing CI feedback 和 actions | [General settings](https://nx.dev/docs/features/ci-features/self-healing-ci#general-settings) | 2026-08-12 |
| 直接事实 | Auto-retry flaky tasks | 自动重跑检测为 flaky 的 tasks；实现方式是向 PR branch 推空提交 | [General settings](https://nx.dev/docs/features/ci-features/self-healing-ci#general-settings) | 2026-08-12 |
| 直接事实 | Allow public link access | 持有链接的人可以 apply 或 reject suggested change；页面注明除非必要不建议启用 | [General settings](https://nx.dev/docs/features/ci-features/self-healing-ci#general-settings) | 2026-08-12 |
| 直接事实 | Draft PR handling | 控制是否为 draft PR 创建 fixes | [General settings](https://nx.dev/docs/features/ci-features/self-healing-ci#general-settings) | 2026-08-12 |
| 直接事实 | Protected branch prefixes | 命中配置前缀的 branch 不生成 fixes | [General settings](https://nx.dev/docs/features/ci-features/self-healing-ci#general-settings) | 2026-08-12 |
| 直接事实 | Default and named branches | 默认 branch 以及名为 `main`、`master`、`trunk`、`dev`、`stable`、`canary` 的 branches 不生成 fixes | [General settings](https://nx.dev/docs/features/ci-features/self-healing-ci#general-settings) | 2026-08-12 |

### Eligible tasks

| 判断类型 | 配置模式 | 官方公开行为 | 原文证据 | 核验日期 |
| --- | --- | --- | --- | --- |
| 直接事实 | Any failing task | Nx Cloud 可以主动尝试修复 PR CI pipeline execution 中的任意 failing task | [Eligible tasks](https://nx.dev/docs/features/ci-features/self-healing-ci#eligible-tasks) | 2026-08-12 |
| 直接事实 | Specific patterns | 只有匹配配置 glob patterns 的 failing tasks 进入修复范围 | [Eligible tasks](https://nx.dev/docs/features/ci-features/self-healing-ci#eligible-tasks) | 2026-08-12 |
| 直接事实 | Never fix | 匹配这些 patterns 的 tasks 不生成 fixes | [Eligible tasks](https://nx.dev/docs/features/ci-features/self-healing-ci#eligible-tasks) | 2026-08-12 |

### Auto-apply verified code changes

| 判断类型 | 原文公开条件 | 原文证据 | 适用条件 | 核验日期 |
| --- | --- | --- | --- | --- |
| 直接事实 | 自动提交 code change suggestion 到 PR branch，需要 task 匹配配置的 glob patterns、AI agent 具有 high confidence，且 suggestion 已被 explicitly verified 能修复 failing task。 | [Auto-apply verified code changes](https://nx.dev/docs/features/ci-features/self-healing-ci#auto-apply-verified-code-changes) | Auto-apply verified code changes | 2026-08-12 |

| 判断类型 | Auto-apply 配置 | 官方公开行为 | 原文证据 | 核验日期 |
| --- | --- | --- | --- | --- |
| 直接事实 | Deterministic Nx checks | 内置 preset 覆盖 `nx format:check`、`nx sync:check`、`nx conformance:check`，并调用对应 writable 版本 | [Deterministic Nx checks](https://nx.dev/docs/features/ci-features/self-healing-ci#deterministic-nx-checks) | 2026-08-12 |
| 直接事实 | Additional include patterns | 匹配这些 patterns 的 task 可以在 high-confidence 且 verified 时自动应用 code changes | [Additional include patterns](https://nx.dev/docs/features/ci-features/self-healing-ci#additional-include-patterns) | 2026-08-12 |
| 直接事实 | Exclude patterns | 即使命中 include patterns 或 preset，也不自动应用 code changes | [Exclude patterns](https://nx.dev/docs/features/ci-features/self-healing-ci#exclude-patterns) | 2026-08-12 |

## 2. CLI overrides

| 判断类型 | Flag | 官方公开行为 | 原文证据 | 核验日期 |
| --- | --- | --- | --- | --- |
| 直接事实 | `--fix-tasks` | 覆盖哪些 tasks 具有 fix eligibility | [Available flags](https://nx.dev/docs/features/ci-features/self-healing-ci#available-flags) · [Nx Cloud CLI](https://nx.dev/docs/reference/nx-cloud-cli#fix-tasks) | 2026-08-12 |
| 直接事实 | `--auto-apply-fixes` | 覆盖哪些 self-healing tasks 可以无人工 review 自动应用 | [Available flags](https://nx.dev/docs/features/ci-features/self-healing-ci#available-flags) · [Nx Cloud CLI](https://nx.dev/docs/reference/nx-cloud-cli#auto-apply-fixes) | 2026-08-12 |

| 判断类型 | 原文公开规则 | 原文证据 | 适用条件 | 核验日期 |
| --- | --- | --- | --- | --- |
| 直接事实 | CLI override 存在时优先；没有 CLI override 时回退 workspace settings；空字符串表示显式 disable。 | [Precedence rules](https://nx.dev/docs/features/ci-features/self-healing-ci#precedence-rules) | Workspace settings 与 CLI override 同时涉及同一配置 | 2026-08-12 |

## 3. 公开的 pattern 语法

| 直接事实 | 原文证据 | 核验日期 |
| --- | --- | --- |
| `--fix-tasks` 接受 comma-separated glob patterns。 | [Nx Cloud CLI — fix-tasks](https://nx.dev/docs/reference/nx-cloud-cli#fix-tasks) | 2026-08-12 |
| 文档公开 `*` wildcard、`!` prefix negation 和多个 comma-separated patterns。 | [Nx Cloud CLI — fix-tasks](https://nx.dev/docs/reference/nx-cloud-cli#fix-tasks) | 2026-08-12 |
| `--auto-apply-fixes` 接受 self-healing tasks 的 comma-separated glob patterns。 | [Nx Cloud CLI — auto-apply-fixes](https://nx.dev/docs/reference/nx-cloud-cli#auto-apply-fixes) | 2026-08-12 |
| Self-Healing CI 页面给出 `*lint*,*test*`、`!*e2e*` 和 `lint` 等 pattern 示例。 | [Pattern syntax](https://nx.dev/docs/features/ci-features/self-healing-ci#pattern-syntax) | 2026-08-12 |

## 4. 任务匹配对象

| 判断类型 | 官方示例 | 原文证据 | 适用条件 | 核验日期 |
| --- | --- | --- | --- | --- |
| 直接事实 | Nx task 使用 `<project>:<task>:<configuration>` 形式的 task name。 | [What's New in Nx Self-Healing CI — Fine-Grained Control](https://nx.dev/blog/whats-new-in-nx-self-healing-ci#fine-grained-control-with---fix-tasks) | Nx task | 2026-08-12 |
| 直接事实 | 通过 `nx-cloud record --` 记录的 command 按完整 command string 匹配；官方示例为 `nx-cloud record -- nx format`。 | [What's New in Nx Self-Healing CI — Fine-Grained Control](https://nx.dev/blog/whats-new-in-nx-self-healing-ci#fine-grained-control-with---fix-tasks) | Recorded command | 2026-08-12 |

## 5. Configurations tab

| 判断类型 | 原文公开内容 | 原文证据 | 适用条件 | 核验日期 |
| --- | --- | --- | --- | --- |
| 直接事实 | CI run 后，Configurations tab 显示当次 CI pipeline execution 相关的 workspace settings、已应用的 CLI overrides 和 final effective configuration。 | [Viewing applied configuration](https://nx.dev/docs/features/ci-features/self-healing-ci#viewing-applied-configuration) | CI run 后 | 2026-08-12 |
| 直接事实 | Configurations tab 显示 `SELF_HEALING.md` 是否被 detected and applied。 | [Viewing configuration status](https://nx.dev/docs/features/ci-features/self-healing-ci#viewing-configuration-status) | CI run 后 | 2026-08-12 |

## 6. 核验范围

- Self-Healing CI 页面与 CLI 参考：Nx v23，核验日期 2026-08-12。

## 相关笔记

- [[Nx - AI-Powered Self-Healing CI]]
- [[CI 自愈上下文策略 - SELF_HEALING 指令|Nx Self-Healing CI：SELF_HEALING.md 配置]]
- [[CI 自愈触发机制 - nx fix-ci 末端触发步骤|Nx Self-Healing CI：nx fix-ci 命令与 CI 配置]]
- [[CI 自愈策略控制平面架构]]
