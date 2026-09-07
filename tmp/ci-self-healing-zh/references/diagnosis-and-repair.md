# 诊断与修复

使用证据决定是进行实验、创建本地候选、发布远端候选还是停止。不要用数值置信度阈值掩盖缺失或冲突的证据。

## 准入正确的失败

本 Skill 处理确定性的代码、测试、依赖、配置、构建和发布工程失败；这些失败必须能够形成候选修改并进行有意义的验证。

将网络中断、资源耗尽、Runner 丢失、临时 Provider 失败和已知 Flaky 行为路由到重试或基础设施恢复。如果还无法判断是否确定性，先收集具有区分度的证据；当路由决策本身尚未解决时，返回 `inconclusive`，而不是 `not_eligible`。

## 建立失败合同

在诊断根因前，记录：

- 原始信号及其观察位置；
- 预期行为与实际行为；
- 仓库、基线、环境、输入和最近变更；
- 可用时，能够重现失败的命令、回放、PoC 或远端检查；
- 受影响对象和当前影响范围。

在新 Attempt 改变日志或工作区前，先保留原始失败。

## 定位但不过度声称因果

将这些层级作为报告词汇，而不是强制检查清单：

| 层级 | 含义 |
| --- | --- |
| L0 Signal | 已识别失败的检查、告警或症状 |
| L1 Execution | 已识别失败的工作流、作业、步骤、任务或命令 |
| L2 Code | 已识别相关仓库、文件、类、函数或代码行 |
| L3 Boundary | 已识别第一个发生偏离的数据、状态或组件边界 |
| L4 Cause | 某个机制得到可证伪实验、对照、追踪或等价证据的支持 |

日志和 `file:line` 通常只能支持 L1 或 L2，不能自动建立 L4。

选择能够区分当前假设的方法：第一个可行动错误、最小复现、复现放大、成功与失败对照、diff 或 bisect、可证伪假设、聚焦插桩、上游数据流追踪、多源证据三角验证，以及从测试、历史、规范或 ADR 重建意图。不要机械地运行每一种方法。

## 路由通用与企业知识

当仓库上下文、公开软件工程知识和标准工具已经足够时，使用通用路径。

只有当正确诊断依赖内部概念、当前业务状态、受治理的组合、私有基线或专项验证器时，才使用企业领域。兼容的 Domain Knowledge Pack 必须标识版本、适用范围、来源、规则、状态查询、工具和验证器。

如果领域存在歧义、兼容知识包缺失或过期、无法查询当前状态、规则冲突，或所需验证器不可用，记录缺口并返回 `inconclusive`。不要为了出现语义相似的规则而扩大检索范围。

历史 Case 只提供假设、负向约束和验证线索。使用 `Retrieve -> Compare -> Adapt -> Verify`，绝不要将旧 Patch 重放为当前修复。

## 记录诊断评估

```yaml
diagnosis:
  classification: <失败类别和领域路由>
  localization:
    level: L0 | L1 | L2 | L3 | L4
    target: <观察到的位置或边界>
  hypotheses:
    - statement: <候选机制>
      supporting_evidence: []
      conflicting_evidence: []
      predicted_signal: <什么结果会支持或反驳它>
  root_cause:
    status: confirmed | supported | unclear | not_investigated
    statement: optional
  expected_behavior: <需要保留的行为或合同>
  repair_constraints:
    must_preserve: []
    allowed_scope: []
    prohibited_directions: []
  unknowns: []
```

`confirmed` 要求当前上下文中存在直接因果证据。`supported` 表示现有证据相较于重要替代解释更支持该机制，并且验证能够有意义地证伪该候选。不要仅因为建议的 Patch 看起来合理，就将 `supported` 升级。

## 决定修复准入

```yaml
repair_admission:
  decision: experiment_only | allow_local_candidate | allow_remote_candidate | stop
  evidence_refs: []
  unknowns: []
  required_verification: []
  rationale: <为什么现在有理由执行此动作>
```

使用以下边界：

- `experiment_only`：在隔离工作区中执行可逆的诊断修改或添加临时插桩。不要将其作为修复候选发布。
- `allow_local_candidate`：失败和基线已绑定，定位足以行动，根因状态为 `confirmed` 或 `supported`，或者确定性工具直接指出机械性修正；预期行为已知，重要替代解释已被考虑，并且存在可执行的验证。
- `allow_remote_candidate`：满足 `allow_local_candidate` 条件，完整候选通过必要的本地守卫，实际差异通过范围和策略检查，并且远端验证可以绑定准确的候选 ref。
- `stop`：证据、意图、策略、领域知识、执行环境或验证不足以继续修改。

诊断实验可以先于修复候选，但除非它属于有意的修复，否则必须移除。绝不要意外 commit 或 push 插桩。

## 构建候选

每个候选都必须是相对于固定基线的完整变更，并包含：

- 所支持的根因及其对应证据；
- 目标行为和需要保留的约束；
- 受影响的文件、配置、依赖和下游范围；
- 风险、可逆性和回滚方式；
- 能够暴露错误候选的验证计划。

优先选择能够处理受支持根因的最小变更，而不是仅让某一个信号变绿的最小 Patch。除非当前意图和证据表明测试、断言、策略或检查本身错误，否则不要仅为压制失败而修改它们。

## 分层验证

| 层级 | 目的 |
| --- | --- |
| V0 Candidate Guard | 检查基线、实际差异、路径、策略、语法、Schema 和候选身份 |
| V1 Root Failure | 重跑原始失败的命令、任务、PoC 或检查 |
| V2 Affected Scope | 运行受影响测试、调用方、依赖检查、配置验证或 dry-run |
| V3 Broader Scope | 在已声明或确有必要时，运行更广的本地或下游回归检查 |
| V4 Outer CI | 观察准确发布候选对应的宿主流水线或 Required Checks |

`local_verified` 要求 V0、V1 以及所有适用且已声明的本地层级通过。`outer_ci_passed` 还要求 V4 通过。通过结果只能证明实际运行检查所覆盖的行为；Patch 和 Agent 自己的解释都不是独立证明。
