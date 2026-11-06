"""Read-only evidence reduction for the frozen synthetic native-Agent A/B.

No model, runner, checker or reviewer is invoked. Only analysis/summary.json and
analysis/report.md are replaceable derived artifacts; trial evidence is immutable.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import tempfile
import os

from common import EvalError, digest, read_json, read_regular, replace_json
from ab_review import CRITERIA, MODEL_ID, RESPONSE_MODEL_ID
from ab_loading_audit import audit_trial
from ab_collection_resolution import resolve_collection

ENGINE = Path(__file__).resolve().parent
DEFAULT_ROOT = ENGINE / "runs/ab-modular-20261104-01"
TOKEN_FIELDS = ("input_total", "cache_read", "cache_creation", "output_total", "reasoning", "output_visible")
DELIVERY_IDS = {"report_scope", "outcome", "enterprise_outputs", "final_bytes", "truthful_delivery"}
LOADING_IDS = {"loading_order"}
BOUNDARY = ("受控 synthetic 场景上的真实 iCode Agent 执行；不是生产故障统计。离线单测、"
            "模型行为评审和真实服务验收是不同证据层。性能仅观察，不作为通过条件。")
LIFECYCLE_LABELS = {"not_started": "尚未启动", "execution_started_outcome_unknown": "执行已启动、结束状态未收集",
    "execution_finished_missing_run_evidence": "执行进程结束、run回执缺失", "execution_finished_incomplete": "执行结束但未完整完成",
    "execution_finished_awaiting_objective": "执行结束、等待客观审计", "execution_finished_awaiting_review": "执行结束、等待评审",
    "review_started_outcome_unknown": "评审已启动、结束状态未收集", "review_finished_missing_result": "评审进程结束、结果未收集",
    "finished_with_evidence_gaps": "执行与评审结束、证据不足", "assessed": "证据评估完整", "invalid_harness": "宿主缺陷已排除"}


def optional_json(path, gaps):
    if not path.exists():
        return None
    try:
        value = read_json(path)
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value
    except (OSError, ValueError, EvalError):
        gaps.append("unreadable:" + path.name)
        return None


def combine(statuses):
    statuses = list(statuses)
    if "fail" in statuses:
        return "fail"
    if not statuses or any(s not in {"pass", "not_applicable"} for s in statuses):
        return "incomplete"
    return "not_applicable" if all(s == "not_applicable" for s in statuses) else "pass"


def number(value):
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def usage_metadata(trial, role, *, directory=None):
    """Whitelist scalar exchange metadata; never retain model/reasoning text."""
    gaps, totals, observed, models, profiles = [], Counter(), Counter(), set(), set()
    directory = Path(directory) if directory is not None else trial if role == "execute" else trial / "review"
    receipt = optional_json(directory / "session-receipt.json", gaps)
    sessions = (receipt or {}).get("sessions", [])
    starts = ends = 0
    seen_sessions = set()
    if not receipt:
        gaps.append("session_receipt_missing")
    for session in sessions:
        meta = session.get("meta", {})
        profiles.add((meta.get("model_profile_id"), meta.get("model_id"), meta.get("app_version")))
        relative = Path(session.get("path", ""))
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            gaps.append("invalid_session_path")
            continue
        base = trial / "native-session" if role == "execute" else directory / "sessions"
        path = base / relative.parent / "trajectory/events.jsonl"
        session_key = (str(path), meta.get("session_id"))
        if session_key in seen_sessions:
            gaps.append("duplicate_session_receipt")
            continue
        seen_sessions.add(session_key)
        try:
            # Native trajectory envelopes carry usage and may also carry text.
            # The latter is neither copied to the accumulator nor returned.
            raw = read_regular(path, limit=64 * 1024 * 1024)
            seen, requested, closed = set(), {}, False
            for line in raw.splitlines():
                event = json.loads(line)
                kind, payload = event.get("event_type"), event.get("payload", {})
                if event.get("session_id") != meta.get("session_id"):
                    gaps.append("trajectory_session_mismatch")
                    continue
                key = (event.get("runtime_id"), event.get("operation_id"))
                if kind == "model.exchange.started":
                    if key in requested:
                        gaps.append("duplicate_exchange_start")
                        continue
                    starts += 1
                    requested[key] = payload.get("request_model")
                elif kind == "model.exchange.finished":
                    if key in seen:
                        gaps.append("duplicate_exchange_finish")
                        continue
                    seen.add(key); ends += 1
                    if key not in requested:
                        gaps.append("unpaired_exchange_identity")
                    models.add((requested.get(key), payload.get("response_model")))
                    normalized = payload.get("usage", {}).get("normalized", {})
                    if normalized.get("normalization_unavailable"):
                        gaps.append("normalization_unavailable")
                    for field in TOKEN_FIELDS:
                        value = normalized.get(field)
                        if number(value):
                            totals[field] += value; observed[field] += 1
                elif kind == "trajectory.runtime.finished":
                    closed = payload.get("reason") == "graceful_shutdown"
            if not closed:
                gaps.append("trajectory_not_closed")
        except (OSError, ValueError, TypeError, AttributeError, EvalError):
            gaps.append("trajectory_metadata_unavailable")
    if starts != ends:
        gaps.append("unpaired_exchanges")
    return {"session_count": len(seen_sessions), "declared_session_rows": len(sessions),
            "native_exchange_starts": starts, "native_exchange_finishes": ends,
            "normalized_tokens": {field: {"sum": totals[field] if observed[field] else None,
                "observed_exchanges": observed[field], "total_finished_exchanges": ends} for field in TOKEN_FIELDS},
            "models": [{"request_model": a, "response_model": b} for a, b in sorted(models, key=str)],
            "profiles": [{"model_profile_id": a, "model_id": b, "app_version": c}
                         for a, b, c in sorted(profiles, key=str)], "gaps": sorted(set(gaps)),
            "boundary": "native exchange counters, not provider SDK wire-request count or backend attestation; token fields overlap"}


def resource_metrics(trace):
    names, reads, loads = set(), 0, 0
    for event in (trace or {}).get("events", []):
        if event.get("kind") != "InvocationToolCallStart":
            continue
        payload = event.get("payload", {})
        args = payload.get("args", {})
        if args.get("skill_name") != "ci-self-healing":
            continue
        if payload.get("tool_name") == "read_skill_resource":
            reads += 1
            if isinstance(args.get("resource_name"), str):
                names.add(args["resource_name"])
        elif payload.get("tool_name") == "load_skill":
            loads += 1
    return {"read_skill_resource_calls": reads if trace else None, "load_skill_calls": loads if trace else None,
            "distinct_resource_count": len(names) if trace else None, "resources": sorted(names),
            "boundary": "attempted native resource calls; full-read and timing acceptance are separate loading checks"}


def lifecycle_state(*, execution_started, run, process, objective, review_started, review,
                    review_process, assessment_complete):
    """Observe persisted phase states without assuming an unclosed process is alive."""
    if not execution_started:
        return "not_started"
    if not process:
        return "execution_started_outcome_unknown"
    if not run:
        return "execution_finished_missing_run_evidence"
    if run.get("status") != "completed" or process.get("status") != "completed" or process.get("returncode") != 0:
        return "execution_finished_incomplete"
    if not objective:
        return "execution_finished_awaiting_objective"
    if not review_started:
        return "execution_finished_awaiting_review"
    if not review:
        return "review_finished_missing_result" if review_process else "review_started_outcome_unknown"
    return "assessed" if assessment_complete else "finished_with_evidence_gaps"


def trial_record(root, version, row, trial=None):
    trial = Path(trial) if trial is not None else root / version / "trials" / row["id"]
    gaps = []
    disposition = optional_json(trial / "harness-disposition.json", gaps)
    run = optional_json(trial / "run.json", gaps) or {}
    objective = optional_json(trial / "objective.json", gaps) or {}
    review = optional_json(trial / "review/result.json", gaps) or {}
    verdict = optional_json(trial / "review/verdict.json", gaps) or {}
    exchange = optional_json(trial / "review/exchange-receipt.json", gaps) or {}
    trace = optional_json(trial / "public-trace.json", gaps)
    review_binding = optional_json(trial / "review/binding.json", gaps) or {}
    review_sessions = optional_json(trial / "review/session-receipt.json", gaps) or {}
    execution_process = optional_json(trial / "execution/process.json", gaps) or {}
    review_process = optional_json(trial / "review/execution/process.json", gaps) or {}
    original_review = dict(review)
    correction = resolve_collection(trial, original_review, exchange, snapshot_root=root / "analysis/collection-corrections")
    if correction["status"] == "corrected":
        verdict = correction["effective_opinion"]
        review = {**review, **correction["effective_review"], "gaps": []}
    process = run.get("process") or execution_process
    effective_review_process = review.get("process") or review_process
    execution_started = bool(run) or bool(execution_process) or (trial / "execution-start.json").exists() or (trial / "execution").exists()
    review_started = (trial / "review-start.json").exists() or bool(review_process) or bool(review_sessions.get("sessions"))
    execution = run.get("status", "finished_without_run_receipt" if execution_process else "started_without_result" if execution_started else "not_started")
    executed = execution == "completed" and process.get("status") == "completed" and process.get("returncode") == 0
    raw_checks = verdict.get("checks", [])
    schema_valid = (isinstance(raw_checks, list) and len(raw_checks) == len(CRITERIA)
                    and all(isinstance(c, dict) for c in raw_checks)
                    and {c.get("id") for c in raw_checks} == set(CRITERIA)
                    and all(c.get("status") in {"pass", "fail", "incomplete", "not_applicable"} for c in raw_checks))
    derived = combine(c.get("status") for c in raw_checks) if schema_valid else "incomplete"
    derived = "pass" if derived == "not_applicable" else derived
    review_valid = (review.get("status") == "completed" and exchange.get("status") == "validated"
                    and effective_review_process.get("status") == "completed" and effective_review_process.get("returncode") == 0
                    and exchange.get("request_model") == MODEL_ID and exchange.get("response_model") == RESPONSE_MODEL_ID
                    and exchange.get("exchange_count") == 1
                    and review.get("verdict") == verdict.get("verdict") == derived and schema_valid)
    checks = verdict.get("checks", []) if review_valid else []
    semantic = review.get("verdict", "incomplete") if review_valid else "incomplete"
    if review and not review_valid:
        gaps.append("review_receipt_or_verdict_unconfirmed")
    loading_host = objective.get("execution_checks", {}).get("dimensions", {}).get("native_protocol", {}).get("status")
    loading = combine([{"passed": "pass", "failed": "fail"}.get(loading_host, "incomplete"),
                       *[c.get("status") for c in checks if c.get("id") in LOADING_IDS],
                       "pass" if checks else "incomplete"])
    delivery = combine([c.get("status") for c in checks if c.get("id") in DELIVERY_IDS])
    if objective.get("pipeline_report") is True:
        host_checks = objective.get("report_check", {}).get("attempts", [])
        checkpoint = objective.get("report_delivery", {}).get("attempts", [])
        host_delivery = "pass" if (any(c.get("returncode") == 0 for c in host_checks)
                                  and any(c.get("status") == "passed" for c in checkpoint)) else "incomplete"
        if any((f.get("check") if isinstance(f, dict) else f) == "formal_report_format_invalid"
               for f in objective.get("failures", [])):
            host_delivery = "fail"
        delivery = combine([delivery, host_delivery])
    behavior = combine([objective.get("status", "incomplete"),
                        *[c.get("status") for c in checks if c.get("id") not in DELIVERY_IDS | LOADING_IDS],
                        "pass" if checks else "incomplete"])
    final = combine([objective.get("status", "incomplete"), semantic,
                     "pass" if executed and not gaps and not objective.get("gaps") and not review.get("gaps") else "incomplete"])
    original_final, original_loading = final, loading
    post_loading = audit_trial(trial, row, snapshot_root=root / "analysis/loading-audits")
    final = combine([final, post_loading["status"]])
    loading = combine([loading, post_loading["status"]])
    reasons = sorted(set(gaps + objective.get("gaps", []) + review.get("gaps", [])))
    if not executed:
        reasons.append("execution:" + execution)
    if not review_valid:
        reasons.append("review_not_complete")
    if post_loading["status"] not in {"pass", "fail"}:
        reasons.append("post_loading_audit_incomplete")
    assessment_complete = (executed and review_valid and semantic in {"pass", "fail"}
                           and objective.get("status") in {"pass", "fail"}
                           and post_loading["status"] in {"pass", "fail"}
                           and not gaps and not objective.get("gaps") and not review.get("gaps") and not post_loading.get("gaps"))
    # Missing deliverables and uncovered business checks can coexist with an
    # adequately evidenced violation. They do not erase a completed fail.
    # Missing/corrupt phase receipts and unfinished execution still block any
    # paired decision, including one based on a confirmed partial failure.
    failure_basis = (["objective_failure"] if objective.get("status") == "fail" else [])
    if semantic == "fail":
        failure_basis.append("model_failure")
    if post_loading["status"] == "fail":
        failure_basis.append("post_loading_failure")
    terminal_receipts_complete = (executed and review_valid
        and objective.get("status") in {"pass", "fail", "incomplete"}
        and post_loading["status"] in {"pass", "fail", "incomplete"}
        and not gaps and not any(str(g).startswith("post_audit_evidence_unavailable:") for g in post_loading.get("gaps", [])))
    decision_complete = (assessment_complete if final == "pass" else
                         bool(final == "fail" and failure_basis and terminal_receipts_complete))
    lifecycle = lifecycle_state(execution_started=execution_started, run=run, process=process, objective=objective,
                                review_started=review_started, review=review, review_process=effective_review_process,
                                assessment_complete=assessment_complete)
    result = {"version": version, **row, "trial_path": str(trial), "status": final,
            "trial_directory_exists": trial.is_dir(),
            "execution_started": execution_started, "review_started": review_started,
            "process_receipts": {"execute": bool(execution_process), "review": bool(review_process)},
            "assessment_complete": assessment_complete, "decision_complete": decision_complete,
            "decision_failure_basis": failure_basis, "terminal_receipts_complete": terminal_receipts_complete,
            "lifecycle": lifecycle,
            "execution_completed_successfully": executed, "review_evidence_valid": review_valid,
            "stages": {"execution": execution, "objective": objective.get("status", "not_assessed"),
                       "review": semantic, "behavior": behavior, "loading": loading, "delivery": delivery,
                       "post_loading": post_loading["status"]},
            "before_post_audit_status": original_final, "before_post_audit_loading": original_loading,
            "post_loading_audit": post_loading,
            "review_process_status": original_review.get("status", "started_without_result" if (trial / "review-start.json").exists() else "not_started"),
            "original_review_status": original_review.get("status"), "original_review_gaps": original_review.get("gaps", []),
            "effective_review": {"status": review.get("status"), "verdict": semantic, "evidence_valid": review_valid},
            "collection_correction": correction,
            "recorded_review_verdict": original_review.get("verdict"),
            "original_model_opinion": correction.get("original_opinion", {}).get("verdict", original_review.get("verdict")),
            "failure_checks": objective.get("failures", []),
            "review_checks": [{k: c.get(k) for k in ("id", "status", "evidence")} for c in checks],
            "uncovered": reasons, "target_coverage": objective.get("target_coverage", {}).get("status"),
            "performance": {"execution_seconds": process.get("duration_seconds"),
                "review_seconds": effective_review_process.get("duration_seconds"), **resource_metrics(trace),
                "execute_usage": usage_metadata(trial, "execute"), "review_usage": usage_metadata(trial, "review")},
            "identity": {"executor_expected_model": run.get("expected_model_id"), "executor_profile_sha256": run.get("profile_sha256"),
                "skill_execution_contract": run.get("skill_execution_contract"),
                "review_profile_name": review_binding.get("profile_name"), "review_profile_sha256": review_binding.get("profile_sha256"),
                "review_request_model": exchange.get("request_model"), "review_response_model": exchange.get("response_model")}}
    if (trial / "harness-disposition.json").exists():
        # Preserve the old outcomes. A harness disposition excludes a trial
        # from Skill inference regardless of whether its old verdict was green.
        result.update(status="invalid_harness", recorded_status=final,
                      recorded_stages=dict(result["stages"]), harness_disposition=disposition,
                      primary_eligible=False, assessment_complete=False, decision_complete=False, lifecycle="invalid_harness")
        for stage in ("objective", "review", "behavior", "loading", "delivery", "post_loading"):
            result["stages"][stage] = "invalid_harness"
        result["uncovered"].append("invalid_harness")
    else:
        result["primary_eligible"] = True
    return result


def replacement_registry(root, rows):
    path = root / "replacements.json"
    if not path.exists():
        return {}
    data = read_json(path)
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ValueError("replacement registry entries must be a list")
    keys = {(v, row["id"]) for v in ("A", "B") for row in rows}
    planned_paths = {str(root / version / "trials" / trial_id) for version, trial_id in keys}
    links, targets = {}, set()
    for entry in entries:
        if (not isinstance(entry, dict) or (entry.get("version"), entry.get("id")) not in keys
                or type(entry.get("harness_revision")) is not int or entry["harness_revision"] < 2
                or not isinstance(entry.get("reason"), str) or not entry["reason"].strip()):
            raise ValueError("invalid replacement identity, revision or reason")
        paths = []
        for field in ("original_trial", "replacement_trial"):
            value = entry.get(field)
            if not isinstance(value, str):
                raise ValueError("replacement path required")
            candidate = Path(value)
            if (not candidate.is_absolute() or ".." in candidate.parts or candidate == root
                    or root not in candidate.parents or any(p.is_symlink() for p in (candidate, *candidate.parents))):
                raise ValueError("replacement path must remain inside the experiment without symlinks")
            paths.append(str(candidate))
        source, target = paths
        if source == target or source in links or target in targets or target in planned_paths:
            raise ValueError("ambiguous or repeated replacement edge")
        disposition = optional_json(Path(source) / "harness-disposition.json", [])
        if not disposition:
            raise ValueError("replacement lacks an explicit readable harness disposition")
        links[source] = {**entry, "original_trial": source, "replacement_trial": target}
        targets.add(target)
    # Every registered edge must be reachable from a planned original, with
    # a matching identity and strictly newer revisions. This also rejects cycles.
    visited = set()
    for version, trial_id in keys:
        source, revision = str(root / version / "trials" / trial_id), 1
        while source in links:
            entry = links[source]
            if source in visited or (entry["version"], entry["id"]) != (version, trial_id) or entry["harness_revision"] <= revision:
                raise ValueError("replacement chain identity, cycle or revision mismatch")
            visited.add(source); revision = entry["harness_revision"]; source = entry["replacement_trial"]
    if visited != set(links):
        raise ValueError("orphaned replacement edge")
    return links


def select_trials(root, rows):
    links = replacement_registry(root, rows)
    primary, history = [], []
    for version in ("A", "B"):
        for row in rows:
            source, chain = str(root / version / "trials" / row["id"]), []
            while True:
                record = trial_record(root, version, row, source)
                history.append(record)
                if source not in links:
                    break
                edge = links[source]
                chain.append(edge)
                source = edge["replacement_trial"]
            selected = {**record, "replacement_chain": chain,
                        "original_trial_path": str(root / version / "trials" / row["id"])}
            if chain and record["status"] != "invalid_harness" and not record["decision_complete"]:
                selected["uncovered"] = [*record["uncovered"], "replacement_not_complete"]
            primary.append(selected)
    return primary, history


def distribution(values):
    values = sorted(v for v in values if number(v))
    return {"observed": len(values), "sum": round(sum(values), 3) if values else None,
            "median": statistics.median(values) if values else None,
            "p95": values[max(0, math.ceil(.95 * len(values)) - 1)] if values else None}


def usage_totals(observations):
    return {"sessions": sum(r["session_count"] for r in observations),
                      "native_exchange_starts": sum(r["native_exchange_starts"] for r in observations),
                      "native_exchange_finishes": sum(r["native_exchange_finishes"] for r in observations),
                      "trials_with_metadata_gaps": sum(bool(r["gaps"]) for r in observations),
                      "tokens": {field: {"sum": sum(r["normalized_tokens"][field]["sum"] or 0 for r in observations)
                                  if any(r["normalized_tokens"][field]["sum"] is not None for r in observations) else None,
                                  "observed_exchanges": sum(r["normalized_tokens"][field]["observed_exchanges"] for r in observations),
                                  "total_finished_exchanges": sum(r["native_exchange_finishes"] for r in observations)}
                                 for field in TOKEN_FIELDS}}


def aggregate(rows, *, include_invalid_performance=False):
    metric_rows = rows if include_invalid_performance else [r for r in rows if r.get("primary_eligible", True)]
    usage = {role: usage_totals([r["performance"][role] for r in metric_rows]) for role in ("execute_usage", "review_usage")}
    return {"trials": len(rows), "unique_scenarios": len({(r["group"], r["scenario"]) for r in rows}),
            "status": dict(Counter(r["status"] for r in rows)),
            "lifecycle": dict(Counter(r["lifecycle"] for r in rows)),
            "assessment_complete": sum(r["assessment_complete"] for r in rows),
            "decision_complete": sum(r["decision_complete"] for r in rows),
            "known_fail_with_incomplete_assessment": sum(r["status"] == "fail" and not r["assessment_complete"] for r in rows),
            "decidable_fail_with_incomplete_assessment": sum(r["status"] == "fail" and r["decision_complete"] and not r["assessment_complete"] for r in rows),
            "stages": {stage: dict(Counter(r["stages"][stage] for r in rows))
                       for stage in ("execution", "objective", "review", "behavior", "loading", "delivery", "post_loading")},
            "trials_with_uncovered_evidence": sum(bool(r["uncovered"]) for r in rows),
            "collection_corrections": dict(Counter(r["collection_correction"]["status"] for r in rows)),
            "performance_eligible_trials": len(metric_rows),
            "usage": usage,
            "performance": {key: distribution(r["performance"][key] for r in metric_rows)
                            for key in ("execution_seconds", "review_seconds", "read_skill_resource_calls", "distinct_resource_count")}}


def attempt_accounting(rows):
    totals = aggregate(rows, include_invalid_performance=True)
    return {"registered_trial_directories": len(rows),
            "existing_trial_directories": sum(r["trial_directory_exists"] for r in rows),
            "execution_attempts_started": sum(r["execution_started"] for r in rows),
            "execution_process_receipts": sum(r["process_receipts"]["execute"] for r in rows),
            "executions_completed_successfully": sum(r["execution_completed_successfully"] for r in rows),
            "review_attempts_started": sum(r["review_started"] for r in rows),
            "review_process_receipts": sum(r["process_receipts"]["review"] for r in rows),
            "host_reviews_skipped": sum(r["review_process_status"] == "not_run_harness_invalid" for r in rows),
            "reviews_with_valid_evidence": sum(r["review_evidence_valid"] for r in rows),
            "usage": totals["usage"], "process_wall_time": {k: v for k, v in totals["performance"].items() if k.endswith("_seconds")},
            "monetary_cost": None, "cost_boundary": "observed normalized tokens and process time; no pricing supplied, so no currency estimate; missing usage is unknown, not zero"}


def calibration_summary(root):
    """Count concrete calibration process directories once, not label-report rows."""
    folder, gaps, labels, reports = root / "calibration", [], {}, []
    for name in ("calibration-result.json", "calibration-result-v2.json"):
        report = optional_json(root / name, gaps)
        if report:
            reports.append({"path": name, "status": report.get("status"), "prior_sample_note": report.get("prior_sample_note")})
            for case in report.get("cases", []):
                labels.setdefault(case.get("name"), []).append({"report": name,
                    **{k: case.get(k) for k in ("expected", "actual", "matched", "status")}})
    records = []
    for target in sorted(folder.iterdir()) if folder.is_dir() and not folder.is_symlink() else []:
        if target.is_symlink() or not target.is_dir():
            continue
        local_gaps = []
        result = optional_json(target / "result.json", local_gaps) or {}
        process = optional_json(target / "execution/process.json", local_gaps) or {}
        binding = optional_json(target / "binding.json", local_gaps) or {}
        usage = usage_metadata(target, "review", directory=target)
        started = bool(process) or usage["session_count"] > 0
        records.append({"name": target.name, "path": str(target), "review_process_started": started,
            "process_status": process.get("status", "not_started"), "recorded_status": result.get("status"),
            "recorded_verdict": result.get("verdict"), "label_history": labels.get(target.name, []),
            "duration_seconds": process.get("duration_seconds"), "usage": usage,
            "identity": {k: binding.get(k) for k in ("model_id", "profile_name", "profile_sha256")},
            "gaps": sorted(set(local_gaps))})
    return {"review_processes_started": sum(r["review_process_started"] for r in records),
            "process_statuses": dict(Counter(r["process_status"] for r in records)),
            "recorded_statuses": dict(Counter(r["recorded_status"] for r in records)),
            "usage": usage_totals([r["usage"] for r in records]),
            "process_wall_time": distribution(r["duration_seconds"] for r in records),
            "records": records, "label_reports": reports, "gaps": gaps,
            "boundary": "diagnostic reviews only, excluded from 84 planned trial outcomes; reused calibration labels/results do not create new invocations; MCP/registration canaries are not model reviews"}


def lease_summary(root):
    path = root / "icode-concurrency.jsonl"
    if not path.exists():
        return {"status": "unobserved", "observed_peak": None, "limit": 6, "unresolved": None, "gaps": []}
    active, peak, gaps, acquisitions = {}, 0, [], Counter()
    try:
        rows = [json.loads(line) for line in read_regular(path).splitlines() if line.strip()]
        for row in rows:
            slot = row["slot"]
            key = (row["owner_pid"], row["phase"], row["trial"])
            if type(slot) is not int or not 0 <= slot < 6:
                gaps.append("slot_outside_limit")
            if row["event"] == "acquired":
                if slot in active:
                    gaps.append("overlapping_slot")
                active[slot] = key; acquisitions[row["phase"]] += 1
                peak = max(peak, len(active))
            elif row["event"] == "released":
                if active.get(slot) != key:
                    gaps.append("unmatched_release")
                else:
                    del active[slot]
            else:
                gaps.append("unknown_lease_event")
    except (OSError, ValueError, KeyError, TypeError, EvalError):
        gaps.append("unreadable_lease_log")
    violation = peak > 6 or bool({"slot_outside_limit", "overlapping_slot"} & set(gaps))
    return {"status": "violation" if violation else "incomplete" if gaps else "within_limit", "observed_peak": peak, "limit": 6,
            "unresolved": len(active), "acquisitions_by_phase": dict(acquisitions), "gaps": sorted(set(gaps)),
            "boundary": "whole experiment lease ledger including calibration; unresolved entries are not proof of live processes"}


def compare(root):
    root = Path(root).resolve()
    plan = read_json(root / "plan.json")
    rows = plan["trials_per_version"]
    if plan["versions"] != ["A", "B"] or len({(r["group"], r["scenario"], r["repeat"]) for r in rows}) != len(rows):
        raise ValueError("unsupported versions or duplicate pair keys")
    if plan.get("executions") != len(rows) * len(plan["versions"]):
        raise ValueError("declared execution count differs from planned primary slots")
    if (len({r["id"] for r in rows}) != len(rows)
            or any(not isinstance(r["id"], str) or r["id"] in {"", ".", ".."}
                   or Path(r["id"]).name != r["id"] for r in rows)):
        raise ValueError("invalid trial ID")
    trials, history = select_trials(root, rows)
    calibration = calibration_summary(root)
    invalid = [r for r in history if r["status"] == "invalid_harness"]
    planned_paths = {str(root / version / "trials" / row["id"]) for version in plan["versions"] for row in rows}
    extras = [r for r in history if r["trial_path"] not in planned_paths]
    indexed = {(r["version"], r["group"], r["scenario"], r["repeat"]): r for r in trials}
    pairs = []
    for row in rows:
        a, b = [indexed[(v, row["group"], row["scenario"], row["repeat"])] for v in ("A", "B")]
        eligible = a["decision_complete"] and b["decision_complete"] and a["primary_eligible"] and b["primary_eligible"]
        state = ({("pass", "fail"): "new_regression", ("fail", "fail"): "existing_failure",
                  ("fail", "pass"): "improved", ("pass", "pass"): "stable_pass"}.get((a["status"], b["status"]), "undetermined")
                 if eligible else "undetermined")
        pairs.append({**row, "A": a["status"], "B": b["status"], "comparison": state,
                      "assessment_complete": {"A": a["assessment_complete"], "B": b["assessment_complete"]},
                      "decision_complete": {"A": a["decision_complete"], "B": b["decision_complete"]},
                      "lifecycle": {"A": a["lifecycle"], "B": b["lifecycle"]}, "pair_eligible": eligible,
                      "both_executions_completed": a["execution_completed_successfully"] and b["execution_completed_successfully"]
                          and a["primary_eligible"] and b["primary_eligible"],
                      "performance_delta_B_minus_A": {key: round(b["performance"][key] - a["performance"][key], 3)
                          if a["primary_eligible"] and b["primary_eligible"] and number(a["performance"][key]) and number(b["performance"][key]) else None
                          for key in ("execution_seconds", "review_seconds", "read_skill_resource_calls", "distinct_resource_count")}})
    scenarios = [{"group": group, "scenario": scenario,
                  "versions": {v: aggregate([r for r in trials if r["version"] == v and (r["group"], r["scenario"]) == (group, scenario)])
                               for v in plan["versions"]}}
                 for group, scenario in sorted({(r["group"], r["scenario"]) for r in rows})]
    # Aggregate uses status for trial counts; keep the all-repeats conclusion in
    # a separate field so an individual successful repeat never proves a case.
    for scenario in scenarios:
        for version, value in scenario["versions"].items():
            value["all_repeats_status"] = combine(r["status"] for r in trials if r["version"] == version
                and r["group"] == scenario["group"] and r["scenario"] == scenario["scenario"])
    return {"contract": "ci-modular-ab-analysis-v1", "generated_at": datetime.now(timezone.utc).isoformat(),
            "analyzer_sha256": digest(read_regular(Path(__file__))),
            "evidence_root": str(root), "plan_sha256": digest(read_regular(root / "plan.json")), "boundary": BOUNDARY,
            "expected": {"unique_scenarios": len({(r["group"], r["scenario"]) for r in rows}), "trials": len(trials),
                         "declared_executions": plan.get("executions")},
            "versions": {v: aggregate([r for r in trials if r["version"] == v]) for v in plan["versions"]},
            "groups": {v: {g: aggregate([r for r in trials if r["version"] == v and r["group"] == g])
                             for g in sorted({r["group"] for r in rows})} for v in plan["versions"]},
            "pair_counts": dict(Counter(p["comparison"] for p in pairs)), "pairs": pairs, "trials": trials,
            "harness_history": {"invalid_trials": invalid, "replacement_trials": extras,
                "all_attempts_accounting": attempt_accounting(history), "invalid_attempts_accounting": attempt_accounting(invalid),
                "extra_attempts_accounting": attempt_accounting(extras),
                "planned_primary_slots": len(trials), "registered_extra_slots": len(extras),
                "registered_total_slots": len(history),
                "boundary": "all original and registered replacement trial attempts retained; calibration is outside these trial counts and remains in the global lease ledger"},
            "calibration": calibration,
            "overall_model_cost": {
                "execution_attempts_started": sum(r["execution_started"] for r in history),
                "review_attempts_started": sum(r["review_started"] for r in history) + calibration["review_processes_started"],
                "calibration_review_processes": calibration["review_processes_started"],
                "execute_usage": usage_totals([r["performance"]["execute_usage"] for r in history]),
                "review_usage": usage_totals([r["performance"]["review_usage"] for r in history] + [r["usage"] for r in calibration["records"]]),
                "execution_wall_time": distribution(r["performance"]["execution_seconds"] for r in history),
                "review_wall_time": distribution([r["performance"]["review_seconds"] for r in history] + [r["duration_seconds"] for r in calibration["records"]]),
                "monetary_cost": None,
                "boundary": "original, replacement and calibration costs, without double-counting reused label reports; process starts are not provider HTTP request counts"},
            "scenarios": scenarios,
            "unique_scenario_status": {v: dict(Counter(s["versions"][v]["all_repeats_status"] for s in scenarios)) for v in plan["versions"]},
            "concurrency": lease_summary(root),
            "interpretation": ["阶段统计可重叠；behavior含确定性门槛，不与loading/delivery构成互斥分类。",
                "post_loading以v6/v7共同既有规则统一补审加载观察缺口；硬失败覆盖最终pass，但原objective及DeepSeek结论原样保留，绝不按版本选择规则。",
                "collection_correction仅将冻结packet中已完整提供的资源键登记到对应E证据；保留原收集错误、引用、模型statuses/reasons/overall，不重评或改写语义裁决。",
                "status保留已证实的硬/语义失败，即使其他评审证据仍缺失；lifecycle独立表示进度，不把未闭合记录直接称为仍运行。",
                "assessment_complete表示全部检查完整；decision_complete表示最终结果有充分依据。pass必须全评估完整；fail可在各阶段回执有效且有确定违反时判定，即使仍有未覆盖项或缺失必交产物。",
                "配对只要求两侧decision_complete；缺失/无效评审、执行未结束或原始证据读取损坏仍未判定，不把业务交付不完整混作判定证据不完整。",
                "usage缺失保留null及观测分母；output_total包含reasoning，不相加；未换算费用。",
                "唯一场景通过要求所有计划重复均通过；任一失败记失败，其余为未完成。",
                "invalid_harness保留原裁决但排除主比较；显式补测只替换原计划槽位，全部额外尝试另记实际次数和用量。",
                "主比较性能也排除invalid_harness；实际尝试用量账本包含这些开销，两者分母不同。",
                "wall time统计包括已记录的失败/超时进程；配对标注双方执行是否完整，不用未完成样本证明性能改善。",
                "session数、native exchange数和真实供应商HTTP请求数不等价；隐藏SDK重试未审计。",
                "运行期间生成的是非原子进度快照；结束后重跑本工具可更新，不重评模型。"]}


def markdown(result):
    lines = ["---", "ai_generated: true", "reviewed: false", "---", "# Skill 模块化 A/B 比较", "", result["boundary"], "",
             "生成时间：" + result["generated_at"], "",
             f"计划覆盖 {result['expected']['unique_scenarios']} 个唯一场景、{result['expected']['trials']} 个试次；未完成试次仍保留在分母中。", "",
             "| 版本 | 主比较槽位 | 通过 | 失败 | 未完成 | 宿主缺陷未补齐 | 存在未覆盖证据 |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for version, stats in result["versions"].items():
        c = stats["status"]
        lines.append(f"| {version} | {stats['trials']} | {c.get('pass', 0)} | {c.get('fail', 0)} | {c.get('incomplete', 0)} | {c.get('invalid_harness', 0)} | {stats['trials_with_uncovered_evidence']} |")
    lines += ["", "失败计数保留已经证实的违反；配对按判定依据是否充分，不按全部业务检查是否通过。"
              + "；".join(f"{v}：全评估完整 {s['assessment_complete']}，结果可判定 {s['decision_complete']}，评估未全但失败已可判定 {s['decidable_fail_with_incomplete_assessment']}"
                         for v, s in result["versions"].items()) + "。", "",
              "| 版本 | 进度 / 证据状态 | 试次 |", "| --- | --- | ---: |"]
    for version, stats in result["versions"].items():
        for state, count in sorted(stats["lifecycle"].items()):
            lines.append(f"| {version} | {LIFECYCLE_LABELS.get(state, state)} | {count} |")
    history = result["harness_history"]
    totals = history["all_attempts_accounting"]
    lines += ["", f"计划主比较仍为 {history['planned_primary_slots']} 槽位；登记补测 {history['registered_extra_slots']} 个，总登记目录槽位 {history['registered_total_slots']} 个（已存在目录 {totals['existing_trial_directories']} 个）。"
              f"实际已启动执行 {totals['execution_attempts_started']} 次、评审 {totals['review_attempts_started']} 次（不包含独立校准）。",
              "宿主缺陷试次保留原始裁决和实际用量，标记 invalid_harness，不作为 Skill 通过或失败；只使用显式登记的补测进入主比较，补测未完成仍未判定。", "",
              "| 用量范围 | 已启动执行 | 已启动评审 | 执行秒数 | 评审秒数 |", "| --- | ---: | ---: | ---: | ---: |"]
    for key, label in (("all_attempts_accounting", "全部实际尝试"), ("invalid_attempts_accounting", "宿主缺陷尝试（含于全部）"),
                       ("extra_attempts_accounting", "登记补测（含于全部）")):
        value = history[key]
        lines.append(f"| {label} | {value['execution_attempts_started']} | {value['review_attempts_started']} | "
                     f"{value['process_wall_time']['execution_seconds']['sum']} | {value['process_wall_time']['review_seconds']['sum']} |")
    lines += ["", "| 实际用量范围 | 角色 | 输入 token | 输出 token（含 reasoning） |", "| --- | --- | ---: | ---: |"]
    for key, label in (("all_attempts_accounting", "全部"), ("invalid_attempts_accounting", "宿主缺陷"), ("extra_attempts_accounting", "补测")):
        for role, usage in history[key]["usage"].items():
            def measured(field):
                value = usage["tokens"][field]
                return f"{value['sum']}（{value['observed_exchanges']}/{value['total_finished_exchanges']} exchanges）"
            lines.append(f"| {label} | {role} | {measured('input_total')} | {measured('output_total')} |")
    lines += ["", "未提供计费单价，金额保持未知；token缺失保持未知，不按零成本计。"]
    calibration = result["calibration"]
    lines += ["", f"独立校准：实际启动 {calibration['review_processes_started']} 个评审进程；不计正式84试次通过率。"
              "同一结果被新版校准报告复用时只计一次；原标注有歧义的 missing-evidence 及替换样本均保留。",
              "MCP和资源注册canary不是模型评审。仅有宿主skip记录、无评审启动或模型session的正式试次不计评审启动。", "",
              "| 校准目录 | 进程状态 | 保留的原评审结论 | 秒数 | 输入 token | 输出 token |", "| --- | --- | --- | ---: | ---: | ---: |"]
    for row in calibration["records"]:
        tokens = row["usage"]["normalized_tokens"]
        lines.append(f"| {row['name']} | {row['process_status']} | {row['recorded_verdict']} | {row['duration_seconds']} | "
                     f"{tokens['input_total']['sum']} | {tokens['output_total']['sum']} |")
    overall = result["overall_model_cost"]
    lines += ["", f"含校准的全部实际尝试：执行 {overall['execution_attempts_started']} 次，评审 {overall['review_attempts_started']} 次。"
              "完整token账本见summary.json的overall_model_cost；原试次、补测、校准分别保留。"]
    if history["invalid_trials"]:
        lines += ["", "| 已排除试次目录 | 原综合结论 | 原评审结论 | 宿主缺陷分类 |", "| --- | --- | --- | --- |"]
        for row in history["invalid_trials"]:
            relative = Path(row["trial_path"]).relative_to(Path(result["evidence_root"]))
            label = (row.get("harness_disposition") or {}).get("classification", "unreadable_disposition")
            lines.append(f"| {relative} | {row['recorded_status']} | {row['recorded_review_verdict']} | {label} |")
    labels = {"existing_failure": "既有失败", "new_regression": "新增回归", "improved": "改善", "stable_pass": "稳定通过", "undetermined": "未判定"}
    lines += ["", "唯一场景（所有重复合并）结论：" + "；".join(f"{v}: {json.dumps(counts, ensure_ascii=False)}" for v, counts in result["unique_scenario_status"].items()) + "。",
              "", "配对分类：" + "；".join(f"{label} {result['pair_counts'].get(key, 0)}" for key, label in labels.items()) + "。", "",
              "| 场景 / 重复 | A | B | 两侧判定依据充分 | 配对结论 |", "| --- | --- | --- | --- | --- |"]
    for pair in result["pairs"]:
        lines.append(f"| {pair['group']}/{pair['scenario']} / {pair['repeat']} | {pair['A']} | {pair['B']} | {'是' if pair['pair_eligible'] else '否'} | {labels[pair['comparison']]} |")
    lines += ["", "| 版本 | 层次 | 状态计数 |", "| --- | --- | --- |"]
    for version, stats in result["versions"].items():
        for stage, counts in stats["stages"].items():
            lines.append(f"| {version} | {stage} | " + "，".join(f"{k}={v}" for k, v in sorted(counts.items())) + " |")
    corrections = [r for r in result["trials"] if r["collection_correction"]["status"] != "not_applicable"]
    if corrections:
        lines += ["", "引用登记校正只映射冻结包中已完整提供的资源键，保留模型原判断和理由；原收集错误仍可追溯，不重跑评审。", "",
                  "| 主试次 | 原收集状态 | 校正状态 | 原模型意见 | 有效收集意见 | 映射 |", "| --- | --- | --- | --- | --- | --- |"]
        for row in corrections:
            correction = row["collection_correction"]
            mapping = ", ".join(f"{key} → {value}" for key, value in correction.get("mapping", {}).items())
            lines.append(f"| {row['version']}/{row['id']} | {row['original_review_status']} | {correction['status']} | "
                         f"{row['original_model_opinion'] or 'unknown'} | {row['effective_review']['verdict']} | {mapping} |")
    post_failures = [r for r in result["trials"] if r.get("primary_eligible", True)
                     and r["post_loading_audit"]["status"] == "fail"]
    if post_failures:
        lines += ["", "统一加载后审计保留原模型评审；以下硬事实适用于两版共同合同，最终通过不能覆盖这些违规。", "",
                  "| 主试次 | 原模型评审 | 规则 | 动作开始seq | 指南首次完整返回seq |", "| --- | --- | --- | ---: | ---: |"]
        for row in post_failures:
            seen = set()
            for violation in row["post_loading_audit"].get("violations", []):
                rule = violation["rule"]
                if rule in seen:
                    continue
                seen.add(rule)
                lines.append(f"| {row['version']}/{row['id']} | {row['original_model_opinion'] or 'unknown'} | {rule} | "
                             f"{violation['action_seq']} | {violation.get('first_observed_complete_result_seq')} |")
    lines += ["", "| 版本 | 性能指标 | 观测数 | 中位数 | P95 |", "| --- | --- | ---: | ---: | ---: |"]
    for version, stats in result["versions"].items():
        for key, values in stats["performance"].items():
            lines.append(f"| {version} | {key} | {values['observed']} | {values['median']} | {values['p95']} |")
    lines += ["", "| 版本 | 角色 | Session | Native exchange 完成数 | 输入 token | 输出 token（含 reasoning） |", "| --- | --- | ---: | ---: | ---: | ---: |"]
    for version, stats in result["versions"].items():
        for role, usage in stats["usage"].items():
            def token(field):
                value = usage["tokens"][field]
                return f"{value['sum']}（{value['observed_exchanges']}/{value['total_finished_exchanges']} exchanges）"
            lines.append(f"| {version} | {role} | {usage['sessions']} | {usage['native_exchange_finishes']} | {token('input_total')} | {token('output_total')} |")
    lease = result["concurrency"]
    lines += ["", f"整体租约账本：状态 `{lease['status']}`，观测峰值 {lease['observed_peak']} / 6，未闭合记录 {lease['unresolved']}。", "",
              "模型请求名、返回名、profile标识及 normalized usage 的逐试次元数据保存在 [summary.json](summary.json)。", ""]
    lines += ["- " + text for text in result["interpretation"]]
    return "\n".join(lines) + "\n"


def write_outputs(root, result):
    folder = Path(root) / "analysis"
    if folder.is_symlink():
        raise EvalError("analysis directory must not be a symlink")
    folder.mkdir(exist_ok=True)
    report = folder / "report.md"
    if report.exists():
        read_regular(report)  # Check frontmatter before replacing derived Markdown.
    replace_json(folder / "summary.json", result)
    fd, name = tempfile.mkstemp(prefix=".report-", dir=folder)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(markdown(result))
        os.replace(name, report)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    result = compare(args.root)
    write_outputs(args.root, result)
    print(json.dumps({"expected": result["expected"], "pairs": result["pair_counts"],
                      "output": str(args.root / "analysis")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
