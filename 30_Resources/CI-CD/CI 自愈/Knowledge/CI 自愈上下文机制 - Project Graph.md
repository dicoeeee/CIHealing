---
title: "Nx Project Graph 与 Task Graph"
type: evergreen
subtype: mechanism
domain: ci-self-healing
status: draft
created: 2026-08-11
updated: 2026-08-17
topics:
  - project-graph
  - task-graph
  - project-configuration
  - self-healing-ci
tags:
  - ci-cd
  - ci-self-healing
  - nx
  - project-graph
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

# Nx Project Graph 与 Task Graph

## 1. Self-Healing CI 页面公开的用途

| 判断类型 | 命题 | 原文证据 | 适用条件 | 核验日期 |
| --- | --- | --- | --- | --- |
| 厂商主张 | Nx 称 Self-Healing CI 的 AI agents 通过 Project Graph 和 metadata 理解 workspace structure、project relationships 与 build configurations。 | [Self-Healing CI — Overview](https://nx.dev/docs/features/ci-features/self-healing-ci#overview) | Nx Cloud Self-Healing CI | 2026-08-12 |

## 2. Project Graph 与 Task Graph

| 直接事实 | 原文证据 | 适用条件 | 核验日期 |
| --- | --- | --- | --- |
| Nx workspace 中的 projects 存在 dependencies，并形成 Project Graph。 | [Explore your Workspace](https://nx.dev/docs/features/explore-graph#explore-the-project-graph) | Nx workspace | 2026-08-12 |
| Nx 通过分析 source code 计算 Project Graph，并保持该图更新。 | [Explore the project graph](https://nx.dev/docs/features/explore-graph#explore-the-project-graph) | Nx 支持的项目与插件分析范围 | 2026-08-12 |
| Task Graph 是 tasks 及其 dependencies 组成的独立图；它基于 Project Graph 并决定 tasks 的执行方式。 | [Explore your Workspace](https://nx.dev/docs/features/explore-graph#explore-your-workspace) | Nx task execution | 2026-08-12 |
| `npx nx graph` 可以启动 Project Graph UI。 | [Launching the project graph](https://nx.dev/docs/features/explore-graph#launching-the-project-graph) | 本地 Nx workspace | 2026-08-12 |
| `nx graph --file=output.json` 可以导出用于 Project Graph visualization 的底层数据。 | [Export project graph to JSON](https://nx.dev/docs/features/explore-graph#export-project-graph-to-json) | 本地 Nx workspace | 2026-08-12 |
| dependency line 可以显示形成 dependency 的文件；UI 可以追踪两个 projects 之间的 dependency chain。 | [Focusing on valuable projects](https://nx.dev/docs/features/explore-graph#focusing-on-valuable-projects) | Project Graph UI 有相应 dependency provenance | 2026-08-12 |
| Nx command 使用 `--graph` 可以查看该命令执行的 Task Graph。 | [Explore the task graph](https://nx.dev/docs/features/explore-graph#explore-the-task-graph) | 支持 `--graph` 的 Nx task command | 2026-08-12 |

## 3. Project Graph 的公开数据对象

### Project node

| 直接事实 | 原文证据 | 核验日期 |
| --- | --- | --- |
| `ProjectGraphProjectNode` 包含 `name`、`type` 和 `data`；`type` 可以是 `app`、`e2e` 或 `lib`。 | [ProjectGraphProjectNode API](https://nx.dev/docs/reference/devkit/ProjectGraphProjectNode) | 2026-08-12 |
| `data` 是 `ProjectConfiguration` 加可选 `description`。 | [ProjectGraphProjectNode API](https://nx.dev/docs/reference/devkit/ProjectGraphProjectNode) | 2026-08-12 |
| `ProjectConfiguration` 公开 `root`、可选 `sourceRoot`、`projectType`、`targets`、`namedInputs`、`implicitDependencies`、`tags` 和 `metadata` 等字段。 | [ProjectConfiguration API](https://nx.dev/docs/reference/devkit/ProjectConfiguration) | 2026-08-12 |
| `TargetConfiguration` 公开 `executor`、`command`、`options`、`configurations`、`dependsOn`、`inputs`、`outputs`、`cache`、`parallelism` 和 `metadata` 等字段。 | [TargetConfiguration API](https://nx.dev/docs/reference/devkit/TargetConfiguration) | 2026-08-12 |

### External node

| 直接事实 | 原文证据 | 核验日期 |
| --- | --- | --- |
| `ProjectGraphExternalNode` 描述 workspace 外部的 dependency。 | [ProjectGraphExternalNode API](https://nx.dev/docs/reference/devkit/ProjectGraphExternalNode) | 2026-08-12 |
| 其 `data` 包含 `packageName`、`version` 和可选 `hash`；文档给出 `npm:packageName` 与带嵌套版本的命名例。 | [ProjectGraphExternalNode API](https://nx.dev/docs/reference/devkit/ProjectGraphExternalNode) | 2026-08-12 |

### Dependency

| 直接事实 | 原文证据 | 核验日期 |
| --- | --- | --- |
| `ProjectGraphDependency` 公开 `source`、`target` 和 `type` 字段。 | [ProjectGraphDependency API](https://nx.dev/docs/reference/devkit/ProjectGraphDependency) | 2026-08-12 |
| Project Graph plugin 的 `createDependencies` 可以创建 static、dynamic 和 implicit dependencies。 | [Extending the Project Graph](https://nx.dev/docs/extending-nx/project-graph-plugins#adding-new-dependencies-to-the-project-graph) | 2026-08-12 |
| static 和 dynamic dependency 可以包含 `sourceFile`；implicit dependency 没有关联 source file。 | [Extending the Project Graph](https://nx.dev/docs/extending-nx/project-graph-plugins#adding-new-dependencies-to-the-project-graph) | 2026-08-12 |
| 项目配置的 `implicitDependencies` 可以显式添加 dependency，也支持 `!project` 排除和 glob。 | [Project configuration — implicitDependencies](https://nx.dev/docs/reference/project-configuration#implicitdependencies) | 2026-08-12 |

## 4. Task Graph 与 Task 的公开字段

| 直接事实 | 原文证据 | 核验日期 |
| --- | --- | --- |
| `TaskGraph` 包含 `tasks`、`dependencies`、`continuousDependencies` 和 `roots`。 | [TaskGraph API](https://nx.dev/docs/reference/devkit/TaskGraph) | 2026-08-12 |
| 单个 `Task` 包含 `id`、`target`、`projectRoot`、`outputs`、`overrides`、cache 信息与 `hash` 等字段。 | [Task API](https://nx.dev/docs/reference/devkit/Task) | 2026-08-12 |
| `Task.target` 使用 project、target 和可选 configuration 标识要运行的 target。 | [Task API](https://nx.dev/docs/reference/devkit/Task) · [Target API](https://nx.dev/docs/reference/devkit/Target) | 2026-08-12 |
| Nx environment variables 包含 `NX_TASK_TARGET_PROJECT`、`NX_TASK_TARGET_TARGET` 和 `NX_TASK_TARGET_CONFIGURATION`。 | [Nx environment variables](https://nx.dev/docs/reference/environment-variables#nx-task-environment-variables) | 2026-08-12 |

## 5. Project Graph 的公开生成机制

| 直接事实 | 原文证据 | 适用条件 | 核验日期 |
| --- | --- | --- | --- |
| Nx plugins 使用 `createNodes` 从匹配的配置或 manifest 文件创建 projects、targets 和 metadata。 | [Extending the Project Graph](https://nx.dev/docs/extending-nx/project-graph-plugins#adding-a-new-project-to-the-project-graph) | 注册相应 plugin | 2026-08-12 |
| package manager workspace 中引用的 `package.json` 可以作为 project 加入 Project Graph。 | [Including package.json files as projects](https://nx.dev/docs/reference/project-configuration#including-packagejson-files-as-projects-in-the-graph) | package manager workspace 配置 | 2026-08-12 |
| project configuration 由 inferred tasks、`targetDefaults` 和 project-level configuration 合并；后者覆盖前者。 | [Project configuration](https://nx.dev/docs/reference/project-configuration) | Nx project configuration | 2026-08-12 |
| Nx Daemon 在本地监视 workspace 文件，并在内存中计算和更新 Project Graph；CI、Docker 和 sandbox 环境默认禁用 daemon。 | [Nx Daemon](https://nx.dev/docs/concepts/nx-daemon) | 本地 Nx workspace 与相应环境 | 2026-08-12 |
| affected 计算将 changed files 映射到 projects，再使用 Project Graph 将 dependent projects 加入 affected set。 | [Nx mental model — Affected commands](https://nx.dev/docs/concepts/mental-model#affected-commands) | affected command | 2026-08-12 |

## 6. 核验范围

- Self-Healing CI 页面：Nx v23，核验日期 2026-08-12。
- Devkit API 与概念文档：Nx v23，核验日期 2026-08-12。

## 相关笔记

- [[Nx - AI-Powered Self-Healing CI]]
- [[CI 自愈触发机制 - nx fix-ci 末端触发步骤|Nx Self-Healing CI：nx fix-ci 命令与 CI 配置]]
- [[图谱驱动的失败上下文裁剪]]
