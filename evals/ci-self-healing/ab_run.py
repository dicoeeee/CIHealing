"""Frozen, resumable A/B trial scheduler with a shared six-iCode lease pool.

Execution and independent review both acquire a lease. Existing started phases
are never replayed automatically; resume only dispatches unstarted phases.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import random
import time

from common import digest, read_json, read_regular, replace_json, write_json, write_new
from native_repair import load_config
from suite import bundle_hash, skill_from_dir
import ab_runtime
import ab_review
import cases
import handoff_cases
import keyword_cases

ENGINE = Path(__file__).resolve().parent
DEFAULT_ROOT = ENGINE / "runs/ab-modular-20261104-01"
ENTERPRISE = ("profile-policy-chain", "profile-policy-missing", "case-source-selection", "case-source-scope", "output-independent", "output-collision")
CRITICAL = {("main", "case-003"), ("main", "outcome-case-unconfirmed-blocked"), ("handoff", "injection"),
            ("enterprise", "profile-policy-missing"), ("enterprise", "case-source-scope"), ("enterprise", "output-collision")}


def append(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(json.dumps(row, ensure_ascii=False) + "\n"); f.flush(); os.fsync(f.fileno())
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextmanager
def slot(root, phase, trial):
    # All A/B runners and calibration share these same six process leases.
    folder = Path(root) / "icode-leases"; folder.mkdir(exist_ok=True)
    handle, index = None, None
    while handle is None:
        for n in range(6):
            candidate = (folder / f"slot-{n}.lock").open("a+")
            try:
                fcntl.flock(candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                candidate.close(); continue
            handle, index = candidate, n; break
        if handle is None:
            time.sleep(.25)
    lease = {"phase": phase, "trial": trial, "slot": index, "owner_pid": os.getpid(), "time_ns": time.monotonic_ns()}
    recorded = False
    try:
        append(Path(root) / "icode-concurrency.jsonl", {**lease, "event": "acquired"})
        recorded = True
        yield
    finally:
        try:
            if recorded:
                append(Path(root) / "icode-concurrency.jsonl", {**lease, "time_ns": time.monotonic_ns(), "event": "released"})
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def registry():
    rows = []
    for group, names in (("main", list(cases.CASES)), ("handoff", handoff_cases.SCENARIOS),
                         ("keyword", keyword_cases.SCENARIOS), ("enterprise", ENTERPRISE)):
        for name in names:
            for repeat in range(1, 4 if (group, name) in CRITICAL else 2):
                rows.append({"id": f"{group}--{name}--r{repeat}", "group": group, "scenario": name, "repeat": repeat})
    assert len(rows) == 42 and len({(r['group'],r['scenario']) for r in rows}) == 30
    return rows


def freeze_plan(root):
    path = root / "plan.json"
    plan = {"contract": "ci-modular-ab-v1", "baseline_sha": "a95582ebfec704657c539b7484becbefcce94d42",
            "versions": ["A", "B"], "trials_per_version": registry(), "executions": 84, "max_reviews": 84,
            "executor": "glm-5.2", "reviewer": "deepseek-v4.1-flash", "execute_timeout_seconds": 1200,
            "review_timeout_seconds": 900, "global_icode_limit": 6, "seed": 20261104,
            "retry_policy": "no automatic replay of any started execution or review",
            "production_services": "not assessed; real native Agent executions on frozen synthetic fixtures"}
    if path.exists() and read_json(path) != plan:
        raise ValueError("frozen A/B plan differs")
    if not path.exists():
        write_json(path, plan)
    return plan


def prepare(root, version, skill):
    root = Path(root).resolve(); plan = freeze_plan(root)
    target = root / version
    if (target / "manifest.json").exists():
        raise ValueError("version already prepared")
    config = load_config(read_json(root / "runtime.json"))
    templates = target / "templates"
    if not (templates / "handoff").exists():
        handoff_cases.prepare_cases(templates / "handoff", skill, git=config["git"])
    if not (templates / "keyword").exists():
        keyword_cases.prepare(templates / "keyword", skill)
    rows = []
    for row in plan["trials_per_version"]:
        trial = target / "trials" / row["id"]
        template = None
        if row["group"] == "handoff":
            template = templates / "handoff/trials" / row["scenario"]
        elif row["group"] == "keyword":
            index = list(keyword_cases.SCENARIOS).index(row["scenario"]) + 1
            template = templates / "keyword/trials" / f"trial-{index:02d}"
        record = ab_runtime.prepare_trial(trial, row["group"], row["scenario"], skill, config, template=template)
        rows.append({**row, **record, "trial": str(trial)})
    manifest = {"version": version, "skill_source": str(Path(skill).resolve()),
                "skill_sha256": bundle_hash(skill_from_dir(skill)), "trials": rows,
                "runtime_sha256": digest(read_regular(root / "runtime.json")),
                "prepared_at": datetime.now(timezone.utc).isoformat()}
    write_json(target / "manifest.json", manifest)
    print(json.dumps({"version": version, "prepared": len(rows), "skill_sha256": manifest["skill_sha256"]}), flush=True)


def one(root, version, row, config, review=True):
    trial = Path(row["trial"])
    label = version + "/" + row["id"]
    try:
        if not (trial / "run.json").exists():
            if (trial / "execution-start.json").exists():
                return {"trial": label, "status": "incomplete", "reason": "started_execution_not_replayed"}
            with slot(root, "execute", label):
                write_json(trial / "execution-start.json", {"time_utc": datetime.now(timezone.utc).isoformat(), "owner_pid": os.getpid()})
                ab_runtime.execute(trial, config)
        if not (trial / "objective.json").exists():
            ab_runtime.objective(trial, config)
        if review and not (trial / "review/result.json").exists():
            if (trial / "review-start.json").exists() or (trial / "review").exists():
                return {"trial": label, "status": "incomplete", "reason": "started_review_not_replayed"}
            packet = ab_runtime.packet(trial)
            with slot(root, "review", label):
                write_json(trial / "review-start.json", {"time_utc": datetime.now(timezone.utc).isoformat(), "owner_pid": os.getpid()})
                ab_review.run(trial / "review", config, packet, timeout=900)
        return trial_status(trial, label)
    except Exception as exc:
        # Exceptions may originate in credentials or model libraries: only a
        # safe type/stack identity is exposed; local native logs retain detail.
        import traceback
        error = {"trial": label, "status": "harness_incomplete", "error_type": type(exc).__name__,
                 "frames": [{"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                            for f in traceback.extract_tb(exc.__traceback__)[-5:]]}
        write_json(trial / ("harness-error-" + str(time.time_ns()) + ".json"), error)
        return error


def trial_status(trial, label):
    trial = Path(trial)
    run = read_json(trial / "run.json") if (trial / "run.json").exists() else {}
    objective = read_json(trial / "objective.json") if (trial / "objective.json").exists() else {}
    review = read_json(trial / "review/result.json") if (trial / "review/result.json").exists() else {}
    statuses = [objective.get("status"), review.get("verdict")]
    final = "fail" if "fail" in statuses else "incomplete"
    if (statuses == ["pass", "pass"] and run.get("status") == "completed"
            and run.get("process", {}).get("status") == "completed"
            and run.get("process", {}).get("returncode") == 0
            and review.get("status") == "completed"):
        final = "pass"
    return {"trial": label, "status": final, "execution_status": run.get("status", "not_started"),
            "execution_returncode": run.get("process", {}).get("returncode"),
            "objective": objective.get("status", "not_assessed"), "review": review.get("verdict", "not_completed"),
            "review_status": review.get("status", "not_started")}


def run(root, version, jobs, selection=None, review=True):
    if not 1 <= jobs <= 6:
        raise ValueError("jobs must be 1..6; all processes share six leases")
    root = Path(root).resolve(); freeze_plan(root)
    manifest = read_json(root / version / "manifest.json")
    config = load_config(read_json(root / "runtime.json"))
    if digest(read_regular(root / "runtime.json")) != manifest["runtime_sha256"]:
        raise ValueError("frozen runtime configuration changed")
    rows = manifest["trials"]
    if selection:
        rows = [r for r in rows if r["id"] in selection]
        if len(rows) != len(selection):
            raise ValueError("unknown trial selection")
    else:
        rows = list(rows); random.Random(20261104).shuffle(rows)
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(one, root, version, row, config, review): row for row in rows}
        for future in as_completed(futures):
            result = future.result()
            append(root / "progress.jsonl", result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    summarize(root)


def summarize(root):
    root = Path(root).resolve()
    result = {"versions": {}, "comparisons": [], "global_icode_limit": 6}
    for version in ("A", "B"):
        path = root / version / "manifest.json"
        if not path.exists():
            continue
        rows = read_json(path)["trials"]
        values = [trial_status(Path(r["trial"]), version + "/" + r["id"]) for r in rows]
        result["versions"][version] = {"trials": values, "counts": {status: sum(v["status"] == status for v in values)
                                                                 for status in ("pass", "fail", "incomplete")}}
    if set(result["versions"]) == {"A", "B"}:
        for a, b in zip(result["versions"]["A"]["trials"], result["versions"]["B"]["trials"]):
            assert a["trial"][2:] == b["trial"][2:]
            result["comparisons"].append({"trial": a["trial"][2:], "A": a["status"], "B": b["status"]})
    ledger = root / "icode-concurrency.jsonl"
    if ledger.exists():
        active, peak = set(), 0
        for line in ledger.read_text().splitlines():
            row = json.loads(line); key = (row["slot"], row["owner_pid"], row["phase"], row["trial"])
            if row["event"] == "acquired":
                active.add(key); peak = max(peak, len(active))
            else:
                active.discard(key)
        result.update(observed_peak_icode_leases=peak, unresolved_leases=len(active))
    replace_json(root / "summary.json", result)
    print(json.dumps({"counts": {k: v["counts"] for k, v in result["versions"].items()},
                      "observed_peak": result.get("observed_peak_icode_leases")}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "run", "summarize"])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--version", choices=["A", "B"])
    parser.add_argument("--skill", type=Path)
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--trial", action="append")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--execution-only", action="store_true")
    args = parser.parse_args()
    if args.action == "prepare":
        if not args.version or not args.skill:
            parser.error("prepare requires --version and --skill")
        prepare(args.root, args.version, args.skill)
    elif args.action == "run":
        if not args.execute_live or not args.version:
            parser.error("run requires --execute-live and --version")
        run(args.root, args.version, args.jobs, args.trial, not args.execution_only)
    else:
        summarize(args.root)


if __name__ == "__main__":
    main()
