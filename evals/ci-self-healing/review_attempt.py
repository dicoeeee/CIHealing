"""Explicit, one-call native re-review of frozen evidence; never rerun repair.

Creates a separate attempt, preserving the original grade/review/summary and
raw packet. No automatic retry, pass selection or promotion into a batch.
"""
from pathlib import Path

from common import EvalError, digest, json_bytes, read_json, read_regular, write_json
from native_runner import native_identity, native_preflight, native_review
from review import make_packet
from suite import load_suite, select_trials


def frozen_source(source):
    source = Path(source).absolute()
    binding = read_json(source / "binding.json")
    packet = read_json(source / "input/packet.json")
    receipt = read_json(source / "receipt.json")
    if binding.get("format") != "ci-native-review-v1" or receipt.get("status") != "incomplete":
        raise EvalError("supplemental review requires an incomplete native attempt; never retry a verdict to seek a pass")
    if (digest(json_bytes(packet)) != binding.get("packet_sha256")
            or digest(read_regular(source / "input/task.txt")) != binding.get("prompt_sha256")
            or receipt.get("packet_sha256") != binding.get("packet_sha256")
            or receipt.get("grade_sha256") != binding.get("grade_sha256")):
        raise EvalError("original review binding changed")
    suite = Path(binding["suite"])
    row = select_trials(load_suite(suite), binding["trial_id"])[0]
    if (digest(read_regular(suite / "trials" / row["id"] / "grade.json")) != binding["grade_sha256"]
            or digest(json_bytes(make_packet(suite, row, binding["max_packet_bytes"]))) != binding["packet_sha256"]):
        raise EvalError("original trial evidence changed; do not silently regrade it")
    return binding, packet, {"directory": str(source), "trial_id": row["id"],
        "packet_sha256": binding["packet_sha256"], "grade_sha256": binding["grade_sha256"],
        "binding_sha256": digest(read_regular(source / "binding.json")),
        "receipt_sha256": digest(read_regular(source / "receipt.json")),
        "boundary": "new independent assessment; original attempt and batch outcome unchanged"}


def review_attempt(source, output, config, *, execute=False):
    binding, packet, origin = frozen_source(source)
    output = Path(output).absolute()
    suite = Path(binding["suite"])
    source = Path(source).absolute()
    if (output.exists() or any(p.is_symlink() for p in (output, *output.parents))
            or output == suite or suite in output.parents or output == source or source in output.parents):
        raise EvalError("new review attempt needs an unused nonsymlink directory outside the frozen suite")
    plan = {"status": "planned", "source": origin, "output": str(output),
            "maximum_model_calls": 1, "timeout_seconds": config["timeout_seconds"],
            "requested_model": "glm-5.3", "models_called": False, "suite_modified": False}
    if not execute:
        return plan
    preflight = output.with_name(output.name + "-preflight")
    if preflight.exists() or any(p.is_symlink() for p in (preflight, *preflight.parents)):
        raise EvalError("preflight directory already exists; choose a fresh attempt path")
    identity = native_preflight(config, preflight)
    if native_identity(config) != identity:
        raise EvalError("native reviewer changed after preflight")
    # Recheck after preflight and before spending. Native CLI remains the only
    # model executor; not the legacy Responses wrapper or an internal host API.
    current, frozen, checked_origin = frozen_source(source)
    if checked_origin != origin:
        raise EvalError("source review changed before dispatch")
    receipt = native_review(current["suite"], current["trial_id"], output, config, identity,
                            publish=False, frozen_packet=frozen, attempt_source=origin)
    source_valid = True
    try:
        if frozen_source(source)[2] != origin:
            raise EvalError("source review changed during dispatch")
    except (OSError, ValueError, EvalError):
        source_valid = False
    result = {**plan, "status": receipt["status"] if source_valid else "incomplete", "models_called": True,
              "source_evidence_unchanged": source_valid,
              "verdict": receipt.get("verdict", {}).get("verdict") if source_valid else None,
              "gap": receipt.get("gap") if source_valid else "source_evidence_drifted"}
    write_json(output / "attempt-result.json", result)
    return result
