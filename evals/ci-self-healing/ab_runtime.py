"""Shared native execution adapter for existing and enterprise A/B fixtures."""
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shlex
import shutil
import threading
import time

from common import EvalError, changes, digest, json_bytes, read_json, read_regular, run_logged, snapshot, write_json, write_new
from native_repair import setup, model_profile, generation_budget, isolated_model, gateway, child_env, policy, probe_build
from repair_support import budget_notice, session_receipt, execution_contract
from grading import normalize_trace, final_state_checks
from runtime_diagnostics import write_diagnostics
from suite import bundle_hash, skill_from_dir
import cases
import keyword_cases
import handoff_cases

ENGINE = Path(__file__).resolve().parent
CLIENT = '''import argparse,json,urllib.request
from pathlib import Path
p=argparse.ArgumentParser(add_help=False);p.add_argument('--binding',required=True);p.add_argument('--source',default='');p.add_argument('--help',action='store_true')
s=p.add_subparsers(dest='command');q=s.add_parser('search');q.add_argument('--query',required=True);q.add_argument('--page',type=int,default=1)
r=s.add_parser('read');r.add_argument('--case-id',required=True)
a=p.parse_args();kind='capability' if a.help else a.command
args={'query':a.query,'page':a.page} if kind=='search' else {'case_id':a.case_id} if kind=='read' else {}
if kind not in ('capability','search','read'):p.error('choose --help, search or read')
b=json.loads(Path(a.binding).read_text());request=urllib.request.Request(b['endpoint'],data=json.dumps({'kind':kind,'source_id':a.source,'args':args}).encode(),headers={'Content-Type':'application/json'})
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(request,timeout=10) as response:print(response.read().decode())
'''


def prepare_trial(trial, group, scenario, skill_source, config, template=None):
    trial, skill_source = Path(trial).resolve(), Path(skill_source).resolve()
    source_skill_hash = bundle_hash(skill_from_dir(skill_source))
    if trial.exists():
        raise ValueError("trial exists; no overwrite")
    if group == "main":
        cases.materialize(trial / "visible", scenario, trial.name, backend="native")
    elif group == "cleanup":
        import cleanup_cases
        cleanup_cases.materialize(trial, scenario, config)
    elif group in {"handoff", "keyword"}:
        template = Path(template).resolve()
        shutil.copytree(template / "visible", trial / "visible")
        for path in (trial / "visible").rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                body = read_regular(path).decode()
                path.write_text(body.replace(str(template), str(trial)))
    elif group == "enterprise":
        import enterprise_cases
        prefix = [config["python"], "-B", str(trial / "adapter/case_client.py"), "--binding", str(trial / "adapter/case-binding.json")]
        enterprise_cases.materialize(trial, scenario, skill_source, case_cli_prefix=prefix)
        skill_source = trial / "skill/ci-self-healing"
    else:
        raise ValueError("unknown fixture group")
    info = setup(config, trial, skill_source)
    write_new(trial / "adapter/ab_worker.py", read_regular(ENGINE / "ab_worker.py"))
    write_new(trial / "adapter/case_client.py", CLIENT)
    task_path = trial / "visible/task.txt"
    task = read_regular(task_path).decode()
    if group in {"handoff", "keyword"}:
        # The original fixtures remain the authority; adapt only transport and
        # physical paths to the isolated native host.
        task = task.replace(str(Path(template).parents[1] / "skill/ci-self-healing"), info["skill"])
    if group == "enterprise":
        task = task.replace(str(skill_source), info["skill"])
    if group == "keyword":
        source = trial / "visible/scene/case-source.txt"
        lines = read_regular(source).decode().splitlines()
        prefix = shlex.join([config["python"], "-B", str(trial / "adapter/case_client.py"), "--binding", str(trial / "adapter/case-binding.json")])
        source.write_text("\n".join("CLI 入口：" + prefix if line.startswith("CLI 入口：") else line for line in lines) + "\n")
    task_path.write_text(task)
    if group == "enterprise":
        # Fixture preparation precedes host transport/path adaptation. Preserve
        # its original snapshot separately; execute owns the actual before-run.
        (trial / "before-run.json").rename(trial / "fixture-before-run.json")
        freeze = read_json(trial / "freeze.json")
        previous_task_hash = freeze["immutable_files"]["visible/task.txt"]
        freeze["immutable_files"]["visible/task.txt"] = digest(read_regular(task_path))
        freeze["native_preparation"] = {"original_task_sha256": previous_task_hash,
            "runtime_skill": info["skill"], "source_skill_sha256": source_skill_hash,
            "scope": "trusted physical Skill path and declared CLI transport only; before execution"}
        (trial / "freeze.json").write_bytes(json_bytes(freeze))
    write_json(trial / "prepared-layout.json", info)
    record = {"group": group, "scenario": scenario, "skill_sha256": bundle_hash(skill_from_dir(info["skill"])),
              "source_skill_sha256": source_skill_hash,
              "configured_source_skill_sha256": bundle_hash(skill_from_dir(skill_source)),
              "pipeline_report": group in {"main", "handoff", "cleanup"} or (group == "enterprise" and scenario.startswith("profile-policy")),
              "expected": cases.CASES[scenario]["expected"] if group == "main" else "repair_caller" if group == "cleanup" else "stop"}
    if group == "enterprise":
        fixture = read_json(trial / "fixture.json")
        record["pipeline_report"] = fixture.get("pipeline_report", record["pipeline_report"])
    write_json(trial / "ab-case.json", record)
    write_json(trial / "prepared-inputs.json", snapshot(trial / "visible"))
    return record


@contextmanager
def case_service(trial, group, scenario):
    """Corpus and audit stay in the parent, outside the Agent's file sandbox."""
    if group not in {"keyword", "enterprise"}:
        yield None
        return
    trial = Path(trial)
    audit = trial / "case-access.jsonl"
    if audit.exists() and audit.read_text():
        raise ValueError("case service audit already populated")
    if not audit.exists():
        write_new(audit, "")
    lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size < 65536:
                    raise ValueError("request limit")
                row = json.loads(self.rfile.read(size)); kind, args = row["kind"], row["args"]
                source = row.get("source_id", "")
                if group == "enterprise":
                    import enterprise_cases
                    result = enterprise_cases.response(scenario, source, kind, args)
                elif kind == "capability":
                    result = {"help": "--help; search --query TEXT [--page N]; read --case-id ID. Normalized whitespace AND title keywords; no semantic expansion."}
                elif kind == "search":
                    result = keyword_cases.search(scenario, **args)
                elif kind == "read":
                    result = keyword_cases.read(scenario, **args)
                else:
                    raise ValueError("unknown operation")
                now = datetime.now(timezone.utc).isoformat()
                if kind == "read" and result.get("status") == "ok":
                    result = {**result, "read_at": now}
                with lock, audit.open("a") as f:
                    f.write(json.dumps({"time_ns": time.monotonic_ns(), "time_utc": now, "source_id": source,
                                        "kind": kind, "args": args, "result": result}, ensure_ascii=False) + "\n")
                body = json.dumps(result, ensure_ascii=False).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            except (ValueError, KeyError, TypeError):
                self.send_error(400)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        write_json(trial / "case-service-cleanup.json", {"server_closed": True, "thread_reaped": not thread.is_alive()})


@contextmanager
def handoff_service(trial, group, scenario, config):
    if group != "handoff":
        yield None
        return
    import socket
    import subprocess
    from common import stop_owned_group
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    trial = Path(trial)
    with (trial / "mcp-service.log").open("xb") as log:
        proc = subprocess.Popen([config["python"], "-B", str(ENGINE / "handoff_cases.py"), "--scenario", scenario,
                                 "--port", str(port), "--audit", str(trial / "case-access.jsonl")],
                                stdout=log, stderr=log, start_new_session=True)
        try:
            deadline = time.monotonic() + 20
            while True:
                if proc.poll() is not None or time.monotonic() >= deadline:
                    raise ValueError("MCP service startup failed")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.2):
                        break
                except OSError:
                    time.sleep(.1)
            yield port
        finally:
            write_json(trial / "case-service-cleanup.json", stop_owned_group(proc))


def execute(trial, config):
    trial = Path(trial).resolve()
    record = read_json(trial / "ab-case.json")
    if (trial / "execution").exists() or (trial / "run.json").exists():
        raise ValueError("execution already started; no replay")
    if snapshot(trial / "visible") != read_json(trial / "prepared-inputs.json"):
        raise ValueError("frozen fixture drift")
    info = read_json(trial / "prepared-layout.json")
    if bundle_hash(skill_from_dir(info["skill"])) != record["skill_sha256"]:
        raise ValueError("frozen Skill drift")
    group, scenario = record["group"], record["scenario"]
    if group == "main":
        baseline = probe_build(config, trial / "visible/scene/sourcecode", trial / "baseline-build")
        write_json(trial / "baseline-build.json", baseline)
        if baseline["status"] != "passed":
            raise ValueError("original failure could not be reproduced")
        logs = baseline["checks"]["maven"]["stdout"] + baseline["checks"]["maven"]["stderr"]
        for name in ("error_logs.txt", "all_logs.txt"):
            write_new(trial / "visible/scene" / name, logs)
    if group == "cleanup":
        import cleanup_cases
        baseline = cleanup_cases.probe(config, trial / "visible/scene/sourcecode", trial / "baseline-build", scenario, baseline=True)
        write_json(trial / "baseline-build.json", baseline)
        if baseline["status"] != "passed":
            raise ValueError("cleanup fixture baseline failure not reproduced")
        for name in ("error_logs.txt", "all_logs.txt"):
            write_new(trial / "visible/scene" / name, baseline["stdout"] + baseline["stderr"])
    write_json(trial / "before-run.json", snapshot(trial / "visible"))
    model, profile_hash = model_profile(config)
    budget = generation_budget(model)
    with case_service(trial, group, scenario) as case_port, handoff_service(trial, group, scenario, config) as mcp_port:
        if case_port:
            write_json(trial / "adapter/case-binding.json", {"endpoint": f"http://127.0.0.1:{case_port}/"})
        if mcp_port:
            info["mcp"] = [{"name": "synthetic_cases", "transport": "http", "url": f"http://127.0.0.1:{mcp_port}/mcp",
                            "tool_name_prefix": "cases", "bypass_proxy": True, "load_prompts": False,
                            "allowed_tools": ["search_cases", "read_case"]}]
        with gateway(config, trial / "gateway", profile_hash) as (port, token):
            binding = read_json(trial / "gateway/binding.json")
            info.update(model=isolated_model(model, port, budget), max_tools=config["max_tool_calls"], clock_id=binding["clock_id"])
            write_json(trial / "adapter/layout.json", info)
            task = read_regular(trial / "visible/task.txt").decode() + budget_notice(config["timeout_seconds"])
            write_new(trial / "adapter/task.txt", task)
            # Read-only fixtures remain read-only; actual attempted writes remain
            # visible in tool events even when the OS denies them.
            writable = [trial / "visible"] if group in {"main", "cleanup"} else [trial / "visible/output"]
            if group == "enterprise":
                writable.append(trial / "visible/scene/enterprise-output")
            sb = policy(config, read=[trial / "adapter", info["skill"], trial / "visible", trial / "runtime/settings.xml"],
                        write=[*writable, *(trial / "runtime" / n for n in ("config", "m2", "tmp", "java-home")), trial / "native-session"],
                        port=port, pty=True)
            for extra in (case_port, mcp_port):
                if extra:
                    sb += f'(allow network-outbound (remote tcp "localhost:{extra}"))\n'
            if group == "enterprise" and scenario == "output-collision":
                sb += '(deny file-write* (literal ' + json.dumps(str(trial / "visible/scene/enterprise-output/diagnosis.json")) + '))\n'
            write_new(trial / "adapter/run.sb", sb)
            argv = ["/usr/bin/sandbox-exec", "-f", str(trial / "adapter/run.sb"), config["python"], "-B",
                    str(trial / "adapter/ab_worker.py"), str(trial / "adapter/layout.json")]
            process = run_logged(argv, trial / "execution", config["timeout_seconds"],
                                 env={**child_env(config, trial / "runtime"), "CHRYS_EVAL_API_KEY": token},
                                 cwd=trial / "visible/scene/sourcecode", grace_seconds=5,
                                 identity={k: binding[k] for k in ("trial_id", "gateway_id")})
            write_json(trial / "session-receipt.json", session_receipt(trial / "native-session"))
        after = snapshot(trial / "visible")
        changed = changes(read_json(trial / "before-run.json"), after)
        write_json(trial / "after-run.json", after)
        diagnostic = write_diagnostics(trial, trial / "native-session", process, public_log=trial / "execution/stdout.log",
                         gateway_log=trial / "gateway/process.log", gateway_binding=binding, changed_paths=changed)
        trace = normalize_trace(read_regular(trial / "execution/stdout.log"), "glm-5.2")
        write_json(trial / "public-trace.json", trace)
        result = {**record, "status": process["status"], "process": process, "expected_model_id": "glm-5.2",
                  "profile_sha256": profile_hash, "generation_budget": budget, "runtime_diagnostics": diagnostic,
                  "trace_gaps": trace["gaps"], "changed_paths": changed, "task_sha256": digest(task.encode()),
                  "skill_execution_contract": execution_contract(info["skill"]),
                  "gateway_cleanup": read_json(trial / "gateway/cleanup.json"), "backend": "native"}
        write_json(trial / "run.json", result)
    return result


def objective(trial, config):
    """Bounded host facts for every group; applicability comes from the fixture."""
    import subprocess
    from delivery_evidence import artifact_checkpoint
    from generation_budget import generation_failure
    from skill_execution import audit_execution
    trial = Path(trial)
    row, run = read_json(trial / "ab-case.json"), read_json(trial / "run.json")
    trace = read_json(trial / "public-trace.json")
    before, after = read_json(trial / "before-run.json"), read_json(trial / "after-run.json")
    failures, gaps = [], list(trace["gaps"])
    if run["status"] != "completed" or run["process"].get("returncode") != 0:
        gaps.append("execution_incomplete")
    if snapshot(trial / "visible") != after:
        gaps.append("final_fixture_changed_after_capture")
    if run["process"].get("cleanup", {}).get("status") != "confirmed":
        gaps.append("execution_cleanup_unconfirmed")
    if run.get("gateway_cleanup", {}).get("status") != "confirmed":
        gaps.append("gateway_cleanup_unconfirmed")
    generation_gap = generation_failure(run)
    if generation_gap:
        gaps.append(generation_gap)
    pipeline_report = row.get("pipeline_report")
    if type(pipeline_report) is not bool:
        raise ValueError("fixture must explicitly declare pipeline report applicability")
    expected = row["expected"] if row["group"] in {"main", "cleanup"} else "stop"
    audit_options = {}
    if row["group"] == "cleanup":
        import cleanup_cases
        audit_options = {"build_detector": cleanup_cases.build_detector, "candidate_prefixes": ("scene/sourcecode/",)}
    audit = audit_execution(trial, trace["events"], after, expected, "native",
                            contract=run["skill_execution_contract"], trace_gaps=trace["gaps"],
                            pipeline_report=pipeline_report, **audit_options)
    failures.extend(audit["failures"]); gaps.extend(audit["gaps"])
    result = {"failures": failures, "gaps": gaps, "pipeline_report": pipeline_report, "execution_checks": audit}
    if row["group"] == "main":
        state = final_state_checks(before, after, row["expected"], contract=run["skill_execution_contract"])
        failures.extend(state["failures"])
        result["final_state"] = state
        if row["expected"] != "stop":
            independent = probe_build(config, trial / "visible/scene/sourcecode", trial / "independent-build", row["scenario"])
            result["independent_oracle"] = independent
            if independent["status"] == "failed":
                failures.append({"check": "independent_behavior_oracle"})
            elif independent["status"] != "passed":
                gaps.append("independent_behavior_oracle_incomplete")
    elif row["group"] == "cleanup":
        import cleanup_cases
        state = cleanup_cases.state_checks(before, after, row["scenario"])
        failures.extend(state["failures"])
        result["final_state"] = state
        independent = cleanup_cases.probe(config, trial / "visible/scene/sourcecode", trial / "independent-build", row["scenario"])
        result["independent_oracle"] = independent
        if independent["status"] == "failed":
            failures.append({"check": "independent_behavior_oracle"})
        elif independent["status"] != "passed":
            gaps.append("independent_behavior_oracle_incomplete")
    elif row["group"] == "enterprise":
        import enterprise_cases
        enterprise = enterprise_cases.audit(trial, events=trace["events"], execution_status=run["status"])
        result["enterprise"] = enterprise
        failures.extend(enterprise.get("failures", [])); gaps.extend(enterprise.get("gaps", []))
    else:
        protected = [p for p in changes(before, after) if p != "output" and not p.startswith("output/")]
        if protected:
            failures.append({"check": "protected_fixture_changed", "paths": protected})
        path = trial / "case-access.jsonl"
        access = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        searches = [r for r in access if r["kind"] == "search"]
        originals = {(r.get("source_id", ""), r.get("args", {}).get("case_id")) for r in access if r["kind"] == "read"}
        result["case_access"] = {"search_count": len(searches), "original_count": len(originals), "events": access}
        if len(searches) > 3 or len(originals) > 3:
            failures.append({"check": "retrieval_count_exceeded"})
        if not access:
            gaps.append("case_target_not_covered")
        if row["group"] == "handoff":
            coverage = handoff_cases.measure_coverage(row["scenario"], trace["events"], access)
            result["target_coverage"] = coverage
            gaps.extend(coverage.get("gaps", []))
            if coverage["status"] != "covered":
                gaps.append("case_target_not_covered")
    if pipeline_report:
        report = trial / "visible/output/analysis-fix.md"
        if not report.is_file() or report.is_symlink():
            gaps.append("formal_report_file_missing")
        else:
            body = read_regular(report)
            # These are trusted synthetic fixture definitions, not a parser for
            # enterprise prose or inferred authorization from the produced report.
            classified = row.get("classification_required", (
                row["group"] == "handoff" and row["scenario"] != "empty") or (
                row["group"] == "enterprise" and row["scenario"] in {"profile-policy-chain", "profile-policy-missing"}))
            if type(classified) is not bool:
                raise ValueError("fixture classification requirement must be boolean")
            classification = "required" if classified else "none"
            modes = ("present",) if audit["builds"] else ("present", "none")
            checker = ENGINE / "contracts/v3/scripts/report_check.py"
            checks, checkpoints = [], []
            for mode in modes:
                checked = subprocess.run([config["python"], "-B", str(checker), "--report", str(report),
                                          "--classification", classification, "--checks", mode],
                                         capture_output=True, text=True, timeout=30)
                checks.append({"checks": mode, "returncode": checked.returncode,
                               "stdout": checked.stdout, "stderr": checked.stderr})
                checkpoints.append(artifact_checkpoint(trace["events"], report, body, "scripts/report_check.py",
                                   {"--classification": classification, "--checks": mode}))
            result["report_check"] = {"contract": "v3", "checker_sha256": digest(read_regular(checker)),
                                      "sha256": digest(body), "attempts": checks}
            result["report_delivery"] = {"attempts": checkpoints}
            if not any(c["returncode"] == 0 for c in checks):
                if any(c["returncode"] == 1 for c in checks):
                    failures.append({"check": "formal_report_format_invalid"})
                else:
                    gaps.append("formal_report_checker_unavailable")
            if not any(c["status"] == "passed" for c in checkpoints):
                gaps.append("formal_report_final_check_or_readback_unconfirmed")
    else:
        report = trial / "visible/output/result.md"
        if not report.is_file() or report.is_symlink() or not read_regular(report).strip():
            gaps.append("ordinary_result_missing_or_empty")
        if (trial / "visible/output/analysis-fix.md").exists():
            failures.append({"check": "unexpected_formal_report"})
    result["gaps"] = sorted(set(gaps))
    result["status"] = "fail" if failures else "incomplete" if gaps else "pass"
    write_json(trial / "objective.json", result)
    return result


def packet(trial):
    """Deduplicate full Skill resource bodies without discarding public actions."""
    trial = Path(trial)
    info, case = read_json(trial / "prepared-layout.json"), read_json(trial / "ab-case.json")
    trace = read_json(trial / "public-trace.json")
    events, resources, calls, omissions = [], {}, {}, []
    for event in trace["events"]:
        e = json.loads(json.dumps(event))
        p = e["payload"]
        if e["kind"] == "InvocationToolCallStart":
            calls[p.get("call_id")] = p
        if e["kind"] == "InvocationToolCallResult":
            start = calls.get(p.get("call_id"), {})
            args = start.get("args", {})
            tool = start.get("tool_name")
            if tool in {"load_skill", "read_skill_resource"} and args.get("skill_name") == "ci-self-healing":
                name = "SKILL.md" if tool == "load_skill" else args.get("resource_name", "")
                if not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts:
                    events.append(e)
                    continue
                target = Path(info["skill"]) / name
                if target.is_file() and not target.is_symlink() and target.resolve().is_relative_to(Path(info["skill"]).resolve()):
                    body = read_regular(target).decode()
                    value = p.get("result")
                    canonical = body.strip()
                    matched = isinstance(value, str) and (value.strip().startswith(canonical) if tool == "load_skill" else value.strip() == canonical)
                    if matched:
                        resources[name] = body
                        p["result"] = {"shared_resource": name, "matched_full_body": True, "sha256": digest(body.encode())}
                        tail = value.strip()[len(canonical):] if tool == "load_skill" else ""
                        if tail:
                            p["result"]["trailing_text"] = tail
        events.append(e)
    files = []
    for p in sorted((trial / "visible").rglob("*")):
        rel = p.relative_to(trial / "visible").as_posix()
        if ".git" in Path(rel).parts:
            continue
        if p.is_symlink():
            omissions.append({"path": rel, "reason": "symlink_not_followed"})
            continue
        if not p.is_file():
            continue
        if "target" in Path(rel).parts:
            omissions.append({"path": rel, "reason": "generated_build_tree_not_in_review_packet"})
            continue
        if p.stat().st_size > 120000:
            omissions.append({"path": rel, "reason": "file_exceeds_review_packet_limit", "bytes": p.stat().st_size})
            continue
        try:
            content = read_regular(p).decode()
        except UnicodeDecodeError:
            omissions.append({"path": rel, "reason": "non_utf8_file"})
            continue
        except (OSError, EvalError):
            omissions.append({"path": rel, "reason": "restricted_or_unreadable_file"})
            continue
        files.append({"id": "F" + str(len(files) + 1), "path": rel, "content": content})
    objective_result = read_json(trial / "objective.json")
    oracle = {"case": case["scenario"], "group": case["group"], "pipeline_report": case["pipeline_report"]}
    if case["group"] == "main":
        oracle["requirements"] = cases.CASES[case["scenario"]]
    elif case["group"] == "cleanup":
        import cleanup_cases
        oracle["requirements"] = cleanup_cases.REQUIREMENTS[case["scenario"]]
    elif case["group"] == "keyword":
        oracle["query_strategy"] = keyword_cases.STRATEGIES[case["scenario"]]
    elif case["group"] == "handoff":
        oracle["target"] = handoff_cases.TARGETS[case["scenario"]]
    elif case["group"] == "enterprise":
        import enterprise_cases
        oracle["fixture"] = read_json(trial / "fixture.json")
        oracle["scenario_expectations"] = enterprise_cases.SCENARIO_EXPECTATIONS[case["scenario"]]
    access = [json.loads(line) for line in (trial / "case-access.jsonl").read_text().splitlines()] if (trial / "case-access.jsonl").exists() else []
    return {"oracle": oracle, "events": events, "shared_resources": resources, "files": files, "omitted_files": omissions,
            "deterministic": {"id": "D1", **objective_result}, "case_access": {"id": "C1", "events": access},
            "execution": {"id": "R1", **read_json(trial / "run.json")},
            "evidence_ids": ["D1", "C1", "R1", *[e["evidence_id"] for e in events], *[f["id"] for f in files]],
            "evidence_boundary": "public answer/tool events and tool-boundary snapshots; no hidden reasoning; not complete syscall audit"}
