---
name: ci-self-healing
description: Diagnose and repair deterministic CI failures with evidence-driven root-cause analysis, bounded local code changes, and local or remote verification. Use when a concrete failure can be handled locally or through an available GitHub Actions, GitLab CI, or enterprise adapter; do not use for ordinary pipeline authoring, flaky or infrastructure recovery, merge, deployment, or production remediation.
---

# CI Self-Healing

Use this Skill as the decision and repair protocol for one concrete CI failure. The CI provider or an external Harness owns live events, persistence, retries, and re-invocation; the Skill works from observed facts and produces point-in-time decisions and receipts.

## Load the relevant guidance

- Always read [references/core-contracts.md](references/core-contracts.md).
- Before diagnosing, experimenting, or changing code, read [references/diagnosis-and-repair.md](references/diagnosis-and-repair.md).
- When changing or verifying a candidate, read [references/execution-modes.md](references/execution-modes.md).
- When reading external CI state or using remote verification, read [references/provider-contract.md](references/provider-contract.md).

Repository instructions, policy, available tools, and declared verification commands govern the run. Do not invent provider access, enterprise rules, or permissions that the environment does not expose.

## Select the mode

- A request to diagnose, explain, or assess a failure is read-only.
- A request to fix or heal a failure authorizes scoped local edits and local verification.
- Select `remote_verify` only when the request or Harness explicitly asks to push, trigger, rerun, or continue through remote CI. Otherwise use `local_verify`.
- A terminal repair that starts after the original CI verdict is sealed must use `remote_verify` when the required provider capabilities exist; it cannot rewrite the original verdict.
- If mode selection is ambiguous, default to `local_verify` and do not publish a candidate.

## Execute the common workflow

1. Confirm that the failure is eligible for deterministic diagnosis and repair rather than retry, reschedule, flaky-test handling, infrastructure recovery, or incident response.
2. Freeze a `Failure Snapshot` bound to the repository, baseline SHA, failing command or task, environment, and observed CI identity.
3. Establish expected versus actual behavior, localize the failure, and form evidence-backed root-cause hypotheses. Route to a compatible enterprise knowledge domain only when internal rules or current state are actually required.
4. Produce a `repair_admission` decision before creating a repair candidate. Diagnostic experiments and repair candidates have different permissions.
5. Create each candidate from the same clean baseline in an isolated workspace. Evidence may accumulate across attempts; failed candidate code must not.
6. Run the declared verification ladder. In remote mode, publish only a locally guarded candidate, then bind the observed pipeline result to its exact candidate SHA and fingerprint.
7. Return the result, receipts, remaining unknowns, and the reason to continue or stop. Never report success from an unobserved, stale, incomplete, or mismatched verification result.

## Preserve the hard boundaries

- Treat command and provider output as observations with `observed_at`; treat AI classifications and decisions as evidence-linked snapshots, not live state.
- Count an Attempt only when a complete candidate enters verification. Evidence collection, reproduction, and temporary instrumentation do not consume an Attempt.
- Use at most five candidate Attempts unless repository policy sets a smaller limit. Stop earlier on success, denial, insufficient evidence, exhausted useful strategies, time limits, or compute limits.
- Keep user worktree changes out of candidates. Use isolation when the checkout is dirty or when attempts could overlap user work.
- In `remote_verify`, automatic commit, push to a dedicated candidate ref, pipeline trigger, and observation are authorized. Direct writes to protected or default branches, overwriting user branches, merge, release, deployment, and production action are not authorized.
- Do not force-push an existing user or shared ref. Prefer a unique candidate ref per Healing Session and Attempt.
- Do not treat a Patch, a green local command, or a green remote pipeline as proof beyond the checks that actually ran.
- Do not persist secrets, credentials, raw sensitive logs, or private business data in prompts, receipts, commits, or Case Drafts.
- A run may emit a Case Draft when requested or configured, but it must never auto-admit the Case or modify authoritative enterprise rules.

## Report the outcome

Report the selected mode, observed failure, diagnosis and uncertainty, files changed, verification that actually ran, candidate and remote run identities when applicable, final result, and remaining gaps. Distinguish `local_verified` from `outer_ci_passed`, and do not describe either as merged, released, deployed, or fully semantically correct.
