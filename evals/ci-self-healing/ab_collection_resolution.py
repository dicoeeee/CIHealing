"""Correct exact frozen shared-resource aliases to existing review evidence IDs.

This changes evidence registration only. It never changes model judgments,
reasons, overall opinion, runtime evidence, or the frozen reviewer.
"""
import json
from pathlib import Path

from ab_review import MODEL_ID, RESPONSE_MODEL_ID, validate
from common import EvalError, digest, json_bytes, read_json, read_regular, write_json

CONTRACT = "ci-ab-shared-resource-collection-v1"


def resolve_collection(trial, original, exchange, *, snapshot_root=None):
    if original.get("gaps") != ["unknown review evidence"]:
        return {"contract": CONTRACT, "status": "not_applicable"}
    trial = Path(trial)
    folder = trial / "review"
    result = {"contract": CONTRACT, "status": "rejected", "trial_path": str(trial),
              "original_review_status": original.get("status"), "original_review_gaps": original.get("gaps")}
    try:
        process = original.get("process", {})
        if (original.get("status") != "incomplete" or process.get("status") != "completed" or process.get("returncode") != 0
                or exchange.get("status") != "validated" or exchange.get("exchange_count") != 1
                or exchange.get("request_model") != MODEL_ID or exchange.get("response_model") != RESPONSE_MODEL_ID):
            raise ValueError("original execution/model receipt not valid")
        binding = read_json(folder / "binding.json")
        if (binding.get("model_id") != MODEL_ID or original.get("model", {}).get("model_id") != MODEL_ID
                or binding.get("profile_sha256") != original.get("model", {}).get("profile_sha256")):
            raise ValueError("original reviewer binding mismatch")
        raw = read_json(folder / "public-response.json")
        if raw != read_json(folder / "execution/stdout.log"):
            raise ValueError("original public response differs from native output")
        sessions = read_json(folder / "session-receipt.json").get("sessions", [])
        if (len(sessions) != 1 or sessions[0].get("meta", {}).get("model_id") != MODEL_ID
                or raw.get("session_id") != sessions[0].get("meta", {}).get("session_id")
                or raw.get("session_id") != exchange.get("session_id")):
            raise ValueError("original session identity mismatch")
        path = Path(sessions[0]["path"])
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("invalid original session path")
        trajectory = folder / "sessions" / path.parent / "trajectory/events.jsonl"
        if digest(read_regular(trajectory, limit=64 * 1024 * 1024)) != exchange.get("trajectory_sha256"):
            raise ValueError("original model trajectory changed")
        packet = read_json(folder / "input/packet.json")
        if digest(json_bytes(packet)) != binding.get("packet_sha256"):
            raise ValueError("frozen review packet changed")
        content = raw.get("result", raw.get("response", raw.get("text", raw.get("content"))))
        if not isinstance(content, str):
            raise ValueError("original model final text missing")
        opinion = json.loads(content)
        ids = set(packet.get("evidence_ids", []))
        try:
            validate(opinion, ids)
        except ValueError as exc:
            if str(exc) != "unknown review evidence":
                raise ValueError("original review has another schema error") from exc
        else:
            raise ValueError("original opinion does not have the claimed registration error")
        resources, calls, aliases = packet.get("shared_resources", {}), {}, {}
        for event in packet.get("events", []):
            payload = event.get("payload", {})
            if event.get("kind") == "InvocationToolCallStart":
                calls[payload.get("call_id")] = event
                continue
            if event.get("kind") != "InvocationToolCallResult":
                continue
            start = calls.get(payload.get("call_id"), {})
            args = start.get("payload", {}).get("args", {})
            tool = start.get("payload", {}).get("tool_name")
            name = "SKILL.md" if tool == "load_skill" else args.get("resource_name") if tool == "read_skill_resource" else None
            value, evidence = payload.get("result"), event.get("evidence_id")
            if (not isinstance(name, str) or name not in resources or not isinstance(resources[name], str)
                    or args.get("skill_name") != "ci-self-healing" or not isinstance(value, dict)
                    or value.get("matched_full_body") is not True or value.get("shared_resource") != name
                    or value.get("sha256") != digest(resources[name].encode()) or evidence not in ids
                    or type(start.get("seq")) is not int or type(event.get("seq")) is not int
                    or start["seq"] >= event["seq"]):
                continue
            aliases.setdefault(name, evidence)
        unknown = {ref for check in opinion["checks"] for ref in check["evidence"] if ref not in ids}
        if not unknown or any(ref not in aliases for ref in unknown):
            raise ValueError("unknown reference is not an exactly matched full shared resource")
        corrected = json.loads(json.dumps(opinion))
        changes = []
        for before, after in zip(opinion["checks"], corrected["checks"]):
            after["evidence"] = [aliases.get(ref, ref) if ref not in ids else ref for ref in before["evidence"]]
            if after["evidence"] != before["evidence"]:
                changes.append({"check_id": before["id"], "original_refs": before["evidence"], "resolved_refs": after["evidence"]})
        verdict = validate(corrected, ids)
        # Only evidence arrays differ; all original semantic values are retained.
        if any({k: c[k] for k in c if k != "evidence"} != {k: d[k] for k in d if k != "evidence"}
               for c, d in zip(opinion["checks"], corrected["checks"])) or opinion["verdict"] != verdict:
            raise ValueError("semantic values changed during registration correction")
        result.update(status="corrected", original_opinion=opinion, effective_opinion=corrected,
            mapping={ref: aliases[ref] for ref in sorted(unknown)}, changed_references=changes,
            effective_review={"status": "completed", "verdict": verdict},
            source_sha256={"original_result": digest(read_regular(folder / "result.json")),
                "public_response": digest(read_regular(folder / "public-response.json")),
                "exchange_receipt": digest(read_regular(folder / "exchange-receipt.json")),
                "packet": binding["packet_sha256"], "resolver": digest(read_regular(Path(__file__))),
                "validator": digest(read_regular(Path(__file__).parent / "ab_review.py"))},
            boundary="exact resource-key to existing full-read event registration; model statuses, reasons and overall opinion unchanged; no model rerun")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, EvalError) as exc:
        result["reason"] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
    if snapshot_root is not None:
        destination = Path(snapshot_root)
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / (digest(json_bytes(result)) + ".json")
        if not path.exists():
            write_json(path, result)
        elif read_regular(path) != json_bytes(result):
            raise EvalError("immutable collection correction differs")
        result = {**result, "snapshot_path": str(path)}
    return result
