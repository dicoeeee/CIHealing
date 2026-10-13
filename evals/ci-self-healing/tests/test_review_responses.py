"""Offline transport contract checks; no model calls or third-party imports."""

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import EvalError
from review_responses import (DEFAULT_OUTPUT_TOKENS, Diagnostics, ReviewFailure, ReviewTimeout, extract_verdict,
                              main, model_receipt, profile_settings, redact_record, request_body, stream_response,
                              validate_budget)


class FakeSchemaError(Exception):
    absolute_path = ["temporal", "admission_seq"]
    validator = "type"


class ReviewResponsesTests(unittest.TestCase):
    def setUp(self):
        self.profile = {"model_id": "glm-fixture", "name": "huoshan-glm5.3", "profile_sha256": "fixture",
                        "api_key": "private-example", "base_url": "https://example.invalid/v1", "max_output_tokens": 8192, "bypass_proxy": True}
        self.response = {"status": "completed", "model": "glm-fixture", "output": [
            {"type": "reasoning", "encrypted_content": "never retained"},
            {"type": "message", "role": "assistant", "status": "completed", "content": [
                {"type": "output_text", "text": '{"verdict":"fail"}'}]}]}

    def test_request_has_no_tools_history_or_credentials(self):
        body = request_body(self.profile, {"instructions": "trusted rubric", "schema": {}, "packet": {"data": "untrusted"}})
        self.assertEqual(body["tools"], [])
        self.assertEqual(body["tool_choice"], "none")
        self.assertFalse(body["store"])
        self.assertFalse(body["stream"])
        self.assertEqual(body["truncation"], "disabled")
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertIn("JSON Schema", body["instructions"])
        self.assertNotIn("previous_response_id", body)
        self.assertNotIn("private-example", json.dumps(body))
        self.assertNotIn("private-example", json.dumps(model_receipt(self.profile)))
        with self.assertRaises(EvalError):
            request_body(self.profile, {"instructions": "rubric", "schema": {}, "packet": {}, "tools": ["shell"]})

    def test_only_complete_matching_model_json_is_accepted(self):
        self.assertEqual(extract_verdict(self.response, "glm-fixture"), {"verdict": "fail"})
        for patch in ({"status": "incomplete"}, {"model": "different"}, {"output": []}):
            with self.assertRaises(EvalError):
                extract_verdict({**self.response, **patch}, "glm-fixture")

    def test_stream_profile_preserves_no_tools_or_history(self):
        body = request_body({**self.profile, "stream": True}, {"instructions": "rubric", "schema": {}, "packet": {}})
        self.assertTrue(body["stream"])
        self.assertEqual(body["tools"], [])
        self.assertFalse(body["store"])

    def test_sse_requires_completed_event_not_text_delta(self):
        raw = ('event: response.completed\r\ndata: ' + json.dumps({"type": "response.completed", "response": self.response}) + '\r\n\r\n').encode()
        self.assertEqual(stream_response(raw[i:i+1] for i in range(len(raw))), self.response)
        for raw in (b'data: {"type":"response.output_text.delta","delta":"{}"}\n\n',
                    b'data: [DONE]\n\n', b'data: {"type":"response.incomplete"}\n\n',
                    b'data: {"type":"response.output_item.added","item":{"type":"function_call"}}\n\n',
                    b'data: {"type":"response.web_search_call.in_progress"}\n\n'):
            with self.assertRaises(EvalError):
                stream_response([raw])

    def test_tool_call_refusal_and_multiple_answers_fail_closed(self):
        for item in ({"type": "function_call", "name": "shell"}, {"type": "web_search_call"},
                     {"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "refusal"}]}):
            response = copy.deepcopy(self.response)
            response["output"].append(item)
            with self.assertRaises(EvalError):
                extract_verdict(response, "glm-fixture")
        response = copy.deepcopy(self.response)
        response["output"].append(response["output"][-1])
        with self.assertRaises(EvalError):
            extract_verdict(response, "glm-fixture")

    def test_budget_is_explicit_bounded_and_respects_profile_cap(self):
        self.assertEqual(DEFAULT_OUTPUT_TOKENS, 16384)
        profile = {**self.profile, "max_output_tokens": 131072}
        self.assertEqual(profile_settings(profile, 16384)["max_output_tokens"], 16384)
        self.assertEqual(profile_settings(self.profile, 16384)["max_output_tokens"], 8192)
        self.assertNotEqual(profile_settings(profile, 16384), profile_settings(profile, 32768))
        for cap in (0, -1, 32769, True, "16384"):
            with self.assertRaises(EvalError):
                validate_budget(cap, 600)
        for seconds in (0, 1801, False):
            with self.assertRaises(EvalError):
                validate_budget(16384, seconds)

    def test_json_is_not_repaired_or_silently_normalized(self):
        for text in ('```json\n{}\n```', '{"verdict":"pass","verdict":"fail"}', '{"score":NaN}', 'prefix {}'):
            response = copy.deepcopy(self.response)
            response["output"][1]["content"][0]["text"] = text
            with self.subTest(text=text), self.assertRaises((EvalError, ValueError)):
                extract_verdict(response, "glm-fixture")

    def test_incomplete_stream_records_usage_without_reasoning(self):
        diagnostics = Diagnostics(600)
        response = {"status": "incomplete", "model": "glm-fixture", "incomplete_details": {"reason": "max_output_tokens"},
                    "usage": {"input_tokens": 100, "output_tokens": 8192, "output_tokens_details": {"reasoning_tokens": 8184},
                              "private": "should not persist"},
                    "output": [{"type": "reasoning", "text": "hidden thought", "encrypted_content": "secret ciphertext"}]}
        events = [{"type": "response.reasoning_text.delta", "delta": "hidden thought"},
                  {"type": "response.incomplete", "response": response}]
        chunks = [('data: ' + json.dumps(e) + '\n\n').encode() for e in events]
        with self.assertRaises(ReviewFailure) as exc:
            stream_response(chunks, diagnostics)
        self.assertEqual(exc.exception.code, "output_token_limit")
        saved = diagnostics.snapshot()
        self.assertEqual(saved["terminal"]["usage"]["reasoning_tokens"], 8184)
        self.assertEqual(saved["terminal"]["public_text_blocks"], 0)
        self.assertGreater(saved["transport_bytes"], 0)
        for private in ("hidden thought", "secret ciphertext", "should not persist"):
            self.assertNotIn(private, json.dumps(saved))

    def test_diagnostics_bound_public_text_not_validation_input(self):
        from review_responses import PUBLIC_TEXT_LIMIT
        response = copy.deepcopy(self.response)
        text = json.dumps({"large": "x" * PUBLIC_TEXT_LIMIT})
        response["output"][1]["content"][0]["text"] = text
        diagnostics = Diagnostics(600)
        diagnostics.record_response(response)
        self.assertTrue(diagnostics.data["public_output_truncated"])
        self.assertLessEqual(len(diagnostics.data["public_output"].encode()), PUBLIC_TEXT_LIMIT)
        self.assertEqual(len(extract_verdict(response, "glm-fixture")["large"]), PUBLIC_TEXT_LIMIT)

    def test_redaction_covers_literal_and_json_encoded_secret(self):
        secret = 'private"value'
        record = {"public": json.dumps([secret]), "nested": [secret]}
        serialized = json.dumps(redact_record(record, secret))
        self.assertNotIn("private", serialized)

    def test_public_text_redaction_precedes_diagnostic_truncation(self):
        from review_responses import PUBLIC_TEXT_LIMIT
        diagnostics = Diagnostics(600)
        diagnostics.secret = "PRIVATE_SECRET_BOUNDARY"
        response = copy.deepcopy(self.response)
        response["output"][1]["content"][0]["text"] = "x" * (PUBLIC_TEXT_LIMIT - 10) + diagnostics.secret
        diagnostics.record_response(response)
        self.assertNotIn("PRIVATE", diagnostics.data["public_output"])

    def worker(self, response=None, schema_error=False, semantic_error=False, failure=None, streaming=False,
               payload=None, max_packet_bytes=None, expected_requests=1):
        """Mock the wire and optional dependency: no credentials, network or 3p imports."""
        response = self.response if response is None else response
        wire = mock.MagicMock()
        wire.status_code = 200
        wire.headers = {"content-type": "text/event-stream" if streaming else "application/json"}
        if failure:
            wire.iter_bytes.side_effect = failure
        else:
            data = ('data: ' + json.dumps({"type": "response.completed", "response": response}) + '\n\n') if streaming else json.dumps(response)
            wire.iter_bytes.return_value = iter([data.encode()])
        wire.__enter__.return_value = wire
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.stream.return_value = wire
        httpx = SimpleNamespace(Client=mock.Mock(return_value=client), Timeout=mock.Mock())
        validator = mock.Mock(side_effect=FakeSchemaError() if schema_error else None)
        jsonschema = SimpleNamespace(validate=validator, ValidationError=FakeSchemaError)
        payload = {"instructions": "rubric", "schema": {}, "packet": {}} if payload is None else payload
        stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(payload, ensure_ascii=False).encode()))
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(sys.modules, {"httpx": httpx, "jsonschema": jsonschema}), \
                mock.patch("review_responses.load_profile", return_value=self.profile), mock.patch("sys.stdin", stdin), \
                mock.patch("review.validate_verdict", side_effect=EvalError("fabricated review evidence reference") if semantic_error else None), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            argv = ["--profile", "fixture.yaml", "--profile-sha256", "fixture", "--timeout", "2"]
            if max_packet_bytes is not None:
                argv += ["--max-packet-bytes", str(max_packet_bytes)]
            code = main(argv)
        self.assertNotIn("never retained", stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(self.profile["api_key"], stdout.getvalue() + stderr.getvalue())
        self.assertEqual(client.stream.call_count, expected_requests)  # No hidden retry.
        if expected_requests:
            from common import digest
            from review_limits import input_limits
            kwargs = client.stream.call_args.kwargs
            self.assertNotIn("json", kwargs)
            self.assertEqual(json.loads(kwargs["content"])["truncation"], "disabled")
            self.assertEqual(json.loads(json.loads(kwargs["content"])["input"][0]["content"]), payload["packet"])
            recorded = json.loads(stdout.getvalue())
            self.assertEqual(recorded["diagnostics"]["request_sha256"], digest(kwargs["content"]))
            self.assertEqual(recorded["input_limits"], input_limits() if max_packet_bytes is None else input_limits(max_packet_bytes))
        return code, json.loads(stdout.getvalue()), validator

    def test_worker_rechecks_packet_size_before_http(self):
        code, result, validator = self.worker(payload={"instructions": "rubric", "schema": {}, "packet": {"text": "x" * 1000}},
                                              max_packet_bytes=100, expected_requests=0)
        self.assertEqual(code, 2)
        self.assertEqual(result["diagnostics"]["stage"], "input_validation")
        self.assertIn("packet too large", result["error"]["reason"])
        validator.assert_not_called()

    def test_worker_validates_locally_even_when_server_completes(self):
        code, result, validator = self.worker(schema_error=True)
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["error"]["code"], "schema_invalid")
        self.assertEqual(result["diagnostics"]["schema_error"]["path"], ["temporal", "admission_seq"])
        self.assertIn('verdict', result["diagnostics"]["public_output"])
        self.assertNotIn("verdict", result)
        validator.assert_called_once()

    def test_worker_rejects_fabricated_evidence_after_schema_passes(self):
        code, result, validator = self.worker(semantic_error=True)
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "semantic_invalid")
        self.assertEqual(result["diagnostics"]["stage"], "semantic_validation")

    def test_worker_success_requires_all_checks(self):
        for stream in (True, False):
            with self.subTest(stream=stream):
                code, result, validator = self.worker(streaming=stream)
                self.assertEqual(code, 0)
                self.assertEqual(result["status"], "validated")
                self.assertEqual(result["diagnostics"]["stage"], "complete")
                self.assertEqual(result["schema_enforcement"], "local_required")

    def test_worker_wall_timeout_and_provider_error_have_safe_receipts(self):
        for exception, expected in ((ReviewTimeout(), "wall_timeout"), (RuntimeError("private-example raw header"), "client_error")):
            with self.subTest(expected=expected):
                code, result, validator = self.worker(failure=exception)
                self.assertEqual(code, 2)
                self.assertEqual(result["error"]["code"], expected)
                validator.assert_not_called()

    def test_real_wall_deadline_produces_receipt_before_parent_kill(self):
        import signal
        import time
        previous = signal.getsignal(signal.SIGALRM)
        # Exercise the actual timer without sending any HTTP request.
        stdout = io.StringIO()
        def stalled_profile(*args):
            time.sleep(5)
        started = time.monotonic()
        with mock.patch.dict(sys.modules, {"httpx": SimpleNamespace()}), \
                mock.patch("review_responses.load_profile", side_effect=stalled_profile), \
                redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = main(["--profile", "fixture.yaml", "--profile-sha256", "fixture", "--timeout", "1"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "wall_timeout")
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_worker_nonstream_truncation_is_not_schema_error(self):
        response = {**self.response, "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
        code, result, validator = self.worker(response=response)
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "output_token_limit")
        self.assertEqual(result["diagnostics"]["terminal"]["status"], "incomplete")
        validator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
