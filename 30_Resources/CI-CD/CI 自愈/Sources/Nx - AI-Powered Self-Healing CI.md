---
type: source
subtype: official-doc
domain: ci-self-healing
status: approved
created: 2026-08-11
updated: 2026-08-12
source: "Nx 官方文档"
url: "https://nx.dev/docs/features/ci-features/self-healing-ci"
canonical_url: "https://nx.dev/docs/features/ci-features/self-healing-ci"
authority: primary
published:
verified_at: 2026-08-12
refresh_days: 30
snapshot: "[[2026-08-11 - Nx - AI-Powered Self-Healing CI]]"
content_fingerprint: "0df4b4a2a8d1a860079c7b1d52b7baa382847f5ce6b1a36bc618cbbd0931f563"
vendor: Nx
topics:
  - failure-detection
  - failure-analysis
  - repair-proposal
  - task-verification
  - controlled-writeback
tags:
  - ci-cd
  - ci-self-healing
  - nx
  - ai-agent
ai_access: true
ai_generated: true
reviewed: false
source_notes: []
content_policy: source-bound
inference_allowed: false
design_content_allowed: false
claim_citation_required: true
authority_scope: public-primary-sources
---

# Nx - AI-Powered Self-Healing CI

> [!important] 来源约束
> 本笔记执行 [[来源约束知识规则]]，只保存 Nx 官方页面直接描述的事实、带归属的厂商主张、证据缺口与来源冲突。`reviewed: false` 表示当前整理结果仍待逐条人工确认。

## 来源信息

| 字段 | 内容 |
| --- | --- |
| 作者或机构 | Nx |
| 来源类型 | 官方产品文档 |
| 发布时间 | 页面未标明 |
| 原始链接 | [AI-Powered Self-Healing CI](https://nx.dev/docs/features/ci-features/self-healing-ci) |
| 本地快照 | [[2026-08-11 - Nx - AI-Powered Self-Healing CI]] |
| 快照 SHA-256 | `0df4b4a2a8d1a860079c7b1d52b7baa382847f5ce6b1a36bc618cbbd0931f563` |
| 最后核验 | 2026-08-12 |

## 直接事实

### CI 触发入口

| 命题 | 原文位置 | 适用条件 | 核验日期 |
| --- | --- | --- | --- |
| Self-Healing CI 需要 Nx Cloud workspace 启用 VCS integration；页面列出 GitHub、GitLab、Azure DevOps 和 Bitbucket。 | [Enable self-healing CI](https://nx.dev/docs/features/ci-features/self-healing-ci#enable-self-healing-ci) | Nx Cloud Self-Healing CI | 2026-08-12 |
| CI 配置需要在运行 `nx start-ci-run` 并启动 `nx run-many` 或 `nx affected` 的 main job 中增加 `nx fix-ci` step。 | [Configure your CI pipeline](https://nx.dev/docs/features/ci-features/self-healing-ci#configure-your-ci-pipeline) | CI provider pipeline | 2026-08-12 |
| `nx fix-ci` 需要使用 `if: always()`、`after_script` 或 provider 等价条件，使其在前序步骤失败时仍执行。 | [Configure your CI pipeline](https://nx.dev/docs/features/ci-features/self-healing-ci#configure-your-ci-pipeline) | CI provider pipeline | 2026-08-12 |
| 所有 tasks 成功时，`fix-ci` 自动成为 no-op。 | [Configure your CI pipeline](https://nx.dev/docs/features/ci-features/self-healing-ci#configure-your-ci-pipeline) | 当前运行的全部 Nx tasks 成功 | 2026-08-12 |

### 配置与任务范围

| 命题 | 原文位置 | 适用条件 | 核验日期 |
| --- | --- | --- | --- |
| Self-Healing CI 的常态配置入口是 Nx Cloud workspace settings。 | [Configuring self-healing CI](https://nx.dev/docs/features/ci-features/self-healing-ci#configuring-self-healing-ci) | Nx Cloud workspace | 2026-08-12 |
| workspace settings 可以控制功能开关、PR 评论、flaky 自动重试、public link access、draft PR 和保护分支前缀。 | [General settings](https://nx.dev/docs/features/ci-features/self-healing-ci#general-settings) | Nx Cloud workspace | 2026-08-12 |
| 默认分支以及名为 `main`、`master`、`trunk`、`dev`、`stable`、`canary` 的分支不生成修复。 | [General settings](https://nx.dev/docs/features/ci-features/self-healing-ci#general-settings) | Self-Healing CI 修复生成 | 2026-08-12 |
| Eligible tasks 可以配置为任意失败任务或 Specific patterns；Never fix patterns 用于排除任务。 | [Eligible tasks](https://nx.dev/docs/features/ci-features/self-healing-ci#eligible-tasks) | PR CI pipeline execution | 2026-08-12 |
| `--fix-tasks` 与 `--auto-apply-fixes` 可以覆盖 workspace settings；CLI override 存在时优先，没有时回退 workspace settings。 | [Advanced: CLI overrides](https://nx.dev/docs/features/ci-features/self-healing-ci#advanced-cli-overrides) | 单次 CI run | 2026-08-12 |
| 两个 CLI flag 的空字符串都表示对当前 CI run 显式禁用，不表示使用默认值。 | [Disabling features via CLI](https://nx.dev/docs/features/ci-features/self-healing-ci#disabling-features-via-cli) | 单次 CI run | 2026-08-12 |

### 修复上下文与建议

| 命题 | 原文位置 | 适用条件 | 核验日期 |
| --- | --- | --- | --- |
| 页面列出 Project Graph 和 metadata，用于提供 workspace 结构、项目关系和构建配置上下文。 | [Overview](https://nx.dev/docs/features/ci-features/self-healing-ci#overview) | Nx 对产品上下文能力的说明 | 2026-08-12 |
| `.nx/SELF_HEALING.md` 是 freeform Markdown，由 AI agent 自然读取和解释。 | [Configuration with SELF_HEALING.md](https://nx.dev/docs/features/ci-features/self-healing-ci#configuration-with-self_healingmd) | 仓库包含该文件 | 2026-08-12 |
| 页面列出的文件内容类型包括 Confidence Rules、Off-Limits Areas、Fix Preferences、Predefined Fixes 和 Context。 | [What to include](https://nx.dev/docs/features/ci-features/self-healing-ci#what-to-include) | `.nx/SELF_HEALING.md` | 2026-08-12 |
| 根目录 `CLAUDE.md` 也会被读取；与 `SELF_HEALING.md` 冲突时，后者优先。 | [Using CLAUDE.md](https://nx.dev/docs/features/ci-features/self-healing-ci#using-claudemd) | 两个文件同时存在 | 2026-08-12 |
| PR/MR 评论可以显示 reasoning 摘要、diff，以及 Apply 和 Reject 操作。 | [On your pull request/Merge request](https://nx.dev/docs/features/ci-features/self-healing-ci#on-your-pull-requestmerge-request) | 已配置相应 VCS 集成与通知 | 2026-08-12 |
| 建议可以从 PR/MR、Nx Cloud UI 或编辑器应用，也可以使用 Apply Locally 在本地取得并修改。 | [Applying and reverting fixes](https://nx.dev/docs/features/ci-features/self-healing-ci#applying-and-reverting-fixes) | 已生成修复建议 | 2026-08-12 |

### 自动应用与撤销

| 命题 | 原文位置 | 适用条件 | 核验日期 |
| --- | --- | --- | --- |
| 自动提交代码建议要求同时满足：任务匹配配置模式、AI agent 具有高置信、建议已被明确验证能够修复 failing task。 | [Auto-apply verified code changes](https://nx.dev/docs/features/ci-features/self-healing-ci#auto-apply-verified-code-changes) | 自动应用到 PR branch | 2026-08-12 |
| Deterministic Nx checks preset 覆盖 `nx format:check`、`nx sync:check`、`nx conformance:check`，并调用对应 writable 版本。 | [Deterministic Nx checks](https://nx.dev/docs/features/ci-features/self-healing-ci#deterministic-nx-checks) | 启用该 preset 且相应命令失败 | 2026-08-12 |
| Exclude patterns 会阻止代码建议自动应用，即使命中 include patterns 或 preset。 | [Exclude patterns](https://nx.dev/docs/features/ci-features/self-healing-ci#exclude-patterns) | auto-apply 配置 | 2026-08-12 |
| 已应用的修复可以通过 Git revert，或使用 Nx Cloud diff viewer 的 Revert changes 撤销。 | [Reverting a fix](https://nx.dev/docs/features/ci-features/self-healing-ci#reverting-a-fix) | 修复已应用 | 2026-08-12 |
| Auto-retry flaky tasks 通过向 PR branch 推送空提交触发重试。 | [General settings](https://nx.dev/docs/features/ci-features/self-healing-ci#general-settings) | 开启该配置 | 2026-08-12 |

## 厂商主张

| 厂商表述 | 原文位置 | 证据边界 | 核验日期 |
| --- | --- | --- | --- |
| Nx 称 Self-Healing CI 能够自动检测、分析并提出 CI failure 的修复。 | [Overview](https://nx.dev/docs/features/ci-features/self-healing-ci#overview) | 页面未提供检测或修复准确率 | 2026-08-12 |
| Nx 称该能力能够改善 Time to Green，并减少人工照看 PR。 | [Overview](https://nx.dev/docs/features/ci-features/self-healing-ci#overview) | 页面未提供独立测量方法或效果数据 | 2026-08-12 |
| Nx 将 Project Graph 与 metadata 带来的能力称为 Deep Context。 | [Overview](https://nx.dev/docs/features/ci-features/self-healing-ci#overview) | 页面未公开上下文实际载荷或裁剪算法 | 2026-08-12 |

## 证据缺口

截至 2026-08-12，本页面没有公开：

- failure 分类算法、分类标签、模型、prompt、阈值或准确率；
- 进入分析过程的完整日志、diff、测试报告、提交历史和环境字段清单；
- Project Graph、metadata 与仓库文件的实际选择和裁剪算法；
- “highly confident”的计算、阈值、校准或审计方式；
- “explicitly verified”所执行的命令、环境、输入一致性、cache 策略和最大验证轮次；
- 修复 Agent 的工具、沙箱、最大修复轮次、超时和 token budget；
- 自动提交使用的 VCS 身份、token scope、提交签名和并发冲突处理；
- 代码建议写回后是否执行完整 PR CI、哪些 Required Checks 运行以及结果；
- 自动写回后外层 CI 失败时的自动回滚或人工接管协议；
- `SELF_HEALING.md` 中 Off-Limits Areas 是否另由独立确定性组件强制校验。

## 来源冲突

本轮未在该页面内部记录到需要归一化的冲突表述。页面未标注产品生命周期状态，不补写 GA、Beta 或 Preview。

## 原文快照

- 快照：[[2026-08-11 - Nx - AI-Powered Self-Healing CI]]
- SHA-256：`0df4b4a2a8d1a860079c7b1d52b7baa382847f5ce6b1a36bc618cbbd0931f563`
- 版本规则：官方页面发生实质变化时创建新日期快照；旧快照与指纹不覆盖。

## 关联知识

- [[CI 自愈触发机制 - nx fix-ci 末端触发步骤]]
- [[CI 自愈上下文机制 - Project Graph]]
- [[CI 自愈上下文策略 - SELF_HEALING 指令]]
- [[CI 自愈控制机制 - 修复作用域与否决门禁]]
