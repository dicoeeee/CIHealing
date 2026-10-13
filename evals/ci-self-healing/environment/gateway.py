"""Evaluation-only Responses gateway; upstream secret arrives on stdin, never argv.

Attach to the isolated model network and a separate egress network. No published
host port, arbitrary URL forwarding, hosted tools, remote media or redirects.
This constrains transport, not malicious content inside a model prompt.
"""

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit

import httpx


class ResponseBudgetAudit:
    """Bounded terminal-metadata observation; never log text or rewrite bytes."""
    LIMIT = 1024 * 1024

    def __init__(self, streaming):
        self.streaming, self.buffer, self.rows, self.invalid = streaming, b"", [], False

    def feed(self, chunk):
        if self.invalid:
            return
        self.buffer += chunk
        if self.streaming:
            while True:
                # SSE supports LF and CRLF, including delimiters split across chunks.
                found = [(self.buffer.find(d), d) for d in (b"\n\n", b"\r\n\r\n")]
                found = [(i, d) for i, d in found if i >= 0]
                if not found:
                    break
                i, delimiter = min(found, key=lambda pair: pair[0])
                record, self.buffer = self.buffer[:i], self.buffer[i + len(delimiter):]
                if len(record) > self.LIMIT:
                    self.invalid = True
                    break
                data = b"\n".join(line[5:].lstrip(b" ") for line in record.splitlines() if line.startswith(b"data:"))
                if data and data != b"[DONE]":
                    self.parse(data, event=True)
        if len(self.buffer) > self.LIMIT or self.invalid:
            self.invalid, self.buffer = True, b""

    def parse(self, raw, *, event):
        def unique(pairs):
            result = {}
            for k, v in pairs:
                if k in result:
                    raise ValueError()
                result[k] = v
            return result
        try:
            obj = json.loads(raw, object_pairs_hook=unique)
            if not isinstance(obj, dict):
                raise ValueError()
            kind = obj.get("type") if event else None
            if event and kind not in {"response.completed", "response.incomplete", "response.failed", "error"}:
                return
            response = obj.get("response") if event else obj
            if not isinstance(response, dict):
                raise ValueError()
            status = response.get("status")
            if status not in {"completed", "incomplete", "failed"} or (event and kind != "response." + status):
                raise ValueError()
            details = response.get("incomplete_details")
            if details is not None and not isinstance(details, dict):
                raise ValueError()
            reason = (details or {}).get("reason")
            reason = reason if reason in {"max_output_tokens", "content_filter"} else "none" if reason is None else "other"
            usage = response.get("usage")
            if usage is not None and not isinstance(usage, dict):
                raise ValueError()
            tokens = (usage or {}).get("output_tokens")
            if tokens is not None and (type(tokens) is not int or tokens < 0):
                raise ValueError()
            self.rows.append({"status": status, "reason": reason, "output_tokens": tokens})
            if len(self.rows) > 1:
                self.invalid = True
        except (ValueError, TypeError, RecursionError):
            self.invalid = True

    def finish(self):
        if not self.streaming and not self.invalid:
            self.parse(self.buffer, event=False)
        if self.invalid or (self.streaming and self.buffer.strip()) or len(self.rows) != 1:
            return {"status": "unconfirmed", "reason": "unconfirmed", "output_tokens": None}
        return self.rows[0]


def allowed_request(body, model):
    if not isinstance(body, dict) or body.get("model") != model:
        return False
    allowed = {"model", "input", "instructions", "tools", "tool_choice", "stream", "store",
               "max_output_tokens", "temperature", "top_p", "reasoning", "text",
               "parallel_tool_calls", "truncation", "include", "metadata", "user",
               "prompt_cache_key", "stream_options"}
    if body.keys() - allowed or body.get("store") not in (None, False):
        return False
    requested_tools = body.get("tools")
    if requested_tools is None:
        requested_tools = []
    if not isinstance(requested_tools, list) or any(not isinstance(t, dict) or t.get("type") != "function" for t in requested_tools):
        return False
    def local_content(value):
        if isinstance(value, dict):
            kind = value.get("type")
            if isinstance(kind, str) and kind in {"input_image", "input_file", "input_audio"}:
                return False
            return all(local_content(v) for v in value.values())
        if isinstance(value, list):
            return all(local_content(v) for v in value)
        return True
    return local_content(body)


def make_server(config, *, host="0.0.0.0", port=8080, event_sink=None):
    parsed = urlsplit(config["base_url"])
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("gateway requires an HTTPS upstream base URL")
    endpoint = config["base_url"].rstrip("/") + "/responses"
    if not config["api_key"] or not config["local_token"]:
        raise ValueError("empty credential")
    lock = threading.Lock()
    event_lock = threading.Lock()
    binding = dict(config.get("event_binding") or {
        "trial_id": "unbound", "gateway_id": uuid.uuid4().hex, "clock_id": uuid.uuid4().hex})
    if (set(binding) != {"trial_id", "gateway_id", "clock_id"}
            or not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v) for v in binding.values())):
        raise ValueError("invalid gateway event binding")
    sequence = 0
    request_sequence = 0

    def emit(kind, request_id=0, **fields):
        nonlocal sequence
        with event_lock:
            sequence += 1
            event = {"schema": "ci-gateway-event-v1", **binding, "seq": sequence,
                     "monotonic_ns": time.monotonic_ns(), "request_id": request_id,
                     "kind": kind, **fields}
            if event_sink is None:
                print(json.dumps(event), flush=True)
            else:
                event_sink(event)

    def error_type(exc):
        # Exception messages and arbitrary class names may contain private data.
        name = type(exc).__name__
        return name if name in {"BrokenPipeError", "ConnectionResetError", "ConnectionAbortedError",
                               "ReadTimeout", "ConnectTimeout", "ReadError", "WriteError", "ConnectError",
                               "RemoteProtocolError", "LocalProtocolError", "PoolTimeout", "WriteTimeout"} else "Other"
    remaining = config.get("max_requests", 256)
    cap = config.get("max_output_tokens")
    if cap is not None and (type(cap) is not int or cap <= 0):
        raise ValueError("invalid selected profile output limit")
    generation_halted = False
    semaphore = threading.BoundedSemaphore(2)
    client = httpx.Client(timeout=httpx.Timeout(180, connect=15), follow_redirects=False, trust_env=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No URLs, prompts, credentials or upstream error bodies in logs.

        def event(self, kind, **fields):
            emit(kind, self.request_id, **fields)

        def downstream(self, phase, operation):
            try:
                operation()
                return True
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
                self.outcome = "downstream_disconnect"
                self.event("downstream_disconnect", phase=phase, error_type=error_type(exc))
            except Exception as exc:
                self.outcome = "internal_error"
                self.event("gateway_internal_error", phase=phase, error_type=error_type(exc))
            self.close_connection = True
            return False

        def reply(self, code, policy=True):
            if policy:
                self.outcome = "rejected"
                self.event("gateway_policy_rejected", phase="request", http_status=code)
            def headers():
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()
            self.downstream("downstream_headers", headers)

        def do_GET(self):
            self.handle_request(False)

        def do_POST(self):
            self.handle_request(True)

        def handle_request(self, post):
            nonlocal request_sequence
            with lock:
                request_sequence += 1
                self.request_id = request_sequence
            self.outcome = "completed"
            self.event("request_started")
            try:
                if not post:
                    return self.reply(200, policy=False) if self.path == "/health" else self.reply(404)
                self.proxy_request()
            except Exception as exc:
                self.outcome = "internal_error"
                self.close_connection = True
                self.event("gateway_internal_error", phase="request", error_type=error_type(exc))
            finally:
                self.event("request_finished", outcome=self.outcome)

        def proxy_request(self):
            nonlocal remaining, generation_halted
            if self.path != "/v1/responses":
                return self.reply(404)
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + config["local_token"]):
                return self.reply(401)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8 * 1024 * 1024 or self.headers.get("Transfer-Encoding"):
                    return self.reply(413)
                body = json.loads(self.rfile.read(size))
                if not allowed_request(body, config["model_id"]):
                    return self.reply(403)
                if cap is not None:
                    # Reject, never silently repair a missing or changed wire cap.
                    if type(body.get("max_output_tokens")) is not int or body["max_output_tokens"] != cap:
                        return self.reply(403)
                    if generation_halted:
                        self.event("generation_continuation_rejected")
                        return self.reply(403)
                    self.event("generation_request", max_output_tokens=cap)
            except (ValueError, TypeError, AttributeError):
                return self.reply(400)
            if not semaphore.acquire(blocking=False):
                return self.reply(429)
            try:
                with lock:
                    if remaining <= 0:
                        return self.reply(429)
                    remaining -= 1
                body["store"] = False
                phase = "upstream_connect"
                try:
                    with client.stream("POST", endpoint, json=body, headers={"Authorization": "Bearer " + config["api_key"]}) as response:
                        self.event("upstream_headers", http_status=response.status_code)
                        if response.status_code != 200:
                            self.outcome = "upstream_error"
                            self.event("upstream_http_error", phase=phase, http_status=response.status_code)
                            phase = "upstream_close"
                            return self.reply(502, policy=False)
                        def headers():
                            self.send_response(200)
                            self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                            self.send_header("Connection", "close")
                            self.end_headers()
                        self.close_connection = True
                        if not self.downstream("downstream_headers", headers):
                            phase = "upstream_close"
                            return
                        phase = "upstream_read"
                        audit = ResponseBudgetAudit("text/event-stream" in response.headers.get("Content-Type", "")) if cap is not None else None
                        try:
                            for chunk in response.iter_bytes():
                                if audit:
                                    audit.feed(chunk)
                                    if audit.invalid or (audit.rows and (
                                            audit.rows[-1]["status"] != "completed" or audit.rows[-1]["reason"] != "none"
                                            or (audit.rows[-1]["output_tokens"] is not None and audit.rows[-1]["output_tokens"] > cap))):
                                        generation_halted = True
                                def write():
                                    self.wfile.write(chunk)
                                    self.wfile.flush()
                                if not self.downstream("downstream_write", write):
                                    phase = "upstream_close"
                                    return
                        finally:
                            if audit:
                                observed = audit.finish()
                                self.event("generation_response", **observed)
                                if (observed["status"] != "completed" or observed["reason"] != "none"
                                        or (observed["output_tokens"] is not None and observed["output_tokens"] > cap)):
                                    generation_halted = True
                        phase = "upstream_close"
                except (httpx.HTTPError, OSError) as exc:
                    self.outcome = "upstream_error"
                    self.close_connection = True
                    self.event("upstream_transport_error", phase=phase, error_type=error_type(exc))
            finally:
                semaphore.release()

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def server_close(self):
            super().server_close()
            client.close()

    server = Server((host, port), Handler)
    server.daemon_threads = True
    emit("gateway_ready")
    return server


def serve(config, *, host="0.0.0.0", port=8080, ready=None):
    server = make_server(config, host=host, port=port)
    if ready is not None:
        ready(server.server_address[1])
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    if sys.argv[1:] == ["--bootstrap-fifo"]:
        fifo = "/run/gateway/bootstrap"
        os.mkfifo(fifo, 0o600)
        with open(fifo) as stream:
            config = json.load(stream)
        os.unlink(fifo)
    else:
        config = json.loads(sys.stdin.buffer.readline(65537))
    serve(config)
