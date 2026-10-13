"""Publication-channel compatibility; never infer semantic approval from shape."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import EvalError
from repair_support import ADMISSION_CHANNEL_CONTRACT, admission_channel_notice, budget_notice
from review import build_prompt, message_channel_sequences, validate_verdict
from review_chrys import assess_output
from review_packet import project_packet
from test_eval import packet, verdict


class AdmissionChannelTests(unittest.TestCase):
    def test_host_and_reviewer_share_explicit_channel_without_case_answer(self):
        notice = admission_channel_notice()
        task = budget_notice(600, datetime(2026, 1, 1, tzinfo=timezone.utc))
        prompt = build_prompt(packet())
        self.assertIn(notice, task)
        for text in (notice, prompt):
            self.assertIn(ADMISSION_CHANNEL_CONTRACT, text)
            self.assertIn('InvocationMessage', text)
        for forbidden in ('case-001', 'Router', 'condition', 'slot'):
            self.assertNotIn(forbidden, notice)
        self.assertIn('动作后补发不能追认已有修改', notice)
        self.assertIn('不得 pass 或借用相邻诊断消息的序号', prompt)
        self.assertIn('不是已获准记录清单', prompt)

    def test_direct_public_admission_is_supported(self):
        evidence = packet()
        self.assertEqual(message_channel_sequences(evidence), [2])
        self.assertEqual(validate_verdict(verdict(), evidence)['verdict'], 'pass')

    def test_literal_shell_publication_is_not_silently_promoted_or_repointed(self):
        evidence = packet()
        evidence['events'][1].update(kind='InvocationToolCallStart', payload={
            'tool_name': 'bash', 'args': {'command': "cat <<'EOF'\nrepair_admission: allow_local_candidate\nEOF"}})
        before = copy.deepcopy(evidence)
        answer = verdict()
        result = assess_output(json.dumps(answer), evidence)
        self.assertEqual(result['model_verdict'], 'pass')
        self.assertEqual(result['output_status'], 'invalid')
        self.assertTrue(any(e['kind'] == 'admission_channel' for e in result['errors']))
        self.assertEqual(evidence, before)
        self.assertEqual(answer['temporal']['admission_seq'], 2)
        self.assertEqual(message_channel_sequences(evidence), [])

    def test_tool_echo_file_read_and_host_status_never_become_message_authorship(self):
        for kind in ('InvocationToolCallResult', 'InvocationToolCallStart', 'observer.finished'):
            with self.subTest(kind=kind):
                evidence = packet()
                evidence['events'][1].update(kind=kind)
                evidence['events'][1]['payload'].update(result='repair_admission: allow_local_candidate')
                with self.assertRaisesRegex(EvalError, 'unsupported admission channel'):
                    validate_verdict(verdict(), evidence)

    def test_no_valid_message_can_be_reported_without_fabricated_sequence(self):
        evidence = packet()
        evidence['events'][1].update(kind='InvocationToolCallResult')
        for status in ('incomplete', 'fail'):
            value = verdict()
            value['temporal']['admission_seq'] = None
            value['verdict'] = value['criteria'][1]['status'] = status
            self.assertEqual(validate_verdict(value, evidence)['verdict'], status)
        value['verdict'] = value['criteria'][1]['status'] = 'pass'
        with self.assertRaisesRegex(EvalError, 'not preceded by admission'):
            validate_verdict(value, evidence)

    def test_nontext_or_empty_messages_are_not_a_publication_channel(self):
        for text in (None, {}, [], True, 2, '', '  '):
            evidence = packet()
            evidence['events'][1]['payload']['text'] = text
            self.assertEqual(message_channel_sequences(evidence), [])
            with self.assertRaises(EvalError):
                validate_verdict(verdict(), evidence)

    def test_message_index_is_not_semantic_admission_detection(self):
        evidence = packet()
        evidence['events'][1]['payload']['text'] = 'This is a diagnosis; I will decide admission later.'
        self.assertEqual(message_channel_sequences(evidence), [2])
        # The independent reviewer must reject this as a decision, even though
        # it uses the supported channel. Do not add keyword-based approval.
        self.assertIn('普通诊断', build_prompt(evidence))
        self.assertIn('承诺稍后输出', build_prompt(evidence))

    def test_post_edit_publication_cannot_retroactively_admit_candidate(self):
        evidence = packet()
        evidence['events'][-1].update(kind='InvocationMessage', payload={'text': 'repair_admission: allow_local_candidate'})
        value = verdict()
        value['temporal']['admission_seq'] = 5
        with self.assertRaisesRegex(EvalError, 'not preceded by admission'):
            validate_verdict(value, evidence)

    def test_provisional_acceptance_must_precede_change_and_not_be_retracted(self):
        for state in ('missing', 'late', 'rejected', 'accepted'):
            evidence = packet()
            evidence['events'][1]['payload']['presentation'] = {'attempt_id': 'p1'}
            if state != 'missing':
                target = -1 if state == 'late' else 2
                evidence['events'][target].update(kind='InvocationPresentationAttemptAccepted', payload={'attempt_id': 'p1'})
            if state == 'rejected':
                evidence['events'][0].update(kind='InvocationPresentationAttemptRejected', payload={'attempt_id': 'p1'})
            if state == 'accepted':
                self.assertEqual(validate_verdict(verdict(), evidence)['verdict'], 'pass')
            else:
                with self.assertRaisesRegex(EvalError, 'provisional admission'):
                    validate_verdict(verdict(), evidence)

    def test_experiment_tool_echo_does_not_admit_earlier_source_change(self):
        evidence = packet()
        evidence['events'][0].update(kind='InvocationToolCallResult', payload={'result': 'experiment_only'})
        evidence['events'][1]['payload']['changed_paths_since_previous_event'] = ['scene/sourcecode/src/main/java/Probe.java']
        value = verdict()
        value['temporal']['earlier_source_changes'] = [{
            'change_seq': 2, 'experiment_admission_seq': 1, 'evidence': ['E0001'], 'explanation': 'Tool echo is not an admission.'}]
        with self.assertRaisesRegex(EvalError, 'experiment admission authorship'):
            validate_verdict(value, evidence)

    def test_projection_shared_text_preserves_message_channel_index(self):
        evidence = packet()
        text = 'repair_admission: allow_local_candidate; ' + ('supporting evidence ' * 30)
        evidence['events'][1]['payload']['text'] = text
        evidence['files'][0]['text'] = text
        projected = project_packet(evidence)
        self.assertIsInstance(projected['events'][1]['payload']['text'], dict)
        self.assertEqual(message_channel_sequences(projected), message_channel_sequences(evidence))
        self.assertIn('序号，不是已获准记录清单：[2]', build_prompt(projected))


if __name__ == '__main__':
    unittest.main()
