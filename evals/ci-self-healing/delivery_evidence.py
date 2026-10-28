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
from repair_support import DELIVERY_CONTRACT, DELIVERY_CONTRACTS, MANIFEST_DELIVERY_CONTRACTS, ENTERPRISE_DELIVERY_CONTRACT, enterprise_outputs_unconfigured, SKILL_NAME, source_state
from skill_execution import audit_execution, options, script_tokens


def artifact_checkpoint(events, path, body, script, required_options=None):
    """Replay native writes/edits and bind checks/readback to final bytes.

    This explicit offline audit does not change the legacy timeout exception.
    Unknown write mechanisms remain incomplete, never assumed successful.
    """
    from manifest_delivery import complete_readback
    path, sha = str(path), digest(body)
    calls, content, writes, intervals, checks, readbacks = {}, None, [], [], [], []
    gaps, invalid, mismatches = [], [], []
    for event in events:
        p, seq = event["payload"], event["seq"]
        if event["kind"] == "InvocationToolCallStart":
            calls[p["call_id"]] = event
            continue
        if event["kind"] != "InvocationToolCallResult" or p.get("call_id") not in calls:
            continue
        start = calls[p["call_id"]]
        args, name = start["payload"].get("args", {}), start["payload"].get("tool_name")
        reply = p.get("result", "")
        if args.get("path") == path and name in {"write_file", "edit_file"}:
            if intervals and start["seq"] <= intervals[-1][1]:
                gaps.append("overlapping_artifact_writes")
            intervals.append((start["seq"], seq))
            if name == "write_file" and isinstance(args.get("content"), str) and isinstance(reply, str) and (
                    reply.startswith(f"Written {len(args['content'])} chars (") and reply.endswith(f"to {path}.")):
                content = args["content"]
                writes.append(seq)
            elif name == "edit_file" and isinstance(content, str) and isinstance(reply, str) and (
                    re.fullmatch(r"Replaced [1-9]\d* occurrence\(s\) in " + re.escape(path) + r"\.", reply.strip())):
                old, new = args.get("old_string"), args.get("new_string")
                if (not isinstance(old, str) or not old or not isinstance(new, str) or old not in content
                        or (not args.get("replace_all") and content.count(old) != 1)
                        or int(reply.split()[1]) != (content.count(old) if args.get("replace_all") else 1)):
                    gaps.append("artifact_edit_not_replayable")
                else:
                    content = content.replace(old, new, -1 if args.get("replace_all") else 1)
                    writes.append(seq)
            else:
                gaps.append("artifact_write_receipt_unconfirmed")
        if name == "read_file" and args.get("path") == path and isinstance(reply, str):
            readbacks.append((start["seq"], seq, reply))
        if name == "run_skill_script" and args.get("skill_name") == SKILL_NAME and args.get("script_name") == script:
            try:
                tokens = script_tokens(args)
                flag = "--report" if script.endswith("report_check.py") else "--manifest"
                if "--help" in tokens or "-h" in tokens or options(tokens, flag) != [path]:
                    continue
                value, end = json.JSONDecoder().raw_decode(reply.lstrip())
                suffix = reply.lstrip()[end:].strip()
            except (ValueError, TypeError, AttributeError):
                continue
            if not isinstance(value, dict) or value.get("sha256") != sha:
                continue
            if any(options(tokens, k) != [v] for k, v in (required_options or {}).items()):
                mismatches.append(seq)
            elif value == {"format_status": "valid", "sha256": sha, "errors": []} and not suffix:
                checks.append((start["seq"], seq))
            elif value.get("format_status") == "invalid" and suffix in {"", "[exit_code: 1]"}:
                invalid.append(seq)
    mutations = [e["seq"] for e in events if "output/" + Path(path).name in e["payload"].get("changed_paths_since_previous_event", [])]
    if any(not any(a <= seq <= z for a, z in intervals) for seq in mutations):
        gaps.append("artifact_mutation_not_attributed")
    final = max(writes + mutations, default=0)
    reads = [r for r in readbacks if r[0] > final]
    complete = complete_readback("\n".join(r[2] for r in reads), body) if reads else False
    valid = [c for c in checks if c[0] > final]
    if content is None or content.encode() != body or not writes:
        gaps.append("artifact_final_bytes_not_replayed")
    if not valid:
        gaps.append("artifact_final_check_missing")
        if mismatches:
            gaps.append("artifact_check_requirement_mismatch")
    if not complete:
        gaps.append("artifact_complete_readback_missing")
    return {"status": "failed" if invalid and not valid else "incomplete" if gaps else "passed",
            "sha256": sha, "last_mutation_seq": final, "check_sequences": valid,
            "readback_sequences": [(a, z) for a, z, _ in reads], "complete_readback": complete,
            "invalid_check_sequences": invalid, "gaps": sorted(set(gaps))}


def audit_handoff_events(events, report, *, case_source_path, profile_path, profile_bound, resource_reads,
                         contract="handoff-evidence-v1"):
    """Focused native MCP checks; trigger meaning/causality/readability stay semantic.

    The source/profile paths and binding flag are supplied by the trusted test
    fixture. No natural-language task text is promoted to authorization here.
    """
    from handoff_judgment import CONTRACT, pending_judgments
    if contract not in {"handoff-evidence-v1", CONTRACT}:
        raise EvalError("unsupported handoff evidence contract")
    semantic = contract == CONTRACT
    failures, gaps, calls, searches, originals, attempted = [], [], {}, [], {}, set()
    observations = {"queries": [], "originals": [], "report_lines": [], "case_literals": [], "time_literals": []}
    reads = {r["resource"]: r["seq"] for r in reversed(resource_reads) if r.get("complete") is True}

    def fail(check, seq, **details):
        failures.append({"check": check, "seq": seq, **details})

    previous_search_end = 0
    messages = []
    for event in events:
        p, seq, kind = event["payload"], event["seq"], event["kind"]
        if kind == "InvocationMessage" and isinstance(p.get("text"), str) and not p.get("presentation"):
            messages.append((seq, p["text"]))
        if kind == "InvocationToolCallStart":
            calls[p["call_id"]] = event
            args, name = p.get("args", {}), p.get("tool_name")
            if name == "read_file" and args.get("path") == str(case_source_path):
                if reads.get("references/case-retrieval.md", seq) >= seq:
                    fail("case_source_before_complete_guide", seq)
            if name == "read_file" and args.get("path") == str(profile_path) and profile_bound is False:
                fail("unbound_profile_read", seq)
            if name == "cases_search_cases":
                query = args.get("query")
                notes = [{"seq": at, "text": text} for at, text in messages if previous_search_end < at < seq]
                observations["queries"].append({"seq": seq, "evidence_id": event.get("evidence_id"),
                    "query": query, "preceding_notes": notes,
                    "literal_query_in_notes": isinstance(query, str) and any(query in n["text"] for n in notes)})
                if searches and previous_search_end < searches[-1]:
                    fail("case_search_before_previous_result", seq)
                if not isinstance(query, str) or not query.strip():
                    gaps.append("unsupported_case_query_arguments")
                elif searches:
                    notes = [text for at, text in messages if previous_search_end < at < seq]
                    if not notes:
                        gaps.append("pre_search_note_not_observed")
                    elif query not in notes[-1] and not semantic:
                        fail("query_text_differs_from_pre_search_note", seq, query=query)
                searches.append(seq)
            elif name == "cases_read_case":
                if isinstance(args.get("case_id"), str):
                    attempted.add(args["case_id"])
                else:
                    gaps.append("unsupported_original_arguments")
        elif kind == "InvocationToolCallResult" and p.get("call_id") in calls:
            call = calls[p["call_id"]]["payload"]
            if call.get("tool_name") == "cases_search_cases":
                previous_search_end = seq
            if call.get("tool_name") != "cases_read_case":
                continue
            try:
                value = json.loads(p.get("result", ""))
            except (ValueError, TypeError):
                gaps.append("original_result_unreadable")
                continue
            if isinstance(value, dict) and value.get("status") == "ok" and value.get("complete") is True:
                case_id = call.get("args", {}).get("case_id")
                if value.get("case_id") != case_id:
                    fail("original_identity_mismatch", seq)
                else:
                    originals.setdefault(case_id, []).append({**value, "_host_observed_at": p.get("observed_at")})
                    observations["originals"].append({"seq": seq, "evidence_id": event.get("evidence_id"),
                        "requested_case_id": case_id, "receipt": value, "host_observed_at": p.get("observed_at")})
    if len(searches) > 3:
        fail("case_search_budget_exceeded", searches[3])
    if len(attempted) > 3:
        fail("case_original_budget_exceeded", 0, count=len(attempted))

    # Only visible prose counts as a nearby citation, never a fenced example.
    sections, section, fence = {}, None, None
    for line_number, line in enumerate(report.splitlines(), 1):
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match:
            fence = None if fence == match[1][0] else match[1][0] if fence is None else fence
            continue
        if fence:
            continue
        match = re.match(r"^## ([1-6])\. ", line)
        if match:
            section = int(match[1])
        elif section:
            sections.setdefault(section, []).append(line)
            observations["report_lines"].append({"line": line_number, "section": section, "text": line})
    intro, citations = "\n".join(sections.get(1, [])), sections.get(6, [])
    for case_id, values in originals.items():
        mentions = re.compile(r"(?<!\w)" + re.escape(case_id) + r"(?!\w)")
        urls = {v.get("url") for v in values if isinstance(v.get("url"), str) and v["url"]}
        observations["case_literals"].append({"case_id": case_id,
            "summary_id_present": bool(mentions.search(intro)),
            "summary_returned_url_literals": sorted(url for url in urls if url in intro),
            "known_urls": sorted(urls)})
        if mentions.search(intro) and urls and not any(url in intro for url in urls) and not semantic:
            fail("case_original_link_missing_in_summary", 0, case_id=case_id)
        time_column = None
        for line in citations:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            headers = [cell.strip(" *`") for cell in cells]
            if line.lstrip().startswith("|") and "读取时间" in headers:
                time_column = headers.index("读取时间")
                continue
            if not mentions.search(line):
                continue
            # Do not mistake a publication/version date for a read timestamp.
            field = (cells[time_column] if line.lstrip().startswith("|") and time_column is not None and len(cells) > time_column
                     else line.split("读取时间", 1)[1] if "读取时间" in line else "")
            times = re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", field)
            # Only explicit observation fields on that original's receipt bind
            # a time. Publication/update time and run start/deadline do not.
            known = {v[k] for v in values for k in ("read_at", "observed_at", "_host_observed_at") if isinstance(v.get(k), str)}
            for stamp in times:
                from datetime import datetime
                try:
                    supported = any(datetime.fromisoformat(stamp.replace("Z", "+00:00")) ==
                                    datetime.fromisoformat(s.replace("Z", "+00:00")) for s in known)
                except ValueError:
                    supported = False
                observations["time_literals"].append({"case_id": case_id, "text": line, "time": stamp,
                    "matches_receipt_time": supported, "receipt_times": sorted(known)})
                if not supported and not semantic:
                    fail("case_read_time_not_bound_to_original", 0, case_id=case_id, reported_time=stamp)
    result = {"contract": contract, "status": "failed" if failures else "incomplete" if gaps else "passed",
            "failures": failures, "gaps": sorted(set(gaps)), "search_count": len(searches),
            "original_attempt_count": len(attempted), "complete_original_ids": sorted(originals),
            "semantic_review_required": ["case trigger before source access", "query relevance and note rationale",
                                         "citation use, non-ISO times and metadata not mechanically matched",
                                         "root-cause claims across all sections", "actionability and readability"],
            "coverage": "focused cases_* MCP schema and native read_file paths; not shell interception or universal citation parsing"}
    if semantic:
        result.update(observations=observations, pending_judgments=pending_judgments(originals),
                      status_scope="mechanical facts only; pending semantic judgments do not establish acceptance")
    return result


def reviewable_timeout(run):
    return (run.get("delivery_contract") in DELIVERY_CONTRACTS
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
    result = {"contract": run.get("delivery_contract") if run.get("delivery_contract") in DELIVERY_CONTRACTS else DELIVERY_CONTRACT, "status": "unconfirmed", "gaps": [],
              "coverage": "observed stop marker, report bytes and non-mutating tool boundaries; intent requires independent review"}
    if run.get("delivery_contract") not in DELIVERY_CONTRACTS:
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
    if run["delivery_contract"] == ENTERPRISE_DELIVERY_CONTRACT:
        suite = read_json(trial.parent.parent / "suite.json")
        rows = [row for row in suite["trials"] if row["id"] == trial.name]
        if len(rows) != 1 or rows[0]["version"] not in {"before", "after", "candidate"}:
            raise EvalError("unbound enterprise output scope")
        skill = trial.parent.parent / "skills" / rows[0]["version"]
        if not enterprise_outputs_unconfigured(skill):
            return {**result, "gaps": ["enterprise_output_review_required"]}
    raw = read_regular(trial / "execution/stdout.log")
    if digest(raw) != run.get("stdout_sha256"):
        raise EvalError("transcript changed")
    trace = normalize_trace(raw, run["expected_model_id"])
    events = trace["events"]
    if (not events or events[0]["payload"].get("delivery_contract") != run["delivery_contract"]
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
    requires_manifest = run["delivery_contract"] in MANIFEST_DELIVERY_CONTRACTS
    manifest = trial / "visible/output/file_change.json"
    allowed_changes = {"output/analysis-fix.md"}
    allowed_scripts = {"scripts/report_check.py"}
    if requires_manifest:
        allowed_changes.add("output/file_change.json")
        allowed_scripts.add("scripts/file_change_check.py")
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
        if any(n not in allowed_changes for n in p.get("changed_paths_since_previous_event", [])):
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
            elif requires_manifest and name == "write_file" and args.get("path") == str(manifest):
                pass  # Final-byte and stop binding are checked below.
            elif name == "bash":
                if not readonly_shell(args.get("command")):
                    gaps.append("opaque_or_mutating_shell")
            elif name == "run_skill_script":
                if (args.get("skill_name") != SKILL_NAME or args.get("script_name") not in allowed_scripts):
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
    manifest_delivery = None
    if requires_manifest:
        from manifest_delivery import check_manifest, manifest_checkpoint
        manifest_check = check_manifest(trial.parent.parent, trial, before, after, contract=run["delivery_contract"])
        result["manifest_check"] = manifest_check
        gaps.extend(manifest_check["gaps"] + manifest_check["failures"])
        manifest_delivery = manifest_checkpoint(events, manifest, read_regular(manifest), stops)
        result["manifest_delivery"] = manifest_delivery
        gaps.extend(manifest_delivery["gaps"])
    if set(changes(before, after)) != allowed_changes:
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
    if requires_manifest:
        checkpoint = max(checkpoint, manifest_delivery["checkpoint_seq"]) if checkpoint and manifest_delivery.get("checkpoint_seq") else None
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
