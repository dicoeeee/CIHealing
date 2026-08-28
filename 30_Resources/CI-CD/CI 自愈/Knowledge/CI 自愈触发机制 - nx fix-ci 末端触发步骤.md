---
title: "Nx Self-Healing CI：nx fix-ci 命令与 CI 配置"
type: evergreen
subtype: mechanism
domain: ci-self-healing
status: draft
created: 2026-08-11
updated: 2026-08-12
topics:
  - nx-fix-ci
  - ci-configuration
  - byoc
tags:
  - ci-cd
  - ci-self-healing
  - nx
  - mechanism
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

# Nx Self-Healing CI：nx fix-ci 命令与 CI 配置

## 1. CI 中的调用位置

| 直接事实 | 原文证据 | 适用条件 | 核验日期 |
| --- | --- | --- | --- |
| 普通配置在 main job 中增加运行 `fix-ci` 的 step；该 main job 是调用 `nx start-ci-run` 并启动 `nx run-many` 或 `nx affected` 的 job。 | [Configure your CI pipeline](https://nx.dev/docs/features/ci-features/self-healing-ci#configure-your-ci-pipeline) | Nx Cloud Self-Healing CI | 2026-08-12 |
| GitHub Actions 示例使用 `if: always()`；GitLab、Azure DevOps 和 Bitbucket 示例使用各自的等价 always-run 机制。 | [Configure your CI pipeline](https://nx.dev/docs/features/ci-features/self-healing-ci#configure-your-ci-pipeline) | 相应 CI provider | 2026-08-12 |
| 所有 tasks 成功时，`fix-ci` 自动 no-op。 | [Configure your CI pipeline](https://nx.dev/docs/features/ci-features/self-healing-ci#configure-your-ci-pipeline) | 当前相关 tasks 全部成功 | 2026-08-12 |
| Nx 官方 BYOC 配置说明要求在 main job（orchestrator）和每个 agent job 中加入 `nx fix-ci`，并使用相应的 always-run 条件；该方式需要 Nx Enterprise plan。 | [Configure your CI pipeline — Bringing your own compute](https://nx.dev/docs/features/ci-features/self-healing-ci#configure-your-ci-pipeline) · [Bring Your Own Compute](https://nx.dev/docs/kb/bring-your-own-compute) | Nx Enterprise BYOC | 2026-08-12 |

## 2. CLI 命令参考与公开源码

| 直接事实 | 公开源码 | 核验日期 |
| --- | --- | --- |
| `nx fix-ci` 的命令参考将其描述为 “Fixes CI failures”，并说明它是 `nx-cloud fix-ci` 的 alias。 | [Nx Commands — nx fix-ci](https://nx.dev/docs/reference/nx-commands#nx-fix-ci) | 2026-08-12 |
| 公开命令选项只有 `help`、`verbose` 和通用的 `version`。 | [Nx Commands — nx fix-ci](https://nx.dev/docs/reference/nx-commands#nx-fix-ci) | 2026-08-12 |
| Nx CLI 的 `commandsObject` 调用 `.command(yargsFixCiCommand)`。 | [`nx-commands.ts`](https://github.com/nrwl/nx/blob/da694024c34c988893a99209a1ce21d25a46de06/packages/nx/src/command-line/nx-commands.ts#L123) | 2026-08-12 |
| command object 声明 `fix-ci [options]`，再延迟加载 `fix-ci.js` 并调用 `fixCiHandler`。 | [`command-object.ts`](https://github.com/nrwl/nx/blob/da694024c34c988893a99209a1ce21d25a46de06/packages/nx/src/command-line/nx-cloud/fix-ci/command-object.ts#L5-L18) | 2026-08-12 |
| `fixCiHandler` 先检查 workspace 是否使用 Nx Cloud；未连接时输出警告并返回 `0`，已连接时调用 `executeNxCloudCommand('fix-ci', args.verbose)`。 | [`fix-ci.ts`](https://github.com/nrwl/nx/blob/da694024c34c988893a99209a1ce21d25a46de06/packages/nx/src/command-line/nx-cloud/fix-ci/fix-ci.ts#L9-L15) | 2026-08-12 |
| `executeNxCloudCommand` 获取 Nx Cloud client、配置其 module resolution，然后调用 `nxCloudClient.commands[commandName]()`。 | [`utils.ts`](https://github.com/nrwl/nx/blob/da694024c34c988893a99209a1ce21d25a46de06/packages/nx/src/command-line/nx-cloud/utils.ts#L28-L41) | 2026-08-12 |

## 3. 与 CI Pipeline Execution 相关的公开对象

| 直接事实 | 原文证据 | 适用条件 | 核验日期 |
| --- | --- | --- | --- |
| `nx start-ci-run` 用于启动 CI run；`--fix-tasks` 和 `--auto-apply-fixes` 在该命令上配置单次运行范围。 | [Nx Cloud CLI — start-ci-run](https://nx.dev/docs/reference/nx-cloud-cli#npx-nxcloud-start-ci-run) | Nx Cloud CI | 2026-08-12 |
| Nx glossary 将 CIPE 定义为一次 CI pipeline execution，其中可以包含多个 runs；一个 run 是一个 Nx command 产生的一组 tasks。 | [Nx Glossary — CIPE and Run](https://nx.dev/docs/reference/glossary#cipe-continuous-integration-pipeline-execution) | Nx Cloud 概念模型 | 2026-08-12 |
| `NX_CI_EXECUTION_ID` 用于标识 CI Pipeline Execution；多个 main jobs 还可以使用 `NX_CI_EXECUTION_ENV` 区分环境。 | [Nx environment variables](https://nx.dev/docs/reference/environment-variables#nx-cloud-environment-variables) | Nx Cloud CI | 2026-08-12 |
| `nx-cloud complete-ci-run` 是使用 `--require-explicit-completion` 时显式完成 CI run 的独立命令。 | [Nx Cloud CLI — complete-ci-run](https://nx.dev/docs/reference/nx-cloud-cli#npx-nxcloud-complete-ci-run) | 显式完成模式 | 2026-08-12 |

## 4. 核验范围

- 文档版本：Nx v23 页面，核验日期 2026-08-12。
- 公开源码 commit：`da694024c34c988893a99209a1ce21d25a46de06`，核验日期 2026-08-12。

## 相关笔记

- [[Nx - AI-Powered Self-Healing CI]]
- [[CI 自愈控制机制 - 修复作用域与否决门禁|Nx Self-Healing CI：Workspace settings 与 CLI overrides]]
- [[末端触发与修复编排架构]]
