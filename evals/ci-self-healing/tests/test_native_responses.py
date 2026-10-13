"""Chrys 0.29 Responses omits finish_reason on normal completion.

Synthetic, redacted event shape calibrated against a real native CLI trace;
no saved model text, credentials or reasoning in this fixture.
"""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import EvalError
from native_runner import exchange_receipt, native_identity


def responses_trace():
    spec = [
        ('trajectory.coverage.started', None, None, {'coverage_reason': 'session_started'}),
        ('trajectory.runtime.started', None, None, {}),
        ('turn.started', None, None, {'turn_id': 'turn-1'}),
        ('model.run.started', 'run-1', None, {}),
        ('model.cycle.started', 'cycle-1', 'run-1', {}),
        ('model.exchange.started', 'exchange-1', 'cycle-1', {'request_model': 'glm-5.3', 'api_style': 'responses'}),
        ('model.validation.finished', 'validation-1', 'exchange-1', {'outcome': 'accepted', 'exchange_operation_id': 'exchange-1'}),
        ('model.exchange.finished', 'exchange-1', 'cycle-1', {'response_model': 'glm-5.3', 'response_id': 'response-1',
                                                           'outcome': 'success', 'api_style': 'responses'}),
        ('model.cycle.finished', 'cycle-1', 'run-1', {'outcome': 'success', 'exchange_count': 1, 'function_call_count': 0,
                                                   'final_exchange_operation_id': 'exchange-1'}),
        ('model.run.finished', 'run-1', None, {'outcome': 'completed'}),
        ('turn.finished', None, None, {'end_reason': 'completed', 'turn_id': 'turn-1'}),
        ('trajectory.coverage.ended', None, None, {'coverage_id': 'coverage-1', 'last_sequence': 11}),
        ('trajectory.runtime.finished', None, None, {'reason': 'graceful_shutdown'}),
    ]
    return [{'schema_version': 1, 'sequence': i, 'session_id': 'session-1', 'runtime_id': 'runtime-1',
             'coverage_id': 'coverage-1', 'turn_id': 'turn-1', 'event_type': kind, 'operation_id': oid,
             'parent_operation_id': parent, 'actor': {'kind': 'agent', 'role': 'main'}, 'payload': payload}
            for i, (kind, oid, parent, payload) in enumerate(spec, 1)]


def assess(rows):
    return exchange_receipt(('\n'.join(json.dumps(r) for r in rows) + '\n').encode(), 'session-1', 'glm-5.3')


class NativeResponsesTests(unittest.TestCase):
    def test_bad_profile_parser_does_not_echo_secret_source(self):
        config = {'profile': str(Path.home() / '.chrys/models/invalid.yaml')}
        with mock.patch('native_runner.read_regular', return_value=b'name: [\napi_key: TEST_SECRET_MARKER\n'):
            with self.assertRaises(EvalError) as caught:
                native_identity(config)
        self.assertNotIn('TEST_SECRET_MARKER', str(caught.exception))
        self.assertIn('invalid YAML', str(caught.exception))

    def test_normal_responses_completion_is_not_fabricated_stop(self):
        result = assess(responses_trace())
        self.assertEqual(result['status'], 'validated')
        self.assertIsNone(result['finish_reason'])
        self.assertEqual(result['completion_basis'], 'chrys-responses-lifecycle')
        self.assertEqual(result['provider_terminal_status'], 'not_recorded')

    def test_missing_finish_reason_alone_is_not_sufficient(self):
        for index, field, value in [(5, 'api_style', 'chat_completions'), (6, 'outcome', 'rejected'),
                                    (6, 'exchange_operation_id', 'other'), (8, 'exchange_count', 2),
                                    (8, 'function_call_count', 1), (8, 'final_exchange_operation_id', 'other'),
                                    (9, 'outcome', 'error'), (10, 'end_reason', 'cancelled')]:
            rows = responses_trace()
            rows[index]['payload'][field] = value
            with self.subTest(index=index, field=field), self.assertRaises(EvalError):
                assess(rows)

    def test_missing_lifecycle_marker_or_wrong_pair_is_rejected(self):
        for index, field, value in [(8, 'event_type', 'unknown'), (8, 'operation_id', 'other'),
                                    (8, 'parent_operation_id', 'other'), (10, 'turn_id', 'other')]:
            rows = responses_trace()
            rows[index][field] = value
            with self.subTest(index=index, field=field), self.assertRaises(EvalError):
                assess(rows)

    def test_truncation_is_not_normal_completion(self):
        rows = responses_trace()
        rows[7]['payload']['finish_reason'] = 'length'
        with self.assertRaises(EvalError):
            assess(rows)


if __name__ == '__main__':
    unittest.main()
