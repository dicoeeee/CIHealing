---
title: "Agent Skills CI 问题分析与修复事实报告"
type: project
subtype: architecture
domain: ci-self-healing
status: draft
created: 2026-08-29
updated: 2026-08-29
tags:
  - ci-cd
  - ci-self-healing
  - agent-skills
  - diagnosis
  - evidence
ai_access: true
ai_generated: true
reviewed: false
authority: conversation
source_notes: []
candidate_notes: []
decision_notes: []
experiment_notes: []
content_policy: design
archive_source_path: "/Users/zhujiayi/Documents/Codex/2026-08-28/qi/outputs/ci-self-healing-skills-evidence-report-2026-08-28.md"
archive_source_sha256: "e1382d3c1798f6aa8aeacc97c27c7f9a4ccbe52385eb5c4a12474a6be8d5888e"
---

# CI 自愈相关问题分析与修复 Skills：事实、数据与证据报告

**快照日期：2026-08-28**  
**范围：跨 CI 平台；仅陈述事实、源码可证机制、基于数据的观察和证据缺口。本文不提供产品方案、MVP 合同、选型或落地建议。**

## 1. 核心事实

1. 本次合并分析了 **100 份可解析的检索/榜单响应**，去重后得到 **813 个 Skill 条目**。其中安装量代理指标达到 100,000 的有 112 个，达到 10,000 的有 269 个，达到 1,000 的有 793 个。宽泛检索会带入大量纯生成、教学、运维模板和重复项，因此这 813 个不是 813 个 CI 修复器。
2. 经名称、description、`SKILL.md` 与配套脚本筛选后，进入 CI 自愈机制编码的高相关样本为 **32 个**；其中 **7 个 ≥100,000、17 个 ≥10,000、26 个 ≥3,000**。安装量最高的 7 个全部属于通用调试、TDD、合并冲突或完成验证，不是直接连接多种 CI 平台的自愈执行器。
3. 在检索快照中，精确主题 `fix ci` 只返回 **2 个**直接候选：[`openai/skills/gh-fix-ci`](https://skills.sh/openai/skills/gh-fix-ci)（9,695）和 [`cursor/plugins/fix-ci`](https://skills.sh/cursor/plugins/fix-ci)（1,629）。前者限 GitHub Actions；后者同样围绕 GitHub PR checks。
4. 对 32 个候选做源码内容编码后：**27/32（84.4%）**明确要求收集证据，**20/32（62.5%）**会修改代码或配置，**17/32（53.1%）**把复验写成明确步骤；只有 **6/32（18.8%）**明确规定根因分析机制，**11/32（34.4%）**直接读取外部 CI/监控状态，**3/32（9.4%）**明确执行工作树之外的流程状态写回。**0/32** 记录可供后续运行使用的跨次修复经验。
5. 只有 **4/32** 同时明确覆盖“证据 → 根因 → 修改 → 复验”：[`diagnosing-bugs`](https://skills.sh/mattpocock/skills/diagnosing-bugs)、[`systematic-debugging`](https://skills.sh/obra/superpowers/systematic-debugging)、[`fix-security-vulnerabilities-with-strix`](https://skills.sh/usestrix/strix/fix-security-vulnerabilities-with-strix) 和 [`golang-troubleshooting`](https://skills.sh/samber/cc-skills-golang/golang-troubleshooting)。其中只有 Strix 条目读取外部 finding/scan 状态，但它的流程写回被编码为“部分”，且四者都没有跨次学习机制。
6. 在这 32 个样本里，没有一个条目把“外部失败状态 → 证据 → 明确根因 → 修改 → 明确复验 → 流程写回 → 跨次学习”全部写成强制闭环。这是对公开源码内容的观察，不是对其未公开实现或真实效果的判断。

## 2. 数据口径与方法

### 2.1 “使用量”实际测到的是什么

[skills.sh 文档](https://www.skills.sh/docs)、[CLI 遥测说明](https://www.skills.sh/docs/cli)和[接口文档](https://www.skills.sh/docs/api)公开的是安装遥测，不是运行时调用次数、修复成功率、用户留存或生产采用率。因此全文使用“安装量”或“安装量代理指标”，不把它改写为真实使用量。

批量枚举采用公开 [skills.sh 数据镜像](https://skills-api.deeptoai.com/)的 2026-08-27 快照；关键条目的行为判断全部回到 skills.sh 官方页面、上游 `SKILL.md`、配套脚本或公开 Issue。第三方镜像只承担枚举和同一时点的精确整数比较，不承担机制判断。

### 2.2 搜索覆盖

检索词覆盖 bug、debug、root cause、error recovery、CI failure、GitHub Actions、GitLab CI、Jenkins、Azure Pipelines、Buildkite、CircleCI、build、compiler、lint、test failure、flaky test、dependency、rollback、retry、logs、static analysis、security remediation、regression 和 verification 等主题。

几个直接主题的返回量如下。这里的 `0` 只表示该查询在本次快照没有返回直接结果，不等于整个生态不存在相关 Skill。

| 查询 | 返回条目数 | 返回内容的事实性质 |
|---|---:|---|
| `fix ci` | 2 | 两个 GitHub PR checks 修复流程 |
| `github actions` | 3 | 1 个 GitHub Actions 操作 Skill、1 个模板 Skill、1 个文档类条目 |
| `gitlab ci` | 1 | GitLab CI 配置模式与流水线生成 |
| `jenkins` | 1 | Jenkinsfile 生成 |
| `azure pipelines` | 1 | VS Code 仓库专用的 Azure Pipeline 迭代流程 |
| `buildkite` | 0 | 本次查询无直接返回 |
| `circleci` | 0 | 本次查询无直接返回 |
| `test failures` | 1 | Salesforce DevOps Center 测试失败分析 |
| `logs` | 9 | 日志检索、分析与供应商连接器为主 |
| `pipeline` | 31 | 大量为流水线设计、生成、审计，而非失败修复 |

### 2.3 内容编码规则

本报告用以下观察链编码公开源码：**外部状态 → 证据 → 根因 → 修改 → 复验 → 流程写回 → 跨次学习**。

- `✓`：源码把该环节写成明确、当前流程内的动作或输出。
- `△`：源码提及该环节，但它是可选项、建议、检查清单、人工后续动作或只覆盖部分情形。
- `—`：公开源码没有规定该环节。它表示“没有公开证据”，不等于作者或底层工具绝不具备该能力。

“根因”只在源码规定了复现、假设检验、因果链/数据流追踪、责任边界定位或经过验证的 finding 时记为 `✓`；只要求寻找“第一个 actionable error”、读日志或按错误类型套修复模板记为 `△` 或 `—`。同理，补丁存在不自动等于根因正确，CI 变绿也不自动等于语义正确。

## 3. 32 个高相关候选的量化矩阵

同一矩阵另存为可排序的 [TSV 数据文件](/Users/zhujiayi/Documents/Codex/2026-08-28/qi/outputs/ci-self-healing-skills-mechanism-matrix-2026-08-28.tsv)。

下表安装量为 2026-08-27 镜像快照的精确整数，用于同一时点比较。`外部态`指直接读取 CI、代码审查、监控、扫描器或事件系统；`写回`指修改工作树之外的流程状态，例如 push、评论、创建 WorkItem、触发/更新外部状态或完成 git merge/rebase 操作；`跨次学习`指把本次失败、根因、修复和验证结果持久化供后续运行检索使用。

| Skill | 安装量* | 主要角色 | 外部态 | 证据 | 根因 | 修改 | 复验 | 写回 | 跨次学习 |
|---|---:|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| [`diagnosing-bugs`](https://skills.sh/mattpocock/skills/diagnosing-bugs) | 486,581 | 通用调试 | — | ✓ | ✓ | ✓ | ✓ | — | — |
| [`systematic-debugging`](https://skills.sh/obra/superpowers/systematic-debugging) | 238,871 | 通用调试 | — | ✓ | ✓ | ✓ | ✓ | — | — |
| [`safe-debug`](https://skills.sh/lllllllama/rigorpilot-skills/safe-debug) | 310,501 | 诊断与计划 | — | ✓ | ✓ | — | — | — | — |
| [`gh-fix-ci`](https://skills.sh/openai/skills/gh-fix-ci) | 9,695 | CI 诊断/修复 | ✓ | ✓ | △ | ✓ | △ | △ | — |
| [`fix-ci`](https://skills.sh/cursor/plugins/fix-ci) | 1,629 | CI 修复循环 | ✓ | ✓ | △ | ✓ | ✓ | ✓ | — |
| [`diagnose-ci-failures`](https://skills.sh/warpdotdev/common-skills/diagnose-ci-failures) | 22,910 | CI 诊断/计划 | ✓ | ✓ | △ | — | — | — | — |
| [`fix-errors`](https://skills.sh/warpdotdev/common-skills/fix-errors) | 23,015 | 构建/lint/test 修复 | — | △ | △ | ✓ | ✓ | — | — |
| [`dx-devops-test-failures-analyze`](https://skills.sh/forcedotcom/sf-skills/dx-devops-test-failures-analyze) | 4,173 | CI 测试失败分析 | ✓ | ✓ | △ | — | — | △ | — |
| [`fingerprint-ci-gate`](https://skills.sh/liarjsdev/liarjs-skills/fingerprint-ci-gate) | 28,582 | 确定性 CI 门禁 | — | ✓ | — | — | ✓ | — | — |
| [`verification-before-completion`](https://skills.sh/obra/superpowers/verification-before-completion) | 191,616 | 完成声明门禁 | — | ✓ | — | — | ✓ | — | — |
| [`verification-loop`](https://skills.sh/affaan-m/ecc/verification-loop) | 8,644 | 通用复验 | — | ✓ | — | — | ✓ | — | — |
| [`tdd`](https://skills.sh/mattpocock/skills/tdd) | 780,828 | 测试驱动修复 | — | ✓ | — | ✓ | ✓ | — | — |
| [`test-driven-development`](https://skills.sh/obra/superpowers/test-driven-development) | 209,396 | 测试驱动修复 | — | ✓ | — | ✓ | ✓ | — | — |
| [`autofix`](https://skills.sh/coderabbitai/skills/autofix) | 6,739 | Review 意见修复 | ✓ | ✓ | △ | ✓ | △ | ✓ | — |
| [`sentry-fix-issues`](https://skills.sh/getsentry/sentry-for-ai/sentry-fix-issues) | 7,299 | 生产错误修复 | ✓ | ✓ | ✓ | ✓ | △ | △ | — |
| [`fix-security-vulnerabilities-with-strix`](https://skills.sh/usestrix/strix/fix-security-vulnerabilities-with-strix) | 4,532 | 安全 finding 修复 | ✓ | ✓ | ✓ | ✓ | ✓ | △ | — |
| [`resolving-merge-conflicts`](https://skills.sh/mattpocock/skills/resolving-merge-conflicts) | 375,786 | 合并冲突修复 | — | ✓ | △ | ✓ | ✓ | ✓ | — |
| [`dependency-upgrade`](https://skills.sh/wshobson/agents/dependency-upgrade) | 9,465 | 依赖升级 | — | ✓ | — | ✓ | ✓ | — | — |
| [`dart-run-static-analysis`](https://skills.sh/dart-lang/skills/dart-run-static-analysis) | 13,525 | 静态分析修复 | — | ✓ | — | ✓ | ✓ | — | — |
| [`dart-fix-runtime-errors`](https://skills.sh/dart-lang/skills/dart-fix-runtime-errors) | 13,387 | 元数据/正文错位 | — | ✓ | △ | ✓ | ✓ | — | — |
| [`golang-lint`](https://skills.sh/samber/cc-skills-golang/golang-lint) | 36,422 | lint 修复 | — | ✓ | — | △ | ✓ | — | — |
| [`golang-troubleshooting`](https://skills.sh/samber/cc-skills-golang/golang-troubleshooting) | 36,305 | Go 调试 | — | ✓ | ✓ | ✓ | ✓ | — | — |
| [`azure-pipelines`](https://skills.sh/microsoft/vscode/azure-pipelines) | 2,637 | CI 流水线迭代 | ✓ | ✓ | △ | ✓ | ✓ | △ | — |
| [`bigquery-pipeline-audit`](https://skills.sh/github/awesome-copilot/bigquery-pipeline-audit) | 8,806 | 数据流水线审计 | — | ✓ | △ | — | — | — | — |
| [`observability-logs-search`](https://skills.sh/elastic/agent-skills/observability-logs-search) | 2,945 | 日志证据 | ✓ | ✓ | △ | — | — | — | — |
| [`dd-logs`](https://skills.sh/datadog-labs/agent-skills/dd-logs) | 1,636 | 日志证据 | ✓ | ✓ | — | — | — | — | — |
| [`dt-obs-logs`](https://skills.sh/dynatrace/dynatrace-for-ai/dt-obs-logs) | 1,790 | 日志证据 | ✓ | ✓ | △ | — | — | — | — |
| [`incident-response`](https://skills.sh/anthropics/knowledge-work-plugins/incident-response) | 5,267 | 事件响应 | △ | ✓ | △ | — | — | △ | — |
| [`ci-cd-and-automation`](https://skills.sh/addyosmani/agent-skills/ci-cd-and-automation) | 24,343 | 流水线编写 | — | — | — | ✓ | △ | — | — |
| [`github-actions-templates`](https://skills.sh/wshobson/agents/github-actions-templates) | 14,701 | 流水线编写 | — | — | — | ✓ | — | — | — |
| [`gitlab-ci-patterns`](https://skills.sh/wshobson/agents/gitlab-ci-patterns) | 11,109 | 流水线编写 | — | — | — | ✓ | — | — | — |
| [`jenkins-pipeline`](https://skills.sh/aj-geddes/useful-ai-prompts/jenkins-pipeline) | 1,165 | 流水线编写 | — | — | — | ✓ | — | — | — |

### 3.1 各环节覆盖率

| 环节 | ✓ | △ | — | 明确覆盖率 |
|---|---:|---:|---:|---:|
| 读取外部失败/事件状态 | 11 | 1 | 20 | 34.4% |
| 收集并呈现证据 | 27 | 1 | 4 | 84.4% |
| 明确根因机制 | 6 | 13 | 13 | 18.8% |
| 修改代码或配置 | 20 | 1 | 11 | 62.5% |
| 明确执行复验 | 17 | 4 | 11 | 53.1% |
| 工作树外流程状态写回 | 3 | 6 | 23 | 9.4% |
| 跨次持久化学习 | 0 | 0 | 32 | 0% |

这些数字表明，样本中覆盖最广的是证据读取，覆盖最少的是工作树外流程状态写回和跨次学习。它们不表示真实运行成功率，也不证明未写入 `SKILL.md` 的底层产品能力不存在。

## 4. 关键样本的源码事实

### 4.1 通用调试 Skill 的安装量明显高于直接 CI 修复 Skill

[`diagnosing-bugs` 源码](https://github.com/mattpocock/skills/blob/main/skills/engineering/diagnosing-bugs/SKILL.md)要求先建立已经实际跑红、能捕捉原始症状的紧反馈回路，再最小化复现、提出 3–5 个可证伪假设、针对性插桩、写回归测试、修复并重跑原始复现；它还要求清理调试代码和脱敏。镜像安装量为 486,581。

[`systematic-debugging` 源码](https://github.com/obra/superpowers/blob/master/skills/systematic-debugging/SKILL.md)用四阶段处理根因调查、模式比较、单变量假设、最小修复与验证；连续三次修复失败后要求停止继续堆补丁并讨论架构。镜像安装量为 238,871。

[`safe-debug`](https://skills.sh/lllllllama/rigorpilot-skills/safe-debug)输出诊断与补丁计划，但明确把代码修改放在批准之后，因此在本矩阵中“修改”和“复验”均记为无公开流程内证据。镜像安装量为 310,501。

这三个通用条目相对两个直接 `fix ci` 条目的安装量约为 **25–299 倍**。这个差异只能说明安装分布，不能推出 CI 修复效果优劣。

### 4.2 两个直接 `fix ci` 条目都围绕 GitHub PR checks

[`openai/skills/gh-fix-ci`](https://github.com/openai/skills/blob/main/skills/.curated/gh-fix-ci/SKILL.md)限定 GitHub Actions，读取 PR checks 和日志，输出失败摘要与修复计划，并要求用户明确批准后才实施；对第三方 CI 只报告详情 URL。它把修复后的复验写为建议而非强制步骤，因此“复验”记为 `△`。

其配套脚本 [`inspect_pr_checks.py`](https://github.com/openai/skills/blob/main/skills/.curated/gh-fix-ci/scripts/inspect_pr_checks.py)在研究快照中仍先取 run 级合并日志，再从尾部匹配通用失败词；只有 run 日志不可用时才回退到 job 日志。公开的 [openai/codex #29062](https://github.com/openai/codex/issues/29062)报告了该控制流可能把其他 job 的尾部输出摘成当前失败片段；2026-08-28 核验时 Issue 为 Open。这里能确认的是“公开缺陷报告存在且当前脚本保留相应控制流”，不能确认每次运行都会误摘。

[`cursor/plugins/fix-ci` 源码](https://github.com/cursor/plugins/blob/main/cursor-team-kit/skills/fix-ci/SKILL.md)在快照中只有 29 行、901 字节，没有配套脚本。流程是 `gh pr checks` → 找第一个 actionable error → 最小修复 → push → 重查 → 重复直到 green；并把 PR checks 定义为整体 CI 状态的事实来源。源码没有规定修改前批准、独立的本地验证门禁或跨次记录。

因此，两者都能从 GitHub PR 状态进入修复流程，但证据链并不相同：OpenAI 条目强调计划和批准；Cursor 条目强调 push 后持续重查。二者公开源码均未定义跨 CI 平台的统一失败模型。

### 4.3 诊断、门禁、修复和写回通常由不同 Skill 分担

- [`diagnose-ci-failures`](https://skills.sh/warpdotdev/common-skills/diagnose-ci-failures)读取 GitHub CI 失败并始终产出计划，但明确不改代码。
- [`dx-devops-test-failures-analyze`](https://skills.sh/forcedotcom/sf-skills/dx-devops-test-failures-analyze)分析 Salesforce DevOps Center 测试失败；创建 WorkItem 是确认后可选动作，不执行代码修复。
- [`fingerprint-ci-gate` 源码](https://github.com/liarjsdev/liarjs-skills/blob/main/skills/fingerprint-ci-gate/SKILL.md)提供确定性的基线 JSON、diff 和退出码，并给出 GitHub Actions、GitLab CI、Docker 和 Playwright 用法；它是检测/验证门禁，不分析根因也不修复。
- [`verification-before-completion`](https://skills.sh/obra/superpowers/verification-before-completion)要求在完成声明前运行新鲜验证命令，但不负责发现根因或生成补丁。
- [`verification-loop` 源码](https://github.com/affaan-m/ECC/blob/main/skills/verification-loop/SKILL.md)依次运行 build、type check、lint、tests/coverage、安全 grep 和 diff review，输出 `READY/NOT READY`；它没有读取外部 CI 状态、生成补丁或写回平台。
- [`github-actions-templates`](https://skills.sh/wshobson/agents/github-actions-templates)、[`gitlab-ci-patterns`](https://skills.sh/wshobson/agents/gitlab-ci-patterns)和 [`jenkins-pipeline`](https://skills.sh/aj-geddes/useful-ai-prompts/jenkins-pipeline)生成或修改流水线配置，但不从一次真实失败开始，也不规定失败后的根因、修复和复验闭环。

样本所呈现的是能力分层，而不是单体闭环：外部状态采集、诊断、代码修改、确定性门禁、平台写回分别出现在不同条目中。

### 4.4 生产与安全样本依赖外部系统提供证据

[`sentry-fix-issues`](https://skills.sh/getsentry/sentry-for-ai/sentry-fix-issues)通过 Sentry MCP 获取 stack trace、事件、tag 分布、trace、附件和 Seer 分析；源码把这些事件数据视为不可信输入，并要求根因与备选假设、代码检查和合成测试。其验证阶段更接近审计清单，未把每个新鲜验证命令规定成必须执行，因此记为 `△`。

[`fix-security-vulnerabilities-with-strix` 源码](https://github.com/usestrix/strix/blob/main/skills/fix-security-vulnerabilities-with-strix/SKILL.md)只接收已有 PoC 的已验证 finding，要求复现、修根因、重跑 Strix/PoC，并运行项目测试。它明确区分扫描完成状态、预算中止和“只对已分析范围为 clean”，因此在“证据—根因—修改—复验”四项均为 `✓`。

Elastic、Datadog 和 Dynatrace 三个日志 Skill 都能从外部服务读取日志证据，但公开流程主要停在搜索、聚合、异常模式或错误率分析，不修改代码，也不承担修复后的验证。

### 4.5 两个可直接观察的数据/内容异常

1. [`dart-fix-runtime-errors` 源码](https://github.com/dart-lang/skills/blob/main/skills/dart-fix-runtime-errors/SKILL.md)的 frontmatter 声称使用活动运行时 stack、LSP 和 hot reload；正文标题与流程实际是静态分析修复，主要执行 `dart analyze`、`dart fix` 和 `dart test`。这是同一上游文件内部的触发契约与正文错位。
2. 安装量并非稳定、单一口径。`verification-loop` 在 2026-08-27 镜像中为 8,644，而 2026-08-28 官方页面抓取显示约 2.3K–2.4K；`fingerprint-ci-gate` 的同日官方页面抓取路径显示过 24.1K 和 29.2K，镜像为 28,582。本报告不推断差异原因，也不选择其中一个作为“真实累计值”；矩阵统一保留镜像快照以保证横向口径一致。

## 5. 基于事实的边界分析

### 5.1 “CI 变绿”与“修复正确”不是同一个被测对象

Cursor `fix-ci` 的源码目标是反复 push 并重查直到 PR checks 全绿。这个信号能证明当前配置下的 checks 通过，但源码没有另外定义原始业务症状、合法对照、语义不变量或失败补丁的反事实验证。因此，能从源码确认的是“它以 checks 变绿作为终止条件”；不能仅凭该源码确认补丁在 checks 未覆盖的语义上正确。

Strix 的边界更窄但证据更直接：它从已有 PoC 的 finding 准入，以同一 PoC/复扫和项目测试复验。它证明的是指定安全 finding 在已分析范围内关闭，不是任意 CI 失败都得到正确修复。

### 5.2 平台覆盖事实

- 直接 CI 修复条目集中于 GitHub Actions/PR checks。
- GitLab、Jenkins 和通用 GitHub Actions 的直接命中主要是流水线模板或配置模式，不是从失败证据到修复复验的自愈流程。
- Azure 候选来自 VS Code 仓库的专用工作流，包含外部状态读取、修改与验证，但不是通用 Azure Pipelines 修复协议。
- 本次 Buildkite 和 CircleCI 直接查询返回 0。该结果只说明公开索引与查询命中，不足以证明平台生态不存在私有 Skill、未索引 Skill 或不同名称的相关实现。

### 5.3 Skill 与 Plugin 是载体边界，不是自愈成熟度等级

OpenAI 官方文档把 [Skill](https://learn.chatgpt.com/docs/build-skills)定义为可复用工作流的编写格式，可包含指令、资源和可选脚本；把 [Plugin](https://developers.openai.com/plugins/concepts/plugins)定义为可发现、安装、分享和发布的包，可包含 Skills、MCP server，或两者，并可由 MCP server 提供结构化结果和可选 UI。[Plugin 构建文档](https://learn.chatgpt.com/docs/build-plugins)也明确说明最小 Plugin 可以只有 manifest 和一个 Skill。

样本中可观察到四种实现形态：

| 形态 | 样本 | 公开能力事实 |
|---|---|---|
| 纯指令 Skill | Cursor `fix-ci` | 29 行 `SKILL.md`；依赖运行环境已有的 `gh` 和 git 能力 |
| Skill + 本地脚本 | OpenAI `gh-fix-ci` | `SKILL.md` 外还包含 Python 日志检查脚本 |
| Skill + 外部 MCP | Sentry `sentry-fix-issues` | 通过 MCP 获取生产事件、trace、附件等外部状态 |
| Skill + 专用 CLI/API | Strix、liarjs gate | CLI/API 负责扫描、结构化结果、退出码或复扫 |

因此，“Skill”描述工作流如何被模型执行；“Plugin”描述如何把 Skill 与可选 MCP/工具能力打包和分发。是否能读取外部 CI、获得凭据、触发重跑、push、评论或保存跨次状态，取决于实际可用工具、连接器、权限和源码流程，而不由名称中的 Skill/Plugin 自动提供。

### 5.4 当前公开证据没有回答的问题

- 安装量不能回答每个 Skill 被真实调用多少次、成功率、误修率、平均修复时长或节省的人力。
- 32 个公开源码没有提供统一的 benchmark 结果，无法据此比较 patch accuracy。
- 多数条目没有记录 `fixed / no_change / blocked / uncertain` 的统一结果模型。
- 没有候选公开描述跨运行 case memory、失败模式统计、策略效果回灌或历史相似案例检索。
- 平台 check 通过只证明已配置检查的结果；若缺少原始症状、回归测试或独立语义证据，公开材料不足以判断修复是否正确。

## 附录 A：可复核源码快照

下表是抓取源码时各上游仓库的 **repo HEAD**，不是每个 `SKILL.md` 的最后修改 commit。

| 仓库 | HEAD（短 SHA） | 时间（UTC） |
|---|---|---|
| [`affaan-m/ECC`](https://github.com/affaan-m/ECC/commit/5eddf1a3ffd311423be2d4ba7d26f7209c91b033) | `5eddf1a3ffd3` | 2026-08-27 17:00 |
| [`cursor/plugins`](https://github.com/cursor/plugins/commit/397c8660da6d3d873a91e18c2ca2f22cac1f0ac1) | `397c8660da6d` | 2026-08-27 23:19 |
| [`getsentry/sentry-for-ai`](https://github.com/getsentry/sentry-for-ai/commit/653c64ebecb07aaf693e372902d1d24134e15e42) | `653c64ebecb0` | 2026-08-27 16:25 |
| [`liarjsdev/liarjs-skills`](https://github.com/liarjsdev/liarjs-skills/commit/4068c79209753c74f895a4a5a6d117fcde8d94d1) | `4068c7920975` | 2026-08-06 06:55 |
| [`mattpocock/skills`](https://github.com/mattpocock/skills/commit/6654f6b60cd9d5be8b54c6fafe44346dabeb3b76) | `6654f6b60cd9` | 2026-08-24 14:19 |
| [`microsoft/vscode`](https://github.com/microsoft/vscode/commit/8512d16a6b20ff9197d24424918ff105703f91f4) | `8512d16a6b20` | 2026-08-28 02:26 |
| [`obra/superpowers`](https://github.com/obra/superpowers/commit/b36e0829c6d0140e93cfef2ca599b1b07d4a7797) | `b36e0829c6d0` | 2026-08-12 16:53 |
| [`openai/skills`](https://github.com/openai/skills/commit/49f948faa9258a0c61caceaf225e179651397431) | `49f948faa925` | 2026-06-24 02:36 |
| [`usestrix/strix`](https://github.com/usestrix/strix/commit/717ffc8f4cbd52614b23cf9504bbfd96b94312b4) | `717ffc8f4cbd` | 2026-08-27 21:10 |
| [`warpdotdev/common-skills`](https://github.com/warpdotdev/common-skills/commit/09a28422e21904eaa721b9c1cebdd8eecc454e9c) | `09a28422e219` | 2026-08-26 19:30 |

## 附录 B：限制

- 公开索引存在 bundle 安装放大、缓存、重建索引和计数变动；安装量不能单独作为质量评分。
- 本报告只编码公开文件明确写出的行为，不对未公开运行时做正面或负面推断。
- `△` 不等于“低质量”，只表示该环节在当前公开流程中不是强制、完整或自动执行。
- 技术机制引用上游仓库或 OpenAI 官方文档；第三方镜像只用于枚举和统一时点统计。
- 本报告不安装、执行或授权任何候选，也没有对其在真实仓库中的修复准确率做实验评估。
