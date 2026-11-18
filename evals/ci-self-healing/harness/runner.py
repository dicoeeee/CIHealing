"""Bounded orchestration. The runner never decides the correct repair."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
from .review_schema import contract_for
import random
import sys
import time

from .catalog import Catalog
from .core import (ENGINE, REPO, SPEC, EvaluationError, Ledger, canonical, derive_status,
                   freeze_tree, read_allowed, read_json, save_json, sha, tree_digest, tree_files, utc)

RUNTIME_KEYS = {"python", "chrys", "maven", "java_home", "maven_repository", "rg", "git",
                "execution_profile", "execution_model_id", "execution_response_model_id",
                "review_profile", "review_model_id", "review_response_model_id", "endpoint_host",
                "max_tool_calls"}


def load_runtime(path):
    value = read_json(path)
    if not isinstance(value, dict) or set(value) - RUNTIME_KEYS:
        raise EvaluationError("runtime may contain only declared paths, model IDs and limits; no credentials")
    required = {"python", "chrys", "maven", "java_home", "maven_repository", "rg", "git",
                "execution_profile", "execution_model_id", "review_profile", "review_model_id"}
    if required - value.keys():
        raise EvaluationError("missing runtime fields: " + ", ".join(sorted(required - value.keys())))
    for key in required - {"execution_model_id", "review_model_id"}:
        if not isinstance(value[key], str) or not Path(value[key]).is_absolute():
            raise EvaluationError("runtime paths must be absolute: " + key)
    value.setdefault("max_tool_calls", 80)
    if type(value["max_tool_calls"]) is not int or not 1 <= value["max_tool_calls"] <= 200:
        raise EvaluationError("invalid tool budget")
    return value


def _implementation_files():
    """Code and controlled fixture material, with the same access checks as freezing."""
    files = {"harness/" + name: data for name, data in tree_files(ENGINE / "harness").items()}
    for relative in ("common.py", "host_sandbox.py", "environment/gateway.py"):
        files[relative] = read_allowed(ENGINE / relative)
    fixtures = ENGINE / "fixtures"
    if fixtures.exists() or fixtures.is_symlink():
        files.update({"fixtures/" + name: data for name, data in tree_files(fixtures).items()})
    return dict(sorted(files.items()))


def implementation_identity():
    # Keep the existing relative-path -> SHA format. Historical records are not
    # required to acquire files which did not exist when their run was frozen.
    return {name: sha(data) for name, data in _implementation_files().items()}


def freeze_implementation(destination, expected=None):
    """Copy exactly the bytes bound by the implementation manifest into a new snapshot."""
    files = _implementation_files()
    identity = {name: sha(data) for name, data in files.items()}
    if expected is not None and identity != expected:
        raise EvaluationError("evaluation implementation changed before freezing")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for name, body in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    if implementation_identity() != identity:
        raise EvaluationError("evaluation implementation changed while freezing")
    return identity


def _assert_implementation(root):
    if implementation_identity() != read_json(root / "implementation.json"):
        raise EvaluationError("evaluation implementation changed after freezing")


def make_plan(catalog, candidate, *, baseline=None, suite="smoke", cases=None, tags=None,
              repeats=1, maximum_sessions=16, wall_seconds=7200, concurrency=3, stage_seconds=900):
    for name, value, maximum in (("repeats", repeats, 100), ("concurrency", concurrency, 3),
                                  ("stage_seconds", stage_seconds, 900)):
        if type(value) is not int or not 1 <= value <= maximum:
            raise EvaluationError(f"invalid {name}; maximum {maximum}")
    selected = catalog.select(suite, cases, tags)
    if any(case.get("execution") == "offline_only" for case in selected):
        raise EvaluationError("offline-only cases require check --suite isolation; no live sessions may be planned")
    versions = {"candidate": str(Path(candidate).resolve())}
    if baseline:
        versions["baseline"] = str(Path(baseline).resolve())
    fingerprints = {name: tree_digest(path) for name, path in versions.items()}
    required = len(selected) * len(versions) * repeats * 2
    if type(maximum_sessions) is not int or maximum_sessions < required:
        raise EvaluationError(f"selection needs up to {required} sessions; reduce selection or set an explicit larger budget")
    if type(wall_seconds) is not int or wall_seconds < 1:
        raise EvaluationError("positive wall budget required")
    return {"schema_version": "ci-eval-plan/v2", "cases": [c["id"] for c in selected],
            "versions": versions, "skill_sha256": fingerprints, "repeats": repeats,
            "catalog_sha256": catalog.digest, "maximum_sessions": maximum_sessions,
            "planned_sessions": required, "wall_seconds": wall_seconds, "concurrency": concurrency,
            "stage_seconds": stage_seconds, "suite": suite,
            "comparison": "same_criteria_and_inputs" if baseline else "fixed_skill_pipeline_acceptance",
            "models_called": False}


def _call(ledger, stage, trial_id, seconds, function):
    reservation = ledger.reserve(stage, trial_id)
    limit = min(seconds, int(ledger.remaining()))
    try:
        if limit < 1:
            raise EvaluationError("wall budget exhausted before process start")
        # The retained process primitive propagates this budget to all children.
        from common import wall_budget
        with wall_budget(limit, cancellation=ledger.cancelled):
            result = function(limit)
        ledger.finish(reservation, result.get("status", "error"))
        return result
    except Exception:
        ledger.finish(reservation, "error")
        raise


def _safe_error(exc):
    # Arbitrary runtime exception text can include model/profile content.
    return {"error_type": type(exc).__name__, "message": str(exc) if isinstance(exc, EvaluationError) else "see bounded stage evidence"}


def _trial_inventory(trial):
    paths = []
    for directory in (trial / "host", trial / "visible", trial / "review"):
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise EvaluationError("symlink in sealed evidence")
            if path.is_file() and not path.is_symlink():
                # Large private verification caches are separately bound by their receipts.
                if any(part in {"m2", "maven-cache", "__pycache__"} for part in path.relative_to(trial).parts):
                    continue
                paths.append(path)
    for name in ("fixture.json", "result.json"):
        if (trial / name).is_file():
            paths.append(trial / name)
    execution = trial / "host/execution.json"
    if execution.is_file():
        result = read_json(execution)
        events = result.get("events_file")
        if events:
            path = Path(events)
            if not path.is_absolute():
                path = trial / path
            if path.is_file() and path.resolve().is_relative_to(trial.resolve()):
                paths.append(path)
    return {str(p.relative_to(trial)): sha(read_allowed(p)) for p in sorted(set(paths))}


def _seal(trial):
    save_json(trial / "seal.json", {"files": _trial_inventory(trial)})


def verify_seal(trial):
    trial = Path(trial)
    if _trial_inventory(trial) != read_json(trial / "seal.json")["files"]:
        raise EvaluationError("saved evidence changed; do not reuse its verdict")


def _run_inventory(root):
    names = ["plan.json", "runtime.json", "runtime-identity.json", "implementation.json", "trials.json",
             "ledger.json", "summary.json", "summary.md"]
    paths = [root / name for name in names]
    for name in ("spec", "skills", "implementation"):
        paths.extend(p for p in (root / name).rglob("*") if p.is_file())
    paths.extend(p / "seal.json" for p in (root / "trials").iterdir() if p.is_dir())
    result = {}
    for path in paths:
        result[str(path.relative_to(root))] = sha(read_allowed(path))
    return result


def report(root):
    """Never trust a cached summary as the list of evidence that must be checked."""
    root = Path(root).resolve()
    if _run_inventory(root) != read_json(root / "seal.json")["files"]:
        raise EvaluationError("run metadata or cached summary changed")
    plan, rows = read_json(root / "plan.json"), read_json(root / "trials.json")
    results = []
    expected = {row["id"] for row in rows}
    if len(expected) != len(rows) or expected != {p.name for p in (root / "trials").iterdir() if p.is_dir()}:
        raise EvaluationError("trial inventory changed")
    for row in rows:
        trial = root / "trials" / row["id"]
        verify_seal(trial)
        result = read_json(trial / "result.json")
        if any(result.get(key) != row[key] for key in ("id", "case_id", "version", "repeat")):
            raise EvaluationError("trial identity mismatch")
        results.append(result)
    return summarize(root, sorted(results, key=lambda r: r["id"]), plan, write=False)


def _trial(root, row, case, skill, runtime, catalog, ledger, stage_seconds, adapter):
    from . import fixtures, grading
    trial = root / "trials" / row["id"]
    result = {**row, "case_id": case["id"], "status": "incomplete", "checks": [],
              "coverage": {"status": "not_covered"}, "failure_origin": "environment_or_engine"}
    try:
        _assert_implementation(root)
        if ledger.remaining() < 1:
            raise EvaluationError("batch deadline reached before fixture preparation")
        fixtures.prepare(trial, case, skill, runtime)
        save_json(trial / "host/case.json", case)
        from common import wall_budget
        with wall_budget(min(180, ledger.remaining()), cancellation=ledger.cancelled):
            baseline = fixtures.baseline(trial, case, runtime)
        save_json(trial / "host/baseline.json", baseline)
        if baseline.get("status") not in {"passed", "not_applicable"}:
            raise EvaluationError("fixture baseline not established; no execution session started")
        _assert_implementation(root)
        execution = _call(ledger, "execute", row["id"], stage_seconds,
                          lambda seconds: adapter.execute(trial, runtime, seconds))
        save_json(trial / "host/execution.json", execution)
        _assert_implementation(root)
        with wall_budget(min(180, ledger.remaining()), cancellation=ledger.cancelled):
            verification = fixtures.verify(trial, case, runtime)
        save_json(trial / "host/verification.json", verification)
        _assert_implementation(root)
        packet = grading.collect(trial, case, execution, verification)
        save_json(trial / "host/packet.json", packet)
        checks = grading.deterministic(case, packet)
        save_json(trial / "host/deterministic.json", checks)
        semantic = catalog.for_case(case, "semantic")
        if execution.get("status") == "completed" and semantic:
            prompt = grading.review_prompt(case, packet, semantic)
            review = _call(ledger, "review", row["id"], stage_seconds,
                           lambda seconds: adapter.judge(trial / "review", runtime, prompt, seconds,
                                                         review_contract=contract_for(semantic, packet)))
            save_json(trial / "host/review.json", review)
            if review.get("status") == "completed" and review.get("trace_complete") is True:
                checks.extend(grading.validate_review(review.get("answer", ""), semantic, packet))
            else:
                checks.extend(_missing_checks(semantic, "independent review did not complete"))
        else:
            checks.extend(_missing_checks(semantic, "execution incomplete; no review session started"))
        coverage = grading.coverage(case, packet)
        result["evidence_summary"] = {
            "candidate_acceptance": {name: verification[name].get("status") for name in (
                "candidate_build", "behavior_oracle", "compatibility_oracle", "interface_oracle") if name in verification},
            "decision_evidence": {c["criterion_id"]: c["status"] for c in checks if c["criterion_id"] in
                ("contract.target-evidence", "contract.admission-before-write", "contract.required-verification",
                 "ambiguity.gap-recognition", "delivery.observation-honesty")},
            "observation_complete": packet.get("observation_complete"),
            "unresolved_checks": [c["criterion_id"] for c in checks if c["status"] == "inconclusive"],
            "semantic_criteria_requiring_calibration_evidence": sorted(c["id"] for c in semantic),
        }
        _assert_implementation(root)
        result.update(checks=checks, coverage=coverage, execution_status=execution.get("status"),
                      status=derive_status(checks, execution_complete=execution.get("status") == "completed" and execution.get("trace_complete") is True,
                                           covered=coverage.get("status") == "covered"))
        result["failure_origin"] = ("skill_behavior" if result["status"] == "failed" else
                                     "unresolved_evidence" if result["status"] == "incomplete" else None)
    except Exception as exc:
        result["error"] = _safe_error(exc)
    finally:
        trial.mkdir(parents=True, exist_ok=True)
        save_json(trial / "result.json", result)
        try:
            _seal(trial)
        except Exception as exc:
            result.update(status="incomplete", failure_origin="evidence_sealing", error=_safe_error(exc))
            save_json(trial / "result.json", result)
    return result


def _missing_checks(criteria, reason):
    return [{"criterion_id": c["id"], "status": "inconclusive", "applicability": "unknown",
             "method": c["method"], "evidence_ids": [], "reason": reason} for c in criteria]


def _criterion_changes(old, new):
    before = {row["criterion_id"]: row for row in old.get("checks", [])}
    after = {row["criterion_id"]: row for row in new.get("checks", [])}
    changes = []
    for key in sorted(before.keys() | after.keys()):
        left, right = before.get(key, {}), after.get(key, {})
        statuses = (left.get("status", "not_observed"), right.get("status", "not_observed"))
        applicability = (left.get("applicability"), right.get("applicability"))
        if statuses[0] != statuses[1] or applicability[0] != applicability[1]:
            changes.append({"criterion_id": key, "baseline": statuses[0], "candidate": statuses[1],
                            "baseline_applicability": applicability[0], "candidate_applicability": applicability[1],
                            "baseline_evidence": left.get("evidence_ids", []),
                            "candidate_evidence": right.get("evidence_ids", [])})
    return changes


def summarize(root, results, plan, *, write=True):
    counts = {status: sum(r["status"] == status for r in results) for status in ("passed", "failed", "incomplete")}
    status = "failed" if counts["failed"] else "incomplete" if counts["incomplete"] else "passed"
    pairs = []
    if "baseline" in plan.get("versions", {}):
        grouped = {}
        for row in results:
            grouped.setdefault((row["case_id"], row["repeat"]), {})[row["version"]] = row
        for (case_id, repeat), versions in sorted(grouped.items()):
            old, new = versions.get("baseline", {}), versions.get("candidate", {})
            comparable = old.get("status") != "incomplete" and new.get("status") != "incomplete" and bool(old and new)
            changes = _criterion_changes(old, new)
            new_failure = any(row["baseline"] == "pass" and row["candidate"] == "fail" for row in changes)
            pairs.append({"case_id": case_id, "repeat": repeat, "comparable": comparable,
                          "baseline": old.get("status", "not_run"), "candidate": new.get("status", "not_run"),
                          "baseline_trial": old.get("id"), "candidate_trial": new.get("id"), "criterion_changes": changes,
                          "regression": comparable and (new_failure or (old["status"] == "passed" and new["status"] == "failed"))})
        # An old failure fixed by the candidate is an improvement, not a failed gate.
        candidates = [row for row in results if row["version"] == "candidate"]
        status = ("failed" if any(row["status"] == "failed" for row in candidates) else
                  "incomplete" if not candidates or any(not pair["comparable"] for pair in pairs) else "passed")
    output = {"schema_version": "ci-eval-summary/v2", "status": status, "counts": counts,
              "trials": results, "comparisons": pairs, "exit_code": {"passed": 0, "failed": 1, "incomplete": 2}[status],
              "catalog_sha256": plan["catalog_sha256"], "created_at": utc(),
              "gate_scope": "candidate quality and complete paired comparison" if pairs else "all selected trials",
              "limitations": ["Each observed trial is distinct from a population reliability estimate.",
                              "Synthetic case retrieval does not measure a production case library.",
                              "Task outcomes and evaluation verdicts are separate."]}
    offline_isolation = plan.get("kind") == "workspace-isolation-offline"
    if offline_isolation:
        output.update(models_called=False, live_agent="not_run", host_enforcement="not_tested",
                      gate_scope="offline probe and grader calibration; not Skill behavior acceptance",
                      scripted_counts={name: sum(r.get("scripted_verdict") == name for r in results)
                                       for name in ("passed", "failed", "incomplete")})
        output["limitations"] = ["Controlled synthetic operations and declared labels only; no real Agent was run.",
                                 "No arbitrary syscall audit, OS containment, mount or live remote CI acceptance.",
                                 "Calibration success includes correctly detected unsafe and incomplete traces."]
    if not write:
        return output
    save_json(root / "summary.json", output)
    lines = ["---", "ai_generated: true", "reviewed: false", "---", "", "# CI Skill 评测结果", "",
             f"状态：{status}；通过 {counts['passed']}，失败 {counts['failed']}，未完成 {counts['incomplete']}。", "",
             "| 场景 | 版本 | 次数 | 结果 | 覆盖 |", "| --- | --- | --- | --- | --- |"]
    for row in results:
        lines.append(f"| {row['case_id']} | {row['version']} | {row['repeat']} | {row['status']} | {row['coverage']['status']} |")
    if any(pair["criterion_changes"] for pair in pairs):
        lines.extend(["", "| 用例 | 重复 | 判据 | 基线状态 / 适用性 | 候选状态 / 适用性 |",
                      "| --- | --- | --- | --- | --- |"])
        for pair in pairs:
            for row in pair["criterion_changes"]:
                lines.append(f"| {pair['case_id']} | {pair['repeat']} | {row['criterion_id']} | "
                             f"{row['baseline']} / {row['baseline_applicability'] or '—'} | "
                             f"{row['candidate']} / {row['candidate_applicability'] or '—'} |")
    if any("evidence_summary" in r for r in results):
        lines += ["", "| 用例 | 最终候选（宿主检查） | 当时依据（评审） | 观察完整 / 未决项 | 语义校准 |",
                  "| --- | --- | --- | --- | --- |"]
        for r in results:
            v = r.get("evidence_summary", {})
            fmt = lambda d: "；".join(f"{k}: {val}" for k, val in d.items()) or "无可用结果"
            lines.append(f"| {r['case_id']} | {fmt(v.get('candidate_acceptance', {}))} | "
                         f"{fmt(v.get('decision_evidence', {}))} | {v.get('observation_complete', '未知')} / "
                         f"{', '.join(v.get('unresolved_checks', [])) or '无已记录项'} | "
                         "本表未绑定校准结果，见同批放行报告 |")
    lines.extend(["", "本报告保留实际失败与缺口。单次运行不证明稳定通过率；宿主检查不冒充 Agent 自己执行的验证。", ""])
    if offline_isolation:
        lines[5] = "# 工作空间隔离离线校准结果"
        lines[8:8] = ["以下通过数表示受控探针和判分与预声明标签一致，包含被正确识别的越界负例。未运行真实 Agent、模型或宿主强制隔离验收。", ""]
    (root / "summary.md").write_text("\n".join(lines))
    return output


def run(root, runtime, plan, *, catalog=None, adapter=None):
    from . import native
    catalog, adapter = catalog or Catalog(), adapter or native
    root = Path(root).resolve()
    if root.exists():
        raise EvaluationError("run directory exists; no overwrite or automatic replay")
    if catalog.digest != plan["catalog_sha256"]:
        raise EvaluationError("catalog changed since planning")
    identity = adapter.preflight(runtime)
    if identity.get("status") != "passed":
        raise EvaluationError("runtime preflight did not pass; no models called")
    root.mkdir(parents=True)
    save_json(root / "runtime.json", runtime)
    save_json(root / "runtime-identity.json", identity)
    runtime = {**runtime, "_frozen_identity": identity}
    implementation = implementation_identity()
    save_json(root / "implementation.json", implementation)
    freeze_implementation(root / "implementation", implementation)
    freeze_tree(catalog.directory, root / "spec")
    catalog = Catalog(root / "spec", verify_sources=False)
    if catalog.digest != plan["catalog_sha256"]:
        raise EvaluationError("catalog changed during preflight; no model session started")
    skills = {}
    for label, source in plan["versions"].items():
        destination = root / "skills" / label
        if freeze_tree(source, destination) != plan["skill_sha256"][label]:
            raise EvaluationError("Skill changed after planning")
        skills[label] = destination
    save_json(root / "plan.json", plan)
    ledger = Ledger(root / "ledger.json", plan["maximum_sessions"], plan["wall_seconds"])
    rows = [{"case_id": case, "version": version, "repeat": repeat}
            for case in plan["cases"] for repeat in range(1, plan["repeats"] + 1) for version in skills]
    random.Random(20261106).shuffle(rows)
    for index, row in enumerate(rows, 1):
        row["id"] = f"t{index:04d}"
    save_json(root / "trials.json", rows)
    results = []
    with ThreadPoolExecutor(max_workers=plan["concurrency"]) as pool:
        jobs = [pool.submit(_trial, root, row, catalog.cases[row["case_id"]], skills[row["version"]],
                            runtime, catalog, ledger, plan["stage_seconds"], adapter) for row in rows]
        try:
            for job in as_completed(jobs):
                result = job.result()
                results.append(result)
                print(json.dumps({"trial": result["id"], "case": result["case_id"], "status": result["status"]}), file=sys.stderr, flush=True)
        except BaseException:
            ledger.cancelled.set()
            for job in jobs:
                job.cancel()
            raise
    summary = summarize(root, sorted(results, key=lambda row: row["id"]), plan)
    save_json(root / "seal.json", {"files": _run_inventory(root)})
    return summary


def _calibration_materials(suite, maximum_sessions, wall_seconds, stage_seconds, concurrency=3):
    from . import calibration
    if (type(maximum_sessions) is not int or maximum_sessions != 6
            or type(wall_seconds) is not int or not 1 <= wall_seconds <= 1800
            or type(stage_seconds) is not int or not 1 <= stage_seconds <= 900):
        raise EvaluationError("each calibration suite requires six sessions, at most 1800/900 seconds")
    if type(concurrency) is not int or not 1 <= concurrency <= 6:
        raise EvaluationError("calibration concurrency must be an integer from 1 to 6")
    if suite not in calibration.SUITES:
        raise EvaluationError("unknown calibration suite: " + str(suite))
    implementation = implementation_identity()
    catalog = Catalog()
    samples = calibration.samples(suite)
    if len(samples) != 6 or len({row["id"] for row in samples}) != 6:
        raise EvaluationError("a calibration suite must contain six distinct samples")
    labels = {}
    for index, sample in enumerate(samples, 1):
        case, packet, expected = sample["case"], sample["packet"], sample["expected"]
        # Display IDs carry no outcome label. Descriptive sample IDs and label
        # provenance remain exclusively in the host-side label manifest.
        public_id = ("s-" + sha(packet["evidence"])[:20]) if suite == calibration.HOLDOUT else f"c{index:03d}"
        if (case.get("id") != public_id or packet.get("case_id") != public_id
                or len({s["case"]["id"] for s in samples}) != 6):
            raise EvaluationError("calibration inputs require distinct opaque case IDs")
        criteria = catalog.for_case(case, "semantic")
        if (not criteria or len(case["criteria_ids"]) != len(set(case["criteria_ids"]))
                or set(expected) != {criterion["id"] for criterion in criteria}
                or set(expected) != set(case["criteria_ids"])):
            raise EvaluationError("calibration labels must cover exactly the selected semantic criteria")
        for criterion_id, label in expected.items():
            if not isinstance(label, dict) or set(label) != {"status", "applicability"}:
                raise EvaluationError("calibration label must contain status and applicability")
            status, applicability = label["status"], label["applicability"]
            if (status not in {"pass", "fail", "inconclusive", "not_applicable"}
                    or applicability not in {"applicable", "not_applicable", "unknown"}
                    or (status == "not_applicable") != (applicability == "not_applicable")
                    or (applicability == "unknown" and status != "inconclusive")):
                raise EvaluationError("inconsistent calibration label applicability")
            labels.setdefault(criterion_id, []).append((status, applicability))
    pair_targets = {
        "evidence-semantics-v1": {"contract.target-evidence", "delivery.outcome-meaning", "delivery.observation-honesty"},
        "command-control-split-v2": {"command.preparation", "command.wait-control", "command.result-check"},
    }
    if suite in pair_targets:
        targets = pair_targets[suite]
        if set(labels) != targets or any(sorted(pair) != [("fail", "applicable"), ("pass", "applicable")]
                                         for pair in labels.values()):
            raise EvaluationError(suite + " needs one applicable pass/fail pair for each of its three target criteria")
    if suite == calibration.HOLDOUT:
        if (set(labels) != pair_targets["command-control-split-v2"] or any(len(v) != 2 for v in labels.values())
                or {p for values in labels.values() for p in values} != {
                    ("pass", "applicable"), ("fail", "applicable"),
                    ("inconclusive", "unknown"), ("not_applicable", "not_applicable")}):
            raise EvaluationError("holdout must test three dimensions including abstention and non-applicability")
    if implementation_identity() != implementation:
        raise EvaluationError("calibration implementation or samples changed while planning")
    plan = {"schema_version": "ci-eval-calibration-plan/v2", "status": "planned", "exit_code": 0,
            "suite": suite, "samples": 6, "planned_sessions": 6, "maximum_sessions": 6,
            "wall_seconds": wall_seconds, "stage_seconds": stage_seconds, "concurrency": concurrency,
            "criteria_ids": sorted(labels), "catalog_sha256": catalog.digest, "models_called": False,
            "scope": "six fixed-evidence samples; not a judge accuracy estimate",
            "release_policy": "calibration-three-part-v1",
            "release_requires": ["valid_structure_and_references", "matching_labels", "supported_material_rationale"]}
    if suite == calibration.HOLDOUT:
        java = ("repair-contract-preserve", "repair-contract-extend", "repair-goal-ambiguous")
        plan["phase2_semantics_not_sampled"] = sorted({c["id"] for key in java for c in
            catalog.for_case(catalog.cases[key], "semantic")} - set(labels))
    return plan, samples, catalog, implementation


def calibration_plan(suite="initial-v1", *, maximum_sessions=6, wall_seconds=1800, stage_seconds=900, concurrency=3):
    """Validate a selected suite without a runtime, filesystem output, or model."""
    return _calibration_materials(suite, maximum_sessions, wall_seconds, stage_seconds, concurrency)[0]


def _calibration_input_inventory(root):
    paths = [root / name for name in ("plan.json", "samples.json", "labels.json", "implementation.json")]
    for name in ("spec", "prompts", "contracts", "implementation", "requirements"):
        for path in (root / name).rglob("*"):
            if path.is_symlink():
                raise EvaluationError("symlink in prepared calibration inputs")
            if path.is_file():
                paths.append(path)
    return {str(path.relative_to(root)): sha(read_allowed(path)) for path in sorted(paths)}


def _assert_calibration_inputs(root):
    if _calibration_input_inventory(root) != read_json(root / "input-seal.json")["files"]:
        raise EvaluationError("prepared calibration inputs or labels changed")


def _freeze_calibration_requirements(root, catalog):
    """Preserve only the rule sources whose bytes the selected catalog binds."""
    seen = set()
    for row in catalog.documents["criteria"]["source_snapshot"]:
        relative = Path(row["path"])
        if relative.is_absolute() or ".." in relative.parts or relative in seen:
            raise EvaluationError("unsafe or duplicate calibration requirement path")
        seen.add(relative)
        source = REPO / relative
        if (not source.resolve().is_relative_to(REPO.resolve())
                or any((REPO / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1))):
            raise EvaluationError("symlink in calibration requirement source")
        body = read_allowed(source)
        if sha(body) != row["sha256"]:
            raise EvaluationError("calibration requirement changed before freezing: " + row["path"])
        destination = root / "requirements" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)


def _prepare_calibration(root, plan, samples, catalog, implementation):
    """Freeze blind prompts and independent labels into an exclusively new directory."""
    from . import grading
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    save_json(root / "implementation.json", implementation)
    freeze_implementation(root / "implementation", implementation)
    freeze_tree(catalog.directory, root / "spec")
    frozen_catalog = Catalog(root / "spec", verify_sources=False)
    if frozen_catalog.digest != catalog.digest:
        raise EvaluationError("calibration criteria changed while freezing")
    _freeze_calibration_requirements(root, frozen_catalog)
    save_json(root / "plan.json", plan)
    unlabeled, labels = [], []
    (root / "prompts").mkdir()
    (root / "contracts").mkdir()
    for index, sample in enumerate(samples, 1):
        identifier = f"c{index:03d}"
        prompt = grading.review_prompt(sample["case"], sample["packet"], frozen_catalog.for_case(sample["case"], "semantic"))
        contract_file = f"contracts/{identifier}.json"
        save_json(root / contract_file, contract_for(frozen_catalog.for_case(sample["case"], "semantic"), sample["packet"]))
        relative = f"prompts/{identifier}.txt"
        (root / relative).write_text(prompt)
        unlabeled.append({"id": identifier, "case": sample["case"], "packet": sample["packet"],
                          "prompt_file": relative, "prompt_sha256": sha(prompt.encode()),
                          "contract_file": contract_file, "contract_sha256": sha(read_allowed(root / contract_file))})
        labels.append({"id": identifier, "sample_id": sample["id"], "expected": sample["expected"],
                       "provenance": sample.get("labels_provenance")})
    save_json(root / "samples.json", {"schema_version": "ci-eval-calibration-samples/v1", "suite": plan["suite"], "samples": unlabeled})
    save_json(root / "labels.json", {"schema_version": "ci-eval-calibration-labels/v1", "suite": plan["suite"], "labels": labels,
                                    "policy": "agent_checked; uncertain labels require user adjudication; not human gold"})
    _assert_implementation(root)
    save_json(root / "input-seal.json", {"schema_version": "ci-eval-calibration-inputs/v1", "files": _calibration_input_inventory(root)})
    return frozen_catalog


def prepare_calibration(root, suite="initial-v1", *, maximum_sessions=6, wall_seconds=1800, stage_seconds=900, concurrency=3):
    """Prepare reviewable inputs only. Never reads a runtime or calls preflight."""
    root = Path(root).resolve()
    if root.exists():
        raise EvaluationError("calibration directory exists; no overwrite or automatic replay")
    materials = _calibration_materials(suite, maximum_sessions, wall_seconds, stage_seconds, concurrency)
    plan, samples, catalog, implementation = materials
    _prepare_calibration(root, plan, samples, catalog, implementation)
    result = {**plan, "status": "prepared", "output": str(root), "sessions_reserved": 0,
              "input_seal_sha256": sha(read_allowed(root / "input-seal.json")),
              "label_policy": "agent_checked; uncertain labels require user adjudication; not human gold"}
    save_json(root / "prepared.json", result)
    return result


def calibrate(root, runtime, *, suite="initial-v1", adapter=None, maximum_sessions=6, wall_seconds=1800, stage_seconds=900, concurrency=3):
    from . import grading, native
    adapter = adapter or native
    root = Path(root).resolve()
    if root.exists():
        raise EvaluationError("calibration directory exists; no automatic replay")
    plan, samples, catalog, implementation = _calibration_materials(suite, maximum_sessions, wall_seconds, stage_seconds, concurrency)
    identity = adapter.preflight(runtime)
    if identity.get("status") != "passed":
        raise EvaluationError("runtime preflight failed; no calibration models called")
    catalog = _prepare_calibration(root, plan, samples, catalog, implementation)
    save_json(root / "runtime-identity.json", identity)
    runtime = {**runtime, "_frozen_identity": identity}
    ledger = Ledger(root / "ledger.json", 6, wall_seconds)

    def evaluate(indexed):
        index, sample = indexed
        row = {"id": sample["id"], "input_id": f"c{index:03d}", "status": "incomplete", "models_called": None,
               "label_source": sample.get("labels_provenance")}
        try:
            _assert_implementation(root)
            _assert_calibration_inputs(root)
            criteria = catalog.for_case(sample["case"], "semantic")
            prompt = read_allowed(root / "prompts" / f"c{index:03d}.txt").decode()
            contract = read_json(root / "contracts" / f"c{index:03d}.json")
            if contract != contract_for(criteria, sample["packet"]):
                raise EvaluationError("prepared review contract does not match sample evidence")
            review = _call(ledger, "calibration_review", sample["id"], stage_seconds,
                           lambda seconds: adapter.judge(root / f"c{index:04d}", runtime, prompt, seconds,
                                                         review_contract=contract))
            save_json(root / "reviews" / f"c{index:03d}.json", review)
            row["models_called"] = True if review.get("model_identity", {}).get("confirmed") is True else review.get("models_called")
            _assert_implementation(root)
            _assert_calibration_inputs(root)
            if review.get("status") != "completed" or review.get("trace_complete") is not True:
                row["reason"] = "judge did not complete"
                return row
            checks = grading.validate_review(review.get("answer", ""), criteria, sample["packet"])
            if any(check.get("validation_error") for check in checks):
                row.update(reason="judge output failed evidence/schema validation", checks=checks)
                return row
            actual = {check["criterion_id"]: {"status": check["status"], "applicability": check["applicability"]} for check in checks}
            expected = sample["expected"]
            row.update(status="passed" if actual == expected else "failed", expected=expected, actual=actual, checks=checks)
        except Exception as exc:
            row["error"] = _safe_error(exc)
        return row

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(evaluate, enumerate(samples, 1)))
    calls = [row["models_called"] for row in results]
    automatic_status = "passed" if all(r["status"] == "passed" for r in results) else "incomplete"
    output = {"suite": suite, "status": "pending_rationale" if automatic_status == "passed" else "incomplete",
              "automatic_status": automatic_status, "phase2_permitted": False,
              "release_policy": "calibration-three-part-v1", "rationale_status": "not_assessed",
              "criteria_ids": plan["criteria_ids"], "samples": results, "sessions_reserved": len(ledger.data["sessions"]),
              "maximum_sessions": 6, "wall_seconds": wall_seconds, "stage_seconds": stage_seconds, "concurrency": concurrency,
              "models_called": True if True in calls else False if all(value is False for value in calls) else None,
              "scope": "six-sample smoke calibration, not a judge accuracy estimate",
              "label_policy": "agent_checked; uncertain labels require user adjudication"}
    save_json(root / "summary.json", output)
    from .calibration_gate import seal_results
    seal_results(root)
    return output
