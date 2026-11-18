"""Gate orchestration and tamper/negative cases; synthetic audits are not semantic proof."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import calibration, calibration_gate as gate, cli, grading, runner
from harness.core import EvaluationError, read_json, save_json
from test_calibration_runner_v2 import FakeJudge


def supported_audit(source):
    audit = gate.audit_template(source)
    for row in audit['checks']:
        row.update(coverage_complete=True, claims=[{
            'quote': 'Synthetic adapter checks orchestration only.', 'assessment': 'supported',
            'evidence_ids': ['task'], 'reason': 'Synthetic audit tests gate combination only; no semantic accuracy claim.'}])
        if 'attributions' in row:
            row['attributions'] = []
    return audit


class CalibrationGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'calibration'
        self.adapter = FakeJudge(calibration.HOLDOUT)
        self.summary = runner.calibrate(self.root, {}, suite=calibration.HOLDOUT, adapter=self.adapter)
        self.source = gate.load_source(self.root)
        self.audit = supported_audit(self.source)

    def test_automatic_match_never_releases_without_rationale(self):
        self.assertEqual(self.summary['automatic_status'], 'passed')
        self.assertEqual(self.summary['status'], 'pending_rationale')
        self.assertFalse(self.summary['phase2_permitted'])
        result = gate.evaluate(self.source)
        self.assertEqual(result['counts'], {'passed': 0, 'failed': 0, 'incomplete': 6})
        self.assertFalse(result['phase2_permitted'])
        self.assertEqual(len(self.adapter.calls), 6)

    def test_all_three_conditions_allow_release_without_launching_any_phase(self):
        result = gate.evaluate(self.source, self.audit)
        self.assertEqual(result['status'], 'passed')
        self.assertFalse(result['phase2_permitted'])  # synthetic reviews cannot release a live phase
        fixture = copy.deepcopy(self.source)
        for row in fixture['records']:
            row['confirmed_live_review'] = True
        self.assertTrue(gate.evaluate(fixture, self.audit)['phase2_permitted'])
        # A prior suite uses its original audit format and is not a release source.
        fixture['suite'] = calibration.PREVIOUS_HOLDOUT
        historical = copy.deepcopy(self.audit)
        historical['schema_version'] = gate.AUDIT_SCHEMA
        for row in historical['checks']:
            row.pop('attributions', None)
        self.assertFalse(gate.evaluate(fixture, historical)['phase2_permitted'])
        self.assertFalse(result['models_called'])
        self.assertEqual(result['model_sessions_added'], 0)

    def test_correct_label_and_supported_core_do_not_excuse_extra_unsupported_claim(self):
        row = self.audit['checks'][1]
        row['claims'].append({**row['claims'][0], 'assessment': 'unsupported',
                              'reason': 'An unsupported material assertion blocks release independently of the label.'})
        result = gate.evaluate(self.source, self.audit)
        self.assertEqual(result['samples'][1]['automatic_status'], 'passed')
        self.assertEqual(result['samples'][1]['rationale_status'], 'failed')
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['phase2_permitted'])

    def test_matching_label_stays_pending_until_result_reason_is_audited(self):
        row = next(r for r in self.audit['checks'] if r['criterion_id'] == 'command.result-check')
        row['attributions'] = None
        decision = gate.evaluate(self.source, self.audit)
        assessed = next(r for r in decision['samples'] if r['id'] == row['sample_id'])
        self.assertEqual((assessed['automatic_status'], assessed['rationale_status']), ('passed', 'incomplete'))
        self.assertFalse(decision['phase2_permitted'])
        row['attributions'] = [{'claim_quote':row['claims'][0]['quote'], 'agent_statement':None}]
        with self.assertRaisesRegex(EvaluationError, 'needs a quoted Agent statement'):
            gate.evaluate(self.source, self.audit)

    def test_unknown_incomplete_or_empty_audits_cannot_pass(self):
        for kind in ('unknown', 'incomplete_coverage', 'empty'):
            audit = copy.deepcopy(self.audit)
            row = audit['checks'][0]
            if kind == 'unknown': row['claims'][0]['assessment'] = 'unknown'
            if kind == 'incomplete_coverage': row['coverage_complete'] = False
            if kind == 'empty': row['claims'] = []
            with self.subTest(kind=kind):
                result = gate.evaluate(self.source, audit)
                self.assertFalse(result['phase2_permitted'])
                self.assertEqual(result['samples'][0]['rationale_status'], 'incomplete')
                self.assertEqual(result['samples'][0]['rationale_checks'][0]['requires_user_adjudication'], kind == 'unknown')

    def test_audit_must_bind_exact_source_claims_evidence_and_selected_criteria(self):
        changes = [lambda a: a.update(source_sha256='stale'), lambda a: a.update(phase2_permitted=True),
            lambda a: a['checks'][0].update(binding_sha256='other-answer'),
            lambda a: a['checks'][0].update(criterion_id='not-selected'),
            lambda a: a['checks'][0]['claims'][0].update(quote='not in original answer'),
            lambda a: a['checks'][0]['claims'][0].update(evidence_ids=['trusted_task']),
            lambda a: a['checks'][0]['claims'][0].update(reason=' '),
            lambda a: a['checks'][0]['claims'][0].update(assessment='approved'),
            lambda a: a['checks'][0].update(coverage_complete='true'),
            lambda a: a['checks'].pop(), lambda a: a['checks'].__setitem__(1, copy.deepcopy(a['checks'][0]))]
        for change in changes:
            audit = copy.deepcopy(self.audit)
            change(audit)
            with self.subTest(change=change), self.assertRaises(EvaluationError):
                gate.evaluate(self.source, audit)

    def test_no_partial_subset_release(self):
        source = copy.deepcopy(self.source)
        source['records'].pop()
        with self.assertRaises(EvaluationError):
            gate.evaluate(source)

    def test_protocol_or_label_failure_cannot_be_overridden_by_support_audit(self):
        # Exact-source binding was tested separately. These copies exercise the
        # conjunctive gate with explicitly supplied synthetic automatic results.
        for automatic in ('incomplete', 'failed'):
            source = copy.deepcopy(self.source)
            source['records'][0]['automatic_status'] = automatic
            source['records'][0]['protocol_ok'] = automatic != 'incomplete'
            result = gate.evaluate(source, self.audit)
            self.assertFalse(result['phase2_permitted'])
            self.assertNotEqual(result['samples'][0]['status'], 'passed')

    def test_source_receipt_and_label_tampering_is_rejected(self):
        for name in ('reviews/c001.json', 'labels.json', 'summary.json', 'input-seal.json'):
            path = self.root / name
            before = path.read_bytes()
            path.write_bytes(before + b' ')
            with self.subTest(name=name), self.assertRaises(EvaluationError):
                gate.load_source(self.root)
            path.write_bytes(before)

    def test_assessment_preserves_original_and_seals_new_record(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        audit_path = self.root.parent / 'audit.json'
        save_json(audit_path, self.audit)
        output = self.root.parent / 'gate'
        with patch('harness.native.judge', side_effect=AssertionError('no model allowed')):
            result = gate.assess(self.root, output, audit_path=audit_path)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(gate.read_gate(output), result)
        self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertIn('rationale-audit.json', read_json(output / 'seal.json')['files'])
        for destination in (output, self.root / 'gate'):
            with self.assertRaises(EvaluationError):
                gate.assess(self.root, destination, audit_path=audit_path)

    def test_offline_cli_requires_no_runtime_and_cannot_hide_pending_status(self):
        for with_audit, expected_code in ((False, 2), (True, 0)):
            output = self.root.parent / f'gate-{with_audit}'
            args = ['calibration-gate', '--run', str(self.root), '--output', str(output)]
            if with_audit:
                path = self.root.parent / 'audit.json'
                save_json(path, self.audit)
                args += ['--audit', str(path)]
            stream = io.StringIO()
            with patch.object(cli, '_runtime', side_effect=AssertionError('runtime accessed')), contextlib.redirect_stdout(stream):
                code = cli.main(args)
            self.assertEqual(code, expected_code)
            self.assertFalse(json.loads(stream.getvalue())['phase2_permitted'])

    def test_prompt_disciplines_rationale_without_exposing_labels(self):
        sample = next(iter(self.adapter.samples.values()))
        criteria = runner.Catalog().for_case(sample['case'], 'semantic')
        prompt = grading.review_prompt(sample['case'], sample['packet'], criteria)
        self.assertIn('标签正确也不能抵消无据的关键理由', prompt)
        payload = json.loads(prompt.split('\n', 1)[1])
        self.assertNotIn('expected', payload)
        self.assertNotIn('label_basis', prompt)
        self.assertNotIn('claims', payload)

    def test_saved_gate_cannot_be_reused_after_source_or_artifact_change(self):
        audit_path = self.root.parent / 'audit.json'
        save_json(audit_path, self.audit)
        output = self.root.parent / 'gate'
        gate.assess(self.root, output, audit_path=audit_path)
        for path in (self.root / 'reviews/c001.json', output / 'rationale-audit.json', output / 'gate.json'):
            before = path.read_bytes()
            path.write_bytes(before + b' ')
            with self.subTest(path=path), self.assertRaises(EvaluationError):
                gate.read_gate(output)
            path.write_bytes(before)
        with patch.object(runner, 'implementation_identity', return_value={}):
            with self.assertRaisesRegex(EvaluationError, 'implementation changed'):
                gate.read_gate(output)
        self.assertFalse(gate.read_gate(output)['phase2_permitted'])

    def test_gate_report_cli_verifies_and_returns_blocked_decision(self):
        output = self.root.parent / 'gate'
        gate.assess(self.root, output)
        stream = io.StringIO()
        with patch.object(cli, '_runtime', side_effect=AssertionError('runtime accessed')), contextlib.redirect_stdout(stream):
            code = cli.main(['calibration-gate-report', '--gate', str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(stream.getvalue())['phase2_permitted'])


if __name__ == '__main__':
    unittest.main()
