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
  baseline_sha: <准确的代码基线>
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

## Attempt 记录

```yaml
attempt:
  index: 1
  baseline_sha: <固定基线>
  evidence_snapshot_ref: <本次决策使用的证据>
  diagnosis_ref: <诊断评估>
  candidate:
    fingerprint: <候选差异指纹>
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

## 持久化结果

| 结果 | 含义 |
| --- | --- |
| `not_eligible` | 失败属于重试、Flaky、基础设施、事故或其他非修复路径 |
| `local_verified` | 候选通过了原始失败和 `local_verify` 声明的全部本地验证 |
| `outer_ci_passed` | 准确发布的候选通过了声明的远端流水线或 Required Checks |
| `no_progress` | 后续候选尝试不再增加有用证据或策略 |
| `inconclusive` | 证据、环境、领域知识、Provider 能力或验证不足 |
| `denied` | 适用策略禁止生成候选、修改或写回 |

观察到的远端状态 `queued` 或 `running` 不是持久化结果。记录运行引用和观察时间；新事件到达时，Harness 必须继续观察或重新调用 Skill。

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
