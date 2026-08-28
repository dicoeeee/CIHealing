---
title: "CI 自愈问题诊断与修复方法论"
type: project
subtype: architecture
domain: ci-self-healing
status: idea
created: 2026-08-24
updated: 2026-08-24
tags:
  - ci-cd
  - ci-self-healing
  - architecture
  - diagnosis
  - domain-routing
  - case-based-reasoning
ai_access: true
ai_generated: true
reviewed: false
authority: conversation
source_notes:
  - "[[CI 自愈上下文机制 - Project Graph]]"
  - "[[CI 自愈上下文策略 - SELF_HEALING 指令]]"
  - "[[CI 自愈控制机制 - 修复作用域与否决门禁]]"
  - "[[CI 自愈触发机制 - nx fix-ci 末端触发步骤]]"
candidate_notes: []
decision_notes: []
experiment_notes: []
content_policy: design
---

# CI 自愈问题诊断与修复方法论

> [!warning] 设计边界
> 本文是当前讨论形成的自有方法论草稿，不代表已由公开资料证明或已经接受的架构决策。`source_notes` 只连接相关公开机制事实；其中的领域划分、路由方式、数据模型和闭环设计仍需通过 ADR 与 Experiment 逐步确认。

## 目标与非目标

本文回答的核心问题不是“CI 失败应该分成多少类”，而是：

> 面对一个可修复的确定性失败，系统如何为 Agent 提供正确的知识、工具和权限，使其基于证据诊断根因，选择受控修复，并用验证结果关闭闭环。

适用范围：

- 代码、测试、依赖、配置、构建工程和发布工程中的确定性失败；
- 需要公共软件工程知识或企业领域专有知识才能修复的问题；
- 需要生成候选修改、执行受控动作并验证结果的 Agentic CI 场景。

非目标：

- 不在本文内解决网络抖动、资源不足、Runner 故障和 Flaky 等重试或基础设施恢复问题；
- 不穷举全部 Error Type，也不建立覆盖所有语言和流水线阶段的强层级分类树；
- 不把任务级复验等同于完整 CI、Required Checks、业务正确性或生产安全；
- 不因 Agent 生成了 Patch 就将失败标记为已自愈。

上游应先确认该 Failure 需要进入“诊断与修复”路径，而不是 Retry、Reschedule、环境恢复或人工事故处置路径。

## 核心结论

CI 自愈不应从完整错误分类树开始，也不应把所有失败直接交给通用模型自由尝试。更合理的原则是：

> 通用问题以 Evidence-driven Diagnosis 为主；企业领域专有问题先做 Domain Identification 和知识路由；两条路径最终统一进入 Diagnosis → Repair → Verification → Learning 闭环。

“分类”不是答案，而是为了完成三种路由：

| 路由 | 回答的问题 | 主要输出 |
| --- | --- | --- |
| Knowledge Routing | Agent 应该知道什么 | 领域规则、Schema、案例和版本化上下文 |
| Tool Routing | Agent 应该使用什么 | 诊断、查询、修改和验证工具集合 |
| Policy Routing | Agent 被允许做什么 | 风险等级、动作权限、人工门禁和失败退出 |

一句话概括：

> CI 自愈不是“先把错误分好类，再查对应答案”，而是从 Failure 上下文识别知识域，把合适的知识、工具和策略交给 Agent，由 Agent 基于证据完成根因诊断，再通过受控修复和分层验证形成闭环。

## Failure Type、Root Cause 与 Repair Strategy 分离

这三个概念分别回答“看到了什么”“为什么发生”“准备怎么改”，不能混为同一个分类维度。

| 概念 | 回答的问题 | 示例 | 产生阶段 |
| --- | --- | --- | --- |
| Failure Type | 在哪里、以什么现象失败 | `test / assertion_failure` | Failure Understanding |
| Root Cause | 哪个事实链解释了失败 | API route 被错误修改 | Diagnosis |
| Repair Strategy | 改哪个对象、采用什么变更模式 | 恢复 route 或迁移调用方 | Repair Planning |
| Verification Strategy | 用什么证据证明修复成立 | affected tests + API contract tests | Verification Planning |

它们不是一一对应关系：

```text
Test Failure
  ├─ Root Cause: production logic defect
  ├─ Root Cause: stale test expectation
  ├─ Root Cause: incorrect mock
  └─ Root Cause: configuration mismatch

同一个 Root Cause
  ├─ Repair A: update implementation
  ├─ Repair B: update call site
  └─ Repair C: restore contract
```

因此不能从 `stage = test` 直接推导“应该修改测试”，也不能从 `symptom = type_mismatch` 直接推导“应该增加类型转换”。Root Cause 必须由证据链建立，Repair Strategy 还要结合业务意图、影响范围、策略和验证成本选择。

## 多维 Failure Model

CI Failure 更适合表达为一组正交维度，而不是树上的单一路径。

| 维度 | 作用 | 示例 |
| --- | --- | --- |
| Identity | 绑定运行、仓库和代码状态 | repository、run、attempt、head SHA |
| Stage | 决定证据收集入口 | Compile、Test、Lint、Package |
| Technology | 决定解析器和工程工具 | Java、Go、Gradle、CMake |
| Symptom | 描述可观察现象 | Symbol Not Found、Assertion Failure |
| Domain | 选择知识空间 | Build Engineering / Build Configuration |
| Context | 限定产品、场景和版本 | product、build system、version、scene |
| Root Cause | 记录因果诊断 | API Change、Parameter Constraint Violation |
| Repair | 记录候选与选定策略 | Update Call Site、Update Baseline |
| Policy | 约束允许动作 | suggest only、review、auto execute |
| Verification | 定义成功证据 | compile module、affected tests、outer CI |
| Result | 记录闭环状态 | healed、not healed、inconclusive |

Language 和 Stage 很重要，但它们主要用于上下文与工具路由：

```text
Language → compiler diagnostics / AST / LSP / build tool
Stage    → log / stack / assertion / artifact / task evidence
Domain   → knowledge space / domain tools / policy namespace
Root Cause → repair direction
```

可以把这一关系压缩为：

> Stage 决定从哪里开始观察，Technology 决定如何观察，Domain 决定在哪套规则世界里推理，Root Cause 决定修复方向。

## 两个知识世界

### 通用软件工程问题

这类问题可以主要依靠公共软件工程先验、仓库上下文和工程工具完成诊断，例如：

```text
Java type mismatch
C++ undefined reference
Python import error
Go interface mismatch
Maven dependency conflict
pytest assertion failure
```

推荐链路：

```text
Failure
  → Evidence Extraction
  → Root Cause Hypotheses
  → Repair Planning
  → Verification
```

这里仍会形成 `language`、`stage`、`symptom` 和 `root_cause` 等结构化状态，但不要求 Agent 先走完 `Language → Error Type → Subtype` 决策树。分类是诊断状态和审计记录，不是阻塞修复的仪式。

### 企业领域专有问题

这类问题的正确诊断依赖企业私域语义，例如：

```text
内部构建参数
产品配置与 Build Variant
内部配置 DSL
版本基线与 Upstream
分支组合规则
内部代码生成系统
发布约束与平台规则
```

通用模型可能理解“两个参数不一致”，但不知道参数来源、继承关系、允许组合、业务意图、修改权限和验证路径。推荐链路是：

```text
Failure
  → Domain Identification
  → Domain Knowledge Pack
  → Domain Diagnosis
  → Repair Planning
  → Verification
```

因此，领域分类的本质不是给错误命名，而是选择正确的知识空间。

## Domain Identification

Domain Router 应判断的是“该 Failure 是否需要进入某个企业知识域”，而不是尽可能生成最细 Error Type。

建议输入包括：

- CI provider、pipeline、job、task 和 stage 元数据；
- repository、module、owner、changed files 和依赖图；
- error code、日志实体、配置键、内部 DSL 和工具标识；
- product、variant、build system、version、scene 和 baseline；
- 当前领域包的适用条件与版本范围。

路由结果至少包含：

```yaml
domain_identification:
  route: private_domain
  primary: build_engineering
  secondary: build_configuration
  evidence:
    - task belongs to internal build pipeline
    - failure contains governed config keys
  confidence: 0.91
  alternatives:
    - dependency_management
  knowledge_pack: build_configuration@v3
```

分类粒度到“足以唯一选择知识包、工具集和策略命名空间”即可停止。例如 `Build Engineering / Build Configuration` 已足够路由，参数缺失、参数冲突或继承错误应留给领域诊断确认。

```text
CI Domain
├── Generic Software Engineering
├── Build Engineering
│   ├── Build Configuration
│   ├── Build Graph
│   ├── Toolchain
│   ├── Dependency
│   ├── Code Generation
│   └── Artifact
├── Test Engineering
│   ├── Test Configuration
│   ├── Test Framework
│   ├── Test Data
│   └── Test Contract
└── Release Engineering
    ├── Version
    ├── Packaging
    ├── Deployment Configuration
    └── Release Policy
```

当路由证据冲突、领域包缺失或置信度不足时，系统应输出 `unknown / ambiguous / pack_unavailable`，停止自动执行，并保留诊断所需的缺口；不能通过扩大检索范围或让模型猜测来伪造领域知识。

## Knowledge、Tool 与 Policy Routing

Domain Identification 完成后，Router 同时建立三类受限上下文。

### Knowledge Routing

先用结构化元数据限定范围，再进行语义检索：

```text
domain + product + build_system + version + scene
  → compatible knowledge pack
  → metadata filtering
  → semantic retrieval
```

这可以降低名称相同、版本不同、产品不同或场景冲突的规则混入当前诊断。检索结果必须保留来源、版本、适用范围和冲突信息。

### Tool Routing

只暴露当前诊断需要的工具，而不是把所有工具交给 Agent。例如：

```text
Build Configuration
├── query_config
├── trace_config_origin
├── validate_config
├── query_baseline
├── compare_configuration
└── propose_configuration_change

Dependency
├── dependency_graph
├── query_version
├── find_conflict
└── propose_dependency_update
```

工具应区分只读诊断、候选生成、受控修改和验证四类 capability。加载某个工具不代表 Agent 已获得执行该工具中写操作的权限。

### Policy Routing

路由阶段加载适用的风险与授权规则，真正的策略决策则在候选修复形成后，基于实际变更范围再次执行。

```text
Build parameter change  → low risk    → validate + optional auto execute
Build script change     → medium risk → patch + review
Release policy change   → high risk   → diagnosis and proposal only
```

策略至少约束：

- 是否允许生成候选；
- 允许读取和修改的对象、路径与配置项；
- 是否需要人工确认；
- 必须执行的验证计划；
- 是否允许提交、推送或触发外层 CI；
- HEAD、候选、策略或权限漂移时如何失效。

详细控制面设计见 [[CI 自愈策略控制平面架构]]。

## Domain Knowledge Pack

Domain Knowledge Pack 不是一组无边界文档，而是一个可版本化、可校验、可授权的领域能力包。

| 内容 | 作用 |
| --- | --- |
| Knowledge | 领域概念、实体关系和术语 |
| Rules | 依赖、互斥、继承、默认值和组合约束 |
| Schema | 参数类型、枚举、来源、版本和作用域 |
| Examples | 正例、反例和边界场景 |
| Case Library Ref | 经治理案例库的版本、索引和准入状态；案例本身保持独立生命周期 |
| Tools | 查询、比较、修改和验证接口 |
| Permissions | 可读、可提议、可修改和必须审批的边界 |
| Validators | Schema、约束、dry-run、任务和下游验证器 |
| Provenance | 来源、所有者、版本、适用产品和更新时间 |

最小契约示例：

```yaml
domain_pack:
  id: build_configuration
  version: v3
  applies_to:
    product: product_a
    build_system: cie
    scene:
      - feature_build
  entities:
    - product_variant
    - target_scene
    - dependency_baseline
  rules_ref: rules/build-configuration-v3
  tools:
    read:
      - query_config
      - trace_config_origin
    propose:
      - propose_configuration_change
  validators:
    - validate_schema
    - validate_constraints
    - dependency_resolution_dry_run
  case_library_ref: cases/build-configuration@v2
  policy_ref: policy/build-configuration-v3
```

领域包缺少适用范围、版本、来源或 Validator 时，不能仅凭语义相似度用于自动修改。

## Case Library：经验记忆层

Case-by-case 积累不是补充材料，而是 Agent 从“能够推理”走向“能够稳定复用已验证经验”的经验记忆层。Domain Knowledge Pack 解决“当前领域一般应满足什么规则”，Case Library 解决“相似问题过去如何被诊断、修复和验证”。

一个可复用 Case 必须是完整的 Healing Experience，而不是“错误日志 + 最终答案”：

```text
Failure Context
  + Diagnosis Process
  + Root Cause
  + Repair Strategy
  + Verification
  + Outcome
```

两类资产可以由同一个 Domain Router 共同加载，但权威层级和生命周期不同：

| 资产 | 回答的问题 | 主要内容 | 使用边界 |
| --- | --- | --- | --- |
| Domain Knowledge Pack | 当前条件下一般应当满足什么 | Rules、Schema、Constraints、Tools、Policy | 用当前适用版本判断约束和权限 |
| Case Library | 相似上下文中曾经发生什么、怎样处理 | Context、Evidence、Root Cause、Repair、Verification、Outcome | 只作为历史先验，必须在当前上下文重新验证 |

Case 不能覆盖当前规则、授权或证据。即使两个 Failure 的日志完全相同，只要产品、版本、HEAD、依赖图、策略或业务意图不同，历史 Repair 都可能不再适用。

### 在三个阶段使用 Case

| 使用位置 | Case 提供的价值 | Agent 必须完成的工作 |
| --- | --- | --- |
| Diagnosis 之前 | 用相似 Case 排序 Root Cause 假设，减少从零搜索 | 对照当前 Evidence，逐项确认或反驳假设 |
| Repair Planning 期间 | 比较成功、失败和被人工改判的策略及其适用条件 | 记录当前上下文差异，生成适配后的 Repair，而不是重放旧 Patch |
| Verification Planning 期间 | 参考历史上能暴露回归的验证层级、命令和成本 | 以当前影响范围和 Policy 为下限，不得用历史 Case 降级必需门禁 |

因此，Case-based Reasoning 的基本模式是：

```text
Retrieve → Compare → Adapt → Verify
```

而不是：

```text
Retrieve → Replay
```

### 多维索引与检索

Case Library 不应按 Error Type 建立另一棵强层级树。索引至少覆盖：

- Domain、Stage、Technology 和 Symptom；
- Product、Version、Scene、Environment 和依赖状态；
- Root Cause、Repair Strategy、Risk 和 Policy Decision；
- Verification Plan、Outcome、人工改判和回滚记录；
- Knowledge Pack 版本、Case 状态、时间和失效条件。

检索先以 Domain、产品、版本和适用范围等硬条件缩小候选，再比较 Failure Context 与 Evidence 特征，最后结合验证完整性、风险、时效性和历史结果排序。失败 Case、误修 Case 和人工否决 Case 也必须可检索，避免只学习成功样本。

```yaml
case_index:
  domain: build_configuration
  symptom: configuration_validation_failure
  context:
    product: product_a
    version: v3
    scene: feature_build
  root_cause: parameter_constraint_violation
  repair_strategy: change_upstream
  risk: medium
  verification:
    - constraint_validation
    - dependency_resolution_dry_run
  outcome: verified
  status: admitted
```

语义相似度只用于召回和排序，不能证明 Root Cause，也不能授予执行权限。检索结果互相冲突、缺少完整 Verification Receipt、适用范围不匹配或已过期时，应降级为普通候选证据。

### 准入与能力演进

每次尝试结束后先形成 Case Draft；只有声明验证完成，并经过适用的人审或外层 CI 裁决，才能进入可复用 Case Library。建议状态机为：

```text
draft → reviewed → admitted → stale / retired
```

Case-by-case 能力可以分阶段演进：

| 阶段 | Case 的产生与使用 | 自动化边界 |
| --- | --- | --- |
| 1. 人工积累 | 人工整理完整 Healing Experience | 仅供检索和分析 |
| 2. 检索辅助 | Agent 检索 Case 并提出诊断与修复建议 | 人工确认后执行 |
| 3. 闭环沉淀 | Agent 自动生成 Case Draft，经验证和审核后准入 | 自动沉淀草稿，不自动提升权威性 |
| 4. 受限自治 | 对高相似、低风险、重复验证成功且规则未漂移的模式复用经验 | 仍需通过当前 Policy Check 和 Verification |

Case 的重复成功可以提高策略优先级和自动化候选等级，但不能跳过 Domain Knowledge、Policy 或 Verification。成熟资产的推荐组合是：Domain Knowledge Pack 提供规则，Case Library 提供经验，Repair Primitives 提供受控动作，Verification Receipt 提供可信结果。

## Evidence-driven Diagnosis

Diagnosis 围绕证据链展开，而不是围绕标签查答案。

通用证据可以包括：

- 原始日志、Compiler Diagnostics、Stack、Assertion 和失败对象；
- 相关代码、测试、Fixture、Mock、配置和最近 Diff；
- Project Graph、Task Graph、依赖版本和执行元数据；
- 失败是否能在当前 HEAD 和声明环境中稳定复现。

领域证据还应包括：

- 配置当前值、来源、继承链和生效范围；
- 适用于当前产品、版本和场景的规则；
- 业务意图、变更历史和允许动作；
- 支持与反驳每个 Root Cause 假设的证据。

领域构建参数示例：

```text
Symptom
  Invalid upstream configuration
      ↓
Current State
  feature_mode = independent
  upstream = weekly_xxx
      ↓
Applicable Rule
  independent requires daily baseline
      ↓
Recent Change
  feature_mode changed from integrated to independent
      ↓
Root Cause
  upstream was not migrated with feature_mode
      ↓
Candidate Repairs
  A. upstream → daily_xxx
  B. feature_mode → integrated
      ↓
Intent Constraint
  current task is independent feature validation
      ↓
Selected Strategy
  change upstream
```

结构化诊断应同时保留结论和可复核的推理材料：

```yaml
diagnosis:
  domain: build_configuration
  root_cause:
    type: parameter_constraint_violation
    statement: upstream was not migrated with feature_mode
  evidence:
    supporting:
      - current feature_mode is independent
      - current upstream is weekly_xxx
      - applicable v3 rule requires a daily baseline
    refuting: []
  unknowns: []
  confidence: 0.93
  candidate_repairs:
    - change_upstream
    - revert_feature_mode
```

Root Cause 类型是 Diagnosis 的输出，不是 Diagnosis 的起点。若关键证据缺失、规则冲突或存在无法排除的替代解释，结果应为 `inconclusive`，不能用高置信标签掩盖缺口。

## Repair Strategy

真正值得持续沉淀的是下面这组关系，而不只是 Error Taxonomy：

```text
Failure Pattern
  + Domain Knowledge
  + Root Cause Evidence
  + Repair Strategy
  + Verification Strategy
```

通用软件修复可以复用有限的 Repair Primitives：

```text
Add / Delete / Replace / Rename / Cast
Update Signature / Update Call Site
Add Dependency / Update Dependency
Update Config / Update Assertion / Update Mock
```

构建配置领域可以沉淀更高层的策略：

```text
补充缺失参数
修正非法值
修复参数组合
修复继承关系
更新 baseline
恢复场景默认值
```

每次 Repair Planning 至少输出：

- 候选策略及各自支持证据；
- 选定策略与未选择其他策略的原因；
- 目标文件、配置项、依赖和影响范围；
- 风险、可逆性、回滚方式和预计验证成本；
- 执行前必须满足的 Policy Decision；
- 与候选变更绑定的 Verification Plan。

策略选择顺序应是：先满足 Root Cause 与业务意图，再比较变更范围、可逆性、策略权限和验证充分性。不能因为某个 Patch 最容易生成，就把它当作正确修复。

## Verification

自愈结束条件不是“修改完成”，而是“有证据证明候选消除了目标问题，并且没有引入当前验证范围能够发现的不可接受回归”。

建议使用分层验证：

| 层级 | 目的 | 示例 |
| --- | --- | --- |
| V0 Candidate Guard | 校验候选本身与策略边界 | Schema、diff、path、HEAD、policy |
| V1 Root Failure | 证明原失败不再出现 | compile module、config validator |
| V2 Affected Scope | 检查直接影响范围 | affected tests、dependency dry-run |
| V3 Broader Scope | 检查下游或更广回归 | downstream tests、integration suite |
| V4 Outer CI | 重新交给宿主门禁判定 | complete CI、Required Checks |

不同修复必须拥有对应计划：

```text
Compile Error
  → compile current module
  → affected tests
  → downstream tests when required

Build Parameter Change
  → schema validation
  → constraint validation
  → dependency resolution dry-run
  → rerun failed task
```

Verification Receipt 应绑定：

```text
repository + head_sha + candidate_fingerprint
+ knowledge_pack_version + policy_hash
+ environment + command + result + timestamp
```

验证失败后返回 Re-Diagnosis，并保留新证据；验证不完整则返回 `inconclusive`；HEAD、候选、知识包或策略变化会使旧 Receipt 失效。任务级微循环的成功不能覆盖原 CI verdict，外层门禁边界见 [[末端触发与修复编排架构]]。

## Learning 闭环

Learning 的目标是改善后续路由、诊断、修复和验证，而不是让未经确认的输出自动进入 Domain Knowledge Pack 或 Case Library。每次执行先产生 Case Draft，再根据验证、审核与失效规则决定是否准入。

每个 Healing Case 至少记录：

- Failure identity、原始证据引用和代码状态；
- Domain Router 的输入、候选、证据和最终选择；
- Knowledge Pack、工具和策略版本；
- Root Cause 假设、支持/反驳证据与置信度；
- 候选 Repair、选定策略、实际变更和审批记录；
- 各层 Verification Receipt、外层 CI 结果和最终状态；
- 人工改判、回滚、误修和知识缺口。

闭环可以产生：

| 学习产物 | 用途 |
| --- | --- |
| Case Draft | 保留本次完整过程，等待验证、审核、去重和准入 |
| Confirmed Case | 相似问题检索和诊断参考 |
| Negative Case | 记录失败 Patch、人工否决、回滚和不适用边界 |
| Eval Case | 评估 Domain Routing、Root Cause 和 Patch 质量 |
| Strategy Metrics | 统计成功率、误修率、验证成本和人工介入 |
| Knowledge Gap | 触发领域知识、Schema 或案例补充候选 |
| Tool Gap | 触发查询、修改或 Validator 能力补充 |
| Policy Signal | 触发风险阈值、权限或门禁调整候选 |

只有通过声明验证并经过适用的人审或外层裁决的 Case，才能成为已确认学习材料。失败 Case 可以在标明结果和边界后作为 Negative Case 准入，但不能被检索器误当作成功 Repair。AI 生成的知识、规则、策略和 Validator 变更先进入候选或草稿，保留来源与版本，不能自动覆盖当前 Domain Knowledge Pack。

## Healing Case 数据模型

建议将一次自愈记录为完整 Case，而不是只保存 `error_type`：

```yaml
healing_case:
  identity:
    repository: repo_a
    ci_run: run_123
    attempt: 1
    head_sha: abc123

  failure:
    stage: build
    language: java
    technology:
      build_system: cie
    symptom:
      type: configuration_validation_failure
      message: invalid upstream configuration
      location: build.yaml

  domain:
    route: private_domain
    primary: build_engineering
    secondary: build_configuration
    confidence: 0.91
    evidence:
      - governed config keys detected
    knowledge_pack: build_configuration@v3

  context:
    product: product_a
    version: v3
    scene: feature_build

  case_memory:
    query:
      hard_filters:
        domain: build_configuration
        product: product_a
        version: v3
      evidence_features:
        - configuration_validation_failure
        - feature_mode_changed
    precedents:
      - case_id: hc_042
        status: admitted
        similarity: 0.89
        differences:
          - dependency baseline differs
    use: hypothesis_prior

  diagnosis:
    root_cause: parameter_constraint_violation
    statement: upstream was not migrated with feature_mode
    evidence:
      - current state violates the applicable v3 rule
      - recent diff changed feature_mode only
    confidence: 0.93

  repair:
    candidates:
      - change_upstream
      - revert_feature_mode
    selected: change_upstream
    rationale: preserve independent feature validation intent
    changes:
      - file: build.yaml
        field: upstream
        value: daily_xxx
    candidate_fingerprint: sha256:...

  policy:
    decision: require_review
    risk: medium
    policy_hash: sha256:...
    allowed_actions:
      - propose_patch
      - run_validators

  verification:
    plan:
      - schema_validation
      - constraint_validation
      - dependency_resolution_dry_run
      - rerun_failed_task
    receipts: []
    outer_ci: pending

  result:
    status: awaiting_verification
    healed: false

  learning:
    case_status: draft
    reusable_index:
      root_cause: parameter_constraint_violation
      repair_strategy: change_upstream
      risk: medium
      outcome: pending
    admission:
      verification_complete: false
      human_reviewed: false
      admitted: false
    gaps: []
```

该模型可以同时服务执行状态、审计、RAG、历史 Case、Eval、策略统计和权限治理，但各消费者应只读取自己需要的字段，不能把统计标签反向当作 Root Cause 证据。

## 最终主链路

完整主链路是：

```text
CI Failure
    ↓
Failure Understanding
Log / Stack / Task / Code / Diff
    ↓
Context Extraction
Stage / Technology / Product / Version / Scene
    ↓
Domain Identification
    ↓
┌───────────────────────────────┬───────────────────────────────┐
│ Generic Software Engineering  │ Enterprise Private Domain     │
│ public prior + repo context   │ versioned Domain Knowledge Pack │
└───────────────────────────────┴───────────────────────────────┘
    ↓
Knowledge / Tool / Policy Routing
    ↓
Domain Knowledge + Case Retrieval
Rules / Schema / Constraints + Similar Cases / Past Outcomes / Verification Evidence
    ↓
Evidence-driven Diagnosis
Root Cause + Evidence + Unknowns + Confidence
    ↓
Repair Planning
Candidate Strategies + Selected Strategy + Verification Plan
    ↓
Policy Check
Risk + Permission + Scope + Approval
    ↓
Execution
    ↓
Verification
V0 Guard → V1 Root Failure → V2 Affected → V3 Broader → V4 Outer CI
    ↓
┌──────────────────────┬─────────────────────────┐
│ Verified             │ Failed / Inconclusive   │
│ close repair loop    │ re-diagnose or stop     │
└──────────────────────┴─────────────────────────┘
    ↓
Learning
Case Draft / Confirmed Case / Negative Case / Eval / Strategy / Knowledge Gap / Policy Signal
```

压缩后的逻辑是：

```text
Context → Domain → Knowledge + Cases → Diagnosis
→ Strategy → Policy → Action → Verification → Learning
```

## 失败退出与降级

| 条件 | 结果 | 禁止行为 |
| --- | --- | --- |
| Domain 无法识别或多个领域冲突 | `ambiguous_domain`，保留证据并停止自动执行 | 全库盲检索后猜测 |
| Knowledge Pack 缺失、不适用或过期 | `pack_unavailable` | 用相似旧规则替代当前规则 |
| Case 仅语义相似、上下文不匹配或结果冲突 | `case_not_applicable`，降级为候选证据 | 复制旧 Patch 或把历史结果当作当前 Root Cause 证明 |
| 关键证据缺失或 Root Cause 不唯一 | `inconclusive_diagnosis` | 用置信度掩盖未知项 |
| Policy deny 或需要审批 | `denied / awaiting_review` | 绕过门禁执行写操作 |
| HEAD、候选、策略或知识包漂移 | `candidate_expired` | 复用旧授权或 Receipt |
| 验证失败 | `verification_failed`，把结果作为新证据重诊断 | 宣布已自愈 |
| 验证未完成 | `verification_incomplete` | 把局部通过等同于完整成功 |
| 达到尝试、时间或成本上限 | `repair_budget_exhausted` | 无限循环尝试 Patch |

Unknown 不是一种需要被强行归类的错误类型，而是一个合法的控制状态。

## 与现有专题架构的关系

| 文档 | 在本方法论中的位置 |
| --- | --- |
| [[CI 自愈上下文机制 - Project Graph]] | Failure Understanding 与结构化上下文的一类公开机制事实 |
| [[CI 自愈上下文策略 - SELF_HEALING 指令]] | 仓库语义上下文的一类公开机制事实，不等同于机器强制策略 |
| [[末端触发与修复编排架构]] | Failure 聚合、触发、候选编排和外层 CI 边界 |
| [[CI 自愈策略控制平面架构]] | Policy Routing、候选作用域、验证门禁与写回授权 |

本文负责上位诊断与修复方法；现有两篇 Architecture 负责主链路中的具体控制与编排，不在本文重复定义其组件。

## 待决 ADR

- Domain Router 采用规则、结构化元数据、模型判断还是组合方式；
- `generic` 与 `private_domain` 的判定条件和置信度门槛；
- Domain Knowledge Pack 的版本、所有权、发布和回滚协议；
- Root Cause、Repair Strategy 与 Verification Strategy 的枚举和扩展机制；
- 哪些 Verification 层级可以授权自动写回，哪些必须等待外层 CI；
- Case Library 的 Schema、硬过滤、相似度排序、去重和负样本处理机制；
- Learning Case 的人工确认、准入、保留期、失效和删除机制；
- Case 驱动的自动执行需要满足哪些重复成功、风险和上下文一致性门槛。

## 待验证问题

- 多维模型能否覆盖真实高频 Failure，而不退化为新的层级 Taxonomy；
- Domain Router 在跨域 Failure、内部缩写和版本漂移场景下的准确率与拒答率；
- 元数据过滤是否能降低跨产品、跨版本知识误检；
- Repair Strategy 复用是否比 Error Type 扩张更稳定；
- Case Retrieval 的召回率、适用性精度和上下文差异识别能力；
- 相似 Case 能否在不提高误修率的前提下降低诊断与验证成本；
- 分层 Verification 对误修率、平均修复时间和 CI 资源消耗的影响；
- Learning 准入规则能否阻止失败 Patch、错误 Root Cause 和陈旧规则污染知识包。

## 变更记录

- 2026-08-24：补充 Case Library 经验记忆层、三阶段使用方式、多维索引、准入治理与受限自治路径。
- 2026-08-24：根据“是否先分类再修复”与“企业领域专有知识如何路由”的讨论建立首版方法论草稿。
