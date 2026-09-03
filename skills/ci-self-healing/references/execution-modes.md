# Execution Modes

The repair logic is shared across modes. Mode changes where verification happens, who owns the live lifecycle, and whether publishing a candidate is authorized.

## Runtime contexts

| Context | Meaning |
| --- | --- |
| `local_checkout` | Interactive or automated repair in a local repository checkout |
| `inline_ci_job` | A failure-aware wrapper invokes repair before the CI job verdict is finalized |
| `terminal_repair` | Repair starts after a step or job failure has already been sealed by the provider |

Runtime context and verification mode are related but distinct. A local checkout may use remote verification; an inline CI job normally starts with local verification; terminal repair requires a new remote run for outer-CI success.

## Isolate the baseline and candidates

- Bind the Healing Session to one `baseline_sha`.
- Preserve user changes and unrelated generated files. If the working tree is dirty or overlap is possible, use a dedicated worktree, temporary checkout, or equivalent isolated workspace.
- Rebuild each candidate from the same baseline. Carry forward logs, receipts, rejected hypotheses, and negative constraints, but not the failed candidate's code state.
- Fingerprint the complete baseline-to-candidate diff before verification.
- Do not use destructive reset or checkout operations against a user workspace to implement candidate isolation.

## `local_verify`

For `local_checkout`:

1. Capture the original failure and baseline.
2. Diagnose and obtain `allow_local_candidate`.
3. Apply the candidate in the isolated or in-scope workspace.
4. Run V0, V1, and applicable V2/V3 checks.
5. Return `local_verified`, continue with a new Attempt, or stop with the relevant outcome.

For `inline_ci_job`:

- The job wrapper must capture the failing command without irrevocably finalizing the job first.
- Preserve the original failure as evidence, repair the ephemeral workspace, and rerun the declared checks.
- The wrapper may return success only when the candidate satisfies the configured local verification contract.
- If the provider has already sealed a failed step or job, do not rewrite history or claim the original run passed. Treat the run as `terminal_repair`.

An ephemeral CI workspace must export the candidate Patch or a durable artifact when later use is required. Do not claim persistence merely because the local job tree was modified.

## `remote_verify`

Preconditions:

- the provider or SCM adapter can publish a candidate and identify its exact resulting SHA;
- the provider can trigger or resolve the pipeline associated with that SHA;
- the run can be observed to a terminal result, directly or through an external Harness;
- the candidate has `allow_remote_candidate`.

Execution:

1. Run the required local preflight and V0 guard.
2. Create a unique, dedicated candidate ref for the Healing Session and Attempt, derived from the fixed baseline.
3. Commit only the coherent candidate diff, then push the dedicated ref.
4. If push already triggers the intended pipeline, resolve that run and do not dispatch a duplicate. Otherwise invoke the provider's explicit trigger.
5. Record the candidate SHA, provider run identity, and observation time.
6. Observe the declared remote checks. On failure, normalize new evidence and decide whether another baseline-derived candidate is justified.
7. Return `outer_ci_passed` only when the observed result belongs to the exact candidate and all declared outer checks pass.

Remote mode authorizes automatic commit, push to the dedicated candidate namespace, pipeline trigger, and observation. It does not authorize:

- direct writes to the default or protected branch;
- overwriting an existing user or shared branch;
- merge, tag, release, deployment, or production action;
- weakening checks or changing branch protection;
- automatic branch deletion or cleanup.

Prefer a unique ref per Attempt. Do not force-push an existing user or shared ref. Any provider-specific alternative must preserve baseline isolation, candidate identity, auditability, and idempotency.

## Terminal transition

When a failure verdict is already sealed:

```text
terminal_repair
  -> local diagnosis and candidate guard
  -> remote candidate publication
  -> new outer CI run
```

If required remote capabilities are unavailable, retain the locally verified candidate and return `inconclusive` with the missing capability. Do not reinterpret the old failed run as healed.

## Bounded loop

- Default to at most five complete candidate Attempts; honor a smaller repository limit.
- Stop immediately on the first valid success.
- Stop early when further work lacks information gain, policy denies the action, evidence is insufficient, or time or compute budgets are exhausted.
- Classify a new failure as related, unrelated, or uncertain using candidate diff, dependency context, baseline evidence, and the observed verification result. Preserve the basis; do not use a fixed score.
- An infrastructure or provider failure during V4 makes that observation incomplete for candidate correctness. Route it to the appropriate recovery mechanism or return `inconclusive`; do not consume unlimited repair Attempts retrying infrastructure.

## Result reporting

For local mode, report the candidate fingerprint, changed files, commands actually run, results, and uncovered outer checks.

For remote mode, additionally report the dedicated ref, candidate SHA, trigger method, provider run identity, last observation time, terminal result when observed, and any Harness continuation requirement.
