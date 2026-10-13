"""Fail-closed parsing and cancellation correlation for host-owned gateway facts."""

import json
import re

from common import EvalError, digest, read_regular

SCHEMA = "ci-gateway-event-v1"
FIELDS = {"schema", "trial_id", "gateway_id", "clock_id", "seq", "monotonic_ns", "request_id", "kind"}
ERROR_TYPES = {"BrokenPipeError", "ConnectionResetError", "ConnectionAbortedError", "ReadTimeout",
               "ConnectTimeout", "ReadError", "WriteError", "ConnectError", "RemoteProtocolError",
               "LocalProtocolError", "PoolTimeout", "WriteTimeout", "Other"}
EXTRA = {"gateway_ready": set(), "request_started": set(), "request_finished": {"outcome"},
         "upstream_headers": {"http_status"}, "upstream_http_error": {"phase", "http_status"},
         "upstream_transport_error": {"phase", "error_type"},
         "downstream_disconnect": {"phase", "error_type"},
         "gateway_policy_rejected": {"phase", "http_status"},
         "gateway_internal_error": {"phase", "error_type"},
         "generation_request": {"max_output_tokens"},
         "generation_response": {"status", "reason", "output_tokens"},
         "generation_continuation_rejected": set()}
PHASES = {"upstream_http_error": {"upstream_connect"},
          "upstream_transport_error": {"upstream_connect", "upstream_read", "upstream_close"},
          "downstream_disconnect": {"downstream_headers", "downstream_write"},
          "gateway_policy_rejected": {"request"},
          "gateway_internal_error": {"request", "downstream_headers", "downstream_write"}}


def positive(value):
    return type(value) is int and value > 0


def valid_binding(binding):
    return (isinstance(binding, dict) and set(binding) == {"trial_id", "gateway_id", "clock_id"}
            and all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v) for v in binding.values()))


def valid_event(row, binding):
    if not isinstance(row, dict) or not isinstance(row.get("kind"), str) or row["kind"] not in EXTRA or row.get("schema") != SCHEMA:
        return False
    kind = row["kind"]
    if set(row) != FIELDS | EXTRA[kind] or any(row[k] != v for k, v in binding.items()):
        return False
    if not positive(row["seq"]) or not positive(row["monotonic_ns"]) or type(row["request_id"]) is not int:
        return False
    if (kind == "gateway_ready" and row["request_id"] != 0) or (kind != "gateway_ready" and row["request_id"] <= 0):
        return False
    if kind in PHASES and (not isinstance(row["phase"], str) or row["phase"] not in PHASES[kind]):
        return False
    if "http_status" in row and (type(row["http_status"]) is not int or not 100 <= row["http_status"] <= 599):
        return False
    if (kind == "upstream_http_error" and row["http_status"] == 200) or (kind == "gateway_policy_rejected" and row["http_status"] < 400):
        return False
    if "error_type" in row and (not isinstance(row["error_type"], str) or row["error_type"] not in ERROR_TYPES):
        return False
    if kind == "downstream_disconnect" and row["error_type"] not in {"BrokenPipeError", "ConnectionResetError", "ConnectionAbortedError"}:
        return False
    if kind == "generation_request" and not positive(row["max_output_tokens"]):
        return False
    if kind == "generation_response":
        if (not isinstance(row["status"], str) or row["status"] not in {"completed", "incomplete", "failed", "unconfirmed"}
                or not isinstance(row["reason"], str) or row["reason"] not in {"none", "max_output_tokens", "content_filter", "other", "unconfirmed"}
                or (row["output_tokens"] is not None and (type(row["output_tokens"]) is not int or row["output_tokens"] < 0))):
            return False
    return kind != "request_finished" or (isinstance(row["outcome"], str) and row["outcome"] in {"completed", "rejected", "upstream_error", "downstream_disconnect", "internal_error"})


def analyze(events, binding):
    """Revalidate even persisted diagnostics; malformed facts never admit a run."""
    if not valid_binding(binding) or not isinstance(events, list) or not events:
        return ["gateway_evidence_missing"], {}
    requests, gaps, last_ns = {}, [], 0
    for seq, row in enumerate(events, 1):
        if not valid_event(row, binding):
            gaps.append("invalid_gateway_event")
            continue
        kind, rid, ns = row["kind"], row["request_id"], row["monotonic_ns"]
        if row["seq"] != seq or ns < last_ns or (seq == 1) != (kind == "gateway_ready"):
            gaps.append("gateway_order_invalid")
        last_ns = ns
        if kind == "gateway_ready":
            continue
        if kind == "request_started":
            if rid in requests:
                gaps.append("gateway_request_reused")
            requests[rid] = {"started_ns": ns, "finished": False, "errors": set()}
        elif rid not in requests or requests[rid]["finished"]:
            gaps.append("gateway_request_unbound")
        elif kind == "request_finished":
            expected = {"rejected": "gateway_policy_rejected", "upstream_error": "upstream",
                        "downstream_disconnect": "downstream_disconnect", "internal_error": "gateway_internal_error"}
            errors = requests[rid]["errors"]
            outcome = row["outcome"]
            if (outcome == "completed" and errors) or (outcome != "completed" and expected[outcome] not in errors):
                gaps.append("gateway_outcome_unsubstantiated")
            requests[rid]["finished"] = True
        elif kind not in {"upstream_headers", "generation_request", "generation_response", "generation_continuation_rejected"}:
            requests[rid]["errors"].add("upstream" if kind.startswith("upstream_") else kind)
    return sorted(set(gaps)), requests


def read_gateway_evidence(path, binding):
    result = {"schema": "ci-gateway-evidence-v1", "binding": binding if valid_binding(binding) else None,
              "events": [], "gaps": [], "http_statuses": []}
    try:
        raw = read_regular(path)
    except (EvalError, OSError):
        result["gaps"] = ["gateway_log_unavailable"]
        return result
    result["source_sha256"] = digest(raw)
    for line in raw.splitlines(keepends=True):
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            result["gaps"].append("invalid_gateway_log_line")
            continue
        if not line.endswith(b"\n"):
            result["gaps"].append("incomplete_gateway_log_line")
        if valid_binding(binding) and valid_event(row, binding):
            result["events"].append(row)
            if row["kind"] == "upstream_headers":
                result["http_statuses"].append(row["http_status"])
        else:
            # Legacy numeric statuses remain observations, never invented 502s.
            # Legacy/unknown records cannot justify cancellation continuation.
            result["gaps"].append("unattributed_gateway_record")
            if isinstance(row, dict):
                for key in ("upstream_status", "gateway_status"):
                    value = row.get(key)
                    if type(value) is int and 100 <= value <= 599:
                        result["http_statuses"].append(value)
    gaps, _ = analyze(result["events"], result["binding"])
    result["gaps"] = sorted(set(result["gaps"] + gaps))
    return result


def cancellation_time(process, binding):
    """Only successful host signals for this trial/gateway/clock can correlate."""
    termination = process.get("termination", {})
    identity = process.get("identity", {})
    if (not valid_binding(binding) or not isinstance(termination, dict) or not isinstance(identity, dict)
            or process.get("clock_id") != binding["clock_id"]
            or any(identity.get(k) != binding[k] for k in ("trial_id", "gateway_id"))
            or not isinstance(process.get("status"), str) or process["status"] not in {"timeout", "output_limit"}
            or termination.get("reason") != process["status"]
            or not positive(termination.get("pid")) or termination.get("pgid") != termination["pid"]):
        return None
    signals = termination.get("signals")
    if not isinstance(signals, list):
        return None
    times = []
    for row in signals:
        if (not isinstance(row, dict) or row.get("reason") != process["status"]
                or not isinstance(row.get("signal"), str) or row["signal"] not in {"SIGINT", "SIGKILL"}
                or not isinstance(row.get("delivery"), str) or row["delivery"] not in {"sent", "process_absent", "failed"}
                or not positive(row.get("requested_ns")) or not positive(row.get("completed_ns"))
                or row["completed_ns"] < row["requested_ns"]):
            return None
        if row["delivery"] == "sent":
            times.append(row["requested_ns"])
    return min(times) if times else None


def gateway_failure(evidence, process):
    """Return a failure code; None means no unhandled transport failure."""
    if (not isinstance(evidence, dict) or evidence.get("schema") != "ci-gateway-evidence-v1"
            or evidence.get("gaps") != []):
        return "gateway_evidence_unconfirmed"
    binding, events = evidence.get("binding"), evidence.get("events")
    gaps, requests = analyze(events, binding)
    if gaps:
        return "gateway_evidence_unconfirmed"
    identity = process.get("identity")
    if (not isinstance(identity, dict) or process.get("clock_id") != binding["clock_id"]
            or any(identity.get(k) != binding[k] for k in ("trial_id", "gateway_id"))):
        return "gateway_evidence_unconfirmed"
    statuses = [r["http_status"] for r in events if r["kind"] in {"upstream_headers", "upstream_http_error"}]
    if any(s in {401, 403} for s in statuses):
        return "provider_auth_rejected"
    if 429 in statuses:
        return "provider_rate_or_quota_limit"
    if any(s != 200 for s in statuses) or any(r["kind"] == "upstream_transport_error" for r in events):
        return "provider_transport_error"
    if any(r["kind"] == "gateway_policy_rejected" for r in events):
        return "gateway_policy_rejected"
    if any(r["kind"] == "gateway_internal_error" for r in events):
        return "gateway_internal_error"
    cancel_ns = cancellation_time(process, binding)
    for row in events:
        if row["kind"] == "downstream_disconnect":
            start_ns = requests[row["request_id"]]["started_ns"]
            if cancel_ns is None or not start_ns <= cancel_ns <= row["monotonic_ns"]:
                return "downstream_disconnect_unattributed"
    # Killing the owned gateway can end an in-flight request without a terminal
    # event. This is not a completed response; only a bound host cancellation
    # plus the caller's affirmative cleanup/snapshot checks can isolate it.
    if any(not r["finished"] and (cancel_ns is None or r["started_ns"] > cancel_ns) for r in requests.values()):
        return "gateway_request_unfinished"
    return None
