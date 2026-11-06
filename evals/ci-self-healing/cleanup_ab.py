"""Version-frozen cleanup A/B: four execution workers, two reviews, six experiment-wide seats."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import threading
import time
import traceback
import uuid

from common import digest, json_bytes, read_json, read_regular, snapshot, write_json, write_new, replace_json, wall_budget
from suite import bundle_hash, skill_from_dir
from native_repair import load_config, model_profile, generation_budget, tool_identity, doctor
from repair_support import EXECUTION_CONTRACT
from grading import final_state_checks
import ab_run
import ab_runtime
import ab_review
import handoff_cases
import keyword_cases
import cleanup_cases

ENGINE = Path(__file__).resolve().parent
REPO = ENGINE.parents[1]
CONTRACT = "ci-cleanup-ab-v1"
COMMON = "ci-cleanup-common-quality-v1"
GLOBAL = ENGINE / "runs/.icode-global"


def registry():
    rows = ab_run.registry()
    for case in ("case-001", "case-002"):
        rows += [{"id": f"main--{case}--r{n}", "group": "main", "scenario": case, "repeat": n} for n in range(2, 6)]
    rows += [{"id": f"cleanup--{case}--r{n}", "group": "cleanup", "scenario": case, "repeat": n}
             for case in cleanup_cases.SCENARIOS for n in range(1, 4)]
    assert len(rows) == 59 and len({(r['group'], r['scenario']) for r in rows}) == 33
    return rows


def engine_files():
    return sorted([*ENGINE.glob("*.py"), *ENGINE.glob("environment/*.py"),
                   *(p for p in (ENGINE / "contracts").rglob("*") if p.is_file() and "__pycache__" not in p.parts)])


def current_engine():
    return {str(p.relative_to(ENGINE)): digest(read_regular(p)) for p in engine_files()}


def init(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    old = ENGINE / "runs/ab-modular-20261104-01"
    # Existing evidence is observed, never rewritten by this experiment.
    preserved = {}
    for parent, dirs, files in os.walk(old):
        for name in files:
            p = Path(parent) / name; s = p.lstat()
            preserved[str(p.relative_to(old))] = [s.st_size, s.st_mtime_ns, s.st_mode]
    write_json(root / "historical-preservation.json", {"root": str(old), "files": preserved,
        "critical_hashes": {n: digest(read_regular(old / n)) for n in ["plan.json", "analysis/summary.json",
            "analysis/failure-clustering/cluster-ledger.json", "analysis/e1-offline-20261104-01/checkpoint-comparison.json",
            "analysis/e2-offline-20261104-01/collection-comparison.json", "analysis/e3-offline-20261105-01/adjudication.json"]}})
    config = read_json(old / "runtime.json")
    shutil.copytree(config["maven_repository"], root / "maven-cache")
    config.update(maven_repository=str(root / "maven-cache"), timeout_seconds=1200, max_tool_calls=80)
    write_json(root / "runtime.json", config)
    sources = {"A": ENGINE / "legacy/cleanup-v7/skill", "B": REPO / "skills/ci-self-healing"}
    versions = {}
    for label, source in sources.items():
        files = skill_from_dir(source)
        for name, body in files.items(): write_new(root / "skills" / label / name, body)
        versions[label] = {"source": str(source), "sha256": bundle_hash(files), "files": {n: digest(v) for n, v in files.items()}}
    plan = {"contract": CONTRACT, "created_at": datetime.now(timezone.utc).isoformat(), "versions": versions,
            "rows": registry(), "executions": 118, "max_reviews": 118, "executor": "glm-5.2",
            "reviewer": "deepseek-v4.1-flash", "execute_timeout_seconds": 1200, "review_timeout_seconds": 900,
            "execution_workers": 4, "review_workers": 2, "global_icode_limit": 6, "global_pool": str(GLOBAL),
            "seed": 20261105, "common_quality_contract": COMMON,
            "thresholds": {"paired_time_reduction": .15, "paired_token_reduction": .15,
                           "minimum_complete_quality_pairs_per_primary_case": 3, "new_severe_regressions": 0},
            "retry_policy": "started phases never replay automatically; all replacement attempts need distinct IDs and recorded reasons",
            "boundary": "real native iCode/model executions on frozen synthetic fixtures; not production incident statistics"}
    order = list(plan["rows"]); random.Random(plan["seed"]).shuffle(order)
    first = next(r for r in order if r["id"] == "main--case-001--r1")
    order.remove(first); order.insert(0, first)
    rng = random.Random(plan["seed"] + 1)
    plan["schedule"] = [{"id": r["id"], "versions": rng.sample(["A", "B"], 2)} for r in order]
    write_json(root / "plan.json", plan)
    print(json.dumps({"root": str(root), "executions": 118, "versions": {k: v['sha256'] for k, v in versions.items()}}), flush=True)


def prepare(root):
    root = Path(root).resolve(); plan = read_json(root / "plan.json"); config = load_config(read_json(root / "runtime.json"))
    for version in ("A", "B"):
        skill = root / "skills" / version
        assert bundle_hash(skill_from_dir(skill)) == plan["versions"][version]["sha256"]
        target = root / version
        if (target / "manifest.json").exists(): continue
        handoff_cases.prepare_cases(target / "templates/handoff", skill, git=config["git"])
        keyword_cases.prepare(target / "templates/keyword", skill)
        rows = []
        for row in plan["rows"]:
            trial = target / "trials" / row["id"]
            template = None
            if row["group"] == "handoff": template = target / "templates/handoff/trials" / row["scenario"]
            if row["group"] == "keyword":
                i = list(keyword_cases.SCENARIOS).index(row["scenario"]) + 1
                template = target / "templates/keyword/trials" / f"trial-{i:02d}"
            record = ab_runtime.prepare_trial(trial, row["group"], row["scenario"], skill, config, template)
            if row["group"] == "enterprise":
                layout = read_json(trial / "prepared-layout.json")
                layout.update(enterprise_resource_registration=True, enterprise_resource_scenario=row["scenario"])
                replace_json(trial / "prepared-layout.json", layout)
                write_new(trial / "adapter/ab_skill_host.py", read_regular(ENGINE / "ab_skill_host.py"))
            rows.append({**row, **record, "trial": str(trial), "version": version})
        write_json(target / "manifest.json", {"version": version, "trials": rows,
                   "skill_sha256": plan["versions"][version]["sha256"]})
        print(json.dumps({"prepared": version, "trials": len(rows)}), flush=True)


def align(root):
    """Align Git-only preparation timestamps before either paired Agent starts."""
    import cases
    root=Path(root).resolve();plan=read_json(root/'plan.json')
    if any(root.glob('*/trials/*/execution-start.json')):raise ValueError('cannot revise inputs after model execution starts')
    revisions=[]
    for row in plan['rows']:
        if row['group']!='handoff':continue
        a=root/'A/trials'/row['id'];b=root/'B/trials'/row['id']
        ra=a/'visible/scene/sourcecode';rb=b/'visible/scene/sourcecode'
        sha_a=cases.git(ra,'rev-parse','HEAD');sha_b=cases.git(rb,'rev-parse','HEAD')
        if sha_a==sha_b:continue
        if cases.git(ra,'rev-parse','HEAD^{tree}')!=cases.git(rb,'rev-parse','HEAD^{tree}'):
            raise ValueError('paired source trees differ')
        archive=b/'preparation-v1';archive.mkdir()
        (rb/'.git').rename(archive/'git')
        shutil.copytree(ra/'.git',rb/'.git')
        info=b/'visible/scene/build_info.json'
        write_new(archive/'build_info.json',read_regular(info))
        value=read_json(info);value['repositories'][0]['commitID']=sha_a
        info.write_bytes(json_bytes(value))
        (b/'prepared-inputs.json').rename(archive/'prepared-inputs.json')
        write_json(b/'prepared-inputs.json',snapshot(b/'visible'))
        revisions.append({'id':row['id'],'old_B_commit':sha_b,'aligned_commit':sha_a,
                          'basis':'identical Git tree; preparation-only commit timestamps; no model started'})
    write_json(root/'preparation-alignment.json',{'revisions':revisions})
    pairs=[]
    for row in plan['rows']:
        versions={}
        for version in ('A','B'):
            trial=root/version/'trials'/row['id'];files={}
            for path in (trial/'visible').rglob('*'):
                if not path.is_file() or '.git' in path.parts:continue
                data=read_regular(path)
                try:data=data.decode().replace(str(root/version),'<VERSION_ROOT>').replace(str(root/'skills'/version),'<SKILL_ROOT>').encode()
                except UnicodeDecodeError:pass
                files[str(path.relative_to(trial/'visible'))]=digest(data)
            versions[version]=files
        differences=[p for p in set(versions['A'])|set(versions['B']) if versions['A'].get(p)!=versions['B'].get(p)]
        pairs.append({'id':row['id'],'differences':differences})
    receipt={'status':'passed' if all(not r['differences'] for r in pairs) else 'failed','pairs':pairs}
    write_json(root/'paired-input-audit-v2.json',receipt)
    if receipt['status']!='passed':raise ValueError('paired input comparison failed')
    print(json.dumps({'paired_inputs':'passed','pairs':len(pairs),'preparation_only_alignments':len(revisions)}),flush=True)


def freeze(root):
    root = Path(root).resolve()
    config = load_config(read_json(root / "runtime.json"))
    model, profile_hash = model_profile(config)
    _, reviewer = ab_review.identity()
    if read_json(root/'paired-input-audit-v2.json')['status']!='passed':raise ValueError('paired input gate missing')
    if read_json(root/'offline-preflight.json')['status']!='passed':raise ValueError('native preflight gate missing')
    if read_json(root/'offline-suite-result.json')['process']['returncode']!=0:raise ValueError('offline tests failed')
    files = current_engine()
    for name in files: write_new(root / "engine" / name, read_regular(ENGINE / name))
    write_json(root / "freeze.json", {"engine": files, "runtime_sha256": digest(read_regular(root / "runtime.json")),
        "plan_sha256": digest(read_regular(root / "plan.json")),
        "policies": {name: digest(read_regular(root/name)) for name in ("concurrency-policy.json", "analysis-contract.json")},
        "executor_profile_sha256": profile_hash,
        "generation_budget": generation_budget(model), "reviewer": reviewer, "tools": tool_identity(config)})


def verify_freeze(root):
    root = Path(root); frozen = read_json(root / "freeze.json")
    if current_engine() != frozen["engine"]: raise ValueError("frozen evaluation engine drift")
    if digest(read_regular(root / "runtime.json")) != frozen["runtime_sha256"]: raise ValueError("runtime drift")
    if digest(read_regular(root / "plan.json")) != frozen["plan_sha256"]: raise ValueError("plan drift")
    if any(digest(read_regular(root/name)) != sha for name, sha in frozen["policies"].items()): raise ValueError("experiment policy drift")
    config = load_config(read_json(root / "runtime.json"))
    _, h = model_profile(config); _, reviewer = ab_review.identity()
    if h != frozen["executor_profile_sha256"] or reviewer != frozen["reviewer"]: raise ValueError("model profile drift")
    return config


def process_inventory():
    # Arguments are inspected transiently only to recognize Python-hosted iCode;
    # never serialize command lines, environments or credentials.
    raw = subprocess.check_output(["/bin/ps", "-axo", "pid=,ppid=,comm=,args="], text=True)
    rows = {}
    for line in raw.splitlines():
        values = line.strip().split(None, 3)
        if len(values) < 3: continue
        pid, parent = int(values[0]), int(values[1]); name = Path(values[2]).name
        args = values[3] if len(values) == 4 else ""
        code = name in {"chrys", "icode", "icode-tui"} or (name.startswith("python") and any(
            marker in args for marker in ("/ab_worker.py ", "/chrys_observer.py ", "/host_worker.py ")))
        rows[pid] = {"parent": parent, "icode": code}
    return rows


def external_processes(rows, owners):
    owned = set(owners)
    while True:
        children = {pid for pid, row in rows.items() if row["parent"] in owned}
        if children <= owned: break
        owned.update(children)
    return sorted(pid for pid, row in rows.items() if row["icode"] and pid not in owned)


class GlobalPool:
    def __init__(self, root):
        self.root = Path(root); GLOBAL.mkdir(parents=True, exist_ok=True)
        self.active = {}; self.guard = threading.Lock(); self.closed = threading.Event()
        self.monitor = threading.Thread(target=self.watch, daemon=True); self.monitor.start()

    @contextmanager
    def state(self):
        with (GLOBAL / "state.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = GLOBAL / "state.json"
            value = read_json(path) if path.exists() else {"leases": {}}
            processes = process_inventory()
            value["leases"] = {k: v for k, v in value["leases"].items() if v["owner_pid"] in processes}
            yield value, processes
            replace_json(path, value)

    @contextmanager
    def lease(self, phase, trial):
        token = uuid.uuid4().hex; cancel = threading.Event()
        while True:
            with self.state() as (state, processes):
                leases = state["leases"]
                external = external_processes(processes, {os.getpid(), *(x["owner_pid"] for x in leases.values())})
                scoped = {k: v for k, v in leases.items() if v["root"] == str(self.root)}
                version = Path(trial).parent.parent.name
                phase_room = sum(v['phase'] == phase for v in scoped.values()) < (4 if phase == 'execute' else 2)
                version_room = phase != 'execute' or sum(v['phase'] == phase and v.get('version') == version for v in scoped.values()) < 2
                if len(scoped) < 6 and phase_room and version_room:
                    row = {"owner_pid": os.getpid(), "phase": phase, "version": version, "trial": str(trial), "root": str(self.root),
                           "acquired_ns": time.monotonic_ns(), "external_pids": external}
                    leases[token] = row
                    with self.guard: self.active[token] = cancel
                    ab_run.append(self.root / "icode-concurrency.jsonl", {"event": "acquired", "token": token,
                        **row, "reserved_total": len(scoped) + 1, "external_count_excluded": len(external)})
                    break
            time.sleep(.2)
        try:
            with wall_budget(cancellation=cancel): yield
        finally:
            with self.state() as (state, _):
                state["leases"].pop(token, None)
                ab_run.append(self.root / "icode-concurrency.jsonl", {"event": "released", "token": token,
                    "time_ns": time.monotonic_ns(), "phase": phase, "trial": str(trial), "cancelled": cancel.is_set()})
            with self.guard: self.active.pop(token, None)

    def watch(self):
        while not self.closed.wait(1):
            try:
                with self.state() as (state, processes):
                    leases = state['leases']; owners = {os.getpid(), *(x['owner_pid'] for x in leases.values())}
                    external = external_processes(processes, owners)
                    scoped = {k: v for k, v in leases.items() if v["root"] == str(self.root)}
                    count = len(scoped)
                    ab_run.append(self.root / "process-concurrency.jsonl", {"observed_ns": time.monotonic_ns(),
                        "external_pids_excluded": external, "reserved_leases": len(scoped), "total_reserved": count})
                    if count > 6:
                        with self.guard:
                            for token in list(self.active)[-(count - 6):]: self.active[token].set()
            except Exception as exc:
                ab_run.append(self.root / "monitor-errors.jsonl", {"type": type(exc).__name__})
                with self.guard:
                    for cancel in self.active.values(): cancel.set()


def common_packet(trial):
    """Keep version conformance verbatim; derive the same outcome checks for both."""
    trial = Path(trial); raw = ab_runtime.packet(trial)
    obj = raw["deterministic"]; audit = obj.get("execution_checks", {})
    audit_failures = audit.get("failures", [])
    failures = [f for f in obj.get("failures", []) if f not in audit_failures and f.get("check") != "protected_inputs_or_cleanup"]
    failures += [f for f in audit_failures if f.get("check") == "agent_build_not_observed"]
    gaps = [g for g in obj.get("gaps", []) if g not in audit.get("gaps", [])]
    row = read_json(trial / "ab-case.json")
    if row["group"] == "main":
        state = final_state_checks(read_json(trial / "before-run.json"), read_json(trial / "after-run.json"),
                                   row["expected"], contract=EXECUTION_CONTRACT)
        for failure in state["failures"]:
            if failure not in failures: failures.append(failure)
    common = {"id": "D1", "failures": failures, "gaps": gaps,
              "status": "fail" if failures else "incomplete" if gaps else "pass",
              "execution_checks": {"contract": audit.get("contract"), "resource_reads": audit.get("resource_reads", []),
                  "builds": audit.get("builds", []), "failures": [f for f in audit_failures if f.get("check") == "agent_build_not_observed"],
                  "boundary": "Common result view; complete original execution audit is retained in version_conformance"},
              "version_conformance": obj,
              "independent_oracle": obj.get("independent_oracle"), "pipeline_report": obj.get("pipeline_report")}
    raw.update(deterministic=common, comparison_contract=COMMON)
    target = trial / "common-quality-objective.json"
    if target.exists():
        if read_json(target) != common: raise ValueError("common objective drift")
    else: write_json(target, common)
    return raw


def safe_error(trial, phase, exc):
    value = {"phase": phase, "type": type(exc).__name__, "frames": [
        {"file": Path(f.filename).name, "line": f.lineno, "function": f.name} for f in traceback.extract_tb(exc.__traceback__)[-6:]]}
    write_json(Path(trial) / (phase + "-error.json"), value)
    return value


def run(root, *, smoke=False):
    root = Path(root).resolve(); config = verify_freeze(root); plan = read_json(root / "plan.json")
    rows = {(v, r['id']): r for v in ('A', 'B') for r in read_json(root / v / 'manifest.json')['trials']}
    schedule = plan['schedule'][:1] if smoke else plan['schedule']
    pool = GlobalPool(root)
    review_futures = []
    def review_one(row):
        trial = Path(row['trial'])
        if (trial / 'review-start.json').exists(): return
        try:
            packet = common_packet(trial)
            with pool.lease('review', trial):
                verify_freeze(root)
                write_json(trial / 'review-start.json', {'time_utc': datetime.now(timezone.utc).isoformat()})
                result = ab_review.run(trial / 'review', config, packet, timeout=900)
            value = {'phase': 'review', 'trial': row['version']+'/'+row['id'], 'status': result['status'], 'verdict': result['verdict']}
        except BaseException as exc:
            value = {'phase': 'review', 'trial': row['version']+'/'+row['id'], 'status': 'harness_incomplete', **safe_error(trial, 'review', exc)}
        ab_run.append(root / 'progress.jsonl', value); print(json.dumps(value), flush=True)
    def execute_one(row):
        trial = Path(row['trial'])
        if (trial / 'run.json').exists():
            if not (trial / 'objective.json').exists(): ab_runtime.objective(trial, config)
            return row
        if (trial / 'execution-start.json').exists(): return None
        try:
            with pool.lease('execute', trial):
                verify_freeze(root)
                write_json(trial / 'execution-start.json', {'time_utc': datetime.now(timezone.utc).isoformat()})
                result = ab_runtime.execute(trial, config)
            ab_runtime.objective(trial, config)
            value = {'phase': 'execute', 'trial': row['version']+'/'+row['id'], 'status': result['status'],
                     'seconds': result['process']['duration_seconds']}
            ab_run.append(root / 'progress.jsonl', value); print(json.dumps(value), flush=True)
            return row
        except BaseException as exc:
            value = {'phase': 'execute', 'trial': row['version']+'/'+row['id'], 'status': 'harness_incomplete', **safe_error(trial, 'execute', exc)}
            ab_run.append(root / 'progress.jsonl', value); print(json.dumps(value), flush=True)
            return None
    try:
        with ThreadPoolExecutor(max_workers=2) as reviewers, ThreadPoolExecutor(max_workers=4) as executors:
            work = [rows[(v, item['id'])] for item in schedule for v in item['versions']]
            futures = [executors.submit(execute_one, row) for row in work]
            for future in as_completed(futures):
                row = future.result()
                if row is not None: review_futures.append(reviewers.submit(review_one, row))
            for future in as_completed(review_futures): future.result()
    finally:
        pool.closed.set(); pool.monitor.join(timeout=5)


def main():
    p = argparse.ArgumentParser(); p.add_argument('action', choices=['init', 'prepare', 'align', 'freeze', 'run'])
    p.add_argument('--root', type=Path, required=True); p.add_argument('--execute-live', action='store_true');p.add_argument('--smoke', action='store_true')
    a = p.parse_args()
    if a.action == 'run':
        if not a.execute_live: p.error('--execute-live required')
        run(a.root, smoke=a.smoke)
    else: globals()[a.action](a.root)


if __name__ == '__main__': main()
