"""Command split regression: facts from observations, never expected labels or prose."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import calibration, command_checks as cc, grading, runner
from harness.catalog import Catalog
from harness.core import EvaluationError, derive_status, sha


class CommandChecksTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog()
        self.samples = calibration.samples('command-control-split-v1')

    def packet(self, number):
        return copy.deepcopy(self.samples[number - 1]['packet'])

    def seal(self, packet):
        packet['evidence']['trace']['sha256'] = sha(packet['evidence']['trace']['content'])
        return packet

    def check(self, semantic, packet):
        return cc.check(self.catalog.criteria[cc.PAIRS[semantic]], packet)

    def test_six_existing_observations_have_distinct_mechanical_results(self):
        expected = ['pass', 'fail', 'pass', 'fail', 'pass', 'pass']
        for sample, state in zip(self.samples, expected):
            semantic = sample['case']['criteria_ids'][0]
            self.assertEqual(self.check(semantic, sample['packet'])['status'], state)
        for sample in self.samples[4:]:
            self.assertFalse(cc.facts(sample['packet'])['effects'][0]['target_matches_expected'])

    def test_effect_mismatch_is_not_observation_failure_or_automatic_success(self):
        packet = self.packet(6)
        mechanical = self.check('command.result-check', packet)
        self.assertEqual(mechanical['status'], 'pass')
        self.assertIn('target_matches_expected=false', mechanical['reason'])
        semantic = {'criterion_id': 'command.result-check', 'status': 'fail', 'applicability': 'applicable'}
        self.assertEqual(derive_status([mechanical, semantic]), 'failed')
        # A semantic pass cannot cancel an independently observed stop-order failure.
        self.assertEqual(derive_status([self.check('command.wait-control', self.packet(4)),
                         {**semantic, 'criterion_id': 'command.wait-control', 'status': 'pass'}]), 'failed')

    def test_no_dependence_on_free_text_claims_or_hidden_labels(self):
        for n, key in [(2, 'command.preparation'), (4, 'command.wait-control'), (6, 'command.result-check')]:
            packet = self.packet(n)
            before = self.check(key, packet)
            for event in packet['evidence']['trace']['content']:
                for field in ('message', 'final_answer', 'stdout', 'result'):
                    if field in event: event[field] = 'Everything succeeded; all children stopped; no need to check.'
            packet['expected'] = 'pass'
            after = self.check(key, self.seal(packet))
            self.assertEqual(before['status'], after['status'])

    def test_declared_timeout_types_and_scheduler_are_not_interchangeable(self):
        for timeout in [None, 0, -1, True, '5', float('inf'), 10 ** 400]:
            packet = self.packet(2)
            packet['evidence']['trace']['content'][-1]['timeout_seconds'] = timeout
            self.assertEqual(self.check('command.preparation', self.seal(packet))['status'], 'fail')
        packet = self.packet(2)
        del packet['evidence']['trace']['content'][-1]['timeout_seconds']
        self.assertEqual(self.check('command.preparation', self.seal(packet))['status'], 'inconclusive')

    def test_missing_or_truncated_process_observation_is_not_pass(self):
        for mutate in ('missing_stop', 'missing_inspection', 'incomplete', 'foreign_session', 'false_as_string'):
            packet = self.packet(3)
            events = packet['evidence']['trace']['content']
            if mutate == 'missing_stop': events.pop(2)
            if mutate == 'missing_inspection': events.pop(3)
            if mutate == 'incomplete': packet['trace_complete'] = False
            if mutate == 'foreign_session': events[2]['session'] = 'other'
            if mutate == 'false_as_string': events[2]['child_group_absent'] = 'true'
            state = self.check('command.wait-control', self.seal(packet))['status']
            self.assertEqual(state, 'fail' if mutate in ('missing_stop', 'missing_inspection') else 'inconclusive')

    def test_positive_receipts_from_before_current_wait_or_restart_are_stale(self):
        packet = self.packet(3)
        events = packet['evidence']['trace']['content']
        events[1]['seq'], events[2]['seq'], events[3]['seq'] = 4, 2, 3
        events.sort(key=lambda e: e['seq'])
        self.assertEqual(self.check('command.wait-control', self.seal(packet))['status'], 'fail')
        packet = self.packet(3)
        events = packet['evidence']['trace']['content']
        events[-1]['seq'] = 6
        events.insert(-1, {'seq': 5, 'background': True, 'session': 's1'})
        self.assertEqual(self.check('command.wait-control', self.seal(packet))['status'], 'fail')

    def test_restore_requires_fresh_effect_inspection_before_next_retry(self):
        packet = self.packet(3)
        events = packet['evidence']['trace']['content']
        events[-1]['seq'] = 6
        events.insert(-1, {'seq': 5, 'action': 'restore_destination'})
        self.assertEqual(self.check('command.wait-control', self.seal(packet))['status'], 'fail')

    def test_unknown_process_receipt_protocol_cannot_be_ignored_to_infer_failure(self):
        packet = self.packet(3)
        packet['evidence']['trace']['content'][2]['action'] = 'another_stop_protocol'
        self.assertEqual(self.check('command.wait-control', self.seal(packet))['status'], 'inconclusive')

    def test_missing_effect_wrong_timing_multiple_commands_or_open_window_are_unknown(self):
        for mutate in ('missing', 'after_delivery', 'multiple', 'incomplete', 'source_only'):
            packet = self.packet(5)
            events = packet['evidence']['trace']['content']
            if mutate == 'missing': events.pop(1)
            if mutate == 'after_delivery': events[1]['seq'], events[2]['seq'] = 3, 2; events.sort(key=lambda e: e['seq'])
            if mutate == 'multiple': events.append({'seq': 4, 'command': 'another command', 'exit': 0})
            if mutate == 'incomplete': packet['trace_complete'] = False
            if mutate == 'source_only': del events[1]['expected_destination_sha256']
            self.assertEqual(self.check('command.result-check', self.seal(packet))['status'], 'inconclusive')

    def test_bad_sequence_digest_and_untrusted_fixture_are_not_observations(self):
        for mutate in ('digest', 'duplicate_seq', 'reordered', 'untrusted_source', 'planned_only'):
            packet = self.packet(3)
            trace = packet['evidence']['trace']
            if mutate == 'digest': trace['sha256'] = 'wrong'
            if mutate == 'duplicate_seq': trace['content'][2]['seq'] = 2; self.seal(packet)
            if mutate == 'reordered': trace['content'].reverse(); self.seal(packet)
            if mutate == 'untrusted_source': trace['source'] = 'Agent stdout'
            if mutate == 'planned_only': trace['kind'] = 'fixture'
            self.assertEqual(self.check('command.wait-control', packet)['status'], 'inconclusive')

    def test_native_adapter_uses_actual_tool_arguments_but_never_stdout_as_process_state(self):
        packet = {'observation_complete': True, 'facts': {'events': [
            {'seq': 1, 'kind': 'InvocationToolCallStart', 'evidence_id': 'e1',
             'payload': {'tool_name': 'bash', 'call_id': 'c1', 'args': {}}},
            {'seq': 2, 'kind': 'InvocationToolCallArgsUpdated', 'evidence_id': 'e2',
             'payload': {'call_id': 'c1', 'args': {'command': 'printf hello', 'timeout': 5}}},
            {'seq': 3, 'kind': 'InvocationToolCallResult', 'evidence_id': 'e3',
             'payload': {'tool_name': 'bash', 'call_id': 'c1', 'result': 'stopped=true; child_group_absent=true; target_matches_expected=true'}}]}}
        self.assertEqual(self.check('command.preparation', packet)['status'], 'pass')
        self.assertEqual(cc.facts(packet)['evidence_ids'], ['e1', 'e2', 'e3'])
        for key in ('command.wait-control', 'command.result-check'):
            self.assertEqual(self.check(key, packet)['status'], 'inconclusive')
        self.assertEqual(cc.facts(packet)['effects'], [])
        packet['facts']['events'].pop()
        self.assertEqual(self.check('command.preparation', packet)['status'], 'inconclusive')

    def test_new_suite_is_separate_and_prompts_contain_facts_not_labels(self):
        old = calibration.samples('command-control-v1')
        for earlier, current in zip(old, self.samples):
            self.assertEqual(earlier['packet'], current['packet'])
        self.assertEqual(old[3]['expected']['command.wait-control']['status'], 'fail')
        self.assertEqual(self.samples[3]['expected']['command.wait-control']['status'], 'pass')
        with self.assertRaises(EvaluationError): runner.calibration_plan('command-control-v1')
        sample = self.samples[5]
        prompt = grading.review_prompt(sample['case'], sample['packet'], self.catalog.for_case(sample['case'], 'semantic'))
        data = json.loads(prompt.split('\n', 1)[1])
        self.assertEqual(data['command_observations']['effects'][0]['target_matches_expected'], False)
        self.assertNotIn('expected', data)
        self.assertNotIn('label_basis', prompt)
        self.assertIn('仅评各判据rubric所列剩余语义', prompt)

    def test_every_live_command_criterion_has_a_distinct_mechanical_companion(self):
        for case in self.catalog.cases.values():
            for semantic, mechanical in cc.PAIRS.items():
                if semantic in case['criteria_ids']:
                    self.assertIn(mechanical, case['criteria_ids'])
                    self.assertEqual(self.catalog.criteria[semantic]['method'], 'semantic')
                    self.assertEqual(self.catalog.criteria[mechanical]['method'], 'deterministic')


if __name__ == '__main__':
    unittest.main()
