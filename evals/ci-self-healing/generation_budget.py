"""Audit inherited profile limits and completion; retain legacy receipt checks."""

from gateway_evidence import analyze, gateway_failure


def generation_failure(run, *, cancelled_tail_after_ns=None):
    if not isinstance(run, dict):
        return "generation_budget_unconfirmed"
    budget = run.get("generation_budget")
    if budget is None:
        return None  # Historical executions did not collect this contract.
    if (not isinstance(budget, dict)
            or any(type(budget.get(k)) is not int or budget[k] <= 0 for k in
                   ("profile_max_output_tokens", "effective_max_output_tokens"))):
        return "generation_budget_unconfirmed"
    cap = budget["effective_max_output_tokens"]
    if budget.get("contract") == "profile-generation-v1":
        if (set(budget) != {"contract", "profile_max_output_tokens", "effective_max_output_tokens"}
                or cap != budget["profile_max_output_tokens"]):
            return "generation_budget_unconfirmed"
    elif budget.get("contract") == "generation-budget-v1":
        # Historical receipts describe the old repair-specific limit; never
        # reinterpret them as inheriting a larger profile value.
        requested = budget.get("requested_max_output_tokens")
        if (type(requested) is not int or not 1 <= requested <= 131072
                or cap != min(requested, budget["profile_max_output_tokens"])):
            return "generation_budget_unconfirmed"
    else:
        return "generation_budget_unconfirmed"
    diagnostics = run.get("runtime_diagnostics")
    if not isinstance(diagnostics, dict):
        return "generation_budget_unconfirmed"
    evidence = diagnostics.get("gateway_evidence")
    process = run.get("process")
    if not isinstance(evidence, dict) or not isinstance(process, dict) or gateway_failure(evidence, process):
        return "generation_budget_unconfirmed"
    events = evidence.get("events")
    gaps, _ = analyze(events, evidence.get("binding"))
    if gaps:
        return "generation_budget_unconfirmed"
    requests = {}
    for event in events:
        rid, kind = event["request_id"], event["kind"]
        if kind == "gateway_ready":
            continue
        request = requests.setdefault(rid, {"sent": [], "reply": [], "headers": []})
        if kind == "generation_request":
            request["sent"].append(event)
        elif kind == "generation_response":
            request["reply"].append(event)
        elif kind == "upstream_headers":
            request["headers"].append(event)
    forwarded = [r for r in requests.values() if r["headers"] or r["sent"] or r["reply"]]
    if not forwarded:
        return "generation_budget_unconfirmed"
    for index, request in enumerate(forwarded):
        if any(len(request[k]) != 1 for k in ("sent", "reply", "headers")):
            return "generation_budget_unconfirmed"
        sent, reply, headers = request["sent"][0], request["reply"][0], request["headers"][0]
        if (sent["max_output_tokens"] != cap or headers["http_status"] != 200
                or not sent["seq"] < headers["seq"] < reply["seq"]):
            return "generation_budget_unconfirmed"
        if reply["output_tokens"] is not None and reply["output_tokens"] > cap:
            return "generation_cap_exceeded"
        if reply["status"] == "incomplete" and reply["reason"] == "max_output_tokens":
            return "generation_output_limit"
        if reply["status"] == "unconfirmed":
            # Only a host-cancelled final exchange after a verified deliverable
            # can be out of semantic-review scope. Keep its raw receipt intact.
            if cancelled_tail_after_ns is not None and index == len(forwarded) - 1:
                from gateway_evidence import cancellation_time
                cancelled = cancellation_time(process, evidence["binding"])
                rid = sent["request_id"]
                starts = [e for e in events if e["request_id"] == rid and e["kind"] == "request_started"]
                ends = [e for e in events if e["request_id"] == rid and e["kind"] == "request_finished"]
                if (process.get("status") == "timeout" and cancelled is not None and len(starts) == len(ends) == 1
                        and cancelled_tail_after_ns < starts[0]["monotonic_ns"] <= cancelled
                        and ends[0]["outcome"] == "downstream_disconnect" and ends[0]["monotonic_ns"] >= cancelled):
                    continue
            return "generation_budget_unconfirmed"
        if reply["status"] != "completed" or reply["reason"] != "none":
            return "generation_response_incomplete"
    # Wire values and observed terminal status, not a backend-compute guarantee.
    return None
