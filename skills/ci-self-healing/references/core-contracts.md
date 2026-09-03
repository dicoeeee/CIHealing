# Core Contracts

These are logical contracts. A Harness may encode them as YAML, JSON, database records, CI outputs, or events, but it must preserve the identity and evidence boundaries.

## Runtime boundary

The Skill is stateless or weakly stateful. It does not claim continuous synchronization.

| Information | Authority | Rule |
| --- | --- | --- |
| Git, command, and CI facts | Tool or provider observation | Record the source and `observed_at` |
| Diagnosis and next-action decisions | AI assessment | Bind to evidence references and recompute after relevant drift |
| Live workflow progress | External Harness or provider | Poll, subscribe, persist, and re-invoke the Skill as needed |
| Success or failure | Verification receipt | Never infer without matching observed evidence |

Transient labels such as `diagnosing`, `candidate_prepared`, `local_verifying`, or `awaiting_outer_ci` may be progress events. They are not durable business outcomes.

## Healing request

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

Discover omitted identities only from current repository, environment, or provider evidence. If multiple plausible runs, jobs, tasks, repositories, or baselines remain, record the ambiguity and stop instead of guessing.

Repository policy may reduce `max_attempts`; it must not raise it above five for this Skill version.

## Failure Snapshot

```yaml
failure_snapshot:
  repository: <stable repository identity>
  baseline_sha: <exact code baseline>
  provider_run:
    provider: <provider or local>
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
    execution_context: <local or CI identity>
    tool_versions: {}
  recent_changes: []
  completeness: complete | incomplete
  observed_at: <timestamp>
```

The snapshot records observed facts, not root cause or repair conclusions. `incomplete` never means healthy. If missing material prevents a reliable diagnosis or verification plan, return `inconclusive`.

## Attempt record

```yaml
attempt:
  index: 1
  baseline_sha: <fixed baseline>
  evidence_snapshot_ref: <evidence used for this decision>
  diagnosis_ref: <diagnosis assessment>
  candidate:
    fingerprint: <candidate diff fingerprint>
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
    reason: <evidence-linked rationale>
    evidence_refs: []
```

An Attempt begins only when a complete candidate enters verification. Temporary instrumentation, focused experiments, log collection, and hypothesis refinement do not increment `index`.

## Durable outcomes

| Outcome | Meaning |
| --- | --- |
| `not_eligible` | The failure belongs to retry, flaky, infrastructure, incident, or another non-repair path |
| `local_verified` | The candidate passed the original failure and all declared local verification for `local_verify` |
| `outer_ci_passed` | The exact published candidate passed the declared remote pipeline or Required Checks |
| `no_progress` | Further candidate attempts no longer add useful evidence or strategies |
| `inconclusive` | Evidence, environment, domain knowledge, provider capability, or verification is insufficient |
| `denied` | Applicable policy forbids candidate generation, modification, or writeback |

An observed remote status of `queued` or `running` is not a durable outcome. Record the run reference and observation time; the Harness must continue observation or re-invoke the Skill when a new event arrives.

## Receipt binding and invalidation

A verification or writeback receipt must bind at least:

```text
repository
+ baseline_sha
+ candidate_fingerprint
+ candidate_head_sha when committed
+ environment or provider run identity
+ command or check identity
+ result
+ observed_at
```

HEAD drift, candidate drift, policy drift, relevant environment drift, or enterprise knowledge-pack drift invalidates affected decisions and receipts. Re-observe and recompute rather than carrying forward a stale pass.

## Optional learning output

A Case Draft may record the Failure Snapshot, diagnosis, candidate Attempts, verification receipts, outcome, negative constraints, and remaining gaps. It remains a draft until an external governance process reviews and admits it. The Skill must not promote a Case, rewrite authoritative knowledge, or treat a historical Patch as current proof.
