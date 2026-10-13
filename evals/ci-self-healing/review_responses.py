"""Tool-free, stateless GLM 5.3 review transport using one selected Chrys profile.

This is a trusted HTTP client, not a coding Agent. It never executes model output
or loads Chrys agents, tools, memory or sessions. Only explicit input is sent.
"""

import argparse
import json
from pathlib import Path
import signal
import sys
import time
from urllib.parse import urlsplit

from common import EvalError, digest, json_bytes, read_regular
from review_limits import DEFAULT_PACKET_BYTES, check_packet, encode_envelope, encode_request, input_limits, read_envelope

PROFILE_NAME = "huoshan-glm5.3"
DEFAULT_OUTPUT_TOKENS = 16384
MAX_OUTPUT_TOKENS = 32768
DEFAULT_TIMEOUT = 600
MAX_TIMEOUT = 1800
SHUTDOWN_GRACE = 5
RESPONSE_BYTE_LIMIT = 2 * 1024 * 1024
PUBLIC_TEXT_LIMIT = 128 * 1024


def validate_budget(max_output_tokens, timeout):
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
        raise EvalError("review output budget must be between 1 and 32768 tokens")
    if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT:
        raise EvalError("review timeout must be between 1 and 1800 seconds")


def profile_settings(profile, max_output_tokens):
    validate_budget(max_output_tokens, DEFAULT_TIMEOUT)
    cap = profile.get("max_output_tokens", DEFAULT_OUTPUT_TOKENS)
    if type(cap) is not int or cap < 1:
        raise EvalError("incomplete review model profile")
    return {"name": PROFILE_NAME, "model_id": profile["model_id"], "base_url": profile["base_url"].rstrip("/"),
            "requested_max_output_tokens": max_output_tokens, "profile_max_output_tokens": cap,
            "max_output_tokens": min(cap, max_output_tokens), "bypass_proxy": profile.get("bypass_proxy") is True,
            "stream": profile.get("stream") is True}


def load_profile(path, max_output_tokens=DEFAULT_OUTPUT_TOKENS):
    # Lazy dependencies keep the evaluator's offline tests dependency-free.
    import yaml
    from chrys.foundation.util.env_templates import resolve_env_templates
    profile = yaml.safe_load(read_regular(path, 65536))
    if not isinstance(profile, dict) or (profile.get("name"), profile.get("provider"), profile.get("api_style")) != (PROFILE_NAME, "openai", "responses"):
        raise EvalError("review requires the explicitly selected non-Flash huoshan-glm5.3 Responses profile")
    url = urlsplit(profile.get("base_url", ""))
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise EvalError("review requires an HTTPS base URL without userinfo, query or fragment")
    model = profile.get("model_id")
    if not isinstance(model, str) or not model:
        raise EvalError("incomplete review model profile")
    selected = profile_settings(profile, max_output_tokens)
    fingerprint = digest(json_bytes(selected))
    selected["profile_sha256"] = fingerprint
    selected["api_key"] = resolve_env_templates(profile.get("api_key", ""), location="GLM review authentication")
    if not selected["api_key"]:
        raise EvalError("review credential is missing")
    return selected


def model_receipt(profile):
    return {"requested_model": "glm-5.3", "profile_name": PROFILE_NAME,
            "requested_model_id": profile["model_id"], "profile_sha256": profile["profile_sha256"],
            "max_output_tokens": profile["max_output_tokens"], "tools_enabled": False, "store": False,
            "requested_max_output_tokens": profile.get("requested_max_output_tokens", profile["max_output_tokens"]),
            "schema_enforcement": "local_required",
            "stream": profile.get("stream", False)}


def request_body(profile, payload):
    if not isinstance(payload, dict) or set(payload) != {"instructions", "schema", "packet"}:
        raise EvalError("invalid review request envelope")
    from review import output_contract
    instructions = payload["instructions"] + output_contract(payload["schema"])
    instructions += ("\n服务端格式参数不替代你的输出责任。直接给出一个完整 JSON 对象，不输出过程叙述；"
                     "explanation 和 summary 简明引用证据，不省略任何必需字段。"
                     "不得因格式要求改变评审结论，证据不足如实判 incomplete。")
    return {"model": profile["model_id"], "instructions": instructions,
            "input": [{"role": "user", "content": json.dumps(payload["packet"], ensure_ascii=False)}],
            "tools": [], "tool_choice": "none", "store": False, "stream": profile.get("stream", False), "truncation": "disabled",
            "max_output_tokens": profile["max_output_tokens"],
            "text": {"format": {"type": "json_schema", "name": "ci_review", "strict": True, "schema": payload["schema"]}}}


class Diagnostics:
    """Allowlisted metadata and bounded public text; never retain raw reasoning."""

    def __init__(self, timeout, progress=None):
        self.started = time.monotonic()
        self.secret = ""
        self.progress = progress
        self.data = {"stage": "setup", "timeout_seconds": timeout, "transport_bytes": 0,
                     "event_counts": {}, "first_event_seconds": {}, "schema_enforcement": "local_required"}

    def elapsed(self):
        return round(time.monotonic() - self.started, 3)

    def stage(self, value):
        self.data["stage"] = value
        if self.progress:
            self.progress({"review_progress": value, "elapsed_seconds": self.elapsed()})

    def observe(self, event):
        kind = event.get("type")
        allowed = {"response.created", "response.in_progress", "response.output_item.added",
                   "response.output_item.done", "response.content_part.added", "response.content_part.done",
                   "response.reasoning_text.delta", "response.reasoning_text.done",
                   "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
                   "response.output_text.delta", "response.output_text.done", "response.completed",
                   "response.incomplete", "response.failed", "error"}
        label = kind if isinstance(kind, str) and kind in allowed else "other"
        counts = self.data["event_counts"]
        counts[label] = counts.get(label, 0) + 1
        self.data["first_event_seconds"].setdefault(label, self.elapsed())
        if kind in {"response.created", "response.completed", "response.incomplete", "response.failed", "error"}:
            if kind != "response.created":
                self.record_response(event.get("response"), kind)
            if self.progress:
                self.progress({"review_progress": kind, "elapsed_seconds": self.elapsed()})

    def record_response(self, response, event=None):
        if not isinstance(response, dict):
            return
        details = response.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        status = response.get("status")
        usage = response.get("usage")
        safe_usage = {}
        if isinstance(usage, dict):
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                if type(usage.get(key)) is int and usage[key] >= 0:
                    safe_usage[key] = usage[key]
            nested = usage.get("output_tokens_details")
            if isinstance(nested, dict) and type(nested.get("reasoning_tokens")) is int and nested["reasoning_tokens"] >= 0:
                safe_usage["reasoning_tokens"] = nested["reasoning_tokens"]
        output = response.get("output")
        texts, types = [], []
        for item in output if isinstance(output, list) else []:
            if not isinstance(item, dict):
                types.append("invalid")
                continue
            kind = item.get("type")
            types.append(kind if kind in ("reasoning", "message") else "unsupported")
            if kind == "message" and isinstance(item.get("content"), list):
                texts.extend(c["text"] for c in item["content"] if isinstance(c, dict)
                             and c.get("type") == "output_text" and isinstance(c.get("text"), str))
        self.data["terminal"] = {"event": event, "status": status if status in ("completed", "incomplete", "failed") else "unknown",
                                 "reason": reason if reason in ("max_output_tokens", "content_filter") else None,
                                 "usage": safe_usage, "output_item_types": types,
                                 "public_text_blocks": len(texts)}
        # Diagnostic truncation never changes the original response used for validation.
        raw = json.dumps(redact_record(texts, self.secret), ensure_ascii=False).encode()
        self.data["public_output"] = raw[:PUBLIC_TEXT_LIMIT].decode("utf-8", errors="ignore")
        self.data["public_output_bytes"] = len(raw)
        self.data["public_output_truncated"] = len(raw) > PUBLIC_TEXT_LIMIT

    def snapshot(self):
        return {**self.data, "duration_seconds": self.elapsed()}


class ReviewTimeout(Exception):
    pass


class ReviewFailure(EvalError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def stream_response(chunks, diagnostics=None):
    """Bound SSE frames; accept only a completed response, never partial text."""
    buffer, lines, total = b"", [], 0
    for chunk in chunks:
        total += len(chunk)
        if diagnostics:
            diagnostics.data["transport_bytes"] = total
        if total > 8 * RESPONSE_BYTE_LIMIT:
            raise EvalError("review stream exceeds transport budget")
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if line.startswith(b"data:"):
                lines.append(line[5:].lstrip())
            elif not line and lines:
                frame, lines = b"\n".join(lines), []
                if frame == b"[DONE]":
                    raise EvalError("stream ended without response.completed")
                event = json.loads(frame)
                if not isinstance(event, dict):
                    raise ReviewFailure("protocol_error", "invalid review SSE event")
                if diagnostics:
                    diagnostics.observe(event)
                kind = event.get("type", "")
                if "_call." in kind or ".function_call_arguments." in kind:
                    raise EvalError("reviewer emitted a tool event")
                if kind in {"response.output_item.added", "response.output_item.done"} and event.get("item", {}).get("type") not in {"message", "reasoning"}:
                    raise EvalError("reviewer emitted an unsupported output item")
                if event.get("type") == "response.completed":
                    return event["response"]
                if event.get("type") in {"error", "response.failed", "response.incomplete"}:
                    state = event.get("response", {})
                    detail = (state.get("incomplete_details") or {}).get("reason")
                    reason = detail if detail in {"max_output_tokens", "content_filter"} else "unspecified"
                    code = "output_token_limit" if reason == "max_output_tokens" else "response_incomplete" if event["type"] == "response.incomplete" else "response_failed"
                    raise ReviewFailure(code, "review stream " + event["type"] + "; reason=" + reason)
                # Do not log deltas, reasoning, or encrypted reasoning payloads.
        if len(buffer) + sum(map(len, lines)) > RESPONSE_BYTE_LIMIT:
            raise EvalError("review SSE frame exceeds transport budget")
    raise EvalError("review stream has no completed response")


def extract_verdict(response, expected_model):
    if not isinstance(response, dict) or response.get("status") != "completed" or response.get("model") != expected_model:
        raise EvalError("review incomplete or response model does not match the selected profile")
    output = response.get("output")
    if not isinstance(output, list):
        raise EvalError("review output is missing")
    texts = []
    for item in output:
        if not isinstance(item, dict):
            raise EvalError("invalid review output item")
        if item.get("type") == "reasoning":
            continue  # Never retain hidden reasoning or encrypted reasoning content.
        if item.get("type") != "message" or item.get("role") != "assistant" or item.get("status") != "completed":
            raise EvalError("reviewer produced a tool call or an unsupported output item")
        if not isinstance(item.get("content"), list):
            raise EvalError("review message content is missing")
        for content in item["content"]:
            if not isinstance(content, dict) or content.get("type") != "output_text" or not isinstance(content.get("text"), str):
                raise EvalError("reviewer refused or produced non-text output")
            texts.append(content["text"])
    if len(texts) != 1:
        raise EvalError("review requires one complete JSON verdict")
    def unique_keys(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ReviewFailure("json_invalid", "review JSON has duplicate object keys")
            obj[key] = value
        return obj

    def reject_constant(value):
        raise ReviewFailure("json_invalid", "review JSON contains a non-JSON numeric constant")

    return json.loads(texts[0], object_pairs_hook=unique_keys, parse_constant=reject_constant)


def validate_response(response, profile, payload, diagnostics):
    import jsonschema
    from review import validate_verdict
    diagnostics.data["model_matches"] = isinstance(response, dict) and response.get("model") == profile["model_id"]
    diagnostics.stage("response_contract")
    if isinstance(response, dict) and response.get("status") == "incomplete":
        reason = (response.get("incomplete_details") or {}).get("reason")
        raise ReviewFailure("output_token_limit" if reason == "max_output_tokens" else "response_incomplete",
                            "review response is incomplete")
    verdict = extract_verdict(response, profile["model_id"])
    diagnostics.stage("json_schema")
    try:
        jsonschema.validate(verdict, payload["schema"])
    except jsonschema.ValidationError as exc:
        diagnostics.data["schema_error"] = {"path": list(exc.absolute_path), "rule": exc.validator}
        raise ReviewFailure("schema_invalid", "public review output violates JSON Schema") from None
    diagnostics.stage("semantic_validation")
    try:
        validate_verdict(verdict, payload["packet"])
    except EvalError as exc:
        raise ReviewFailure("semantic_invalid", str(exc)) from None
    return verdict


def redact_record(value, secret):
    if isinstance(value, str):
        # Also protect JSON-encoded strings retained as public-output diagnostics.
        return value.replace(secret, "[REDACTED]").replace(json.dumps(secret)[1:-1], "[REDACTED]") if secret else value
    if isinstance(value, list):
        return [redact_record(item, secret) for item in value]
    if isinstance(value, dict):
        return {redact_record(key, secret): redact_record(item, secret) for key, item in value.items()}
    return value


def emit_progress(value):
    print(json.dumps(value), file=sys.stderr, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--profile-sha256", required=True)
    parser.add_argument("--timeout", required=True, type=int)
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_OUTPUT_TOKENS)
    parser.add_argument("--max-packet-bytes", type=int, default=DEFAULT_PACKET_BYTES)
    args = parser.parse_args(argv)
    diagnostics = Diagnostics(args.timeout, emit_progress)
    receipt = {"requested_model": "glm-5.3", "status": "incomplete"}
    profile, previous_handler = None, None
    returncode = 2
    try:
        validate_budget(args.max_output_tokens, args.timeout)
        receipt["input_limits"] = input_limits(args.max_packet_bytes)
        def deadline(signum, frame):
            raise ReviewTimeout()
        previous_handler = signal.signal(signal.SIGALRM, deadline)
        signal.setitimer(signal.ITIMER_REAL, args.timeout)
        import httpx
        profile = load_profile(args.profile, args.max_output_tokens)
        diagnostics.secret = profile["api_key"]
        receipt.update(model_receipt(profile))
        if profile["profile_sha256"] != args.profile_sha256:
            raise EvalError("review profile changed before request")
        diagnostics.stage("input_validation")
        raw = read_envelope(sys.stdin.buffer, args.max_packet_bytes)
        payload = json.loads(raw)
        body = request_body(profile, payload)
        packet = check_packet(payload["packet"], args.max_packet_bytes)
        encode_envelope(payload, args.max_packet_bytes)
        request = encode_request(body, args.max_packet_bytes)
        diagnostics.data["input_sizes"] = {"packet_bytes": len(packet), "envelope_bytes": len(raw), "request_bytes": len(request)}
        diagnostics.data["packet_sha256"] = digest(packet)
        diagnostics.data["request_sha256"] = digest(request)
        diagnostics.stage("request_start")
        # No retries, redirects, continuation IDs, tool dispatcher or session state.
        with httpx.Client(timeout=httpx.Timeout(args.timeout, connect=min(15, args.timeout)),
                          follow_redirects=False, trust_env=not profile["bypass_proxy"]) as client:
            with client.stream("POST", profile["base_url"] + "/responses", content=request,
                               headers={"Authorization": "Bearer " + profile["api_key"], "Content-Type": "application/json"}) as response:
                diagnostics.data["http_status"] = response.status_code
                if response.status_code != 200:
                    raise ReviewFailure("http_error", "review endpoint returned non-200 HTTP status")
                diagnostics.stage("response_stream")
                if "text/event-stream" in response.headers.get("content-type", ""):
                    response = stream_response(response.iter_bytes(), diagnostics)
                else:
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        diagnostics.data["transport_bytes"] = len(data)
                        if len(data) > RESPONSE_BYTE_LIMIT:
                            raise ReviewFailure("response_byte_limit", "review response exceeds transport budget")
                    response = json.loads(data)
                    diagnostics.record_response(response)
        verdict = validate_response(response, profile, payload, diagnostics)
        receipt.update(status="validated", reported_model_id=profile["model_id"],
                       usage=diagnostics.data["terminal"]["usage"], verdict=verdict)
        diagnostics.stage("complete")
        returncode = 0
    except Exception as exc:
        # API exceptions may contain headers, private URLs or bodies. Never echo them.
        code = "client_error"
        if isinstance(exc, ReviewFailure):
            code = exc.code
        elif isinstance(exc, ReviewTimeout):
            code = "wall_timeout"
        elif isinstance(exc, json.JSONDecodeError):
            code = "json_invalid"
        elif isinstance(exc, EvalError):
            code = "contract_invalid"
        elif type(exc).__name__ in {"ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout"}:
            code = "transport_timeout"
        receipt["error"] = {"code": code, "type": type(exc).__name__}
        if isinstance(exc, EvalError):
            receipt["error"]["reason"] = str(exc)
        emit_progress({"status": "incomplete", "error_code": code, "stage": diagnostics.data["stage"]})
    finally:
        if previous_handler is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
    receipt["diagnostics"] = diagnostics.snapshot()
    print(json.dumps(redact_record(receipt, profile["api_key"] if profile else ""), ensure_ascii=False), flush=True)
    return returncode


if __name__ == "__main__":
    sys.exit(main())
