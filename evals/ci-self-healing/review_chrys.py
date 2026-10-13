#!/usr/bin/env python3
"""Offline preparation/collection for native `chrys run` evidence reviews.

This is not a new runner, sandbox, or HTTP client. It never launches a model,
loads credentials, repairs a verdict, or promotes the suite's grade.
"""

import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

from common import EvalError, digest, json_bytes, read_json, read_regular, write_json, write_new
from review import RUBRIC, build_prompt, make_packet, message_channel_sequences, schema, validate_verdict
from review_limits import DEFAULT_PACKET_BYTES
from suite import load_suite, select_trials

PUBLIC_OUTPUT_LIMIT = 1024 * 1024


def strict_json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise EvalError("duplicate JSON key")
            value[key] = item
        return value

    def reject_constant(_):
        raise EvalError("non-JSON numeric constant")

    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise EvalError("invalid JSON; no fence stripping, coercion or repair") from exc


def assess_output(raw, packet):
    """Return public diagnostics, keeping model opinion separate from validity."""
    errors = []
    try:
        value = strict_json(raw)
    except EvalError as exc:
        return {"model_verdict": None, "output_status": "invalid", "errors": [{"kind": "json", "message": str(exc)}]}
    # Collect common field/sequence mistakes together instead of hiding later
    # issues behind the first missing field. The authoritative validator still
    # runs below and is never replaced with inference from prose or ordering.
    if isinstance(value, dict):
        criteria = value.get("criteria")
        if isinstance(criteria, list):
            for index, item in enumerate(criteria):
                if isinstance(item, dict):
                    for field in ("id", "status", "evidence", "explanation"):
                        if field not in item:
                            errors.append({"kind": "missing_field", "path": ["criteria", index, field]})
        temporal = value.get("temporal")
        if isinstance(temporal, dict):
            admission = temporal.get("admission_seq")
            if type(admission) is int and admission not in message_channel_sequences(packet):
                errors.append({"kind": "admission_channel", "path": ["temporal", "admission_seq"],
                               "message": "reference must identify a public Agent message, not a tool event or file", "seq": admission})
            seq = temporal.get("first_candidate_change_seq")
            indexed = {e["seq"]: e for e in packet["events"]}
            if type(seq) is int and seq in indexed and not any(
                p.startswith("scene/sourcecode/src/main/java/")
                for p in indexed[seq].get("payload", {}).get("changed_paths_since_previous_event", [])
            ):
                errors.append({"kind": "temporal", "path": ["temporal", "first_candidate_change_seq"],
                               "message": "reference must identify observed source change, not tool-call start", "seq": seq})
    try:
        validate_verdict(value, packet)
    except EvalError as exc:
        errors.append({"kind": "contract", "message": str(exc)})
    opinion = value.get("verdict") if isinstance(value, dict) else None
    return {"model_verdict": opinion if opinion in ("pass", "fail", "incomplete") else None,
            "output_status": "invalid" if errors else "valid", "errors": errors}


def prepare_native(suite, trial_id, output, profile_id, endpoint_host, chrys_version,
                   max_packet_bytes=DEFAULT_PACKET_BYTES, *, compact=False, frozen_packet=None, cli="icode"):
    suite, output = Path(suite).absolute(), Path(output).absolute()
    if output == suite or suite in output.parents:
        raise EvalError("native review output must be outside the frozen suite")
    if output.exists() or any(p.is_symlink() for p in (output, *output.parents)):
        raise EvalError("native output already exists or traverses a symlink")
    if not all(isinstance(v, str) and v.strip() for v in (profile_id, endpoint_host, chrys_version)):
        raise EvalError("explicit model profile, endpoint host and Chrys version are required")
    if not isinstance(cli, str) or not cli.strip() or any(c in cli for c in "\x00\n\r"):
        raise EvalError("explicit native CLI executable required")
    if any(c in endpoint_host for c in "/:@?#\\ \n\r"):
        raise EvalError("endpoint-host must be a hostname, not a URL or credential")
    manifest = load_suite(suite)
    if manifest["reviewer_model"] != "glm-5.3":
        raise EvalError("native GLM preparation cannot change the suite's reviewer")
    rows = select_trials(manifest, trial_id)
    if len(rows) != 1:
        raise EvalError("native preparation requires exactly one trial")
    row = rows[0]
    packet = make_packet(suite, row, max_packet_bytes) if frozen_packet is None else frozen_packet
    from review_limits import check_packet
    check_packet(packet, max_packet_bytes)
    model_packet = packet
    if compact:
        from review_packet import project_packet
        model_packet = project_packet(packet, suite / "trials" / row["id"])
        check_packet(model_packet, max_packet_bytes)
        write_json(output / "input/model-packet.json", model_packet)
    prompt = build_prompt(model_packet).encode()
    binding = {"format": "ci-native-review-v1", "transport": "chrys-run-native",
               "suite": str(suite), "trial_id": row["id"],
               "grade_sha256": digest(read_regular(suite / "trials" / row["id"] / "grade.json")),
               "packet_sha256": digest(json_bytes(packet)), "prompt_sha256": digest(prompt),
               "max_packet_bytes": max_packet_bytes,
               "rubric_sha256": digest(RUBRIC.encode()), "schema_sha256": digest(json_bytes(schema())),
               "profile_name": "huoshan-glm5.3", "profile_id": profile_id, "requested_model_id": "glm-5.3",
               "endpoint_host": endpoint_host, "chrys_version": chrys_version, "cli": cli,
               "agent_name": "QA", "agent_profile_id": "b0119a000005", "workdir": str(output / "work"),
               "identity_source": "operator-declared; checked against native session at collection"}
    if compact:
        from review_packet import FORMAT
        binding.update(projection_format=FORMAT, model_packet_sha256=digest(json_bytes(model_packet)),
                       projection_policy_sha256=digest(read_regular(Path(__file__).with_name("review_packet.py"))))
    write_json(output / "binding.json", binding)
    write_json(output / "input/packet.json", packet)
    write_json(output / "input/schema.json", schema())
    write_new(output / "input/task.txt", prompt)
    (output / "work").mkdir()
    # Informational argv only. The caller supplies an approved bounded runner
    # and records its real exit/timeout status. --workdir is not isolation.
    argv = [cli, "run", "--agent", "QA", "--model", "huoshan-glm5.3",
            "--task", str(output / "input/task.txt"), "--workdir", str(output / "work"), "--json", "--quiet"]
    write_json(output / "invocation.json", {"argv": argv, "models_called": False,
               "requirements": ["fresh session", "approved evidence/destination", "external isolation and timeout",
                                "capture stdout and process status", "preserve native session locally"]})
    return {"status": "prepared", "output": str(output), "models_called": False, "packet_bytes": len(json_bytes(packet))}


def assess_native(raw, process, session, packet, prompt, binding):
    gaps = []
    public = None
    reply = None
    try:
        reply = strict_json(raw)
        if (not isinstance(reply, dict) or not isinstance(reply.get("session_id"), str)
                or not reply["session_id"] or not isinstance(reply.get("result"), str)):
            raise EvalError("missing native session_id/result")
        public = reply["result"]
    except EvalError as exc:
        gaps.append(str(exc))
    output = assess_output(public, packet) if public is not None else {
        "model_verdict": None, "output_status": "invalid", "errors": [{"kind": "envelope", "message": "native public result unavailable"}]}
    process = process if isinstance(process, dict) else {}
    if process.get("status") != "completed" or type(process.get("returncode")) is not int or process["returncode"] != 0:
        gaps.append("native process did not complete successfully")
    # Only allowlisted metadata is retained; never copy session reasoning,
    # tool arguments, credentials, raw HTTP or arbitrary session properties.
    meta = session.get("meta", {}) if isinstance(session, dict) else {}
    state = session.get("state", {}) if isinstance(session, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    state = state if isinstance(state, dict) else {}
    if not meta or not state:
        gaps.append("native session evidence missing")
    for key, expected in (("model_id", binding["requested_model_id"]), ("model_profile_id", binding["profile_id"]),
                          ("agent_profile_id", binding["agent_profile_id"]), ("app_version", binding["chrys_version"]),
                          ("primary_cwd", binding["workdir"]), ("session_id", reply.get("session_id") if isinstance(reply, dict) else None)):
        if expected is None or meta.get(key) != expected:
            gaps.append("native session mismatch: " + key)
    url = urlsplit(meta.get("model_base_url", "") if isinstance(meta.get("model_base_url"), str) else "")
    if url.scheme != "https" or url.hostname != binding["endpoint_host"] or url.username or url.password or url.query or url.fragment:
        gaps.append("native endpoint does not match declared HTTPS gateway")
    messages = state.get("messages")
    messages_present = isinstance(messages, list) and bool(messages)
    messages = messages if isinstance(messages, list) else []
    users, answers, tool_count, unsupported = [], [], 0, False
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("contents"), list):
            unsupported = True
            continue
        text = []
        if message.get("role") not in ("user", "assistant", "tool"):
            unsupported = True
        if message.get("role") == "tool":
            tool_count += 1
        for content in message["contents"]:
            if not isinstance(content, dict) or not isinstance(content.get("type"), str):
                unsupported = True
                continue
            kind = content["type"]
            if "function" in kind or "tool" in kind:
                tool_count += 1
            elif kind == "text" and isinstance(content.get("text"), str):
                text.append(content["text"])
            elif kind != "text_reasoning":
                unsupported = True
        props = message.get("additional_properties") or {}
        marker = props.get("_chrys_kind") if isinstance(props, dict) else None
        if message.get("role") == "user":
            users.append("".join(text))
        elif message.get("role") == "assistant" and not marker:
            answers.append("".join(text))
        elif marker and marker != "turn":
            gaps.append("native session contains non-turn status marker")
    if len(users) != 1 or digest(users[0].encode()) != digest(prompt):
        gaps.append("native session does not contain exactly the frozen review task")
    if public is None or answers != [public]:
        gaps.append("native final answer differs from saved public assistant answer")
    if tool_count or unsupported:
        gaps.append("native reviewer used tools or has unsupported message content")
    compressed = state.get("compressed_msgs")
    if not isinstance(compressed, list) or compressed:
        gaps.append("native context compaction is present or cannot be checked")
    return {"transport": "chrys-run-native", **output,
            "process": {key: process.get(key) for key in ("status", "returncode", "duration_seconds")},
            "session_id": meta.get("session_id"),
            "identity": {"requested_model_id": binding["requested_model_id"], "runtime_model_id": meta.get("model_id"),
                         "runtime_profile_id": meta.get("model_profile_id"), "provider_reported_model_id": None},
            "persisted_tool_item_count": tool_count if messages_present and not unsupported else None,
            "persisted_compaction_count": len(compressed) if isinstance(compressed, list) else None,
            "gaps": gaps, "call_record_gaps": ["provider response model and complete request/retry history not collected"],
            "formal_evaluation_status": "incomplete", "suite_grade_modified": False}


def collect_native(directory, stdout, process_file, session_file=None):
    directory = Path(directory).absolute()
    if (directory / "collected").exists() or (directory / "collected").is_symlink():
        raise EvalError("native collection already exists; never overwrite an attempt")
    binding = read_json(directory / "binding.json")
    if binding.get("format") != "ci-native-review-v1":
        raise EvalError("unsupported native preparation")
    packet = read_json(directory / "input/packet.json")
    prompt = read_regular(directory / "input/task.txt")
    if (digest(json_bytes(packet)) != binding["packet_sha256"] or digest(prompt) != binding["prompt_sha256"]
            or digest(RUBRIC.encode()) != binding["rubric_sha256"] or digest(json_bytes(schema())) != binding["schema_sha256"]):
        raise EvalError("native inputs or review contract drifted")
    if binding.get("model_packet_sha256") and digest(read_regular(directory / "input/model-packet.json")) != binding["model_packet_sha256"]:
        raise EvalError("native projected evidence changed")
    suite = Path(binding["suite"])
    row = select_trials(load_suite(suite), binding["trial_id"])[0]
    if (digest(read_regular(suite / "trials" / row["id"] / "grade.json")) != binding["grade_sha256"]
            or digest(json_bytes(make_packet(suite, row, binding["max_packet_bytes"]))) != binding["packet_sha256"]):
        raise EvalError("original trial evidence changed")
    raw = read_regular(stdout, PUBLIC_OUTPUT_LIMIT)
    process = read_json(process_file)
    session_bytes = read_regular(session_file) if session_file else None
    session = strict_json(session_bytes) if session_bytes is not None else None
    receipt = assess_native(raw, process, session, packet, prompt, binding)
    receipt.update(trial_id=binding["trial_id"], packet_sha256=binding["packet_sha256"],
                   prompt_sha256=binding["prompt_sha256"], stdout_sha256=digest(raw),
                   session_sha256=digest(session_bytes) if session_bytes is not None else None,
                   collector_sha256=digest(read_regular(Path(__file__))))
    write_new(directory / "collected/stdout.log", raw)
    # Keep exact public text even when it is invalid; do not fill fields, replace
    # event numbers, strip fences, or retry until a pass is produced.
    try:
        reply = strict_json(raw)
        if isinstance(reply, dict) and isinstance(reply.get("result"), str):
            write_new(directory / "collected/result.txt", reply["result"])
    except EvalError:
        pass
    write_json(directory / "collected/receipt.json", receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    prepare = commands.add_parser("prepare", help="freeze one native review task; no model calls")
    for name in ("suite", "trial", "output", "profile-id", "endpoint-host", "chrys-version"):
        prepare.add_argument("--" + name, required=True)
    prepare.add_argument("--max-packet-bytes", type=int, default=DEFAULT_PACKET_BYTES)
    prepare.add_argument("--cli", default="icode", help="selected native CLI executable; no automatic fallback")
    collect = commands.add_parser("collect", help="validate existing CLI evidence; never promote suite grades")
    for name in ("directory", "stdout", "process-file"):
        collect.add_argument("--" + name, required=True)
    collect.add_argument("--session-file")
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            result = prepare_native(args.suite, args.trial, args.output, args.profile_id,
                                    args.endpoint_host, args.chrys_version, args.max_packet_bytes, cli=args.cli)
            code = 0
        else:
            result = collect_native(args.directory, args.stdout, args.process_file, args.session_file)
            # Collection complete is not acceptance. Missing request provenance
            # stays incomplete even if the public verdict is well formed.
            code = 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except (EvalError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "incomplete", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
