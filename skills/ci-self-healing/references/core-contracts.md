# 核心合同

本文件用于修复调用。以下是逻辑合同：宿主可编码为 YAML、JSON、记录或事件；没有指定格式时，简要表达所需内容并引用已有证据即可，无需新增状态机或重复表单。字段的简写不降低身份、权限或验证要求，也不允许省略行动前所需的记录或用事后补写替代；准入时序见 [候选修改前的准入记录](repair-and-verification.md#候选修改前的准入记录)。

## 运行时边界

本 Skill 是无状态或弱状态的，不声称提供持续同步或工具层写入拦截。准入记录使判断可核查，但字段齐全不证明语义判断正确；如需工具层拦截，须由宿主实现，不由 Skill 文本保证。

| 信息 | 权威来源 | 规则 |
| --- | --- | --- |
| Git、命令和 CI 事实 | 工具或 Provider 观察 | 记录来源和 `observed_at` |
| 诊断与下一步行动决策 | AI 评估 | 绑定证据引用，并在相关内容漂移后重新计算 |
| 工作流实时进度 | 外部 Harness 或 Provider | 按需轮询、订阅、持久化并重新调用 Skill |
| 修复成功 | 当前有效准入与验证收据 | 区分检查事实与修复结论，遵循 [结果判定](repair-and-verification.md#结果判定) |

`diagnosing`、`candidate_prepared`、`local_verifying` 或 `awaiting_outer_ci` 等临时标签可以作为进度事件，但不是持久化业务结果。

## Healing Request

以下字段只用于修复任务；只读任务按入口结束，不因未执行修复验证而返回修复结果。失败事实与诊断记录定义在 [建立失败合同](diagnosis-and-repair.md#建立失败合同)。

```yaml
healing_request:
  invocation_id: <本次独立调用的唯一身份>
  executor_role: main | subagent | unknown
  collaboration:
    can_ask_user: false
    can_handoff_to_parent: false
    parent_ref: optional
  mode: local_verify | remote_verify
  runtime_context: local_checkout | inline_ci_job | terminal_repair
  provider: auto | github_actions | gitlab_ci | enterprise
  failure_ref:
    run_id: optional
    job_id: optional
    task_or_command: optional
  baseline_sha: auto | explicit
  limits:
    max_attempts: 5
    time_budget: optional
    compute_budget: optional
```

## 执行身份与协作能力

`executor_role` 和 `collaboration` 来自调用参数或宿主可信上下文。缺省角色为 `unknown`，两种能力缺省为 `false`。能力须已声明且有实际可用通道；本地目录、终端或主 Agent 身份本身不证明可以交互。向主 Agent 移交还须能确认接收者，`parent_ref` 可以由宿主提供或明确路由。

这些字段描述协作条件，不授予代码修改、外部发布或改变策略的权限，也不由 `local_verify` / `remote_verify` 推导。宿主负责实际提问和 Agent 移交通道；本 Skill 提供当前执行者的行为约定。具体分支见[交互与移交](execution-modes.md#交互与移交)。

## 调用身份与限额

一次独立自愈调用对应一个固定的 `invocation_id`，也是本 Skill 中的 Healing Session 身份；由宿主提供或在本次入口确定。同一次执行因用户交互暂停后继续，沿用该身份、Attempt 记录和预算，不重置或自行延长限额。

调用方再次委派或明确重新发起独立调用时使用新身份，次数与预算独立计算，不跨调用累计。历史证据可以按有效性复用，保留其原始来源；重新计数不意味着旧候选已被撤销或旧收据自动适用。耗尽限额后结束本次调用，不自行重开调用来重置次数。

仓库策略可以降低每次调用的 `max_attempts`，但在此 Skill 版本中不能将其提高到五次以上。时间与计算预算的口径遵循宿主声明。

只有完整候选进入验证时才开始计数一个 Attempt。临时插桩、聚焦实验、日志收集、假设细化、询问用户和移交本身不增加 `index`。

第一个有效成功出现时停止；后续工作不再增加有用证据或策略、必要动作被策略禁止、Attempt 上限或预算耗尽时结束本次调用。按 [结果判定](repair-and-verification.md#结果判定)报告实际结果；未解决的关键缺口按诊断指南处理。

## Attempt 记录

```yaml
attempt:
  invocation_id: <本次调用身份>
  index: 1
  baseline_sha: <固定的 Git 提交基线>
  evidence_snapshot_ref: <本次决策使用的证据>
  diagnosis_ref: <诊断评估>
  candidate:
    fingerprint: <候选代码状态指纹>
    head_sha: optional
    ref: optional
  observations:
    local_verification:
      result: passed | failed | incomplete | not_run
      evidence_refs: []
      observed_at: optional
    remote_pipeline:
      run_ref: optional
      status: queued | running | passed | failed | canceled | unknown | not_run
      evidence_refs: []
      observed_at: optional
  evidence_delta: []
  decision:
    control: continue | pass | stop
    reason: <绑定证据的理由>
    evidence_refs: []
```

`observations` 仅汇总各自执行范围。逐项 `evidence_refs` 对应验证计划的检查标识；记录规则见 [分层验证](repair-and-verification.md#分层验证)，身份要求见下方收据绑定。

`fingerprint` 标识实际候选代码状态，不表示 Patch 已提取或已持久化。候选未提交时，`head_sha` 留空，不能用基线 SHA 代替；工作空间交付见 [执行模式](execution-modes.md#ci-job-工作空间交付)。

## 收据绑定与失效

验证或写回收据至少必须绑定：

```text
repository
+ 产生该收据的 invocation_id
+ baseline_sha
+ candidate_fingerprint
+ candidate_head_sha（已提交时）
+ 环境或 Provider 运行身份
+ 命令或检查身份
+ 结果
+ observed_at
```

HEAD 漂移、候选漂移、策略漂移、相关环境漂移、目标行为的相关变化或企业知识包漂移，会使受影响的决策和收据失效。应重新观察并计算，不要沿用过期的通过结果。跨调用引用旧收据时，保留原调用身份并核对其对当前基线、候选、环境和验证要求的适用性；不把旧结果改写成本次实际执行。

协作时读取 [交互与移交](execution-modes.md#交互与移交)，该处集中定义行为和交接内容。修复结果的唯一判定口径见 [结果判定](repair-and-verification.md#结果判定)。

## 可选的学习输出

Case Draft 可以记录 Failure Snapshot、诊断、候选 Attempt、验证收据、结果、负向约束和剩余缺口。它在外部治理流程审核并准入前始终是草稿。本 Skill 不得提升 Case 的权威级别、改写权威知识，或将历史 Patch 视为当前证明。
