"""Native review contract tests: no Chrys process, credentials, or network."""

import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import EvalError, digest, json_bytes, read_json, write_json, write_new
from review import CRITERIA, build_prompt, output_contract, schema, validate_verdict
from review_chrys import assess_native, assess_output, collect_native, main, prepare_native, strict_json
from test_eval import packet, verdict


class NativeReviewTests(unittest.TestCase):
    def setUp(self):
        self.packet = packet()
        self.verdict = verdict()
        self.public = json.dumps(self.verdict)
        self.prompt = build_prompt(self.packet).encode()
        self.binding = {"requested_model_id": "glm-5.3", "profile_id": "synthetic-profile",
                        "agent_profile_id": "b0119a000005", "chrys_version": "0.28.0",
                        "workdir": "/synthetic/work", "endpoint_host": "example.invalid"}
        self.reply = {"result": self.public, "session_id": "synthetic-session", "duration": 1.0}
        self.process = {"status": "completed", "returncode": 0, "duration_seconds": 1.1}
        self.session = {"meta": {"session_id": "synthetic-session", "model_id": "glm-5.3",
                                 "model_profile_id": "synthetic-profile", "agent_profile_id": "b0119a000005",
                                 "app_version": "0.28.0", "primary_cwd": "/synthetic/work",
                                 "model_base_url": "https://example.invalid/v1"},
                        "state": {"compressed_msgs": [], "messages": [
                            {"role": "user", "contents": [{"type": "text", "text": self.prompt.decode()}]},
                            {"role": "assistant", "contents": [{"type": "text_reasoning", "text": "PRIVATE_THINKING"},
                                                                {"type": "text", "text": self.public}]},
                            {"role": "assistant", "additional_properties": {"_chrys_kind": "turn"},
                             "contents": [{"type": "text", "text": "turn complete"}]}]}}
        self.temp = tempfile.TemporaryDirectory(prefix="native-review-tests-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()

    def assess(self):
        return assess_native(json.dumps(self.reply).encode(), self.process, self.session,
                             self.packet, self.prompt, self.binding)

    def test_prompt_includes_schema_complete_scaffold_and_snapshot_semantics(self):
        prompt = self.prompt.decode()
        self.assertIn('"minItems": 5', prompt)
        self.assertIn('payload.changed_paths_since_previous_event', prompt)
        self.assertIn('不是开始调用编辑工具', prompt)
        contract = output_contract(schema())
        template = json.loads(contract.split('字段骨架（不是评审答案；替换占位符，按证据填写序号、引用和结论）：\n')[1].split('\n输出前自检：')[0])
        self.assertEqual([c['id'] for c in template['criteria']], CRITERIA)
        for item in template['criteria']:
            self.assertEqual(set(item), {'id', 'status', 'evidence', 'explanation'})
            self.assertNotEqual(item['status'], 'pass')

    def test_missing_fields_and_start_event_are_reported_together_without_repair(self):
        value = copy.deepcopy(self.verdict)
        for item in value['criteria']:
            del item['id']
            del item['status']
        value['temporal']['first_candidate_change_seq'] = 3
        raw = json.dumps(value)
        before = copy.deepcopy(value)
        result = assess_output(raw, self.packet)
        self.assertEqual(result['model_verdict'], 'pass')
        self.assertEqual(result['output_status'], 'invalid')
        self.assertEqual(sum(e['kind'] == 'missing_field' for e in result['errors']), 10)
        self.assertTrue(any(e['kind'] == 'temporal' for e in result['errors']))
        self.assertEqual(value, before)

    def test_valid_output_is_not_formal_acceptance_or_backend_identity_proof(self):
        result = self.assess()
        self.assertEqual(result['output_status'], 'valid')
        self.assertEqual(result['gaps'], [])
        self.assertEqual(result['model_verdict'], 'pass')
        self.assertEqual(result['formal_evaluation_status'], 'incomplete')
        self.assertIsNone(result['identity']['provider_reported_model_id'])
        self.assertTrue(result['call_record_gaps'])
        self.assertFalse(result['suite_grade_modified'])
        self.assertNotIn('PRIVATE_THINKING', json.dumps(result))

    def test_stop_and_fail_remain_distinct_from_format_errors(self):
        for status in ('fail', 'incomplete'):
            value = copy.deepcopy(self.verdict)
            value['criteria'][0]['status'] = value['verdict'] = status
            result = assess_output(json.dumps(value), self.packet)
            self.assertEqual(result['output_status'], 'valid')
            self.assertEqual(result['model_verdict'], status)

    def test_duplicate_keys_fences_nonfinite_and_nonobject_rejected(self):
        for raw in ('{"verdict":"pass","verdict":"fail"}', '{"v":NaN}', '{"v":Infinity}',
                    '```json\n{}\n```', 'null', '[]', '"pass"'):
            with self.subTest(raw=raw):
                self.assertEqual(assess_output(raw, self.packet)['output_status'], 'invalid')

    def test_field_types_unknown_fields_and_fake_references_rejected(self):
        changes = [lambda v: v.update(summary=[]), lambda v: v['criteria'].__setitem__(0, None),
                   lambda v: v['criteria'][0].update(id=3), lambda v: v['criteria'][0].update(status=[]),
                   lambda v: v['criteria'][0].update(explanation=True), lambda v: v.update(extra='x'),
                   lambda v: v['criteria'][0].update(evidence=['E9999']),
                   lambda v: v['temporal'].update(admission_seq=True),
                   lambda v: v['temporal'].update(earlier_source_changes=[{}]),
                   lambda v: v['criteria'][0].update(status='fail')]
        for change in changes:
            value = copy.deepcopy(self.verdict)
            change(value)
            with self.subTest(value=value):
                self.assertEqual(assess_output(json.dumps(value), self.packet)['output_status'], 'invalid')

    def test_native_identity_fields_do_not_use_model_self_claims(self):
        for key in ('model_id', 'model_profile_id', 'agent_profile_id', 'app_version', 'primary_cwd', 'session_id'):
            saved = self.session['meta'][key]
            self.session['meta'][key] = 'different'
            self.assertTrue(any(key in gap for gap in self.assess()['gaps']))
            self.session['meta'][key] = saved
        self.session['meta']['model_base_url'] = 'https://other.invalid/v1'
        self.assertTrue(any('endpoint' in gap for gap in self.assess()['gaps']))

    def test_tools_compaction_extra_context_and_incomplete_process_are_gaps(self):
        self.session['state']['messages'].append({'role': 'assistant', 'contents': [{'type': 'function_call', 'name': 'shell'}]})
        self.session['state']['compressed_msgs'] = ['compressed']
        self.session['state']['messages'][0]['contents'][0]['text'] += 'extra history'
        self.process.update(status='timeout', returncode=-9)
        result = self.assess()
        self.assertEqual(result['persisted_tool_item_count'], 1)
        self.assertEqual(result['persisted_compaction_count'], 1)
        self.assertTrue(any('frozen' in g for g in result['gaps']))
        self.assertTrue(any('process' in g for g in result['gaps']))

    def test_missing_session_or_stdout_does_not_become_a_zero_tool_claim(self):
        result = assess_native(b'{}', self.process, None, self.packet, self.prompt, self.binding)
        self.assertEqual(result['output_status'], 'invalid')
        self.assertIsNone(result['persisted_tool_item_count'])
        self.assertIsNone(result['persisted_compaction_count'])

    def test_public_answer_must_match_native_session(self):
        self.session['state']['messages'][1]['contents'][1]['text'] = '{}'
        self.assertTrue(any('final answer' in g for g in self.assess()['gaps']))

    def prepare(self, **kwargs):
        suite, directory = self.base / 'suite', self.base / 'native'
        write_json(suite / 'trials/trial-test/grade.json', {'status': 'awaiting_review'})
        mocks = [mock.patch('review_chrys.load_suite', return_value={'reviewer_model': 'glm-5.3'}),
                 mock.patch('review_chrys.select_trials', return_value=[{'id': 'trial-test'}]),
                 mock.patch('review_chrys.make_packet', return_value=self.packet)]
        for item in mocks:
            item.start()
            self.addCleanup(item.stop)
        prepare_native(suite, 'trial-test', directory, 'synthetic-profile', 'example.invalid', '0.28.0', **kwargs)
        return suite, directory

    def test_selected_icode_path_is_recorded_in_native_invocation(self):
        _, directory = self.prepare(cli='/selected/runtime/bin/icode')
        argv = read_json(directory / 'invocation.json')['argv']
        self.assertEqual(argv[:2], ['/selected/runtime/bin/icode', 'run'])
        self.assertEqual(read_json(directory / 'binding.json')['cli'], argv[0])
        self.assertIn('--json', argv)
        self.assertNotIn('--session', argv)

    def test_default_review_entry_is_icode_without_implicit_chrys_fallback(self):
        _, directory = self.prepare()
        self.assertEqual(read_json(directory / 'invocation.json')['argv'][0], 'icode')

    def test_prepare_does_not_call_model_or_modify_original_and_never_overwrites(self):
        suite, directory = self.prepare()
        self.assertFalse(read_json(directory / 'invocation.json')['models_called'])
        self.assertNotIn('--session', read_json(directory / 'invocation.json')['argv'])
        self.assertEqual(read_json(suite / 'trials/trial-test/grade.json'), {'status': 'awaiting_review'})
        with self.assertRaises(EvalError):
            prepare_native(suite, 'trial-test', directory, 'synthetic-profile', 'example.invalid', '0.28.0')
        with self.assertRaises(EvalError):
            prepare_native(suite, 'trial-test', suite / 'inside', 'synthetic-profile', 'example.invalid', '0.28.0')

    def test_collection_preserves_invalid_output_and_emits_no_hidden_reasoning(self):
        suite, directory = self.prepare()
        self.session['meta']['primary_cwd'] = str(directory / 'work')
        bad = copy.deepcopy(self.verdict)
        del bad['criteria'][0]['status']
        self.public = json.dumps(bad)
        self.reply['result'] = self.public
        self.session['state']['messages'][1]['contents'][1]['text'] = self.public
        raw = json.dumps(self.reply).encode()
        write_new(self.base / 'stdout', raw)
        write_json(self.base / 'process.json', self.process)
        write_json(self.base / 'session.json', self.session)
        result = collect_native(directory, self.base / 'stdout', self.base / 'process.json', self.base / 'session.json')
        self.assertEqual(result['output_status'], 'invalid')
        self.assertEqual((directory / 'collected/stdout.log').read_bytes(), raw)
        self.assertEqual((directory / 'collected/result.txt').read_text(), self.public)
        self.assertNotIn('PRIVATE_THINKING', (directory / 'collected/receipt.json').read_text())
        self.assertFalse((suite / 'trials/trial-test/review.json').exists())
        with self.assertRaises(EvalError):
            collect_native(directory, self.base / 'stdout', self.base / 'process.json', self.base / 'session.json')

    def test_drift_is_rejected_before_creating_collection(self):
        _, directory = self.prepare()
        (directory / 'input/task.txt').write_text('different task')
        with self.assertRaises(EvalError):
            collect_native(directory, 'unused', 'unused')
        self.assertFalse((directory / 'collected').exists())

    def test_collection_cli_incomplete_has_nonzero_exit(self):
        with mock.patch('review_chrys.collect_native', return_value={'formal_evaluation_status': 'incomplete'}), redirect_stdout(io.StringIO()):
            self.assertEqual(main(['collect', '--directory', 'unused', '--stdout', 'unused', '--process-file', 'unused']), 2)


if __name__ == '__main__':
    unittest.main()
