"""Lossless review collection, plus read-only replay of frozen review records.

No model invocation, retry, semantic adjudication or writes live in this module.
Only observed representation variants and exact full-resource aliases are accepted.
The legacy v1 resolver remains unchanged for historical aggregate comparisons.
"""
from collections import Counter
import copy
import json
from pathlib import Path

from common import EvalError, digest, json_bytes, read_regular

CONTRACT = "ci-ab-review-collection-v2"


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result

    def constant(value):
        raise ValueError("non-JSON numeric constant: " + value)

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def _parse(text, changes):
    if not isinstance(text, str):
        raise ValueError("review final text missing")
    # Escape only literal TABs inside strings. Do not strip text, extract a JSON
    # substring, fix syntax, accept fences, or permit other raw control characters.
    quoted, escaped, offsets, output = False, False, [], []
    for index, char in enumerate(text):
        if quoted and not escaped and char == "\t":
            output.append("\\t")
            offsets.append(index)
            continue
        output.append(char)
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
    value = strict_json("".join(output))
    if offsets:
        changes.append({"kind": "escape_literal_string_tabs", "character_offsets": offsets,
                        "decoded_characters_preserved": True})
    return value


def resource_aliases(packet):
    """Return only unambiguous paired full reads, never filename guesses.

    The packet is host-frozen evidence. File names alone are not evidence;
    matching call identity, origin, order, full-body marker and digest are needed.
    """
    resources = packet.get("shared_resources", {})
    ids = packet.get("evidence_ids", [])
    events = packet.get("events", [])
    if not isinstance(resources, dict) or not isinstance(ids, list) or not isinstance(events, list):
        raise ValueError("invalid frozen evidence registry")
    if any(not isinstance(x, str) or not x for x in ids) or len(ids) != len(set(ids)):
        raise ValueError("invalid or duplicate frozen evidence IDs")
    starts, results, evidence_counts = {}, {}, Counter()
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
            raise ValueError("invalid frozen event")
        evidence = event.get("evidence_id")
        if isinstance(evidence, str):
            evidence_counts[evidence] += 1
        target = starts if event.get("kind") == "InvocationToolCallStart" else results if event.get("kind") == "InvocationToolCallResult" else None
        call_id = event["payload"].get("call_id")
        if target is not None and isinstance(call_id, str) and call_id:
            target.setdefault(call_id, []).append(event)
    aliases = {}
    for call_id, rows in results.items():
        candidates = starts.get(call_id, [])
        if len(rows) != 1 or len(candidates) != 1:
            continue
        start, event = candidates[0], rows[0]
        before, after = start["payload"], event["payload"]
        args = before.get("args", {})
        origin = before.get("origin")
        if (not isinstance(args, dict) or not isinstance(origin, dict) or origin != after.get("origin")
                or any(not isinstance(origin.get(k), str) or not origin[k] for k in ("session_id", "invocation_id"))):
            continue
        tool = before.get("tool_name")
        name = "SKILL.md" if tool == "load_skill" else args.get("resource_name") if tool == "read_skill_resource" else None
        value, evidence = after.get("result"), event.get("evidence_id")
        if (not isinstance(name, str) or name not in resources or not isinstance(resources[name], str)
                or args.get("skill_name") != "ci-self-healing" or not isinstance(value, dict)
                or value.get("matched_full_body") is not True or value.get("shared_resource") != name
                or value.get("sha256") != digest(resources[name].encode()) or evidence not in ids
                or evidence_counts[evidence] != 1 or type(start.get("seq")) is not int
                or type(event.get("seq")) is not int or start["seq"] >= event["seq"]):
            continue
        aliases.setdefault(name, {"evidence_id": evidence, "call_id": call_id,
            "start_seq": start["seq"], "result_seq": event["seq"], "sha256": value["sha256"], "origin": origin})
    return aliases


def collect(content, packet):
    """Pure transport/schema normalization; incomplete data stays incomplete."""
    from ab_review import CRITERIA, validate
    result = {"contract": CONTRACT, "status": "incomplete", "verdict": None, "gaps": [],
              "normalizations": [], "mapping": {}, "stage": "json",
              "packet_sha256": digest(json_bytes(packet)),
              "collector_sha256": digest(read_regular(Path(__file__))),
              "validator_sha256": digest(read_regular(Path(__file__).with_name("ab_review.py")))}
    try:
        if isinstance(content, str):
            result["raw_text_sha256"] = digest(content.encode())
        original = _parse(content, result["normalizations"])
        result["original_opinion"] = original
        value = copy.deepcopy(original)
        result["stage"] = "schema"
        if isinstance(value, dict) and isinstance(value.get("checks"), dict):
            checks = value["checks"]
            if set(checks) != set(CRITERIA):
                raise ValueError("review criteria missing or duplicate: " + json.dumps({
                    "missing": sorted(set(CRITERIA) - set(checks)), "unknown": sorted(set(checks) - set(CRITERIA))}))
            converted = []
            for key in CRITERIA:
                row = checks[key]
                if not isinstance(row, dict) or ("id" in row and row["id"] != key):
                    raise ValueError("review criterion map key/id conflict or invalid row: " + key)
                converted.append({**row, "id": key})
            value["checks"] = converted
            result["normalizations"].append({"kind": "checks_map_to_array", "ids": list(CRITERIA)})
        if isinstance(value, dict) and "evidence_note" in value:
            result["normalizations"].append({"kind": "optional_evidence_note_retained"})
        registry = resource_aliases(packet)
        ids = set(packet.get("evidence_ids", []))
        # Validate all shapes and judgments first; known exact resource aliases
        # are the only additional references permitted at this intermediate step.
        try:
            validate(value, ids | set(registry))
        except ValueError as exc:
            if str(exc) == "unknown review evidence":
                unknown = [{"check_id": row["id"], "reference": ref}
                           for row in value["checks"] if isinstance(row.get("evidence"), list)
                           for ref in row["evidence"] if not isinstance(ref, str) or ref not in ids | set(registry)]
                result["unresolved_references"] = unknown
                result["stage"] = "evidence"
            raise
        result["stage"] = "evidence"
        for row in value["checks"]:
            before = row["evidence"]
            for ref in before:
                if ref not in ids:
                    result["mapping"][ref] = registry[ref]
            row["evidence"] = [registry[ref]["evidence_id"] if ref not in ids else ref for ref in before]
            if before != row["evidence"]:
                result["normalizations"].append({"kind": "exact_full_resource_reference", "check_id": row["id"],
                                                 "original_refs": before, "resolved_refs": row["evidence"]})
        verdict = validate(value, ids)
        result.update(status="accepted", stage="complete", verdict=verdict, opinion=value)
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        result["gaps"].append(str(exc))
    return result


def collect_record(directory):
    """Read an existing review with its frozen provenance. Never write or run it."""
    from ab_review import MODEL_ID, RESPONSE_MODEL_ID
    from native_runner import exchange_receipt
    folder = Path(directory)
    sources = {}
    result = {"contract": CONTRACT, "status": "incomplete", "verdict": None, "stage": "provenance",
              "gaps": [], "source_sha256": sources, "review_directory": str(folder), "models_run": 0}

    def read(name, as_json=True):
        data = read_regular(folder / name, limit=64 * 1024 * 1024)
        sources[str(name)] = digest(data)
        return strict_json(data) if as_json else data

    try:
        original, binding = read("result.json"), read("binding.json")
        result["original_review"] = {key: original.get(key) for key in ("status", "verdict", "gaps")}
        process = original.get("process", {})
        if process.get("status") != "completed" or process.get("returncode") != 0:
            raise ValueError("original review execution did not complete successfully")
        if process != read("execution/process.json"):
            raise ValueError("original review process receipt differs")
        if (binding.get("model_id") != MODEL_ID or original.get("model", {}).get("model_id") != MODEL_ID
                or not binding.get("profile_sha256")
                or binding["profile_sha256"] != original.get("model", {}).get("profile_sha256")):
            raise ValueError("original reviewer binding mismatch")
        raw = read("public-response.json")
        if raw != read("execution/stdout.log"):
            raise ValueError("original public response differs from native output")
        sessions = read("session-receipt.json").get("sessions", [])
        if (len(sessions) != 1 or sessions[0].get("meta", {}).get("model_id") != MODEL_ID
                or raw.get("session_id") != sessions[0].get("meta", {}).get("session_id")):
            raise ValueError("original session identity mismatch")
        path = Path(sessions[0]["path"])
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("invalid original session path")
        session_path = Path("sessions") / path
        session = read(session_path)
        if (sources[str(session_path)] != sessions[0].get("sha256")
                or any(session.get("meta", {}).get(k) != v for k, v in sessions[0]["meta"].items())):
            raise ValueError("original session metadata changed")
        trajectory = read(session_path.parent / "trajectory/events.jsonl", as_json=False)
        exchange = read("exchange-receipt.json")
        replayed = exchange_receipt(trajectory, raw["session_id"], MODEL_ID, expected_response_model=RESPONSE_MODEL_ID)
        if replayed != exchange:
            raise ValueError("original model exchange receipt changed")
        packet = read("input/packet.json")
        if digest(json_bytes(packet)) != binding.get("packet_sha256"):
            raise ValueError("frozen review packet changed")
        task = read("input/task.txt", as_json=False)
        if digest(task) != binding.get("prompt_sha256"):
            raise ValueError("frozen review prompt changed")
        model_packet = read("input/model-packet.json")
        expected = json.loads(json.dumps(packet, ensure_ascii=False).replace(str(folder.parent), "<TRIAL>"))
        if model_packet != expected:
            raise ValueError("frozen model packet differs from bound packet")
        content = raw.get("result", raw.get("response", raw.get("text", raw.get("content"))))
        result.update(collect(content, packet), provenance="validated")
    except (OSError, EvalError, ValueError, TypeError, KeyError) as exc:
        result["gaps"].append(str(exc))
    return result
