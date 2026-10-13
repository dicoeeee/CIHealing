"""Bounded artifact review after a stopped, non-mutating native repair timeout.

This is review eligibility, NOT semantic admission or successful execution.
Legacy receipts are never upgraded. No model output or report is generated here.
"""
import json
from pathlib import Path
import re
import shlex

from common import EvalError, changes, digest, json_bytes, read_json, read_regular, snapshot
from generation_budget import generation_failure
from gateway_evidence import cancellation_time, gateway_failure
from repair_support import DELIVERY_CONTRACT, SKILL_NAME, source_state
from skill_execution import audit_execution, options, script_tokens


def reviewable_timeout(run):
    return (run.get("delivery_contract") == DELIVERY_CONTRACT
            and run.get("status") == "timeout" and run.get("process", {}).get("status") == "timeout"
            and run.get("delivery_assessment", {}).get("status") == "reviewable_stop_artifact")


def readonly_shell(command):
    """Deliberately narrow; unknown/opaque shell commands do not qualify.

    Not a sandbox or general shell parser. Native read tools are preferable.
    Reject expansions, redirects, pipelines and interpreter escapes altogether.
    """
    if not isinstance(command, str) or any(c in command for c in "$`><|\n()"):
        return False
    try:
        parts = re.split(r"&&|;", command)
        for part in parts:
            args = shlex.split(part)
            if not args:
                return False
            name = args[0]
            if name in {"cd", "pwd", "ls", "echo", "cat", "head", "tail", "wc"}:
                continue
            if name in {"rg", "grep"} and not any(a.startswith(("--pre", "--hostname-bin")) for a in args[1:]):
                continue
            if name == "git" and len(args) > 1 and args[1] in {"status", "log", "show", "diff", "ls-files", "rev-parse"}:
                if any(a.startswith(("--output", "--ext-diff", "--textconv")) for a in args[2:]):
                    return False
                continue
            return False
        return True
    except ValueError:
        return False


def assess_delivery(trial, run):
    """Recomputable host receipt, bound by run/grade hashes; no persistent writes."""
    result = {"contract": DELIVERY_CONTRACT, "status": "unconfirmed", "gaps": [],
              "coverage": "observed stop marker, report bytes and non-mutating tool boundaries; intent requires independent review"}
    if run.get("delivery_contract") != DELIVERY_CONTRACT:
        return {**result, "status": "not_assessed", "gaps": ["legacy_or_unsupported_contract"]}
    if run.get("process", {}).get("status") != "timeout":
        return {**result, "status": "not_applicable", "gaps": []}
    try:
        return _assess(Path(trial), run, result)
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, EvalError) as exc:
        # Do not expose arbitrary file contents or model text in diagnostics.
        return {**result, "status": "unconfirmed", "gaps": ["delivery_evidence_invalid:" + type(exc).__name__]}


def _assess(trial, run, result):
    from grading import normalize_trace  # Runtime import avoids module cycle.
    raw = read_regular(trial / "execution/stdout.log")
    if digest(raw) != run.get("stdout_sha256"):
        raise EvalError("transcript changed")
    trace = normalize_trace(raw, run["expected_model_id"])
    events = trace["events"]
    if (not events or events[0]["payload"].get("delivery_contract") != DELIVERY_CONTRACT
            or not events[0]["payload"].get("clock_id")
            or events[0]["payload"]["clock_id"] != run.get("process", {}).get("clock_id")
            or any(type(e.get("observed_ns")) is not int for e in events)
            or any(a["observed_ns"] > b["observed_ns"] for a, b in zip(events, events[1:]))):
        raise EvalError("unbound or unordered public clock")
    before, after = read_json(trial / "before-run.json"), read_json(trial / "after-run.json")
    if snapshot(trial / "visible") != after:
        raise EvalError("final state changed")
    report = trial / "visible/output/analysis-fix.md"
    report_body = read_regular(report)
    sha = digest(report_body)
    result["report_sha256"] = sha
    calls, writes, checks, readbacks, stops = {}, [], [], [], []
    baseline = digest(json_bytes(source_state(before)))
    gaps = list(trace["gaps"])
    if run.get("process", {}).get("status") == "timeout":
        gaps = [g for g in gaps if g != "agent_execution_failed"]
    for e in events:
        p, kind, seq = e["payload"], e["kind"], e["seq"]
        if kind.startswith("Invocation") and (not p.get("source_state_sha256") or not isinstance(p.get("changed_paths_since_previous_event"), list)):
            gaps.append("tool_boundary_state_missing")
        if p.get("source_state_sha256", baseline) != baseline:
            gaps.append("source_state_changed_during_run")
        if any(n != "output/analysis-fix.md" for n in p.get("changed_paths_since_previous_event", [])):
            gaps.append("other_workspace_changes")
        if kind == "InvocationMessage":
            text = p.get("text", "")
            if (isinstance(text, str) and "repair_admission" in text and not p.get("presentation")
                    and re.search(r"\bdecision\s*:\s*['\"]?stop\b", text)):
                stops.append(seq)
        if kind == "InvocationToolCallStart":
            calls[p["call_id"]] = e
            name, args = p.get("tool_name"), p.get("args", {})
            if name == "write_file" and args.get("path") == str(report):
                if not stops or args.get("content", "").encode() != report_body:
                    gaps.append("report_write_not_bound_to_stop_and_final_bytes")
            elif name == "bash":
                if not readonly_shell(args.get("command")):
                    gaps.append("opaque_or_mutating_shell")
            elif name == "run_skill_script":
                if (args.get("skill_name") != SKILL_NAME or args.get("script_name") != "scripts/report_check.py"):
                    gaps.append("non_report_script")
            elif name not in {"read_file", "list_directory", "glob", "grep", "load_skill", "read_skill_resource"}:
                gaps.append("unsupported_or_mutating_tool")
        elif kind == "InvocationToolCallResult" and p.get("call_id") in calls:
            start = calls[p["call_id"]]; args = start["payload"].get("args", {})
            name = start["payload"].get("tool_name")
            if name == "write_file" and args.get("path") == str(report) and "output/analysis-fix.md" in p.get("changed_paths_since_previous_event", []):
                writes.append(seq)
            if name == "run_skill_script" and args.get("script_name") == "scripts/report_check.py":
                tokens = script_tokens(args)
                if "--help" in tokens or "-h" in tokens:
                    continue
                value = json.loads(p["result"])
                if (args.get("skill_name") == SKILL_NAME and options(tokens, "--report") == [str(report)]
                        and options(tokens, "--classification") == ["none"]
                        and options(tokens, "--checks") in (["present"], ["none"])
                        and value == {"format_status": "valid", "sha256": sha, "errors": []}):
                    checks.append(seq)
                else:
                    gaps.append("report_check_not_valid")
            if name == "read_file" and args.get("path") == str(report):
                # Native read_file may prepend line numbers/header; require every
                # nonblank line in order, not a summary or truncated readback.
                value = p.get("result", "")
                lines = [re.sub(r"^\d+\|", "", x).rstrip() for x in value.splitlines()]
                wanted = [x.rstrip() for x in report_body.decode().splitlines() if x.strip()]
                pos = 0
                for line in lines:
                    if pos < len(wanted) and line == wanted[pos]:
                        pos += 1
                if pos == len(wanted):
                    readbacks.append(seq)
    if not (stops and writes and checks and readbacks and stops[-1] < writes[-1] < min(checks[-1], readbacks[-1])):
        gaps.append("stop_report_checkpoint_missing")
    if changes(before, after) != ["output/analysis-fix.md"]:
        gaps.append("not_report_only_change")
    audit = audit_execution(trial, events, after, "stop", run.get("backend", "native"),
                            contract=run.get("skill_execution_contract"), trace_gaps=trace["gaps"])
    if audit["failures"] or audit["gaps"]:
        gaps.append("native_execution_audit_not_clean")
    receipt = read_json(trial / "session-receipt.json")
    if digest(read_regular(trial / "session-receipt.json")) != run.get("session_receipt_sha256") or receipt.get("gaps"):
        gaps.append("session_receipt_not_bound")
    finals = [e["payload"] for e in events if e["kind"] == "observer.finished"]
    metas = finals[0].get("model_receipts", []) if len(finals) == 1 else []
    if (len(metas) != 1 or not any(s["meta"].get("session_id") == metas[0].get("session_id")
                                 and s["meta"].get("model_id") == run["expected_model_id"] for s in receipt.get("sessions", []))):
        gaps.append("persisted_session_identity_unconfirmed")
    from cases import git_metadata
    if git_metadata(trial / "visible/scene/sourcecode") != read_json(trial / "git-initial.json"):
        gaps.append("git_metadata_changed")
    process = run.get("process", {})
    if (process.get("cleanup", {}).get("status") != "confirmed" or run.get("gateway_cleanup_confirmed") is not True
            or run.get("isolation_confirmed") is not True or run.get("filesystem_status") != "captured"):
        gaps.append("execution_cleanup_or_snapshot_unconfirmed")
    checkpoint = max(checks[-1], readbacks[-1]) if checks and readbacks else None
    if checkpoint:
        cp = events[checkpoint - 1]
        result.update(checkpoint_seq=checkpoint, checkpoint_ns=cp["observed_ns"],
                      stop_seq=stops[-1] if stops else None, check_seq=checks[-1], readback_seq=readbacks[-1])
        if any(e["kind"] in {"InvocationToolCallStart", "InvocationMessage"} for e in events[checkpoint:]):
            # A normal final message is allowed for normally completed runs only.
            if process.get("status") == "timeout":
                gaps.append("actions_or_decisions_after_checkpoint")
    if process.get("status") == "timeout":
        evidence = run.get("runtime_diagnostics", {}).get("gateway_evidence", {})
        if (run.get("backend") != "native" or not evidence or gateway_failure(evidence, process)
                or cancellation_time(process, evidence.get("binding", {})) is None):
            gaps.append("not_confirmed_host_timeout")
        if generation_failure(run, cancelled_tail_after_ns=result.get("checkpoint_ns")):
            gaps.append("generation_prefix_or_cancelled_tail_unconfirmed")
    elif process.get("status") != "completed" or process.get("returncode") != 0 or generation_failure(run):
        gaps.append("execution_not_eligible")
    result["gaps"] = sorted(set(gaps))
    if not gaps:
        result["status"] = "reviewable_stop_artifact" if process.get("status") == "timeout" else "stop_artifact_confirmed"
    return result
