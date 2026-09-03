---
title: "软件问题分析与定位方法论 - Agent Skills 归纳"
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
  - localization
  - methodology
ai_access: true
ai_generated: true
reviewed: false
authority: conversation
source_notes: []
candidate_notes: []
decision_notes: []
experiment_notes: []
content_policy: design
archive_source_path: "/Users/zhujiayi/Documents/Codex/2026-08-28/qi/outputs/problem-analysis-localization-methodology-from-skills-2026-08-29.md"
archive_source_sha256: "b120d10d93aa8d18ef5de381978c5ccd516e2824454fa1e2ecc763da2ef0a8a9"
---

# 软件问题分析与定位方法论

## 基于高安装量 Agent Skills 的事实归纳

**快照日期：2026-08-27 至 2026-08-29**  
**覆盖场景：CI 失败、构建/编译错误、测试失败、编码缺陷、运行时异常、生产事件、依赖/配置问题。**  
**范围声明：本文归纳公开 Skill 中反复出现的问题分析与定位方法，不把自动修复或 CI 自愈作为准入条件，也不提供 Skill/Plugin 产品方案。**

## 1. 结论摘要

从已有的 813 个检索命中、104 个高安装相关候选和 32 个源码深度编码样本中，可以归纳出一条反复出现的公共主线；它把分析和定位放在代码修改之前：

> **失败信号 → 上下文与预期 → 可复现证据 → 分类 → 缩小范围 → 假设与实验 → 故障定位 → 根因状态 → 可选修复 → 独立验证 → 证据收据**

这条主线是对多个 Skill 源码的综合抽象，不是某一上游仓库宣称的行业标准。

量化数据说明为什么需要区分这些阶段：32 个深度样本中，**27/32（84.4%）**明确收集证据，**20/32（62.5%）**修改代码或配置，**17/32（53.1%）**明确执行复验，但只有 **6/32（18.8%）**明确规定根因分析机制。换言之，“有日志”“改了代码”“检查变绿”在公开 Skill 中很常见；“已经用因果证据确认为什么失败”要少得多。

安装量最高的诊断类条目也不集中在专用 CI 修复器：[`azure-diagnostics`](https://www.skills.sh/microsoft/azure-skills/azure-diagnostics)约 548.5K、[`diagnosing-bugs`](https://www.skills.sh/mattpocock/skills/diagnosing-bugs)约 486.6K、[`safe-debug`](https://www.skills.sh/lllllllama/rigorpilot-skills/safe-debug)约 310.5K、[`systematic-debugging`](https://www.skills.sh/obra/superpowers/systematic-debugging)约 238.9K；直接以 `fix ci` 命中的两个条目只有 9,695 和 1,629 安装快照。问题分析方法主要分布在通用调试、平台诊断、日志、测试、静态分析和验证 Skill 中。

## 2. 先区分五个容易混淆的结果

| 结果 | 回答的问题 | 最小证据 | 不能自动推出 |
|---|---|---|---|
| 问题描述 | 实际发生了什么，与预期差在哪 | 原始错误、失败 check、异常行为或事件 | 故障在哪里 |
| 问题分析 | 在什么版本、环境、输入、时间和变更背景下发生 | 上下文、日志、配置、变更与影响范围 | 已找到根因 |
| 故障定位 | 失败集中在哪个层级或边界 | workflow/job/step、服务、文件、函数、数据流边界 | 为什么失败 |
| 根因判断 | 哪个机制导致了原始症状 | 可证伪假设、对照、实验、追踪或已验证 PoC | 修复不存在副作用 |
| 修复验证 | 干预后原始失败是否消失，预期行为是否保留 | 重跑原始信号、回归测试、合法对照和适用的更广门禁 | 所有未覆盖行为都正确 |

这种区分能解释样本中的差异：Salesforce 测试失败分析可以输出文件、方法和行号，但不写修复；Warp `diagnose-ci-failures`输出计划但不改代码；Cursor `fix-ci`可以持续修改并把 PR checks 跑绿，但源码没有独立定义 checks 未覆盖的业务语义正确性。

## 3. 一套跨场景的分析定位主线

### 阶段 A：把“有问题”变成可分析的失败对象

1. **冻结现场**：保存原始错误、失败 check、stack、日志窗口、输入、版本、环境和最近变更，不先用新的尝试覆盖现场。
2. **写清失败契约**：明确 expected、actual、触发条件和影响对象，避免把邻近错误当成原问题。
3. **建立红信号**：优先构造能够捕捉原始症状、已实际运行失败、可重复执行的反馈回路；远端或 UI 问题则保存可回放 artifact、截图/录屏或 trace。

对应源码事实：[`debugging-and-error-recovery`](https://www.skills.sh/addyosmani/agent-skills/debugging-and-error-recovery)要求异常发生后停止继续加功能并保留证据；[`diagnosing-bugs`](https://github.com/mattpocock/skills/blob/main/skills/engineering/diagnosing-bugs/SKILL.md)在进入假设前要求一个已经跑红、能捕捉用户原始症状的反馈命令；[`reproduce-bug-report`](https://www.skills.sh/warpdotdev/common-skills/reproduce-bug-report)针对交互类问题要求实际运行应用并保存视觉证据。

### 阶段 B：缩小搜索空间

4. **先分类再选工具**：区分 CI 配置、构建/编译、测试断言、lint、依赖、运行时、基础设施、资源健康和生产数据等类型。
5. **从外到内逐层定位**：平台/check → workflow/job → step/command → 服务/模块 → 文件/函数/行 → 数据或控制边界。
6. **处理错误级联**：识别第一个可行动错误，区分它与后续派生错误；一次只改变一个主要变量。
7. **最小化或放大**：稳定问题删减输入和组件；低概率问题固定随机数、时间、并发条件并循环触发，提高复现率。

对应源码事实：[`azure-diagnostics`](https://www.skills.sh/microsoft/azure-skills/azure-diagnostics)采用“症状 → Resource Health → logs → metrics → recent changes”的五步路径，并按 Azure 服务路由；[`dx-devops-test-failures-analyze`](https://www.skills.sh/forcedotcom/sf-skills/dx-devops-test-failures-analyze)先分类，再输出 offending file/class、method、line、规则或断言；两个 `fix-ci` 条目都先从 PR checks 下钻到失败 job/日志，其中 [Cursor `fix-ci`](https://github.com/cursor/plugins/blob/main/cursor-team-kit/skills/fix-ci/SKILL.md)明确处理第一个 actionable error。

### 阶段 C：从位置证据进入因果判断

8. **比较工作与失败状态**：比较 good/bad commit、成功/失败环境、相邻模块、旧/新配置或同输入下的不同实现；搜索空间仍大时使用二分或差分。
9. **提出可证伪假设**：列出多个候选原因，为每个原因写出预测，再用最小实验排除或支持；不要让第一个合理解释直接变成结论。
10. **沿数据流或责任边界反向追踪**：从错误值、异常状态或失败输出向上游追到第一次偏离预期的位置，而不是停在最终抛错行。
11. **多源证据交叉验证**：将日志、stack、trace、tag 分布、metrics、resource health、变更记录和代码路径放到同一时间线或因果链中。

对应源码事实：[`diagnosing-bugs`](https://github.com/mattpocock/skills/blob/main/skills/engineering/diagnosing-bugs/SKILL.md)要求 3–5 个排序且可证伪的假设，并提供 bisection、differential loop 和 instrumentation；[`systematic-debugging`](https://github.com/obra/superpowers/blob/main/skills/systematic-debugging/SKILL.md)使用工作/失败模式比较、单一假设实验和反向追踪；[`parallel-debugging`](https://www.skills.sh/wshobson/agents/parallel-debugging)把多个候选根因放进 Analysis of Competing Hypotheses；[`sentry-fix-issues`](https://www.skills.sh/getsentry/sentry-for-ai/sentry-fix-issues)结合 stack、事件、tags、trace、附件和代码；Azure Skill 结合 Resource Health、AppLens、Monitor、KQL、logs、metrics 和 recent changes。

### 阶段 D：输出结论，但不越过证据

12. **把分析、计划和修改分开**：先输出当前证据、定位、根因状态和下一步实验；是否修改代码是后续权限与工作流问题。
13. **声明不确定性和停止条件**：无法复现、证据冲突、第三方日志不可得或连续修复失败时，不把推测写成已确认根因。
14. **验证同一个原始信号**：修改后首先重跑准入时的原始失败，再运行针对性回归、相关子系统检查和适用的更广门禁。
15. **留下证据收据**：记录问题上下文、定位层级、使用的命令/查询、假设及其证据、根因状态、验证结果与剩余未知。

对应源码事实：[`safe-debug`](https://www.skills.sh/lllllllama/rigorpilot-skills/safe-debug)把诊断、补丁计划和批准后的修改分开；[`diagnose-ci-failures`](https://www.skills.sh/warpdotdev/common-skills/diagnose-ci-failures)只输出计划；[`verification-before-completion`](https://www.skills.sh/obra/superpowers/verification-before-completion)要求完成声明基于新鲜验证证据；[`fix-security-vulnerabilities-with-strix`](https://github.com/usestrix/strix/blob/main/skills/fix-security-vulnerabilities-with-strix/SKILL.md)用原 PoC/复扫和项目测试复验，并明确“clean”只覆盖实际分析范围。

## 4. 16 种可复用的方法

下表每种方法至少能在两个独立上游 Skill 中找到对应机制。安装量为 2026-08-27 统一镜像快照，主要用于说明这些方法出现在哪些高安装样本中；不同 Skill 可能来自整包安装，数值不能相加为用户数。

| ID | 方法 | 核心动作 | 直接输出 | 代表 Skill（安装快照） |
|---|---|---|---|---|
| M01 | Stop-the-line 与现场保全 | 发现异常后暂停无关变更，保存原始失败与环境 | 未污染的 failure snapshot | `debugging-and-error-recovery` 26,930；`safe-debug` 310,501 |
| M02 | 失败契约 | 明确 expected、actual、触发条件、影响对象 | 可检验的问题定义 | `diagnosing-bugs` 486,581；`reproduce-bug-report` 21,296 |
| M03 | 反馈回路优先 | 在推理前建立能捕捉原症状的红/绿命令或回放 | 可重复失败信号 | `diagnosing-bugs` 486,581；`systematic-debugging` 238,871；`golang-troubleshooting` 36,305 |
| M04 | 复现放大与最小化 | 提高低概率故障触发率，再删除非必要变量 | 高复现率或最小复现 | `diagnosing-bugs` 486,581；`debugging-strategies` 11,555 |
| M05 | 分类后路由 | 先判定 failure class，再选服务、语言或专用工具 | 类别、优先级和处理路径 | `azure-diagnostics` 549,823；`dx-devops-test-failures-analyze` 4,173；`dart-run-static-analysis` 13,525 |
| M06 | 第一个可行动错误 | 从级联错误中找最早且可修正的失败点，一次处理一个 | primary failure 与 downstream failures | `gh-fix-ci` 9,695；Cursor `fix-ci` 1,629；`fix-errors` 23,015 |
| M07 | 分层定位 | 从平台状态逐层下钻到 command、服务、文件、函数和边界 | 明确的 localization level | `gh-fix-ci` 9,695；Salesforce 分析 4,173；`sentry-fix-issues` 7,299 |
| M08 | 差分与二分 | 比较 working/failing 状态或对提交、配置、数据做 bisect | 首个差异或最小变化区间 | `diagnosing-bugs` 486,581；`systematic-debugging` 238,871；`debugging-strategies` 11,555 |
| M09 | 可证伪假设 | 生成多个候选原因、预测和最小实验，逐项排除 | hypothesis/evidence matrix | `diagnosing-bugs` 486,581；`parallel-debugging` 8,397；`golang-troubleshooting` 36,305 |
| M10 | 数据流/责任边界反向追踪 | 从坏输出向上游寻找第一次偏离预期的位置 | causal boundary 与上游来源 | `systematic-debugging` 238,871；`sentry-fix-issues` 7,299；`golang-troubleshooting` 36,305 |
| M11 | 多源证据三角验证 | 联合 logs、stack、trace、metrics、health 与 recent changes | 时间线、关联模式与冲突证据 | `azure-diagnostics` 549,823；`sentry-fix-issues` 7,299；Elastic/Datadog/Dynatrace 日志 Skills |
| M12 | 历史与意图重建 | 使用 commits、PR、Issue、ADRs、兼容性说明确定预期 | behavior intent 与约束 | `resolving-merge-conflicts` 375,786；`dependency-upgrade` 9,465 |
| M13 | 分析—计划—修改分离 | 把只读诊断、修复计划、批准和执行拆开 | diagnosis/plan/approval 状态 | `safe-debug` 310,501；`diagnose-ci-failures` 22,910；`gh-fix-ci` 9,695 |
| M14 | 确定性工具闭环 | 使用 analyzer/linter/scanner 的结构化输出、退出码和复跑 | 可机器判断的 pass/fail | `dart-run-static-analysis` 13,525；`golang-lint` 36,422；`fingerprint-ci-gate` 28,582 |
| M15 | 验证阶梯 | 原始信号 → 针对性回归 → 相关检查 → 更广门禁 | verification receipt | `verification-before-completion` 191,616；`verification-loop` 8,644；Strix 4,532 |
| M16 | 停止规则与未知状态 | 无复现、证据冲突、覆盖不足或连续失败时停止升级结论 | blocked/uncertain 与缺失证据 | `diagnosing-bugs` 486,581；`systematic-debugging` 238,871；Strix 4,532 |

## 5. “定位到哪里”可以分为五级

“已定位”在不同 Skill 中含义不同。根据源码输出可以拆成五级：

| 级别 | 定位对象 | 例子 | 代表 Skill |
|---|---|---|---|
| L0 信号定位 | 哪个 check、告警或症状失败 | PR check 红、Sentry issue 激增 | `gh-fix-ci`、Sentry |
| L1 执行定位 | 哪个 workflow/job/step/command 失败 | test step、编译命令、部署阶段 | 两个 `fix-ci`、Azure Pipelines |
| L2 代码定位 | 哪个 repo/file/class/method/line 相关 | assertion 对应的类、stack frame | Salesforce、Sentry、编译器/linter Skills |
| L3 边界定位 | 数据或状态在哪个组件边界首次变坏 | API→service、parser→model、producer→consumer | `systematic-debugging`、Go troubleshooting |
| L4 因果定位 | 哪个机制在给定上下文中导致原始症状 | 单一假设实验、PoC、差分或反向追踪成立 | `diagnosing-bugs`、Strix、`systematic-debugging` |

L0–L2 能让问题变得可行动，但不自动等于根因；L3 描述首次偏离预期的边界；L4 才要求因果证据。CI 日志 Skill 经常能达到 L1，结构化测试/静态分析经常达到 L2，系统调试 Skill 才明确追求 L3–L4。

## 6. 不同问题场景对应的主要方法组合

这张表描述公开 Skill 中实际出现的方法组合，不代表唯一或推荐流程。

| 问题场景 | 高频输入 | 主要方法 | 常见分析输出 |
|---|---|---|---|
| CI check 失败 | checks、job logs、external links、最近提交 | M05、M06、M07、M13、M15 | 主失败 job、第一可行动错误、计划、当前 check 状态 |
| 构建/编译/lint | compiler/analyzer 输出、file:line、规则 ID | M05、M06、M14、M15 | 错误分类、源码位置、机械修复结果、复跑结果 |
| 测试失败 | assertion、test name、stack、fixture | M02、M03、M05、M07、M09、M15 | 失败类型、production/test code 归属、根因假设、回归结果 |
| 间歇性/flaky | 低概率信号、时间/并发/随机性 | M03、M04、M08、M09、M16 | 复现率、最小触发条件、相关变量、剩余不确定性 |
| 运行时编码错误 | stack、输入、trace、代码路径 | M03、M07、M09、M10、M15 | 首次坏状态、函数/边界、根因与回归测试 |
| 生产事件 | logs、metrics、traces、resource health、recent changes | M01、M05、M07、M10、M11、M16 | 时间线、影响范围、相关变化、支持/冲突证据 |
| 依赖/配置问题 | lockfile、版本约束、release notes、环境差异 | M05、M08、M12、M14、M15 | 兼容性边界、最小版本差异、验证矩阵 |
| 合并冲突/行为冲突 | commits、PR、Issue、测试、ADRs | M08、M12、M15 | 双方意图、保留的行为、冲突解决验证 |

## 7. 证据强度：从“看到错误”到“支持根因”

下面是基于这些 Skill 的证据要求抽象出的六级尺度，不是公开生态的统一标准：

| 等级 | 已掌握的证据 | 能支持的结论 |
|---|---|---|
| E0 症状 | 用户描述、红灯、错误文本 | 存在待调查问题 |
| E1 现场材料 | 日志、stack、trace、截图、配置与版本 | 可描述上下文并开始分类 |
| E2 可复现信号 | 已实际运行的 red-capable command、PoC 或稳定回放 | 原始问题能够被重复观察 |
| E3 定位证据 | job/step/file/function/boundary 与失败相关 | 已缩小故障位置 |
| E4 假设证据 | 对照、差分、二分、插桩或候选原因被排除 | 某个原因受到支持，替代解释减少 |
| E5 干预与复验 | 针对根因的改变使原始信号转绿，合法行为及回归检查保留 | 在已覆盖范围内支持修复有效 |

`diagnosing-bugs`明确拒绝在缺少 E2 时进入假设；Salesforce 分析主要把 E1 转成 L2/E3 定位；`systematic-debugging`从 E2/E3推进到 E4；Strix 从已有 PoC 准入并以复扫推进到 E5；`verification-before-completion`防止在缺少新鲜 E5 证据时宣称完成。

## 8. 方法论的事实边界

- **安装量不是效果数据**：它不能证明诊断准确率、平均定位时间、根因确认率或修复成功率。
- **日志不是根因**：日志和 stack 通常是位置与时间证据；除非有追踪、对照、假设实验或已验证 PoC，否则只能支持候选解释。
- **file:line 不是根因**：抛错行可能只是发现坏状态的位置，坏状态可能在更早的组件边界产生。
- **CI 变绿不是完整语义证明**：它证明已配置 checks 通过；checks 未覆盖的业务行为仍是未知。
- **并行调查不是默认更强**：Go troubleshooting 的公开源码把单一已知症状保持为顺序调查，仅在大范围未知 bug hunt 时并行；`parallel-debugging`针对多种合理根因和跨模块问题。
- **无法复现不等于不存在**：`diagnosing-bugs` 等代表性 Skill 会转向请求 artifact、环境访问或临时观测，而不是直接生成根因。
- **方法可以在不修改代码时结束**：诊断报告、定位结果、假设矩阵、下一步实验和 `uncertain/blocked` 都是有效输出。

## 9. 可复用的问题分析记录

以下字段是从代表性 Skill 的输出要求归并出的“分析收据”，不是产品数据模型：

| 字段 | 内容 |
|---|---|
| Failure Signal | 原始错误、check、症状或告警 |
| Expected / Actual | 预期行为与实际行为 |
| Context | repo、commit、版本、环境、输入、时间范围、最近变化 |
| Reproduction | 命令、PoC、回放、复现率或无法复现说明 |
| Classification | CI/构建/测试/lint/运行时/依赖/基础设施等 |
| Localization | L0–L4 当前达到的定位层级与具体位置 |
| Hypotheses | 候选原因、预测、支持证据、冲突证据 |
| Root Cause Status | confirmed / supported / unclear / not investigated |
| Action | 无修改、建议实验、修复计划或已执行变更 |
| Verification | 原始信号、回归检查、更广门禁及结果 |
| Remaining Unknowns | 缺失环境、覆盖盲区、冲突证据和后续条件 |

## 10. 数据与来源说明

- 发现池与完整高安装目录见此前的 [问题修复 Skills 分析报告](/Users/zhujiayi/Documents/Codex/2026-08-28/qi/outputs/problem-fixing-skills-analysis-2026-08-28.md)。
- 32 个深度样本及能力覆盖数据见 [CI 自愈相关事实报告](/Users/zhujiayi/Documents/Codex/2026-08-28/qi/outputs/ci-self-healing-skills-evidence-report-2026-08-28.md)和 [机制矩阵](/Users/zhujiayi/Documents/Codex/2026-08-28/qi/outputs/ci-self-healing-skills-mechanism-matrix-2026-08-28.tsv)。
- skills.sh 安装量是安装遥测代理指标，不是运行量或质量评分；本文统一使用 2026-08-27 镜像快照的精确整数，并在正文链接官方页面供当前值复核。
- 行为机制来自上游 `SKILL.md`、配套脚本或 skills.sh 展示的源内容；没有对候选在真实仓库中的诊断准确率做实验。
