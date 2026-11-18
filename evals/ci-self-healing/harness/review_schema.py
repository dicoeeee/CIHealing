"""One controlled review contract for local validation and provider requests."""
import json
import math
from pathlib import Path

from .core import EvaluationError, read_json, sha

SCHEMA_PATH = Path(__file__).with_name("review.schema.json")
DIAGNOSTIC_KEYS = {"checks", "criterion_id", "applicability", "status", "evidence_ids", "reason", "title"}


def evidence_for_judge(packet):
    """Only the task input, observed public actions and delivery enter native semantic review."""
    if not packet.get("facts", {}).get("trial_root"):
        return packet["evidence"]  # separately authored calibration materials
    evidence = {}
    for ref, item in packet["evidence"].items():
        kind = item["kind"]
        if ref == "task" or kind in ("final_answer", "final_output", "agent_build_receipt"):
            evidence[ref] = item
        elif kind == "public_event":
            event = item["content"]
            if event.get("kind", "").startswith("observer."):
                continue
            body = {**event, "payload": {k: v for k, v in event.get("payload", {}).items()
                    if k not in {"path_changes", "changed_paths_since_previous_event", "visible_snapshot",
                                 "command_receipt", "file_read_receipt"}}}
            evidence[ref] = {**item, "content": body, "sha256": sha(body)}
    return evidence


def contract_for(criteria, packet, *, original=False):
    """Public identifier allowlists only; no expected verdicts or label provenance."""
    contract = {"criterion_ids": sorted(c["id"] for c in criteria if c["method"] == "semantic"),
                "evidence_ids": sorted(packet["evidence"] if original else evidence_for_judge(packet))}
    _check_contract(contract)
    return contract


def _check_contract(contract):
    if not isinstance(contract, dict) or set(contract) != {"criterion_ids", "evidence_ids"}:
        raise EvaluationError("invalid review identifier contract")
    for ids in contract.values():
        if (not isinstance(ids, list) or not ids
                or any(not isinstance(item, str) or not item.strip() for item in ids)
                or len(ids) != len(set(ids))):
            raise EvaluationError("review identifier contract requires nonempty unique string IDs")


def response_schema(contract=None):
    # Draft selection is local metadata, not a provider-specific format option.
    schema = read_json(SCHEMA_PATH)
    schema.pop("$schema", None)
    if contract is not None:
        _check_contract(contract)
        properties = schema["properties"]["checks"]["items"]["properties"]
        properties["criterion_id"]["enum"] = list(contract["criterion_ids"])
        properties["evidence_ids"]["items"]["enum"] = list(contract["evidence_ids"])
    return schema


def require_validator(contract=None):
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise EvaluationError("JSON Schema validation requires jsonschema==4.26.0; use the configured iCode Python or install evals/ci-self-healing/requirements.txt in the selected environment") from exc
    schema = response_schema(contract)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise EvaluationError("invalid controlled review JSON Schema") from exc
    return Draft202012Validator(schema)


def response_format(contract=None):
    validator = require_validator(contract)
    return {"type": "json_schema", "json_schema": {
        "name": "ci_review_v2" if contract is not None else "ci_review_v1",
        "strict": True, "schema": validator.schema}}


def wire_format(contract=None):
    """Responses API representation of the same controlled format."""
    return {"type": "json_schema", **response_format(contract)["json_schema"]}


def strict_loads(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate object key")
            result[key] = value
        return result

    def nonfinite(_):
        raise ValueError("non-finite JSON number")

    def number(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        return value

    return json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite, parse_float=number)


def _safe_key(key):
    return key if isinstance(key, str) and key in DIAGNOSTIC_KEYS else "<unrecognized-field>"


def validate_structure(value, contract=None):
    """Return bounded keyword/path diagnostics, never the response body or values."""
    validator = require_validator(contract)
    errors = []
    try:
        for error in validator.iter_errors(value):
            pointer = "/" + "/".join(str(p) if isinstance(p, int) else _safe_key(p)
                                      for p in error.absolute_path)
            detail = {"path": pointer, "keyword": error.validator}
            if error.validator == "additionalProperties" and isinstance(error.instance, dict):
                names, count = [], 0
                for key in error.instance:
                    if key not in error.schema.get("properties", {}):
                        count += 1
                        if len(names) < 8:
                            names.append(_safe_key(key))
                detail["unexpected_fields"] = sorted(names)
                if count > len(names):
                    detail.update(unexpected_field_count=count, fields_truncated=True)
            elif error.validator == "required" and isinstance(error.instance, dict):
                detail["missing_fields"] = [k for k in error.validator_value if k not in error.instance]
            errors.append(detail)
            if len(errors) == 8:
                break
    except (RecursionError, TypeError, ValueError):
        return [{"path": "/", "keyword": "invalid_json_instance"}]
    return errors
