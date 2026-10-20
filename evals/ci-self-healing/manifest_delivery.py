"""File-change delivery checks for explicitly versioned v2 evaluation runs.

The checker is frozen by suite.prepare, never selected from candidate code.
These fixture checks bind paths/operations to observed final source changes;
they do not determine whether a repair is semantically correct.
"""
import json
from pathlib import Path
import re
import subprocess
import sys

from common import EvalError, changes, digest, read_regular
from repair_support import SKILL_NAME, source_state
from skill_execution import options, script_tokens


MANIFEST_NAME = "output/file_change.json"


def check_manifest(root, trial, before, after):
    """Independent final-file check. Missing output is a gap, invalid output fails."""
    path = Path(trial) / "visible" / MANIFEST_NAME
    result = {"status": "incomplete", "failures": [], "gaps": []}
    try:
        body = read_regular(path, 2 * 1024 * 1024)
    except (OSError, EvalError):
        result["gaps"].append("file_change_manifest_unavailable")
        return result
    sha = digest(body)
    result["sha256"] = sha
    if after.get(MANIFEST_NAME, {}).get("sha256") != sha:
        result["gaps"].append("file_change_manifest_snapshot_mismatch")
        return result
    try:
        checked = subprocess.run([sys.executable, "-B", str(Path(root) / "checker/scripts/file_change_check.py"),
                                  "--manifest", str(path)], capture_output=True, text=True, timeout=10)
        value = json.loads(checked.stdout)
        if not isinstance(value, dict):
            raise ValueError("checker response is not an object")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        result["gaps"].append("file_change_checker_unavailable")
        return result
    result["format_check"] = {"exit_code": checked.returncode, "result": value}
    if checked.returncode == 1 and value.get("format_status") == "invalid" and value.get("sha256") == sha:
        result["failures"].append("file_change_manifest_format")
    elif checked.returncode != 0 or value != {"format_status": "valid", "sha256": sha, "errors": []}:
        result["gaps"].append("file_change_check_unconfirmed")
    else:
        document = json.loads(body)
        # Fixture repository mapping is trusted only while the protected scene
        # metadata still matches the before-run snapshot.
        mapping_path = "scene/build_info.json"
        mapping = Path(trial) / "visible" / mapping_path
        metadata, repositories = None, None
        try:
            metadata = read_regular(mapping)
            if before.get(mapping_path, {}).get("sha256") != digest(metadata):
                result["gaps"].append("file_change_repository_mapping_changed")
            else:
                info = json.loads(metadata)
                repositories = info.get("repositories") if isinstance(info, dict) else None
                if (not isinstance(repositories, list) or len(repositories) != 1
                        or not isinstance(repositories[0], dict)
                        or not isinstance(repositories[0].get("name"), str)
                        or not repositories[0]["name"].strip()):
                    raise ValueError("unsupported fixture repository mapping")
        except (OSError, EvalError, ValueError):
            repositories = None
            result["gaps"].append("file_change_repository_mapping_unavailable")
        if repositories is not None:
            repo = repositories[0]["name"]
            old, new = source_state(before), source_state(after)
            expected = set()
            for name in changes(old, new):
                if name == "." or all(state.get(name, {}).get("kind") in {None, "directory"} for state in (old, new)):
                    continue
                op = "create" if name not in old else "delete" if name not in new else "modify"
                expected.add((repo, name, op))
            actual = {(item["repo"], item["file"], item["op"]) for item in document["changes"]}
            if actual != expected:
                result["failures"].append("file_change_manifest_does_not_match_source")
    result["status"] = "failed" if result["failures"] else "incomplete" if result["gaps"] else "passed"
    return result


def complete_readback(value, body):
    """Match every nonblank line, accommodating native line-number headers."""
    if not isinstance(value, str):
        return False
    wanted = [line.rstrip() for line in body.decode().splitlines() if line.strip()]
    pos = 0
    for line in value.splitlines():
        if pos < len(wanted) and re.sub(r"^\d+\|", "", line).rstrip() == wanted[pos]:
            pos += 1
    return pos == len(wanted)


def manifest_checkpoint(events, path, body, stops):
    """Bind the empty stop manifest to final bytes and native tool receipts.

    Used only by the narrow timeout-artifact exception. Ordinary completed runs
    retain their existing trace audit and independent final-file checks.
    """
    path, sha = str(path), digest(body)
    result = {"sha256": sha, "gaps": []}
    calls, writes, checks, readbacks = {}, [], [], []
    for event in events:
        p, kind, seq = event["payload"], event["kind"], event["seq"]
        if kind == "InvocationToolCallStart":
            calls[p["call_id"]] = event
            args, name = p.get("args", {}), p.get("tool_name")
            if name == "write_file" and args.get("path") == path:
                if not any(stop < seq for stop in stops) or args.get("content", "").encode() != body:
                    result["gaps"].append("manifest_write_not_bound_to_stop_and_final_bytes")
        elif kind == "InvocationToolCallResult" and p.get("call_id") in calls:
            start = calls[p["call_id"]]
            args, name = start["payload"].get("args", {}), start["payload"].get("tool_name")
            if name == "write_file" and args.get("path") == path and MANIFEST_NAME in p.get("changed_paths_since_previous_event", []):
                writes.append(seq)
            if name == "run_skill_script" and args.get("script_name") == "scripts/file_change_check.py":
                tokens = script_tokens(args)
                if "--help" in tokens or "-h" in tokens:
                    continue
                value = json.loads(p["result"])
                if (args.get("skill_name") == SKILL_NAME and options(tokens, "--manifest") == [path]
                        and value == {"format_status": "valid", "sha256": sha, "errors": []}):
                    checks.append((start["seq"], seq))
                else:
                    result["gaps"].append("manifest_check_not_valid")
            if name == "read_file" and args.get("path") == path and complete_readback(p.get("result"), body):
                readbacks.append((start["seq"], seq))
    if not (stops and writes and checks and readbacks
            and stops[-1] < writes[-1] < min(checks[-1][0], readbacks[-1][0])):
        result["gaps"].append("stop_manifest_checkpoint_missing")
    else:
        result.update(write_seq=writes[-1], check_seq=checks[-1][1], readback_seq=readbacks[-1][1],
                      checkpoint_seq=max(checks[-1][1], readbacks[-1][1]))
    return result
