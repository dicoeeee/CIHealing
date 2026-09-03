# Diagnosis and Repair

Use evidence to decide whether to experiment, create a local candidate, publish a remote candidate, or stop. Do not use a numeric confidence threshold to hide missing or conflicting evidence.

## Admit the right failures

This Skill handles deterministic code, test, dependency, configuration, build, and release-engineering failures for which a candidate change and meaningful verification are possible.

Route network interruption, exhausted resources, runner loss, transient provider failure, and known flaky behavior to retry or infrastructure recovery. If determinism cannot yet be assessed, collect discriminating evidence; return `inconclusive` rather than `not_eligible` when the routing decision itself is unresolved.

## Establish the failure contract

Before diagnosing root cause, record:

- the original signal and where it was observed;
- expected versus actual behavior;
- repository, baseline, environment, input, and recent changes;
- a red-capable command, replay, PoC, or remote check when available;
- the affected object and the current impact.

Preserve the original failure before new attempts alter logs or the workspace.

## Localize without overstating causality

Use the levels as reporting vocabulary, not as a mandatory checklist:

| Level | Meaning |
| --- | --- |
| L0 Signal | The failed check, alert, or symptom is identified |
| L1 Execution | The workflow, job, step, task, or command is identified |
| L2 Code | The relevant repository, file, class, function, or line is identified |
| L3 Boundary | The first data, state, or component boundary that diverges is identified |
| L4 Cause | A mechanism is supported by a falsifiable experiment, contrast, trace, or equivalent evidence |

Logs and `file:line` often support L1 or L2 only. They do not automatically establish L4.

Select methods that discriminate among current hypotheses: first actionable error, minimal reproduction, reproduction amplification, working-versus-failing comparison, diff or bisect, falsifiable hypotheses, focused instrumentation, upstream data-flow tracing, multi-source evidence triangulation, and intent reconstruction from tests, history, specifications, or ADRs. Do not mechanically run every method.

## Route generic and enterprise knowledge

Use the generic path when repository context, public software-engineering knowledge, and standard tools are sufficient.

Use an enterprise domain only when correct diagnosis depends on internal concepts, current business state, governed combinations, private baselines, or specialized validators. A compatible Domain Knowledge Pack must identify its version, applicability, provenance, rules, state queries, tools, and validators.

If the domain is ambiguous, the compatible pack is missing or stale, current state cannot be queried, rules conflict, or a required validator is unavailable, record the gap and return `inconclusive`. Do not widen retrieval until a semantically similar rule appears.

Historical Cases provide hypotheses, negative constraints, and verification clues only. Apply `Retrieve -> Compare -> Adapt -> Verify`; never replay an old Patch as the current repair.

## Record the diagnosis assessment

```yaml
diagnosis:
  classification: <failure class and domain route>
  localization:
    level: L0 | L1 | L2 | L3 | L4
    target: <observed location or boundary>
  hypotheses:
    - statement: <candidate mechanism>
      supporting_evidence: []
      conflicting_evidence: []
      predicted_signal: <what would support or refute it>
  root_cause:
    status: confirmed | supported | unclear | not_investigated
    statement: optional
  expected_behavior: <behavior or contract to preserve>
  repair_constraints:
    must_preserve: []
    allowed_scope: []
    prohibited_directions: []
  unknowns: []
```

`confirmed` requires direct causal evidence in the current context. `supported` means the available evidence favors the mechanism over material alternatives and the candidate can be meaningfully falsified by verification. Do not upgrade `supported` merely because the proposed Patch looks plausible.

## Decide repair admission

```yaml
repair_admission:
  decision: experiment_only | allow_local_candidate | allow_remote_candidate | stop
  evidence_refs: []
  unknowns: []
  required_verification: []
  rationale: <why this action is justified now>
```

Use these boundaries:

- `experiment_only`: make reversible diagnostic edits or add temporary instrumentation in an isolated workspace. Do not publish them as a repair candidate.
- `allow_local_candidate`: the failure and baseline are bound, localization is actionable, the root cause is `confirmed` or `supported` or a deterministic tool directly identifies a mechanical correction, expected behavior is known, material alternatives were considered, and executable verification exists.
- `allow_remote_candidate`: `allow_local_candidate` conditions hold, the complete candidate passed required local guards, the actual diff passed scope and policy checks, and remote verification can bind to an exact candidate ref.
- `stop`: evidence, intent, policy, domain knowledge, execution environment, or verification cannot justify further modification.

Diagnostic experiments may precede a repair candidate and must be removed unless they belong to the intentional repair. Never commit or push instrumentation by accident.

## Build the candidate

Each candidate must be a complete change relative to the fixed baseline and include:

- the supported root cause and evidence it addresses;
- the intended behavior and constraints it preserves;
- files, configuration, dependencies, and downstream scope affected;
- risk, reversibility, and any rollback method;
- a verification plan capable of exposing a wrong candidate.

Prefer the smallest change that addresses the supported cause, not the smallest Patch that turns one signal green. Do not modify tests, assertions, policies, or checks merely to suppress the failure unless current intent and evidence show that those expectations are wrong.

## Verify in layers

| Layer | Purpose |
| --- | --- |
| V0 Candidate Guard | Check baseline, actual diff, paths, policy, syntax, schema, and candidate identity |
| V1 Root Failure | Re-run the original failed command, task, PoC, or check |
| V2 Affected Scope | Run affected tests, callers, dependency checks, configuration validation, or dry-runs |
| V3 Broader Scope | Run broader local or downstream regression checks when declared or warranted |
| V4 Outer CI | Observe the host pipeline or Required Checks for the exact published candidate |

`local_verified` requires V0, V1, and all applicable declared local layers. `outer_ci_passed` additionally requires V4. A pass establishes only the behavior covered by the checks that ran; the Patch and the Agent's own explanation are not independent proof.
