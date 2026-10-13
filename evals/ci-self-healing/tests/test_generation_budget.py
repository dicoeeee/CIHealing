"""Inherited profile limits and terminal audits; loopback only, no real models."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import EvalError
from environment.gateway import ResponseBudgetAudit, make_server
from execution_outcome import classify_execution
from gateway_evidence import analyze, gateway_failure
from generation_budget import generation_failure
from native_repair import generation_budget, isolated_model, load_config


def terminal(status='completed', reason=None, tokens=12):
    return {'id': 'response-synthetic', 'status': status, 'output': [{'text': 'PRIVATE_RESPONSE'}],
            'incomplete_details': {'reason': reason} if reason else None,
            'usage': {'output_tokens': tokens}}


def sse(response):
    return ('data: ' + json.dumps({'type': 'response.' + response['status'], 'response': response}) + '\n\n').encode()


@contextmanager
def gateway_fixture(response, *, streaming=True, profile_cap=8192):
    events, upstream = [], []
    body = sse(response) if streaming else json.dumps(response).encode()
    binding = {'trial_id': 'trial-test', 'gateway_id': 'gateway-test', 'clock_id': 'clock-test'}
    def handler(request):
        upstream.append(json.loads(request.content))
        return httpx.Response(200, content=body, headers={'Content-Type': 'text/event-stream' if streaming else 'application/json'})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    config = {'base_url': 'https://example.invalid', 'api_key': 'NOT_A_REAL_KEY', 'local_token': 'local-test',
              'model_id': 'model-test', 'event_binding': binding, 'max_output_tokens': profile_cap}
    with mock.patch('environment.gateway.httpx.Client', return_value=client):
        server = make_server(config, host='127.0.0.1', port=0, event_sink=events.append)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        with httpx.Client(trust_env=False, timeout=5) as downstream:
            def send(cap=profile_cap):
                payload = {'model': 'model-test', 'input': 'PRIVATE_PROMPT', 'stream': streaming}
                if cap is not None:
                    payload['max_output_tokens'] = cap
                return downstream.post(f'http://127.0.0.1:{server.server_address[1]}/v1/responses',
                                       json=payload, headers={'Authorization': 'Bearer local-test'})
            yield send, events, upstream, binding, body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if thread.is_alive():
            raise AssertionError('test server did not terminate')


def receipt(events, binding, profile_cap=8192):
    return {'status': 'completed', 'backend': 'native', 'filesystem_status': 'captured',
            'isolation_confirmed': True, 'gateway_cleanup_confirmed': True,
            'generation_budget': generation_budget({'max_output_tokens': profile_cap}),
            'process': {'status': 'completed', 'returncode': 0, 'cleanup': {'status': 'confirmed'},
                        'clock_id': binding['clock_id'], 'identity': {k: binding[k] for k in ('trial_id', 'gateway_id')}},
            'runtime_diagnostics': {'gateway_statuses': [200], 'gateway_evidence': {
                'schema': 'ci-gateway-evidence-v1', 'binding': binding, 'events': events, 'gaps': []}}}


class GenerationBudgetTests(unittest.TestCase):
    def test_profile_limit_is_inherited_without_an_additional_repair_cap(self):
        model = {'id': 'same-id', 'name': 'same-name', 'model_id': 'same-model', 'max_output_tokens': 131072,
                 'api_key': 'DO_NOT_COPY', 'base_url': 'https://upstream.invalid'}
        original = copy.deepcopy(model)
        budget = generation_budget(model)
        selected = isolated_model(model, 43210, budget)
        self.assertEqual(selected['max_output_tokens'], 131072)
        self.assertEqual(budget['contract'], 'profile-generation-v1')
        self.assertNotIn('requested_max_output_tokens', budget)
        self.assertEqual(selected['model_id'], model['model_id'])
        self.assertEqual(selected['base_url'], 'http://127.0.0.1:43210/v1')
        self.assertNotIn('DO_NOT_COPY', json.dumps(selected))
        self.assertEqual(model, original)
        for cap in (2048, 8192, 131072, 262144):
            inherited = generation_budget({'max_output_tokens': cap})
            self.assertEqual(inherited['effective_max_output_tokens'], cap)
            self.assertEqual(isolated_model(model, 43210, inherited)['max_output_tokens'], cap)

    def test_invalid_profile_limit_fails_closed(self):
        for value in (True, 0, -1, '8192', 8192.5, None):
            with self.subTest(value=value), self.assertRaises(EvalError):
                generation_budget({'max_output_tokens': value})
        with self.assertRaises(EvalError):
            generation_budget({})

    def test_runtime_does_not_add_a_cap_or_silently_accept_the_retired_option(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory(prefix='budget-config-') as d:
            root = Path(d).resolve()
            cache = root/'runs/cache';cache.mkdir(parents=True)
            original = {'backend': 'native', 'python': sys.executable, 'chrys': sys.executable,
                        'maven': sys.executable, 'git': sys.executable, 'rg': sys.executable,
                        'java_home': str(root/'jdk'), 'maven_repository': str(cache),
                        'model_profile': str(root/'not-read.yaml'), 'endpoint_host': 'example.invalid'}
            before = copy.deepcopy(original)
            with mock.patch('native_repair.ROOT', root):
                config = load_config(original)
                self.assertNotIn('repair_max_output_tokens', config)
                self.assertEqual(config['timeout_seconds'], 600)
                for old_value in (8192, False, None):
                    with self.assertRaisesRegex(EvalError, 'no longer supported'):
                        load_config({**original, 'repair_max_output_tokens': old_value})
            self.assertEqual(original, before)

    def test_terminal_parser_handles_split_streams_without_retaining_text(self):
        for newline in (b'\n', b'\r\n'):
            raw = sse(terminal()).replace(b'\n', newline)
            audit = ResponseBudgetAudit(True)
            for byte in raw:
                audit.feed(bytes([byte]))
            self.assertEqual(audit.finish(), {'status': 'completed', 'reason': 'none', 'output_tokens': 12})
            self.assertNotIn('PRIVATE_RESPONSE', json.dumps(audit.rows))

    def test_missing_conflicting_duplicate_or_truncated_terminal_is_not_success(self):
        payloads = [b'', sse(terminal())[:-2], b'data: {invalid}\n\n', sse(terminal())*2,
                    b'data: {"type":"response.completed","response":{"status":"incomplete"}}\n\n',
                    b'data: {"type":"response.completed","response":{"status":"completed","status":"completed"}}\n\n',
                    b'x'*(ResponseBudgetAudit.LIMIT+1)]
        for raw in payloads:
            with self.subTest(length=len(raw)):
                audit = ResponseBudgetAudit(True);audit.feed(raw)
                self.assertEqual(audit.finish()['status'], 'unconfirmed')

    def test_exact_wire_limit_is_forwarded_and_request_body_is_not_logged(self):
        for streaming in (True, False):
            with gateway_fixture(terminal(), streaming=streaming) as (send, events, upstream, binding, body):
                response = send()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, body)
                self.assertEqual(upstream[0]['max_output_tokens'], 8192)
                self.assertFalse(upstream[0]['store'])
            self.assertEqual(analyze(events, binding)[0], [])
            run = receipt(events, binding)
            self.assertIsNone(gateway_failure(run['runtime_diagnostics']['gateway_evidence'], run['process']))
            self.assertIsNone(generation_failure(run))
            self.assertIsNone(classify_execution(run, 'run'))
            for private in ('PRIVATE_PROMPT', 'PRIVATE_RESPONSE', 'NOT_A_REAL_KEY', 'local-test'):
                self.assertNotIn(private, json.dumps(events))

    def test_missing_changed_or_invalid_wire_cap_is_rejected_before_upstream(self):
        with gateway_fixture(terminal()) as (send, events, upstream, binding, body):
            for cap in (None, True, '8192', 8192.0, 8193, 0, 4096):
                self.assertEqual(send(cap).status_code, 403)
            self.assertEqual(upstream, [])

    def test_truncation_cannot_be_overridden_by_zero_exit_or_next_request(self):
        with gateway_fixture(terminal('incomplete', 'max_output_tokens', 8192)) as (send, events, upstream, binding, body):
            self.assertEqual(send().content, body)
            first = copy.deepcopy(events)
            run = receipt(first, binding)
            self.assertEqual(generation_failure(run), 'generation_output_limit')
            self.assertEqual(classify_execution(run, 'run')['code'], 'generation_output_limit')
            self.assertEqual(send().status_code, 403)
            self.assertEqual(len(upstream), 1)
        self.assertTrue(any(e['kind']=='generation_continuation_rejected' for e in events))
        self.assertIsNotNone(classify_execution(receipt(events, binding), 'run'))

    def test_provider_cap_violation_and_other_incomplete_reasons_are_not_pass(self):
        for response, code in ((terminal(tokens=8193), 'generation_cap_exceeded'),
                               (terminal('incomplete', 'content_filter'), 'generation_response_incomplete'),
                               (terminal('failed'), 'generation_response_incomplete')):
            with gateway_fixture(response) as (send, events, upstream, binding, body):
                send()
            self.assertEqual(generation_failure(receipt(events, binding)), code)

    def test_completion_metadata_missing_or_reordered_cannot_satisfy_budget(self):
        with gateway_fixture(terminal()) as (send, events, upstream, binding, body):
            send()
        for kind in ('generation_request', 'generation_response'):
            altered = copy.deepcopy(events)
            altered = [e for e in altered if e['kind'] != kind]
            for i, e in enumerate(altered,1):e['seq']=i
            self.assertEqual(generation_failure(receipt(altered,binding)), 'generation_budget_unconfirmed')
        run = receipt(events,binding)
        run['generation_budget']['effective_max_output_tokens'] = 131072
        self.assertEqual(generation_failure(run), 'generation_budget_unconfirmed')

    def test_legacy_receipts_are_not_retroactively_given_generation_evidence(self):
        self.assertIsNone(generation_failure({'status':'completed'}))
        self.assertEqual(generation_failure({'generation_budget':{}}), 'generation_budget_unconfirmed')

    def test_legacy_repair_caps_keep_their_original_meaning(self):
        with gateway_fixture(terminal()) as (send, events, upstream, binding, body):
            send()
        run = receipt(events, binding)
        run['generation_budget'] = {'contract': 'generation-budget-v1',
                                    'requested_max_output_tokens': 8192,
                                    'profile_max_output_tokens': 131072,
                                    'effective_max_output_tokens': 8192}
        before = copy.deepcopy(run)
        self.assertIsNone(generation_failure(run))
        self.assertEqual(run, before)
        run['generation_budget']['effective_max_output_tokens'] = 131072
        self.assertEqual(generation_failure(run), 'generation_budget_unconfirmed')

    def test_real_wire_inherits_large_profiles_and_accepts_complete_output_above_8192(self):
        for profile_cap in (131072, 262144):
            with gateway_fixture(terminal(tokens=12000), profile_cap=profile_cap) as (send, events, upstream, binding, body):
                self.assertEqual(send().content, body)
                self.assertEqual(upstream[0]['max_output_tokens'], profile_cap)
            run = receipt(events, binding, profile_cap)
            self.assertIsNone(generation_failure(run))
            self.assertIsNone(classify_execution(run, 'run'))
            run['generation_budget']['requested_max_output_tokens'] = 8192
            self.assertEqual(generation_failure(run), 'generation_budget_unconfirmed')

    def test_malformed_terminal_usage_does_not_disguise_itself_as_missing_usage(self):
        for tokens in (-1, True, '8192', {}):
            audit = ResponseBudgetAudit(True);audit.feed(sse(terminal(tokens=tokens)))
            self.assertEqual(audit.finish()['status'], 'unconfirmed')
        for key in ('usage', 'incomplete_details'):
            for value in ([], '', 0, False):
                response = terminal();response[key] = value
                audit = ResponseBudgetAudit(True);audit.feed(sse(response))
                self.assertEqual(audit.finish()['status'], 'unconfirmed')
        audit = ResponseBudgetAudit(True);audit.feed(sse(terminal(tokens=None)))
        self.assertIsNone(audit.finish()['output_tokens'])

    def test_missing_or_cross_trial_generation_evidence_fails_closed(self):
        with gateway_fixture(terminal()) as (send, events, upstream, binding, body):
            send()
        for diagnostics in (None, [], {}, {'gateway_evidence': []}):
            run = receipt(events,binding);run['runtime_diagnostics']=diagnostics
            self.assertEqual(generation_failure(run), 'generation_budget_unconfirmed')
        run = receipt(events,binding);run['process']['identity']['trial_id']='another-trial'
        self.assertEqual(generation_failure(run), 'generation_budget_unconfirmed')


if __name__ == '__main__':
    unittest.main()
