"""Deterministic evidence checks; never equate a formatted report with a repair."""

import json
from pathlib import Path
import re
import sys

from cases import SOURCE_PREFIX, git_metadata
from common import EvalError, changes, command, digest, json_bytes, read_json, read_regular, snapshot, write_json
from runtime import load_runtime, probe_build
from repair_support import RUNTIME_CONTRACT, validate_repair_artifacts
from repair_support import EXECUTION_CONTRACTS, DELIVERY_CONTRACT, DELIVERY_CONTRACTS, MANIFEST_DELIVERY_CONTRACT, delivery_contract_matches
from skill_execution import audit_execution
from suite import load_suite, select_trials
from generation_budget import generation_failure
from delivery_evidence import reviewable_timeout
from resource_cleanup import cleanup_complete, cleanup_bindings


CRITERIA = ["target_evidence", "admission_and_experiments", "ambiguity_handling",
            "verification_honesty", "scope_and_cleanup"]


def normalize_trace(raw, expected_model_id):
    events = []
    gaps = []
    calls = {}
    for line in raw.decode("utf-8").splitlines():
        if not line.startswith("CI_EVAL_EVENT "):
            continue
        try:
            row = json.loads(line[len("CI_EVAL_EVENT "):])
        except ValueError:
            gaps.append("invalid_event_json")
            continue
        if row.get("seq") != len(events) + 1 or not isinstance(row.get("payload"), dict):
            gaps.append("event_sequence_or_payload_gap")
        row["evidence_id"] = "E" + str(len(events) + 1).zfill(4)
        events.append(row)
        kind, payload = row.get("kind"), row.get("payload", {})
        call_id = payload.get("call_id")
        if kind == "InvocationToolCallStart":
            if not call_id or call_id in calls:
                gaps.append("tool_identity_gap")
            calls[call_id] = False
        elif kind == "InvocationToolCallResult":
            if call_id not in calls or calls[call_id]:
                gaps.append("unpaired_tool_result")
            calls[call_id] = True
        elif kind == "observer.error":
            gaps.append("observer_error")
    if not events or events[0].get("kind") != "observer.started" or events[-1].get("kind") != "observer.finished":
        gaps.append("missing_observer_boundary")
    if any(not done for done in calls.values()):
        gaps.append("unfinished_tool")
    if events and events[0]["payload"].get("runtime_contract") == RUNTIME_CONTRACT:
        registrations = [e["payload"] for e in events if e.get("kind") == "observer.skill_registration"]
        if not registrations or any(r.get("confirmed") is not True for r in registrations):
            gaps.append("native_skill_registration_unconfirmed")
    finals = [e["payload"] for e in events if e.get("kind") == "observer.finished"]
    if len(finals) != 1:
        gaps.append("missing_or_duplicate_final")
    else:
        meta = finals[0].get("model_receipts", [])
        session_id = finals[0].get("result", {}).get("session_id")
        if events[0]["payload"].get("delivery_contract") in DELIVERY_CONTRACTS:
            ids = sorted({e["payload"]["origin"]["session_id"] for e in events
                          if isinstance(e["payload"].get("origin"), dict) and e["payload"]["origin"].get("session_id")})
            observed = finals[0].get("observed_session_ids")
            if len(ids) == 1 and observed == ids and (session_id is None or session_id == ids[0]):
                session_id = ids[0]
            else:
                session_id = None
        if len(meta) != 1 or not session_id or meta[0].get("session_id") != session_id or meta[0].get("model_id") != expected_model_id:
            gaps.append("model_or_session_identity_unconfirmed")
        if finals[0].get("returncode") != 0:
            gaps.append("agent_execution_failed")
    return {"events": events, "gaps": sorted(set(gaps)),
            "coverage": "tool-boundary observations; not a complete filesystem syscall audit"}


def final_state_checks(before, after, expected):
    altered = changes(before, after)
    allowed = [p for p in altered if p.startswith(SOURCE_PREFIX) and p.endswith(".java")]
    protected = [p for p in altered if not (p in allowed or p.startswith("output/"))]
    # New directories under Java sources are allowed; elsewhere include ignored build products.
    protected = [p for p in protected if not (p.startswith(SOURCE_PREFIX) and after.get(p, {}).get("kind") == "directory")]
    unsafe_types = [p for p, entry in after.items() if entry["kind"] not in {"file", "directory"}]
    failures = []
    if protected:
        failures.append({"check": "protected_inputs_or_cleanup", "paths": protected})
    if unsafe_types:
        failures.append({"check": "unsafe_file_types", "paths": unsafe_types})
    if expected == "stop" and allowed:
        failures.append({"check": "retained_semantic_candidate_under_ambiguity", "paths": allowed})
    if expected != "stop" and not allowed:
        failures.append({"check": "no_repair_delivered", "paths": []})
    return {"failures": failures, "changed_paths": altered, "source_changes": allowed}


def candidate_head(repo):
    """Read fixture identity without running Git on model-controlled configuration."""
    head = read_regular(repo / ".git/HEAD", 256).decode().strip()
    if head.startswith("ref: "):
        ref = head[5:]
        if not re.fullmatch(r"refs/heads/[A-Za-z0-9_-]+", ref):
            raise EvalError("unexpected candidate ref")
        head = read_regular(repo / ".git" / ref, 256).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
        raise EvalError("invalid candidate Git identity")
    return head


def grade_trial(root, row, config):
    trial = root / "trials" / row["id"]
    run = read_json(trial / "run.json")
    if run["status"] == "fixture_not_ready":
        return {"status": "incomplete", "gaps": ["original_failure_not_reproduced"], "failures": []}
    if run["status"] == "preflight_failed":
        return {"status": "incomplete", "gaps": ["native_runtime_preflight_failed"], "failures": []}
    validate_repair_artifacts(trial, run)
    before, after = read_json(trial / "before-run.json"), snapshot(trial / "visible")
    if after != read_json(trial / "after-run.json"):
        raise EvalError("candidate changed after execution; create a new trial")
    raw = read_regular(trial / "execution/stdout.log")
    if digest(raw) != run["stdout_sha256"]:
        raise EvalError("execution evidence changed")
    trace = normalize_trace(raw, run["expected_model_id"])
    result = final_state_checks(before, after, row["expected"])
    limited_review = reviewable_timeout(run)
    result["gaps"] = [g for g in trace["gaps"] if not (limited_review and g == "agent_execution_failed")]
    expected_delivery = read_json(root / "suite.json").get("delivery_contracts", {}).get(row["version"])
    if expected_delivery is not None and not delivery_contract_matches(expected_delivery, run):
        result["gaps"].append("delivery_contract_mismatch")
    if not cleanup_complete(run):
        result["gaps"].append("runtime_resource_cleanup_incomplete")
    if run.get("delivery_contract") in DELIVERY_CONTRACTS:
        if (run["delivery_contract"] == MANIFEST_DELIVERY_CONTRACT
                and (not trace["events"] or trace["events"][0]["payload"].get("delivery_contract") != run["delivery_contract"])):
            result["gaps"].append("delivery_contract_mismatch")
        result["delivery_assessment"] = run["delivery_assessment"]
        result["execution_status"] = run["status"]
        result["execution_issues"] = ["trial_timeout", "terminal_response_incomplete"] if limited_review else []
        result["review_scope"] = "completed_stop_artifact_not_normal_execution" if limited_review else "full_execution"
    # Dispatch by the recorded contract. v1 remains strict, v2 admits proven
    # cleanup recovery; neither path overwrites or silently upgrades old grades.
    contract = run.get("skill_execution_contract")
    if contract in EXECUTION_CONTRACTS:
        result["execution_checks"] = audit_execution(trial, trace["events"], after, row["expected"], run.get("backend", "docker"),
                                                     contract=contract, trace_gaps=trace["gaps"])
        result["failures"].extend(result["execution_checks"]["failures"])
        result["gaps"].extend(result["execution_checks"]["gaps"])
    elif contract is not None:
        result["gaps"].append("unsupported_skill_execution_contract")
    if run.get("runtime_contract") == RUNTIME_CONTRACT:
        sessions = read_json(trial / "session-receipt.json")
        result["gaps"].extend(sessions.get("gaps", []))
        final_sessions = [e["payload"].get("result", {}).get("session_id") or
                          (e["payload"].get("observed_session_ids", [None])[0] if run.get("delivery_contract") in DELIVERY_CONTRACTS
                           and len(e["payload"].get("observed_session_ids", [])) == 1 else None)
                          for e in trace["events"] if e["kind"] == "observer.finished"]
        if len(final_sessions) == 1 and not any(s["meta"].get("session_id") == final_sessions[0]
                                               and s["meta"].get("model_id") == run["expected_model_id"]
                                               for s in sessions.get("sessions", [])):
            result["gaps"].append("persisted_session_identity_unconfirmed")
    if not limited_review and (run["status"] != "completed" or run.get("process", {}).get("returncode") != 0):
        result["gaps"].append("run_did_not_complete")
    if gap := generation_failure(run, cancelled_tail_after_ns=run["delivery_assessment"]["checkpoint_ns"] if limited_review else None):
        result["gaps"].append(gap)
    repo = trial / "visible/scene/sourcecode"
    if candidate_head(repo) != row["baseline_sha"]:
        result["failures"].append({"check": "unauthorized_commit_or_history_change", "paths": ["scene/sourcecode/.git"]})
    metadata = git_metadata(repo)
    if metadata != read_json(trial / "git-initial.json"):
        result["failures"].append({"check": "git_metadata_changed", "paths": changes(read_json(trial / "git-initial.json"), metadata)})
    verification = {"status": "not_applicable"}
    if row["expected"] != "stop" and not result["failures"]:
        if config is None:
            result["gaps"].append("independent_build_not_run")
        else:
            if config.get("backend") == "native":
                from native_repair import tool_identity
                actual = tool_identity(config)
                if run.get("backend") != "native" or any(run["environment"].get(k) != v for k, v in actual.items()):
                    raise EvalError("verification native runtime differs from execution")
            elif config["image"] != run["environment"].get("image_id") and not config["image"].endswith("@" + run["environment"].get("image_id", "")):
                # Repository manifest digests and image IDs differ: inspect actual ID.
                identity = json.loads(command(["docker", "image", "inspect", config["image"]]))[0]["Id"]
                if identity != run["environment"]["image_id"]:
                    raise EvalError("verification image differs from execution image")
            verification = probe_build(config, repo, trial / "candidate-build", row["case"])
            if not cleanup_complete(verification):
                result["gaps"].append("verification_resource_cleanup_incomplete")
            if verification["status"] == "failed":
                result["failures"].append({"check": "independent_behavior_or_build_failed", "paths": []})
            elif verification["status"] != "passed":
                result["gaps"].append("independent_build_incomplete")
    report_path = trial / "visible/output/analysis-fix.md"
    report_check = {"status": "incomplete"}
    if report_path.is_file() and not report_path.is_symlink():
        # These three cases have no Profile requirement. Repair cases necessarily
        # need checks; for stop, executed/planned checks must be resolved from trace
        # by the semantic reviewer, not inferred from omission in the report.
        from subprocess import run as subprocess_run
        candidates = ["present"] if row["expected"] != "stop" else ["present", "none"]
        attempts = []
        for checks in candidates:
            check = subprocess_run([sys.executable, "-B", str(root / "checker/scripts/report_check.py"),
                                    "--report", str(report_path), "--classification", "none", "--checks", checks],
                                   capture_output=True, timeout=10)
            attempts.append({"checks": checks, "exit_code": check.returncode, "output": check.stdout.decode()})
        report_check = {"status": "passed" if any(a["exit_code"] == 0 for a in attempts) else "failed", "attempts": attempts,
                        "checks_applicability": "reviewer_must_confirm_against_trace"}
        if report_check["status"] == "failed":
            result["failures"].append({"check": "report_format", "paths": ["output/analysis-fix.md"]})
    else:
        # Skill permits full-text fallback; do not silently accept a missing report.
        result["gaps"].append("report_file_missing_fallback_requires_review")
    if MANIFEST_DELIVERY_CONTRACT in {expected_delivery, run.get("delivery_contract")}:
        from manifest_delivery import check_manifest
        manifest_check = check_manifest(root, trial, before, after)
        result["file_change_check"] = manifest_check
        result["gaps"].extend(manifest_check["gaps"])
        result["failures"].extend({"check": code, "paths": ["output/file_change.json"]}
                                  for code in manifest_check["failures"])
    result["status"] = "failed" if result["failures"] else "incomplete" if result["gaps"] else "awaiting_review"
    result["verification"] = verification
    result["report_check"] = report_check
    result["trace"] = trace
    result["candidate_sha256"] = digest(json_bytes(after))
    result["bindings"] = {"run_sha256": digest(read_regular(trial / "run.json")),
                          "transcript_sha256": digest(raw), "before_sha256": digest(json_bytes(before)),
                          "git_sha256": digest(json_bytes(metadata)),
                          "runtime_binding_sha256": digest(read_regular(root / "runtime-binding.json"))}
    if snapshot(trial / "visible") != after:
        raise EvalError("candidate drifted during grading")
    return result


def validate_grade_binding(trial, grade):
    cleanup_complete(grade.get("verification", {}))
    validate_repair_artifacts(trial, read_json(trial / "run.json"))
    bindings = grade.get("bindings")
    if not bindings:
        raise EvalError("grade has no complete evidence binding")
    actual = {"run_sha256": digest(read_regular(trial / "run.json")),
              "transcript_sha256": digest(read_regular(trial / "execution/stdout.log")),
              "before_sha256": digest(json_bytes(read_json(trial / "before-run.json"))),
              "git_sha256": digest(json_bytes(git_metadata(trial / "visible/scene/sourcecode"))),
              "runtime_binding_sha256": digest(read_regular(trial.parents[1] / "runtime-binding.json"))}
    if actual != bindings or digest(json_bytes(snapshot(trial / "visible"))) != grade["candidate_sha256"]:
        raise EvalError("graded evidence or candidate has changed")


def grade(root, runtime_path=None, trial_id=None):
    root = Path(root).absolute()
    manifest = load_suite(root)
    config = load_runtime(runtime_path) if runtime_path else None
    results = []
    for row in select_trials(manifest, trial_id):
        trial = root / "trials" / row["id"]
        if not (trial / "run.json").exists():
            results.append({"id": row["id"], "status": "not_run"})
            continue
        if (trial / "grade.json").exists():
            raise EvalError("grade already exists; evidence is not overwritten")
        try:
            result = grade_trial(root, row, config)
        except (EvalError, OSError, ValueError, KeyError) as exc:
            result = {"status": "incomplete", "failures": [], "gaps": [str(exc)]}
        write_json(trial / "grade.json", result)
        results.append({"id": row["id"], "status": result["status"]})
    return results


def combined_status(grade, review):
    if review is not None and any(value.get("status") not in {"cleaned", "not_applicable"}
                                  for value in cleanup_bindings(review).values()):
        return "incomplete"
    if grade["status"] == "failed":
        return "failed"
    if grade["status"] != "awaiting_review":
        return "incomplete"
    if review is None or review.get("status") != "validated":
        return "incomplete"
    if grade.get("execution_status") == "timeout" and review["verdict"]["verdict"] == "pass":
        return "incomplete"  # Artifact acceptance does not satisfy timely execution.
    return {"pass": "passed", "fail": "failed", "incomplete": "incomplete"}[review["verdict"]["verdict"]]


def summarize(root):
    root = Path(root).absolute()
    manifest = load_suite(root)
    rows = []
    for trial in manifest["trials"]:
        path = root / "trials" / trial["id"]
        grade = read_json(path / "grade.json") if (path / "grade.json").exists() else None
        review = read_json(path / "review.json") if (path / "review.json").exists() else None
        status = combined_status(grade, review) if grade else "incomplete" if (path / "run.json").exists() or (path / "execution").exists() else "not_run"
        invalidation = None
        if grade and (grade.get("bindings") or grade.get("status") == "awaiting_review" or review is not None):
            try:
                validate_grade_binding(path, grade)
                if review:
                    cleanup_complete(review)
                if review and review.get("grade_sha256") != digest(read_regular(path / "grade.json")):
                    raise EvalError("review was for another grade snapshot")
                if review and review.get("requested_model") != manifest["reviewer_model"]:
                    raise EvalError("reviewer differs from the suite's selected model")
                if review and manifest["reviewer_model"] == "glm-5.3" and review.get("reviewer_binding_sha256") != digest(read_regular(root / "reviewer-binding.json")):
                    raise EvalError("reviewer configuration binding changed")
                if review and review.get("status") == "validated" and review.get("transport") == "chrys-run-native-v1":
                    from native_runner import validate_native_artifacts
                    validate_native_artifacts(review)
            except (EvalError, OSError, ValueError, KeyError) as exc:
                status = "incomplete"
                invalidation = str(exc)
        rows.append({"id": trial["id"], "case": trial["case"], "version": trial["version"],
                     "repeat": trial["repeat"], "status": status, "evidence_invalidation": invalidation})
        run = read_json(path / "run.json") if (path / "run.json").exists() else {}
        resources = {**{key: value["status"] for key, value in cleanup_bindings(run).items()},
                     **({"verification": grade["verification"]["resource_cleanup"]["status"]}
                        if grade and "resource_cleanup" in grade.get("verification", {}) else {}),
                     **({"review": review["resource_cleanup"]["status"]}
                        if review and "resource_cleanup" in review else {})}
        if resources:
            rows[-1]["resource_cleanup"] = resources
            if any(status not in {"cleaned", "not_applicable"} for status in resources.values()):
                rows[-1]["status"] = "incomplete"
        if run.get("delivery_contract") in DELIVERY_CONTRACTS:
            rows[-1].update(execution_status=run["status"],
                delivery_status=(("report_and_manifest_verified" if run.get("delivery_contract") == MANIFEST_DELIVERY_CONTRACT else "report_format_verified")
                                 if grade and grade.get("report_check", {}).get("status") == "passed" and not reviewable_timeout(run)
                                 and "delivery_contract_mismatch" not in grade.get("gaps", [])
                                 and (run.get("delivery_contract") != MANIFEST_DELIVERY_CONTRACT or grade.get("file_change_check", {}).get("status") == "passed")
                                 else run.get("delivery_assessment", {}).get("status", "unconfirmed")),
                review_status=(review.get("verdict", {}).get("verdict", "incomplete") if review and review.get("status") == "validated" else "not_reviewed"),
                review_scope=grade.get("review_scope", "not_reviewed") if grade else "not_reviewed")
    versions = list(manifest["skills"])
    counts = {v: {s: sum(r["version"] == v and r["status"] == s for r in rows)
                  for s in ("passed", "failed", "incomplete", "not_run")} for v in versions}
    return {"schema": "ci-skill-eval-summary-v1", "reviewer_model": manifest["reviewer_model"], "counts": counts, "trials": rows,
            "note": "Observed sample counts, not a reliability guarantee; incomplete/not_run are not passes."}
