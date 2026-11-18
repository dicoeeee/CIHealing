"""Strict review transport checks with fixed local responses; never a model API."""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from environment.gateway import ResponseBudgetAudit, make_server, review_format_audit
from harness import grading, native, review_schema

CONTRACT = {"criterion_ids": ["synthetic.criterion"], "evidence_ids": ["task", "trace"]}
ANSWER = json.dumps({"checks": [{"criterion_id": "synthetic.criterion", "applicability": "applicable",
    "status": "pass", "evidence_ids": ["task", "trace"], "reason": "Fixed local response; no model called."}]})


def terminal(answer=ANSWER):
    return {"id": "resp_synthetic", "object": "response", "created_at": 1, "status": "completed",
            "model": "synthetic-model", "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
            "output": [{"type": "message", "id": "msg_synthetic", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": answer, "annotations": []}]}],
            "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20}}


@contextmanager
def fixture(*, answer=ANSWER, streaming=False, upstream_status=200):
    events, upstream = [], []
    response = terminal(answer)
    body = ("data: " + json.dumps({"type": "response.completed", "response": response}) + "\n\n").encode() if streaming else json.dumps(response).encode()
    def handler(request):
        upstream.append(json.loads(request.content))
        return httpx.Response(upstream_status, content=body,
            headers={"Content-Type": "text/event-stream" if streaming else "application/json"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    config = {"base_url": "https://example.invalid/v1", "api_key": "SYNTHETIC_UPSTREAM_TOKEN",
              "local_token": "synthetic-token", "model_id": "synthetic-model", "max_output_tokens": 1024,
              "review_wire_format": review_schema.wire_format(CONTRACT)}
    with mock.patch("environment.gateway.httpx.Client", return_value=client):
        server = make_server(config, host="127.0.0.1", port=0, event_sink=events.append)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], events, upstream, body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if thread.is_alive():
            raise AssertionError("local fixture did not stop")


def request_body():
    return {"model": "synthetic-model", "input": "SYNTHETIC_PRIVATE_PROMPT", "max_output_tokens": 1024,
            "text": {"format": review_schema.wire_format(CONTRACT)}}


class ReviewProtocolTests(unittest.TestCase):
    def send(self, port, body, *, raw=False):
        with httpx.Client(trust_env=False, timeout=5) as client:
            return client.post(f"http://127.0.0.1:{port}/v1/responses",
                headers={"Authorization": "Bearer synthetic-token", "Content-Type": "application/json"},
                **({"content": body} if raw else {"json": body}))

    def test_correct_format_and_public_answer_are_bound_without_logging_content(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming), fixture(streaming=streaming) as (port, events, upstream, wire):
                response = self.send(port, request_body())
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, wire)
                self.assertEqual(len(upstream), 1)
            observed = native._review_protocol(events, ANSWER, review_schema.wire_format(CONTRACT))
            self.assertEqual(observed["status"], "confirmed")
            for text in (ANSWER, "SYNTHETIC_PRIVATE_PROMPT", "SYNTHETIC_UPSTREAM_TOKEN", "synthetic-token"):
                self.assertNotIn(text, json.dumps(events))
            self.assertFalse(native._review_protocol(events, ANSWER + " ", review_schema.wire_format(CONTRACT))["returned_text_confirmed"])
            changed = copy.deepcopy(events)
            next(row for row in changed if row['kind'] == 'generation_response')['request_id'] += 1
            self.assertEqual(native._review_protocol(changed, ANSWER, review_schema.wire_format(CONTRACT))["status"], "unconfirmed")

    def test_missing_relaxed_replaced_or_changed_ids_never_reach_upstream(self):
        good = request_body()
        variants = [{k: v for k, v in good.items() if k != "text"}, {**good, "text": None},
                    {**good, "text": {"format": {"type": "json_object"}}}]
        for field in ("strict", "root_closed", "item_closed", "evidence_enum", "criterion_enum", "extra_field"):
            body = copy.deepcopy(good)
            fmt = body['text']['format']
            item = fmt['schema']['properties']['checks']['items']
            if field == "strict": fmt['strict'] = False
            if field == "root_closed": fmt['schema']['additionalProperties'] = True
            if field == "item_closed": item['additionalProperties'] = True
            if field == "evidence_enum": item['properties']['evidence_ids']['items']['enum'] = ['trusted_task']
            if field == "criterion_enum": del item['properties']['criterion_id']['enum']
            if field == "extra_field": item['properties']['reason_note'] = {'type': 'null'}
            variants.append(body)
        with fixture() as (port, events, upstream, _):
            for body in variants:
                with self.subTest(body=body):
                    self.assertEqual(self.send(port, body).status_code, 403)
            self.assertEqual(upstream, [])
            audits = [row for row in events if row['kind'] == 'review_format_request']
            self.assertEqual(len(audits), len(variants))
            self.assertTrue(all(row['matched'] is False for row in audits))

    def test_duplicate_request_format_key_is_rejected_before_forwarding(self):
        raw = json.dumps(request_body()).replace('"strict": true', '"strict": false, "strict": true')
        with fixture() as (port, _, upstream, _):
            self.assertEqual(self.send(port, raw, raw=True).status_code, 400)
            self.assertEqual(upstream, [])

    def test_provider_ignoring_schema_is_not_silently_repaired(self):
        for change, category in [({'evidence_ids': ['trusted_task', 'public_execution_trace']}, 'reference'),
                                 ({'reason_note': None}, 'schema')]:
            raw = json.loads(ANSWER)
            raw['checks'][0].update(change)
            answer = json.dumps(raw)
            with fixture(answer=answer) as (port, events, upstream, wire):
                self.assertEqual(self.send(port, request_body()).content, wire)
                self.assertEqual(len(upstream), 1)
            self.assertEqual(native._review_protocol(events, answer, review_schema.wire_format(CONTRACT))['status'], 'confirmed')
            packet = {'evidence': {key: {'kind': 'trusted_task' if key == 'task' else 'public_execution_trace',
                                        'complete': True} for key in CONTRACT['evidence_ids']}}
            criteria = [{'id': 'synthetic.criterion', 'method': 'semantic'}]
            result = grading.validate_review(answer, criteria, packet)
            self.assertEqual(result[0]['validation_error_category'], category)
            self.assertEqual(result[0]['status'], 'inconclusive')
            self.assertEqual(json.loads(answer), raw)

    def test_schema_rejection_does_not_retry(self):
        with fixture(upstream_status=400) as (port, events, upstream, _):
            self.assertEqual(self.send(port, request_body()).status_code, 502)
            self.assertEqual(len(upstream), 1)
        self.assertEqual(native._review_protocol(events, '', review_schema.wire_format(CONTRACT))['status'], 'unconfirmed')

    def test_split_sse_and_ambiguous_public_output_remain_bounded(self):
        response = terminal()
        raw = ('data: ' + json.dumps({'type': 'response.completed', 'response': response}) + '\r\n\r\n').encode()
        audit = ResponseBudgetAudit(True, review=True)
        for offset in range(0, len(raw), 7):
            audit.feed(raw[offset:offset + 7])
        self.assertEqual(audit.finish()['output_text_sha256'], hashlib.sha256(ANSWER.encode()).hexdigest())
        for mutate in ('multiple', 'missing', 'truncated'):
            other = copy.deepcopy(response)
            if mutate == 'multiple': other['output'] *= 2
            if mutate == 'missing': del other['output']
            encoded = json.dumps(other).encode()
            if mutate == 'truncated': encoded = encoded[:-10]
            audit = ResponseBudgetAudit(False, review=True)
            audit.feed(encoded)
            self.assertIsNone(audit.finish().get('output_text_sha256'))


@unittest.skipUnless(os.environ.get('CI_EVAL_V2_NATIVE_RUNTIME'), 'requires installed iCode and macOS sandbox')
class InstalledReviewGatewayTests(unittest.TestCase):
    def test_real_judge_adapter_cli_and_gateway_use_same_contract(self):
        runtime = json.loads(Path(os.environ['CI_EVAL_V2_NATIVE_RUNTIME']).read_text())
        with tempfile.TemporaryDirectory(prefix='ci-review-protocol-', dir='/private/tmp') as temporary:
            root = Path(temporary).resolve()
            profile = root / 'profile.yaml'
            profile.write_text(json.dumps({'id': 'synthetic', 'name': 'synthetic', 'provider': 'openai',
                'api_style': 'responses', 'model_id': 'synthetic-model', 'base_url': 'https://example.invalid/v1',
                'api_key': 'synthetic-token', 'max_context_tokens': 32768, 'max_output_tokens': 1024, 'stream': False}))
            runtime.update(review_profile=str(profile), review_model_id='synthetic-model',
                           review_response_model_id='synthetic-model', endpoint_host='example.invalid')
            @contextmanager
            def fake_gateway(profile, directory, timeout_seconds):
                self.assertEqual(profile['_review_wire_format'], review_schema.wire_format(CONTRACT))
                directory.mkdir()
                with fixture() as (port, events, upstream, _):
                    yield port, 'synthetic-token'
                self.assertEqual(len(upstream), 1)
                (directory / 'events.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in events))
                (directory / 'cleanup.json').write_text('{"status":"confirmed"}')
            with mock.patch.object(native, '_gateway', fake_gateway):
                result = native.judge(root / 'review', runtime, 'Return the fixed synthetic review.', 90,
                                      review_contract=CONTRACT)
            self.assertTrue(result['trace_complete'], result['gaps'])
            self.assertEqual(result['answer'], ANSWER)
            self.assertEqual(result['review_protocol']['status'], 'confirmed')
            self.assertEqual(json.loads((root / 'review/review-contract.json').read_text())['allowed_ids'], CONTRACT)


if __name__ == '__main__':
    unittest.main()
