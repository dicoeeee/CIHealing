"""Host-side native Skill/cleanup evidence audit for the synthetic fixtures.

Never executes candidate code, parses prose as approval, or decides intent.
Tool-boundary hashes bind captures to observed states. Shell detection covers
direct Maven/javac invocations only; opaque commands still require review.
"""
import json
from pathlib import Path
import re
import shlex

from common import EvalError, digest, json_bytes, read_json, read_regular
from repair_support import (EXECUTION_CONTRACT, EXECUTION_CONTRACTS, RECOVERY_EXECUTION_CONTRACTS,
                            SKILL_NAME, execution_contract, source_state)


RECOVERABLE_CAPTURE_CHECKS = {"build_without_confirmed_native_capture", "capture_not_current_at_build"}


def capture_recoveries(failures, builds, captures, latest_check, final_state, gaps):
    """Narrow same-state recovery, not retrospective approval of the first build.

    Every digest covers the full fixture root (including ignored/empty paths).
    The later capture must equal the host-observed state BEFORE the invalid
    build, then precede a new bound build and final match. Changed candidates or
    missing/opaque observations need review, not a guessed clean reconstruction.
    Protocol/integrity failures are never waived by this recovery path.
    """
    if (gaps or not failures or any(f["check"] not in RECOVERABLE_CAPTURE_CHECKS for f in failures)
            or not latest_check or latest_check["result"] != "match" or latest_check["state"] != final_state):
        return []
    capture = captures.get(latest_check["binding"])
    if not capture:
        return []
    reruns = [b for b in builds if capture["seq"] < b["seq"] < latest_check["seq"]
              and b["valid_capture"] and b["binding"] == capture["binding"]]
    if not reruns or reruns[0]["state"] != capture["state"]:
        return []
    by_seq = {b["seq"]: b for b in builds}
    recovered = []
    for failure in failures:
        prior = by_seq.get(failure["seq"])
        if prior and prior["seq"] < capture["seq"] and prior["state"] == capture["state"]:
            recovered.append({"violation": dict(failure), "prebuild_seq": prior["seq"],
                              "capture_seq": capture["seq"], "reverification_build_seqs": [b["seq"] for b in reruns],
                              "check_seq": latest_check["seq"], "source_state_sha256": final_state,
                              "binding": list(capture["binding"]),
                              "scope": "same full observed source state; cleanup timing only, not semantic admission or acceptance"})
    return recovered


def script_tokens(args):
    tokens = args.get("arguments", [])
    if not isinstance(tokens, list) or any(not isinstance(t, str) for t in tokens):
        raise ValueError("invalid arguments")
    tokens = list(tokens)
    extra = args.get("args")
    if isinstance(extra, list):
        tokens += extra
    elif isinstance(extra, dict):
        for key, value in extra.items():
            flag = key if key.startswith("-") else "--" + key
            if value is True:
                tokens.append(flag)
            elif value is not False and value is not None:
                tokens.extend([flag, str(value)])
    elif extra is not None:
        raise ValueError("invalid args")
    if any(not isinstance(t, str) for t in tokens):
        raise ValueError("invalid arguments")
    return tokens


def options(tokens, flag):
    values = []
    for i, token in enumerate(tokens):
        if token == flag:
            if i + 1 >= len(tokens):
                raise ValueError("missing option")
            values.append(tokens[i + 1])
        elif token.startswith(flag + "="):
            values.append(token[len(flag) + 1:])
    return values


def workspace_result(value):
    if not isinstance(value, str):
        raise ValueError("nontext result")
    objects = []
    for line in value.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("schema") == "ci-workspace-check/v1":
            objects.append(obj)
    codes = re.findall(r"^\[exit_code: (-?\d+)\]$", value, re.M)
    if len(objects) != 1 or len(codes) > 1:
        raise ValueError("missing/ambiguous native receipt")
    obj = objects[0]
    # Preflight exercises this native convention: zero is omitted, nonzero is
    # explicit. This is not a general rule for shell or arbitrary tool output.
    code = int(codes[0]) if codes else 0
    expected = {"captured": 0, "match": 0, "mismatch": 1, "incomplete": 2}.get(obj.get("result"))
    if code != expected or expected is None:
        raise ValueError("exit/result conflict")
    return obj


def is_build(payload):
    if payload.get("tool_name") != "bash":
        return False
    try:
        lex = shlex.shlex(payload.get("args", {}).get("command", ""), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return False
    for i, token in enumerate(tokens):
        name = Path(token).name
        tail = tokens[i + 1:]
        if name in {"mvn", "mvnw"} and any(t in {"compile", "test", "package", "verify", "install", "test-compile"} for t in tail):
            return True
        if name == "javac" and any(t.endswith(".java") or t.startswith("@") for t in tail):
            return True
    return False


def direct_skill_access(name, args, skill):
    """Observed direct access only, not a shell security policy or prose scan."""
    if name == "bash":
        return skill in str(args.get("command", ""))
    if name in {"read_file", "edit_file", "write_file"}:
        return any(str(args.get(key, "")).startswith(skill + "/")
                   for key in ("path", "file_path", "filename"))
    return False


def audit_execution(trial, events, after, expected, backend="native", *, contract=EXECUTION_CONTRACT, trace_gaps=()):
    if contract not in EXECUTION_CONTRACTS:
        raise EvalError("unsupported skill execution contract")
    trial = Path(trial)
    skill = trial / ("runtime/skills/ci-self-healing" if backend == "native" else "runtime-skill")
    exposed_skill = str(skill) if backend == "native" else "/skills/ci-self-healing"
    exposed_root = str(trial / "visible/scene/sourcecode") if backend == "native" else "/trial/scene/sourcecode"
    if contract == EXECUTION_CONTRACT and execution_contract(skill) != contract:
        raise EvalError("Skill package does not declare the split resource layout")
    candidate_resources = ("references/diagnosis-and-repair.md", "references/repair-and-verification.md")
    if contract == EXECUTION_CONTRACT:
        candidate_resources += ("references/repair-basis.md",)
    result = {"contract": contract, "failures": [], "gaps": [], "resource_reads": [],
              "builds": [], "workspace_checks": [], "non_native_workspace_observations": [], "script_invocations": [],
              "coverage": "native tool receipts and boundary states; unconfirmed native evidence does not prove no shell execution; opaque commands/intent require semantic review"}
    calls, reads, captures = {}, {}, {}
    loaded = False
    active_capture, latest_check = None, None
    last_build = 0
    build_states = []

    def fail(check, seq=0):
        item = {"check": check, "seq": seq, "paths": []}
        if item not in result["failures"]:
            result["failures"].append(item)

    def require(names, seq):
        for name in names:
            if name not in reads or reads[name] >= seq:
                fail("required_skill_resource_not_loaded_before_action:" + name, seq)

    def manifest_file(raw):
        if not isinstance(raw, str) or not Path(raw).is_absolute() or ".." in Path(raw).parts:
            raise ValueError("invalid manifest location")
        path = Path(raw)
        if backend != "native":
            if not raw.startswith("/trial/output/"):
                raise ValueError("uncollected manifest")
            path = trial / "visible" / path.relative_to("/trial")
        # Model cannot make grading read an unrelated host path or private session.
        if not any(parent in path.parents for parent in (trial / "visible/output", trial / "runtime/tmp")):
            raise ValueError("uncollected manifest")
        return read_json(path)

    for event in events:
        seq, kind, p = event.get("seq", 0), event.get("kind"), event.get("payload", {})
        state = p.get("source_state_sha256")
        changed = p.get("changed_paths_since_previous_event", [])
        if any(path.startswith("scene/sourcecode/src/main/java/") for path in changed):
            require(candidate_resources, seq)
        if any(path.startswith("scene/sourcecode/") and not path.startswith("scene/sourcecode/target/") for path in changed):
            active_capture = None
        if kind == "InvocationToolCallStart":
            calls[p.get("call_id")] = event
            name, args = p.get("tool_name"), p.get("args", {})
            if name in {"read_skill_resource", "run_skill_script"} and args.get("skill_name") == SKILL_NAME and not loaded:
                fail("skill_resource_before_native_load", seq)
            if direct_skill_access(name, args, exposed_skill):
                fail("skill_native_tool_bypassed", seq)
            script = args.get("script_name") if name == "run_skill_script" else None
            if contract == EXECUTION_CONTRACT and script == "scripts/workspace_check.py":
                try:
                    tokens = script_tokens(args)
                except ValueError:
                    tokens = []  # Receipt validation below owns malformed arguments.
                if (tokens and tokens[0] == "capture" and not any(t in {"--help", "-h"} for t in tokens)
                        and any(f["check"] in RECOVERABLE_CAPTURE_CHECKS for f in result["failures"])):
                    require(("references/workspace-recovery.md",), seq)
            if name == "bash":
                for filename in ("workspace_check.py", "report_check.py", "log_evidence.py"):
                    if exposed_skill + "/scripts/" + filename in str(args.get("command", "")):
                        script = "scripts/" + filename
            if script:
                result["script_invocations"].append({"seq": seq, "script": script,
                    "channel": "native" if name == "run_skill_script" else "shell"})
            if is_build(p):
                result["builds"].append(seq)
                require(("references/workspace-cleanup.md",), seq)
                # A group can reuse its capture despite generated outputs, but
                # must not reuse it after candidate changes (checked below).
                if active_capture is None:
                    fail("build_without_confirmed_native_capture", seq)
                elif last_build < active_capture["seq"] and state != active_capture["state"]:
                    fail("capture_not_current_at_build", seq)
                build_states.append({"seq": seq, "state": state,
                                     "binding": active_capture["binding"] if active_capture else None,
                                     "valid_capture": active_capture is not None and not any(f["seq"] == seq for f in result["failures"])})
                last_build = seq
            continue
        if kind != "InvocationToolCallResult":
            continue
        start = calls.get(p.get("call_id"))
        if not start:
            continue  # normalize_trace owns pairing errors
        args = start["payload"].get("args", {})
        name = start["payload"].get("tool_name")
        value = p.get("result")
        if name == "bash" and exposed_skill + "/scripts/workspace_check.py" in str(args.get("command", "")):
            # Record what the shell returned, without pretending to parse shell
            # substitution or granting it native provenance. Protocol still fails.
            try:
                observed = workspace_result(value)
                result["non_native_workspace_observations"].append(
                    {"seq": seq, "operation": observed.get("operation"), "result": observed["result"],
                     "binding": "not_confirmed_by_native_tool_audit"})
            except ValueError:
                pass
        if args.get("skill_name") != SKILL_NAME:
            continue
        if name in {"load_skill", "read_skill_resource"}:
            resource = "SKILL.md" if name == "load_skill" else args.get("resource_name", "")
            path = Path(resource)
            if path.is_absolute() or ".." in path.parts or path.name.lower() == "readme.md":
                fail("invalid_runtime_skill_resource", seq)
                continue
            try:
                body = read_regular(skill / path).decode().strip()
                complete = isinstance(value, str) and (value.strip().startswith(body) if name == "load_skill" else value.strip() == body)
            except (OSError, EvalError, UnicodeError):
                complete = False
            result["resource_reads"].append({"resource": resource, "seq": seq, "complete": complete})
            if complete:
                reads[resource] = seq
                if name == "load_skill":
                    loaded = True
            continue
        if name != "run_skill_script" or args.get("script_name") != "scripts/workspace_check.py":
            continue
        try:
            tokens = script_tokens(args)
            if "--help" in tokens or "-h" in tokens:
                continue
            receipt = workspace_result(value)
            observation = {"seq": seq, "result": receipt["result"], "operation": receipt.get("operation"),
                           "binding": "unconfirmed"}
            result["workspace_checks"].append(observation)
            if receipt["result"] == "incomplete":
                latest_check = None
                continue
            if (not tokens or tokens[0] != receipt.get("operation") or receipt.get("roots") != [exposed_root]
                    or options(tokens, "--root") != [exposed_root] or receipt.get("gaps") != []):
                raise ValueError("scope or operation mismatch")
            for flag, key in (("--manifest", "manifest"), ("--invocation-id", "invocation_id"), ("--candidate-id", "candidate_id")):
                if options(tokens, flag) != [receipt.get(key)] or not receipt.get(key):
                    raise ValueError("request/receipt binding mismatch")
            manifest = manifest_file(receipt["manifest"])
            sha = digest(json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode())
            roots = manifest.get("roots", [])
            if (sha != receipt.get("manifest_sha256") or len(roots) != 1 or roots[0].get("path") != exposed_root
                    or any(manifest.get(k) != receipt.get(k) for k in ("invocation_id", "candidate_id"))):
                raise ValueError("manifest binding mismatch")
            captured_state = digest(json_bytes(roots[0]["entries"]))
            binding = (receipt["manifest"], sha, receipt["invocation_id"], receipt["candidate_id"])
            if receipt["operation"] == "capture":
                if receipt["result"] != "captured" or not state or state != start["payload"].get("source_state_sha256") or state != captured_state:
                    raise ValueError("capture differs from observed state")
                active_capture = {"seq": seq, "state": state, "binding": binding}
                captures[binding] = active_capture
                latest_check = None
            else:
                if (receipt["operation"] != "check" or binding not in captures
                        or options(tokens, "--manifest-sha256") != [sha]):
                    raise ValueError("check lacks original capture")
                latest_check = {"seq": seq, "result": receipt["result"], "state": state, "binding": binding,
                                "capture_seq": captures[binding]["seq"]}
                if receipt["result"] == "match" and (not state or state != captured_state
                        or state != start["payload"].get("source_state_sha256")
                        or receipt.get("counts") != {"added": 0, "changed": 0, "missing": 0}):
                    raise ValueError("match conflicts with observed state")
            observation["binding"] = "confirmed"
        except (KeyError, TypeError, ValueError, OSError, EvalError):
            result["gaps"].append("workspace_receipt_unconfirmed:E" + str(seq).zfill(4))
            latest_check = None

    if not loaded:
        fail("native_skill_load_unconfirmed")
    if expected != "stop" and not result["builds"]:
        fail("agent_build_not_observed")
    cleanup_required = expected != "stop" or bool(result["builds"])
    if cleanup_required:
        if not latest_check or latest_check["result"] != "match" or latest_check["seq"] <= last_build:
            fail("final_native_cleanup_match_unconfirmed")
        elif latest_check["capture_seq"] >= last_build:
            fail("final_capture_has_no_subsequent_build")
        elif latest_check["state"] != digest(json_bytes(source_state(after))):
            fail("workspace_changed_after_cleanup_check")
    if contract in RECOVERY_EXECUTION_CONTRACTS:
        result["violations"] = [dict(f) for f in result["failures"]]
        result["recoveries"] = capture_recoveries(result["failures"], build_states, captures, latest_check,
                                                  digest(json_bytes(source_state(after))), [*trace_gaps, *result["gaps"]])
        resolved = [r["violation"] for r in result["recoveries"]]
        result["failures"] = [f for f in result["failures"] if f not in resolved]
    protocol_failures = [f for f in result["failures"] if f["check"].startswith("required_skill_resource_not_loaded_before_action:") or f["check"] in
                         {"skill_native_tool_bypassed", "skill_resource_before_native_load", "native_skill_load_unconfirmed", "invalid_runtime_skill_resource"}]
    all_observations = result["workspace_checks"] + result["non_native_workspace_observations"]
    cleanup_failures = [f for f in result["failures"] if f not in protocol_failures]
    result["dimensions"] = {
        "native_protocol": {"status": "failed" if protocol_failures else "passed", "violations": protocol_failures},
        "workspace_observations": {"count": len(all_observations),
            "latest": max(all_observations, key=lambda row: row["seq"]) if all_observations else None,
            "meaning": "observed tool output; not an independently admitted repair or a native binding"},
        "cleanup_evidence": {"status": "not_required" if not cleanup_required else
            ("confirmed" if not cleanup_failures and not result["gaps"] and not trace_gaps else "unconfirmed"),
            "basis": "bound native capture, build ordering, final match and final state; v2/v3 recovery additionally requires full-state reconstruction proof"}}
    if contract in RECOVERY_EXECUTION_CONTRACTS:
        result["dimensions"]["process_compliance"] = {
            "status": "violations_observed" if result["violations"] else "no_observed_violation",
            "violations": result["violations"], "recoveries": result["recoveries"],
            "meaning": "recovery can unblock independent verification; it does not erase process history or establish repair correctness"}
    return result
