"""Local offline checks and replay of pinned, previously reviewed Java evidence."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from . import grading, runner
from .core import ENGINE, REPO, SPEC, EvaluationError, read_allowed, read_json, save_json, sha, tree_digest, utc


def local_tests(env=None):
    outcomes = []
    pattern = "test_*.py"
    for directory in (ENGINE / "tests", REPO / "skills/ci-self-healing/tests"):
        process = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", str(directory), "-p", pattern, "-q"],
            cwd=REPO, env=env, stdout=sys.stderr, stderr=subprocess.PIPE, text=True)
        sys.stderr.write(process.stderr)
        count = re.search(r"^Ran (\d+) tests? in ", process.stderr, re.M)
        skipped = re.search(r"^(?:OK|FAILED) \([^\n]*?skipped=(\d+)", process.stderr, re.M)
        outcomes.append({"directory": str(directory), "pattern": pattern, "returncode": process.returncode,
                         "ran": int(count[1]) if count else None,
                         "skipped": int(skipped[1]) if skipped else 0})
    return outcomes


def replay(catalog, baseline, source):
    if not (source / "seal.json").is_file():
        raise EvaluationError(f"missing local Java evidence at {source}; supply the sealed phase2 directory with --source")
    if sha(read_allowed(source / "seal.json")) != baseline["seal_sha256"]:
        raise EvaluationError("Java reference seal differs from the reviewed local baseline")
    saved = runner.report(source)
    expected = baseline["cases"]
    if sorted(t["case_id"] for t in saved["trials"]) != sorted(expected):
        raise EvaluationError("local Java reference case selection changed")
    comparisons = []
    for trial in saved["trials"]:
        directory = source / "trials" / trial["id"] / "host"
        packet = read_json(directory / "packet.json")
        case = read_json(directory / "case.json")
        manual = baseline["manual_review"]
        if case["id"] == manual["case_id"] and not set(manual["evidence_ids"]) <= packet["evidence"].keys():
            raise EvaluationError("manual review reference cannot be bound to the frozen trace")
        for identifier, (label, *refs) in expected[case["id"]].items():
            criterion = catalog.criteria[identifier]
            if criterion["method"] != "deterministic" or identifier not in catalog.cases[case["id"]]["criteria_ids"]:
                raise EvaluationError(f"review required for changed local criterion {identifier}")
            check = grading._deterministic(criterion, case, packet)
            matched = (check["status"] == label and check["applicability"] == "applicable"
                       and set(refs) <= set(check["evidence_ids"]) <= packet["evidence"].keys())
            comparisons.append({"case_id": case["id"], "trial": trial["id"], "expected": label,
                                "expected_evidence": refs, "matched": matched, **check})
    return comparisons


def run(catalog, source=None, env=None):
    spec = SPEC / "local-regression.json"
    baseline = read_json(spec)
    source = Path(source or ENGINE / "runs" / baseline["run"]).resolve()
    checks = replay(catalog, baseline, source)
    tests = local_tests(env)
    passed = all(row["matched"] for row in checks) and all(t["returncode"] == 0 and t["ran"] for t in tests)
    parent = ENGINE / "runs/local-regression"
    parent.mkdir(parents=True, exist_ok=True)
    report_file = Path(tempfile.mkdtemp(prefix="run-", dir=parent)) / "summary.json"
    result = {"status": "passed" if passed else "failed", "scope": "offline tests and frozen evidence replay",
              "models_called": False, "new_skill_execution": False, "release_decision": "manual",
              "created_at": utc(), "skill_sha256": tree_digest(REPO / "skills/ci-self-healing"),
              "implementation_sha256": sha(runner.implementation_identity()),
              "reference_run": str(source), "reference_seal": baseline["seal_sha256"],
              "baseline_manifest_sha256": sha(read_allowed(spec)), "catalog_sha256": catalog.digest,
              "tests": tests, "matched": sum(row["matched"] for row in checks), "total": len(checks),
              "checks": checks, "manual_review_reference": baseline["manual_review"], "report_file": str(report_file)}
    save_json(report_file, result)
    return result
