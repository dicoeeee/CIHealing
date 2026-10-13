"""Host execution failures, separate from the Agent's semantic result."""

from gateway_evidence import cancellation_time, gateway_failure
from generation_budget import generation_failure
from resource_cleanup import cleanup_complete


def failure(code, scope, action):
    return {"code": code, "scope": scope, "batch_action": action}


def classify_execution(data, stage):
    if not isinstance(data, dict):
        return failure("invalid_execution_receipt", "unknown", "stop")
    if "status" in data and not isinstance(data["status"], str):
        return failure("invalid_execution_receipt", "unknown", "stop")
    if data.get("status") in {"fixture_not_ready", "preflight_failed"}:
        return failure(data["status"], "environment", "stop")
    process = data.get("process", {})
    if not isinstance(process, dict) or not isinstance(process.get("cleanup", {}), dict):
        return failure("invalid_process_receipt", "unknown", "stop")
    if "status" in process and not isinstance(process["status"], str):
        return failure("invalid_process_receipt", "unknown", "stop")
    cleanup = process.get("cleanup", {}).get("status")
    if cleanup == "unconfirmed" or data.get("gateway_cleanup_confirmed") is False:
        return failure("process_cleanup_unconfirmed", "safety", "stop")
    if not cleanup_complete(data):
        return failure("resource_cleanup_incomplete", "safety", "stop")
    diagnostics = data.get("runtime_diagnostics", {})
    statuses = diagnostics.get("gateway_statuses", []) if isinstance(diagnostics, dict) else None
    if not isinstance(statuses, list) or any(type(s) is not int or not 100 <= s <= 599 for s in statuses):
        return failure("invalid_transport_receipt", "unknown", "stop")
    if stage == "run" and data.get("backend") == "native" and "gateway_evidence" not in diagnostics:
        return failure("gateway_evidence_unconfirmed", "unknown", "stop")
    if "gateway_evidence" in diagnostics:
        code = gateway_failure(diagnostics["gateway_evidence"], process)
        if code:
            scope = "shared_service" if code in {"provider_auth_rejected", "provider_rate_or_quota_limit"} else "unknown"
            return failure(code, scope, "stop")
    if any(s in {401, 403} for s in statuses):
        return failure("provider_auth_rejected", "shared_service", "stop")
    if 429 in statuses:
        return failure("provider_rate_or_quota_limit", "shared_service", "stop")
    if any(s >= 400 for s in statuses):
        return failure("provider_transport_error", "unknown", "stop")
    status = process.get("status")
    if status == "interrupted":
        return failure("operator_interrupted", "operator", "stop")
    if status in {"timeout", "output_limit"}:
        # A per-trial budget can be isolated only with affirmative cleanup and
        # snapshot evidence. An old/missing receipt is not proof of safety.
        ready = (cleanup == "confirmed" and data.get("isolation_confirmed") is True
                 and (stage != "run" or data.get("filesystem_status") == "captured"))
        if "gateway_evidence" in diagnostics:
            ready = (ready and data.get("gateway_cleanup_confirmed") is True
                     and cancellation_time(process, diagnostics["gateway_evidence"]["binding"]) is not None)
        return failure("trial_" + status, "case" if ready else "unknown", "continue" if ready else "stop")
    if stage == "run":
        generation_gap = generation_failure(data)
        if generation_gap:
            # Never convert a truncated response into success or implicit retries.
            return failure(generation_gap, "unknown", "stop")
    if process.get("returncode") != 0:
        return failure("execution_error_unclassified", "unknown", "stop")
    if stage == "review" and data.get("status") != "validated":
        # Invalid output, wrong model, and missing lifecycle evidence must not
        # be silently classified as a harmless semantic review disagreement.
        return failure("review_evidence_unconfirmed", "unknown", "stop")
    return None


def classify_grade(data):
    """Semantic failures can continue; identity/integrity gaps cannot."""
    gaps = data.get("gaps", [])
    if not isinstance(gaps, list) or any(not isinstance(g, str) for g in gaps) or not data.get("bindings"):
        return failure("grade_evidence_unconfirmed", "unknown", "stop")
    identities = {"model_or_session_identity_unconfirmed", "persisted_session_identity_unconfirmed", "native_skill_registration_unconfirmed"}
    if identities.intersection(gaps):
        return failure("runtime_identity_unconfirmed", "environment", "stop")
    # Missing an Agent deliverable is a case result, not proof of a shared fault.
    if set(gaps) - {"report_file_missing_fallback_requires_review"}:
        return failure("grade_evidence_unconfirmed", "unknown", "stop")
    return None
