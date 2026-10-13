"""Serial suite orchestration: immutable attempts, safe resume, bounded spend.

One entrypoint owns the whole suite, not a shell loop over handpicked cases.
Completed evidence is checked on resume; uncertain attempts are never replayed.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone

from common import (BudgetExhausted, EvalError, digest, json_bytes, read_json, read_regular,
                    replace_json, wall_budget, write_json)
from cases import CASES
from grading import grade, summarize, validate_grade_binding
from native_runner import load_native, native_identity, native_preflight, native_review
from runtime import docker_probe, runtime_probe, load_runtime, run_trials
from suite import ROOT, prepare, load_suite
from execution_outcome import classify_execution, classify_grade, failure
from delivery_evidence import reviewable_timeout


def load_config(path):
    path = Path(path).absolute()
    value = read_json(path)
    required = {"output_root", "runtime", "reviewer"}
    optional = {"backend", "docker_context", "max_agent_invocations", "max_wall_seconds", "stop_after_transport_errors"}
    if not isinstance(value, dict) or required - value.keys() or value.keys() - required - optional:
        raise EvalError("batch config requires output_root/runtime/docker_context/reviewer; unknown fields are rejected")
    value = dict(value)
    value.setdefault("backend", "native")
    if value["backend"] not in {"native", "docker"}:
        raise EvalError("unknown batch backend")
    for field in ("output_root", "runtime"):
        value[field] = str((path.parent / value[field]).absolute())
    reviewer = dict(value["reviewer"])
    for field in ("chrys", "python", "profile"):
        reviewer[field] = str((path.parent / reviewer[field]).absolute())
    value["reviewer"] = load_native(reviewer)
    if value["backend"] == "docker" and not re.fullmatch(r"[A-Za-z0-9_.-]+", value.get("docker_context", "")):
        raise EvalError("invalid Docker context")
    for key, default, cap in (("max_agent_invocations", 36, 200), ("max_wall_seconds", 14400, 86400),
                               ("stop_after_transport_errors", 2, 10)):
        # Legacy threshold remains parseable for local configs, but the typed
        # failure policy below supersedes counting arbitrary nonzero exits.
        value.setdefault(key, default)
        if type(value[key]) is not int or not 1 <= value[key] <= cap:
            raise EvalError("invalid batch budget: " + key)
    return value


def engine_digest():
    paths = sorted([*ROOT.glob("*.py"), *ROOT.glob("environment/*.py")])
    return digest(json_bytes({p.relative_to(ROOT).as_posix(): digest(read_regular(p)) for p in paths}))


def make_plan(config, candidate, mode="single", preset="smoke", baseline_ref="HEAD", case_ids=None):
    if mode not in {"single", "compare"} or preset not in {"smoke", "regression"}:
        raise EvalError("unknown mode or preset")
    cases = list(CASES) if case_ids is None else list(case_ids)
    if not cases or len(cases) != len(set(cases)) or any(c not in CASES for c in cases):
        raise EvalError("unknown, duplicate or empty case selection")
    repeats = 1 if preset == "smoke" else 3
    count = len(cases) * repeats * (2 if mode == "compare" else 1)
    return {"mode": mode, "preset": preset, "backend": config.get("backend", "docker"), "candidate": str(Path(candidate).absolute()),
            "baseline_ref": baseline_ref if mode == "compare" else None, "cases": cases, "repeats": repeats,
            "trial_count": count, "maximum_agent_invocations": count * 2,
            "budget_agent_invocations": config["max_agent_invocations"],
            "budget_wall_seconds": config["max_wall_seconds"],
            "failure_policy": "typed-v1: isolated case budgets continue; unknown/shared/safety failures stop",
            "legacy_transport_threshold": "accepted for config compatibility; no longer used to classify nonzero exits",
            "reviewer": "Chrys native GLM 5.3", "models_called": False}


@contextmanager
def run_lock(root):
    path = Path(root) / ".batch.lock"
    if path.is_symlink():
        raise EvalError("symlink lock refused")
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise EvalError("this suite already has an active coordinator") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def docker_context(name):
    old = os.environ.get("DOCKER_CONTEXT")
    os.environ["DOCKER_CONTEXT"] = name
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("DOCKER_CONTEXT", None)
        else:
            os.environ["DOCKER_CONTEXT"] = old


def new_run(config, plan):
    root = Path(config["output_root"]) / ("run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:10])
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise EvalError("output path contains symlink")
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    prepare(root / "suite", plan["candidate"], baseline_ref=plan["baseline_ref"] or "HEAD",
            repeats=plan["repeats"], mode=plan["mode"], case_ids=plan["cases"], backend=plan.get("backend", "docker"))
    write_json(root / "batch.json", {"format": "ci-batch-v1", "plan": plan, "config": config,
                                    "engine_sha256": engine_digest(), "suite_sha256": digest(read_regular(root / "suite/suite.json"))})
    return root


def stage_paths(root, trial, stage):
    prefix = Path(root) / "journal" / trial
    return prefix / (stage + ".started.json"), prefix / (stage + ".finished.json")


def stage_state(root, trial, stage):
    started, finished = stage_paths(root, trial, stage)
    if finished.exists():
        return read_json(finished)["status"]
    return "uncertain" if started.exists() else "pending"


def write_reports(root, reason=None):
    root = Path(root)
    summary = summarize(root / "suite")
    for row in summary["trials"]:
        row["stages"] = {s: stage_state(root, row["id"], s) for s in ("run", "grade", "review")}
        row["execution_failures"] = []
        for stage in ("run", "grade", "review"):
            finished = stage_paths(root, row["id"], stage)[1]
            if finished.exists() and read_json(finished).get("failure"):
                row["execution_failures"].append({"stage": stage, **read_json(finished)["failure"]})
        if row["execution_failures"] and row["status"] != "failed":
            row["status"] = "incomplete"
        # A pre-execution stage error is infrastructure uncertainty, not a pass
        # or a not-run case that can silently be replayed after spending tokens.
        if row["status"] == "not_run" and any(s != "pending" for s in row["stages"].values()):
            row["status"] = "incomplete"
        if any(s in {"error", "interrupted", "uncertain"} for s in row["stages"].values()):
            row["status"] = "incomplete"
    versions = list(load_suite(root / "suite")["skills"])
    summary["counts"] = {v: {s: sum(r["version"] == v and r["status"] == s for r in summary["trials"])
                             for s in ("passed", "failed", "incomplete", "not_run")} for v in versions}
    summary["by_case"] = {c: {v: {s: sum(r["case"] == c and r["version"] == v and r["status"] == s for r in summary["trials"])
                                  for s in ("passed", "failed", "incomplete", "not_run")} for v in versions}
                          for c in sorted({r["case"] for r in summary["trials"]})}
    if versions == ["before", "after"] or set(versions) == {"before", "after"}:
        summary["comparison"] = {s: summary["counts"]["after"][s] - summary["counts"]["before"][s]
                                 for s in ("passed", "failed", "incomplete", "not_run")}
    statuses = [r["status"] for r in summary["trials"]]
    code = 2 if reason or any(s in ("incomplete", "not_run") for s in statuses) else 1 if "failed" in statuses else 0
    summary.update(batch_stop_reason=reason, exit_code=code,
                   reserved_agent_invocations=sum(1 for p in (root / "journal").glob("*/*.started.json") if p.name in ("run.started.json", "review.started.json")))
    replace_json(root / "summary.json", summary)
    # A derived human report; regeneration is explicit and never changes evidence.
    lines = ["---", "ai_generated: true", "reviewed: false", "---", "", "# CI Skill 回归测试", "",
             "退出码：" + str(code) + "；停止原因：" + (reason or "无"), "",
             "| 用例 | 版本 | 重复 | 整体结果 | 执行 | 报告 | 评审 | 执行缺口 | trial |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines += [f"| {r['case']} | {r['version']} | {r['repeat']} | {r['status']} | {r.get('execution_status', '旧合同')} | {r.get('delivery_status', '未单列')} | {r.get('review_status', '未单列')} | {', '.join(f['code'] for f in r['execution_failures']) or '无'} | {r['id']} |" for r in summary["trials"]]
    lines += ["", "passed/failed 是完成判定的结果；incomplete/not_run 不计为通过。模型意见不能覆盖确定性失败。", ""]
    from common import write_new
    text = "\n".join(lines)
    report = root / "summary.md"
    if report.exists():
        read_regular(report)  # Recheck frontmatter/access before replacing a derived report.
        # Use atomic replacement via a sibling file; this is a derived artifact.
        import tempfile
        fd, name = tempfile.mkstemp(prefix=".summary-md-", dir=root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
            if report.is_symlink():
                raise EvalError("summary symlink refused")
            os.replace(name, report)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    else:
        write_new(report, text)
    return summary


def completed_evidence(root, trial_id, stage):
    path = Path(root) / "suite/trials" / trial_id
    finished = read_json(stage_paths(root, trial_id, stage)[1])
    artifact = path / (stage + ".json")
    if finished.get("artifact_sha256") != digest(read_regular(artifact)):
        raise EvalError("completed stage evidence changed: " + stage)
    if stage in {"grade", "review"}:
        g = read_json(path / "grade.json")
        if g.get("bindings"):
            validate_grade_binding(path, g)
    elif stage == "run":
        from common import snapshot
        from repair_support import validate_repair_artifacts
        r = read_json(artifact)
        validate_repair_artifacts(path, r)
        if r.get("filesystem_status") == "captured" and snapshot(path / "visible") != read_json(path / "after-run.json"):
            raise EvalError("completed repair workspace changed")
        if "stdout_sha256" in r and digest(read_regular(path / "execution/stdout.log")) != r["stdout_sha256"]:
            raise EvalError("completed repair transcript changed")
    if stage == "review":
        review = read_json(artifact)
        from resource_cleanup import cleanup_complete
        cleanup_complete(review)
        if review.get("diagnostics_sha256") and digest(read_regular(Path(review["diagnostics_directory"]) / "runtime-diagnostics.json")) != review["diagnostics_sha256"]:
            raise EvalError("review runtime diagnostics changed")
        if review.get("status") == "validated" and review.get("transport") == "chrys-run-native-v1":
            from native_runner import validate_native_artifacts
            validate_native_artifacts(review)


def has_pending_work(root):
    """Validate completed evidence even when no environment or paid work remains."""
    pending = False
    for row in load_suite(Path(root) / "suite")["trials"]:
        for stage in ("run", "grade", "review"):
            state = stage_state(root, row["id"], stage)
            finished = stage_paths(root, row["id"], stage)[1]
            outcome = read_json(finished).get("failure") if finished.exists() else None
            if outcome and outcome["batch_action"] == "stop" and outcome["scope"] != "operator":
                raise EvalError("previous unresolved execution failure: " + outcome["code"])
            if state == "completed":
                completed_evidence(root, row["id"], stage)
                if outcome and not can_review_artifact(root, row["id"], stage, outcome):
                    break
                continue
            if state != "pending":
                break
            if stage == "review" and read_json(Path(root) / "suite/trials" / row["id"] / "grade.json")["status"] != "awaiting_review":
                break
            pending = True
            break
    return pending


def can_review_artifact(root, trial_id, stage, outcome):
    """A recorded timeout remains a failure; only the next review stages proceed."""
    if stage != "run" or not outcome or outcome.get("code") != "trial_timeout" or outcome.get("batch_action") != "continue":
        return False
    return reviewable_timeout(read_json(Path(root) / "suite/trials" / trial_id / "run.json"))


def execute_batch(root, config):
    root = Path(root).absolute()
    descriptor = read_json(root / "batch.json")
    if (descriptor.get("format") != "ci-batch-v1" or descriptor["config"] != config
            or descriptor["engine_sha256"] != engine_digest()
            or descriptor["suite_sha256"] != digest(read_regular(root / "suite/suite.json"))):
        raise EvalError("batch configuration, engine or suite drift; create a new run")
    native = config.get("backend") == "native"
    with run_lock(root), (nullcontext() if native else docker_context(config["docker_context"])):
        try:
            if not has_pending_work(root):
                return write_reports(root)
        except (EvalError, OSError, ValueError, KeyError) as exc:
            return write_reports(root, str(exc))
        segments = list((root / "segments").glob("*.started.json"))
        used = 0.0
        for start in segments:
            end = start.with_name(start.name.replace(".started.", ".finished."))
            if not end.exists():
                return write_reports(root, "previous coordinator outcome is uncertain; do not replay live calls")
            used += read_json(end)["elapsed_seconds"]
        remaining = config["max_wall_seconds"] - used
        if remaining <= 0:
            return write_reports(root, "suite wall-clock budget exhausted")
        segment = uuid.uuid4().hex
        began = time.monotonic()
        write_json(root / "segments" / (segment + ".started.json"), {"started_at": datetime.now(timezone.utc).isoformat()})
        reason = None
        try:
            with wall_budget(remaining):
                if not native and not os.environ.get("CHRYS_EVAL_API_KEY"):
                    raise EvalError("repair gateway token environment CHRYS_EVAL_API_KEY is missing")
                runtime = load_runtime(config["runtime"])
                if native != (runtime.get("backend") == "native"):
                    raise EvalError("batch/runtime backend mismatch; no automatic fallback")
                environment = runtime_probe(runtime) if native else docker_probe(runtime)
                identity = native_preflight(config["reviewer"], root / "preflight" / segment)
                env_binding = {"repair_runtime": runtime, "environment": environment, "reviewer": identity}
                path = root / "environment-binding.json"
                if path.exists():
                    if read_json(path) != env_binding:
                        raise EvalError("shared runtime/model environment drift")
                else:
                    write_json(path, env_binding)
                for row in load_suite(root / "suite")["trials"]:
                    trial_id = row["id"]
                    trial = root / "suite/trials" / trial_id
                    for stage in ("run", "grade", "review"):
                        if descriptor["engine_sha256"] != engine_digest():
                            raise EvalError("evaluation engine changed during execution")
                        if load_runtime(config["runtime"]) != runtime or native_identity(config["reviewer"]) != identity:
                            raise EvalError("shared runtime/model configuration changed during execution")
                        state = stage_state(root, trial_id, stage)
                        if state == "completed":
                            completed_evidence(root, trial_id, stage)
                            prior = read_json(stage_paths(root, trial_id, stage)[1]).get("failure")
                            if prior and not can_review_artifact(root, trial_id, stage, prior):
                                break  # Never replay; only explicitly bounded artifact review.
                            continue
                        if state in {"uncertain", "error", "interrupted"}:
                            # Never spend again on a partially observed attempt.
                            break
                        if stage == "review" and read_json(trial / "grade.json")["status"] != "awaiting_review":
                            break
                        if stage in {"run", "review"}:
                            reserved = sum(1 for p in (root / "journal").glob("*/*.started.json") if p.name in ("run.started.json", "review.started.json"))
                            if reserved >= config["max_agent_invocations"]:
                                raise BudgetExhausted("agent invocation budget exhausted")
                        started, finished = stage_paths(root, trial_id, stage)
                        from common import remaining_timeout
                        remaining_timeout(1)
                        write_json(started, {"stage": stage, "trial_id": trial_id, "started_at": datetime.now(timezone.utc).isoformat()})
                        print(json.dumps({"trial": trial_id, "case": row["case"], "version": row["version"], "stage": stage}), file=sys.stderr, flush=True)
                        tick = time.monotonic()
                        result = {"status": "error"}
                        try:
                            if stage == "run":
                                run_trials(root / "suite", config["runtime"], trial_id)
                            elif stage == "grade":
                                grade(root / "suite", config["runtime"], trial_id)
                            else:
                                native_review(root / "suite", trial_id, root / "reviews" / trial_id, config["reviewer"], identity)
                            artifact = trial / (stage + ".json")
                            result = {"status": "completed", "artifact_sha256": digest(read_regular(artifact))}
                            data = read_json(artifact)
                            if stage in {"run", "review"}:
                                result["failure"] = classify_execution(data, stage)
                            elif stage == "grade":
                                result["failure"] = classify_grade(data)
                        except KeyboardInterrupt:
                            result = {"status": "interrupted", "error": "operator interrupted; attempt is not replayable"}
                            result["failure"] = failure("operator_interrupted", "operator", "stop")
                            raise
                        except (EvalError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                            result = {"status": "error", "error": str(exc)}
                            result["failure"] = failure("batch_budget_exhausted" if isinstance(exc, BudgetExhausted) else "framework_error_unclassified", "budget" if isinstance(exc, BudgetExhausted) else "unknown", "stop")
                        finally:
                            result["elapsed_seconds"] = round(time.monotonic() - tick, 3)
                            write_json(finished, result)
                        write_reports(root)
                        outcome = result.get("failure")
                        if outcome:
                            if outcome["batch_action"] == "stop":
                                raise EvalError("execution stopped (" + outcome["scope"] + "): " + outcome["code"])
                            if not can_review_artifact(root, trial_id, stage, outcome):
                                break  # The next independent case, not another attempt.
                        if result["status"] != "completed":
                            break
        except KeyboardInterrupt:
            reason = "operator interrupted; completed stages retained, interrupted attempt will not be replayed"
        except (EvalError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            reason = str(exc)
        finally:
            write_json(root / "segments" / (segment + ".finished.json"), {"elapsed_seconds": round(time.monotonic() - began, 3), "stop_reason": reason})
        return write_reports(root, reason)
