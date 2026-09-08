# 核心合同

这些是逻辑合同。Harness 可以将它们编码为 YAML、JSON、数据库记录、CI 输出或事件，但必须保留身份与证据边界。

## 运行时边界

本 Skill 是无状态或弱状态的，不声称提供持续同步。

| 信息 | 权威来源 | 规则 |
| --- | --- | --- |
| Git、命令和 CI 事实 | 工具或 Provider 观察 | 记录来源和 `observed_at` |
| 诊断与下一步行动决策 | AI 评估 | 绑定证据引用，并在相关内容漂移后重新计算 |
| 工作流实时进度 | 外部 Harness 或 Provider | 按需轮询、订阅、持久化并重新调用 Skill |
| 成功或失败 | 验证收据 | 没有匹配的观察证据时绝不推断 |

`diagnosing`、`candidate_prepared`、`local_verifying` 或 `awaiting_outer_ci` 等临时标签可以作为进度事件，但不是持久化业务结果。

## Healing Request

```yaml
healing_request:
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

只从当前仓库、环境或 Provider 证据中发现省略的身份信息。如果仍存在多个可能的运行、作业、任务、仓库或基线，记录歧义并停止，不要猜测。

仓库策略可以降低 `max_attempts`，但在此 Skill 版本中不能将其提高到五次以上。

## Failure Snapshot

```yaml
failure_snapshot:
  repository: <稳定的仓库身份>
  baseline_sha: <准确的 Git 提交基线>
  provider_run:
    provider: <Provider 或 local>
    pipeline_id: optional
    run_attempt: optional
    job_id: optional
    task_id: optional
  failure:
    command: optional
    exit_code: optional
    error_refs: []
    log_refs: []
    artifact_refs: []
  environment:
    execution_context: <local 或 CI 身份>
    tool_versions: {}
  recent_changes: []
  completeness: complete | incomplete
  observed_at: <时间戳>
```

快照记录观察到的事实，不记录根因或修复结论。`incomplete` 永远不表示健康。如果材料缺失导致无法可靠诊断或制定验证计划，则返回 `inconclusive`。

`baseline_sha` 标识来源 Git 提交，不代表工作空间没有额外修改。Agent 对相关文件保留介入时的内容作为恢复依据，具体见[基线与候选恢复](execution-modes.md#基线与候选恢复)；不要求新增实时状态或快照服务。

## Attempt 记录

```yaml
attempt:
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

只有完整候选进入验证时，一个 Attempt 才开始计数。临时插桩、聚焦实验、日志收集和假设细化都不会增加 `index`。

`observations` 中的汇总结果只描述各自的执行范围。`evidence_refs` 指向的逐项收据必须对应 `repair_admission.required_verification` 的检查标识，保留实际执行位置、命令或 Job/Check、结果、未执行/未完成原因及候选身份。本地预检的 `passed` 不代表所有 V1/V2/V3 已在本地通过；远端取得通过证据后，本地未执行的检查仍保持 `not_run`，已经执行但未完成的检查仍保持 `incomplete`。

流水线内候选可以是指定工作空间中尚未 commit 的代码修改，不要求提供 Patch 文件引用。向后续提取脚本交接时，报告实际工作空间绝对路径、`baseline_sha`、修改文件和验证收据，区分工作空间原有修改与本轮修复，并将已验证代码留在该目录。`fingerprint` 用于标识代码状态，不表示 Patch 已提取或已持久化；候选未提交时不要将基线 SHA 填作候选的 `head_sha`。

## 持久化结果

| 结果 | 含义 |
| --- | --- |
| `not_eligible` | 失败属于重试、Flaky、基础设施、事故或其他非修复路径 |
| `local_verified` | 候选通过了原始失败和 `local_verify` 声明的全部本地验证 |
| `outer_ci_passed` | 候选通过发布前检查；V1 及必需的 V2/V3 在计划指定位置实际通过，且准确候选对应的 V4 通过；不要求先取得 `local_verified` |
| `no_progress` | 后续候选尝试不再增加有用证据或策略 |
| `inconclusive` | 证据、环境、领域知识、Provider 能力或验证不足 |
| `denied` | 适用策略禁止生成候选、修改或写回 |

观察到的远端状态 `queued` 或 `running` 不是持久化结果。记录运行引用和观察时间；新事件到达时，Harness 必须继续观察或重新调用 Skill。

两种模式的执行位置与成功条件见[分层验证规则](diagnosis-and-repair.md#分层验证)。即使 Provider 汇总状态为 `passed`，若原失败检查或任一必需检查没有实际通过证据，也不能得出 `outer_ci_passed`。

## 收据绑定与失效

验证或写回收据至少必须绑定：

```text
repository
+ baseline_sha
+ candidate_fingerprint
+ candidate_head_sha（已提交时）
+ 环境或 Provider 运行身份
+ 命令或检查身份
+ 结果
+ observed_at
```

HEAD 漂移、候选漂移、策略漂移、相关环境漂移或企业知识包漂移，会使受影响的决策和收据失效。应重新观察并计算，不要沿用过期的通过结果。

## 可选的学习输出

Case Draft 可以记录 Failure Snapshot、诊断、候选 Attempt、验证收据、结果、负向约束和剩余缺口。它在外部治理流程审核并准入前始终是草稿。本 Skill 不得提升 Case 的权威级别、改写权威知识，或将历史 Patch 视为当前证明。
