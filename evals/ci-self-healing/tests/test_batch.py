"""Whole-suite acceptance using fake backends; never paid-model evidence."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from batch import execute_batch, make_plan, new_run, run_lock, stage_paths, write_reports
from common import BudgetExhausted, EvalError, bind_json, digest, read_json, read_regular, run_logged, snapshot, wall_budget, write_json, write_new
from eval import main
from native_runner import exchange_receipt, policy, validate_native_artifacts
from suite import load_suite, REPO


def trace():
    values = [
        ('trajectory.coverage.started', {'coverage_reason': 'session_started'}),
        ('trajectory.runtime.started', {}),
        ('model.exchange.started', {'request_model': 'glm-5.3'}),
        ('model.exchange.finished', {'response_model': 'glm-5.3', 'response_id': 'response-test', 'outcome': 'success', 'finish_reason': 'stop'}),
        ('trajectory.coverage.ended', {'coverage_id': 'coverage-test', 'last_sequence': 4}),
        ('trajectory.runtime.finished', {'reason': 'graceful_shutdown'})]
    return [{'schema_version': 1, 'sequence': i, 'session_id': 'session-test', 'runtime_id': 'runtime-test',
             'coverage_id': 'coverage-test',
             'event_type': kind, 'payload': payload, 'operation_id': 'exchange-test',
             'actor': {'kind': 'agent', 'role': 'main'}} for i, (kind, payload) in enumerate(values, 1)]


def wire(rows):
    return ('\n'.join(json.dumps(r) for r in rows) + '\n').encode()


class BatchFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ci-batch-tests-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.candidate = self.base / 'skill'
        write_new(self.candidate / 'SKILL.md', '---\nname: fixture\n---\n# Fixture\n')
        self.config = {'output_root': str(self.base / 'runs'), 'runtime': str(self.base / 'runtime.json'),
                       'docker_context': 'isolated-test', 'reviewer': {'version': '0.29.0'},
                       'max_agent_invocations': 36, 'max_wall_seconds': 600, 'stop_after_transport_errors': 2}
        self.calls = []
        self.failed_cases = set()
        self.interrupt_case = None
        self.environment = {'internal_network': True}
        self.identity = {'version': 'synthetic'}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stderr(io.StringIO()))
        for target, value in [('batch.load_runtime', {}), ('batch.docker_probe', self.environment),
                              ('batch.native_preflight', self.identity), ('batch.native_identity', self.identity)]:
            self.stack.enter_context(mock.patch(target, return_value=value))
        self.stack.enter_context(mock.patch.dict(os.environ, {'CHRYS_EVAL_API_KEY': 'local-fixture-only'}))
        self.stack.enter_context(mock.patch('batch.run_trials', side_effect=self.fake_run))
        self.stack.enter_context(mock.patch('batch.grade', side_effect=self.grade))
        self.stack.enter_context(mock.patch('batch.native_review', side_effect=self.review))
        self.stack.enter_context(mock.patch('batch.validate_grade_binding'))
        self.stack.enter_context(mock.patch('grading.validate_grade_binding'))

    def row(self, root, tid):
        return next(r for r in load_suite(root)['trials'] if r['id'] == tid)

    def fake_run(self, root, runtime, tid):
        row = self.row(root, tid)
        self.calls.append(('run', row['case'], tid))
        if row['case'] == self.interrupt_case:
            raise KeyboardInterrupt()
        write_json(Path(root) / 'trials' / tid / 'run.json', {'status': 'completed', 'process': {'returncode': 0}})

    def grade(self, root, runtime, tid):
        row = self.row(root, tid)
        self.calls.append(('grade', row['case'], tid))
        write_json(Path(root) / 'trials' / tid / 'grade.json', {'status': 'failed' if row['case'] in self.failed_cases else 'awaiting_review',
                                                             'failures': [], 'gaps': [], 'bindings': {'synthetic': True}})

    def review(self, root, tid, directory, config, identity):
        row = self.row(root, tid)
        self.calls.append(('review', row['case'], tid))
        b = Path(root) / 'reviewer-binding.json'
        bind_json(b, {'synthetic': True}, 'synthetic reviewer drift')
        write_json(Path(root) / 'trials' / tid / 'review.json', {
            'status': 'validated', 'requested_model': 'glm-5.3', 'verdict': {'verdict': 'pass'},
            'process': {'returncode': 0}, 'reviewer_binding_sha256': digest(read_regular(b)),
            'grade_sha256': digest(read_regular(Path(root) / 'trials' / tid / 'grade.json'))})

    def create(self, preset='smoke', mode='single'):
        return new_run(self.config, make_plan(self.config, self.candidate, mode, preset))


class BatchTests(BatchFixture, unittest.TestCase):
    def test_whole_suite_runs_twice_with_fresh_ids_and_workspaces(self):
        first = self.create()
        a = execute_batch(first, self.config)
        second = self.create()
        b = execute_batch(second, self.config)
        self.assertEqual(a['counts']['candidate']['passed'], 3)
        self.assertEqual(b['exit_code'], 0)
        self.assertEqual(len(self.calls), 18)
        self.assertTrue({r['id'] for r in a['trials']}.isdisjoint(r['id'] for r in b['trials']))
        self.assertTrue((first / 'summary.md').is_file())
        self.assertNotEqual(first, second)

    def test_native_batch_never_requires_docker_or_inherited_gateway_token(self):
        self.config["backend"] = "native"
        self.config.pop("docker_context")
        with mock.patch("batch.load_runtime", return_value={"backend": "native"}), \
             mock.patch("batch.runtime_probe", return_value=self.environment), \
             mock.patch("batch.docker_probe", side_effect=AssertionError("native must not call Docker")), \
             mock.patch.dict(os.environ, {"CHRYS_EVAL_API_KEY": ""}):
            result = execute_batch(self.create(), self.config)
        self.assertEqual(result["counts"]["candidate"]["passed"], 3)

    def test_resume_does_not_repeat_completed_stages(self):
        root = self.create()
        execute_batch(root, self.config)
        calls = list(self.calls)
        with mock.patch('batch.docker_probe', side_effect=AssertionError('completed suite should not need environment')):
            self.assertEqual(execute_batch(root, self.config)['exit_code'], 0)
        self.assertEqual(self.calls, calls)

    def test_one_failed_case_does_not_skip_other_cases(self):
        self.failed_cases.add('case-001')
        result = execute_batch(self.create(), self.config)
        self.assertEqual(result['counts']['candidate']['failed'], 1)
        self.assertEqual(result['counts']['candidate']['passed'], 2)
        self.assertEqual(result['exit_code'], 1)
        self.assertFalse(any(stage == 'review' and case == 'case-001' for stage, case, tid in self.calls))

    def test_reviewable_timeout_runs_review_once_but_overall_remains_incomplete(self):
        from repair_support import DELIVERY_CONTRACT
        original = self.fake_run
        def stopped(root, runtime, tid):
            if self.row(root, tid)['case'] != 'case-003':
                return original(root, runtime, tid)
            self.calls.append(('run', 'case-003', tid))
            write_json(Path(root) / 'trials' / tid / 'after-run.json', snapshot(Path(root) / 'trials' / tid / 'visible'))
            write_json(Path(root) / 'trials' / tid / 'run.json', {
                'status': 'timeout', 'process': {'status': 'timeout', 'returncode': 130, 'cleanup': {'status': 'confirmed'}},
                'isolation_confirmed': True, 'filesystem_status': 'captured',
                'delivery_contract': DELIVERY_CONTRACT,
                'delivery_assessment': {'status': 'reviewable_stop_artifact'}})
        root = self.create()
        with mock.patch('batch.run_trials', side_effect=stopped):
            result = execute_batch(root, self.config)
        self.assertEqual(result['reserved_agent_invocations'], 6)
        row = next(r for r in result['trials'] if r['case'] == 'case-003')
        self.assertEqual(row['stages']['review'], 'completed')
        self.assertEqual(row['review_status'], 'pass')
        self.assertEqual(row['execution_status'], 'timeout')
        self.assertEqual(row['status'], 'incomplete')
        count = len(self.calls)
        self.assertEqual(execute_batch(root, self.config)['exit_code'], 2)
        self.assertEqual(len(self.calls), count)

    def test_interrupted_attempt_is_not_replayed_but_pending_cases_continue(self):
        root = self.create()
        self.interrupt_case = load_suite(root / 'suite')['trials'][1]['case']
        a = execute_batch(root, self.config)
        self.assertEqual(a['exit_code'], 2)
        self.interrupt_case = None
        b = execute_batch(root, self.config)
        self.assertEqual(b['counts']['candidate']['passed'], 2)
        self.assertEqual(b['counts']['candidate']['incomplete'], 1)
        attempts = [tid for stage, case, tid in self.calls if stage == 'run']
        self.assertEqual(len(attempts), len(set(attempts)))

    def test_completed_run_resumes_at_grading_without_model_repair_repeat(self):
        root = self.create()
        tid = load_suite(root / 'suite')['trials'][0]['id']
        self.fake_run(root / 'suite', None, tid)
        start, done = stage_paths(root, tid, 'run')
        write_json(start, {'stage': 'run'})
        write_json(done, {'status': 'completed', 'artifact_sha256': digest(read_regular(root / 'suite/trials' / tid / 'run.json'))})
        self.assertEqual(execute_batch(root, self.config)['exit_code'], 0)
        self.assertEqual(sum(stage == 'run' and t == tid for stage, case, t in self.calls), 1)

    def test_budget_cap_prevents_extra_agent_launches(self):
        self.config['max_agent_invocations'] = 2
        result = execute_batch(self.create(), self.config)
        self.assertEqual(result['reserved_agent_invocations'], 2)
        self.assertEqual(sum(s in ('run', 'review') for s, c, t in self.calls), 2)
        self.assertEqual(result['exit_code'], 2)
        self.assertIn('budget', result['batch_stop_reason'])

    def test_dead_coordinator_uncertainty_stops_without_spending(self):
        root = self.create()
        write_json(root / 'segments/unknown.started.json', {})
        result = execute_batch(root, self.config)
        self.assertEqual(self.calls, [])
        self.assertIn('uncertain', result['batch_stop_reason'])

    def test_drift_and_concurrent_runner_are_rejected(self):
        root = self.create()
        with run_lock(root), self.assertRaises(EvalError):
            execute_batch(root, self.config)
        changed = {**self.config, 'max_agent_invocations': 40}
        with self.assertRaises(EvalError):
            execute_batch(root, changed)
        execute_batch(root, self.config)
        tid = load_suite(root / 'suite')['trials'][0]['id']
        (root / 'suite/trials' / tid / 'run.json').write_text('{}')
        result = execute_batch(root, self.config)
        self.assertEqual(result['exit_code'], 2)
        self.assertIn('changed', result['batch_stop_reason'])

    def test_single_version_and_compare_presets(self):
        single = self.create('regression')
        self.assertEqual(len(load_suite(single / 'suite')['trials']), 9)
        # Coordinator behavior must not depend on the surrounding checkout's
        # HEAD (the same tests also run from an immutable source snapshot).
        baseline = {'SKILL.md': b'---\nname: fixture\n---\n# Synthetic baseline\n'}
        with mock.patch('suite.skill_from_ref', return_value=(baseline, 'a' * 40)):
            compare = self.create('smoke', 'compare')
        self.assertEqual(len(load_suite(compare / 'suite')['trials']), 6)
        result = execute_batch(compare, self.config)
        self.assertEqual(result['comparison']['passed'], 0)
        self.assertEqual(set(result['counts']), {'before', 'after'})

    def test_case_extension_does_not_change_coordinator(self):
        from cases import CASES
        extra = {**CASES['case-003'], 'contract': 'Synthetic additional stop case'}
        with mock.patch.dict(CASES, {'case-004': extra}):
            result = execute_batch(self.create(), self.config)
            self.assertEqual(result['counts']['candidate']['passed'], 4)

    def test_deadline_kills_subprocess_but_not_evidence(self):
        with wall_budget(.08):
            result = run_logged([sys.executable, '-c', 'import time; time.sleep(10)'], self.base / 'limited', 20)
            self.assertEqual(result['status'], 'timeout')
            with self.assertRaises(BudgetExhausted):
                run_logged([sys.executable, '-c', 'print(1)'], self.base / 'never', 1)
        self.assertFalse((self.base / 'never').exists())

    def test_native_complete_exchange_is_not_permanent_incomplete(self):
        r = exchange_receipt(wire(trace()), 'session-test', 'glm-5.3')
        self.assertEqual(r['status'], 'validated')
        self.assertEqual(r['response_model'], 'glm-5.3')

    def test_native_trajectory_fail_closed(self):
        changes = [lambda r: r.pop(), lambda r: r[2]['payload'].update(request_model='other'),
                   lambda r: r[3]['payload'].update(response_model='other'),
                   lambda r: r[3]['payload'].update(finish_reason='length'),
                   lambda r: r[3]['payload'].update(outcome='error'),
                   lambda r: r[2].update(sequence=20), lambda r: r[2].update(session_id='other'),
                   lambda r: r[1].update(event_type='trajectory.gap'),
                   lambda r: r[1].update(event_type='tool.operation.started'),
                   lambda r: r[1].update(event_type='compaction.started'),
                   lambda r: r[0].update(sequence=True), lambda r: r[0].update(schema_version=True),
                   lambda r: r[2].update(actor=None), lambda r: r[1].update(coverage_id='other'),
                   lambda r: r[4]['payload'].update(last_sequence=1),
                   lambda r: r[2]['actor'].update(role='title_gen')]
        for change in changes:
            rows = trace(); change(rows)
            with self.subTest(rows=rows), self.assertRaises(EvalError):
                exchange_receipt(wire(rows), 'session-test', 'glm-5.3')

    def test_native_artifact_drift_rejected(self):
        for i in range(6):
            write_new(self.base / ('reply' + str(i)), 'original')
        receipt = {'native_directory': str(self.base), 'native_artifacts': {'reply' + str(i): digest(b'original') for i in range(6)}}
        validate_native_artifacts(receipt)
        (self.base / 'reply0').write_text('changed')
        with self.assertRaises(EvalError):
            validate_native_artifacts(receipt)

    def test_native_policy_allows_exact_parent_traversal_not_subtree_reads(self):
        config = {'chrys': '/isolated/runtime/bin/chrys', 'python': '/isolated/runtime/bin/python',
                  'profile': '/selected/model.yaml'}
        directory = self.base / 'batch/review'
        with mock.patch('native_runner.platform.system', return_value='Darwin'):
            text = policy(config, directory, False)
        for parent in directory.parents:
            self.assertIn('(literal ' + json.dumps(str(parent)) + ')', text)
        self.assertNotIn('(subpath ' + json.dumps(str(self.base)) + ')', text)
        self.assertNotIn('remote tcp', text)

    def test_cli_plan_has_no_model_or_docker_calls(self):
        cfg = read_json(REPO / 'evals/ci-self-healing/batch.example.json')
        write_json(self.base / 'config.json', cfg)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['test', '--config', str(self.base / 'config.json')]), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan['trial_count'], 3)
        self.assertFalse(plan['models_called'])
        self.assertEqual(self.calls, [])

    def test_environment_failure_makes_no_model_calls(self):
        with mock.patch('batch.docker_probe', side_effect=EvalError('Docker unavailable')):
            result = execute_batch(self.create(), self.config)
        self.assertEqual(self.calls, [])
        self.assertEqual(result['counts']['candidate']['not_run'], 3)
        self.assertEqual(result['exit_code'], 2)

    def test_unknown_error_stops_without_claiming_shared_fault_or_retrying(self):
        root = self.create()
        with mock.patch('batch.run_trials', side_effect=EvalError('transport failed')) as runner:
            result = execute_batch(root, self.config)
            execute_batch(root, self.config)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(result['counts']['candidate']['incomplete'], 1)
        self.assertEqual(result['counts']['candidate']['not_run'], 2)
        self.assertIn('unknown', result['batch_stop_reason'])
        self.assertNotIn('shared-environment', result['batch_stop_reason'])

    def timed_out_run(self, root, runtime, tid):
        row = self.row(root, tid)
        self.calls.append(('run', row['case'], tid))
        trial = Path(root) / 'trials' / tid
        write_json(trial / 'after-run.json', snapshot(trial / 'visible'))
        write_json(trial / 'run.json', {'status': 'timeout', 'filesystem_status': 'captured', 'isolation_confirmed': True,
                                       'process': {'status': 'timeout', 'returncode': -9, 'cleanup': {'status': 'confirmed'}}})

    def test_single_timeout_continues_whole_suite_and_resume_never_replays(self):
        self.config['stop_after_transport_errors'] = 1  # Legacy threshold must not stop an isolated timeout.
        root = self.create()
        first = load_suite(root / 'suite')['trials'][0]['id']
        def run(suite, runtime, tid):
            return self.timed_out_run(suite, runtime, tid) if tid == first else self.fake_run(suite, runtime, tid)
        with mock.patch('batch.run_trials', side_effect=run):
            result = execute_batch(root, self.config)
            before = list(self.calls)
            resumed = execute_batch(root, self.config)
        self.assertEqual(self.calls, before)
        self.assertEqual(result['counts']['candidate'], {'passed': 2, 'failed': 0, 'incomplete': 1, 'not_run': 0})
        self.assertEqual(resumed['exit_code'], 2)
        self.assertFalse(any(s in ('grade', 'review') and t == first for s, c, t in self.calls))
        failed = next(r for r in result['trials'] if r['id'] == first)
        self.assertEqual(failed['execution_failures'][0]['code'], 'trial_timeout')

    def test_shared_auth_failure_stops_even_with_large_legacy_threshold(self):
        self.config['stop_after_transport_errors'] = 10
        def run(root, runtime, tid):
            self.timed_out_run(root, runtime, tid)
            path = Path(root) / 'trials' / tid / 'run.json'
            d = read_json(path)
            d['runtime_diagnostics'] = {'gateway_statuses': [401]}
            path.write_text(json.dumps(d))
        with mock.patch('batch.run_trials', side_effect=run):
            result = execute_batch(self.create(), self.config)
        self.assertEqual(len(self.calls), 1)
        self.assertIn('provider_auth_rejected', result['batch_stop_reason'])

    def test_review_timeout_keeps_repair_evidence_and_continues(self):
        root = self.create()
        first = load_suite(root / 'suite')['trials'][0]['id']
        def review(suite, tid, directory, config, identity):
            self.review(suite, tid, directory, config, identity)
            if tid == first:
                path = Path(suite) / 'trials' / tid / 'review.json'
                d = read_json(path)
                d.update(status='incomplete', isolation_confirmed=True,
                         process={'status': 'timeout', 'returncode': -9, 'cleanup': {'status': 'confirmed'}})
                path.write_text(json.dumps(d))
        with mock.patch('batch.native_review', side_effect=review):
            result = execute_batch(root, self.config)
        self.assertEqual(result['counts']['candidate']['passed'], 2)
        self.assertEqual(result['counts']['candidate']['incomplete'], 1)
        self.assertEqual(read_json(root / 'suite/trials' / first / 'grade.json')['status'], 'awaiting_review')

    def test_real_stream_cancel_flows_through_batch_and_is_not_replayed(self):
        from gateway_fixture import cancelled_stream
        root = self.create()
        first = load_suite(root / 'suite')['trials'][0]['id']
        def run(suite, runtime, tid):
            if tid != first:
                return self.fake_run(suite, runtime, tid)
            self.calls.append(('run', self.row(suite, tid)['case'], tid))
            trial = Path(suite) / 'trials' / tid
            data = cancelled_stream(trial / 'loopback', tid, workspace=trial / 'visible')
            write_json(trial / 'after-run.json', snapshot(trial / 'visible'))
            write_json(trial / 'run.json', data)
        with mock.patch('batch.run_trials', side_effect=run):
            result = execute_batch(root, self.config)
            calls = list(self.calls)
            resumed = execute_batch(root, self.config)
        self.assertEqual(result['counts']['candidate'], {'passed': 2, 'failed': 0, 'incomplete': 1, 'not_run': 0})
        self.assertIsNone(result['batch_stop_reason'])
        self.assertEqual(resumed['counts'], result['counts'])
        self.assertEqual(calls, self.calls)
        self.assertEqual(sum(s == 'run' and tid == first for s, _, tid in self.calls), 1)
        self.assertFalse(any(s in ('grade', 'review') and tid == first for s, _, tid in self.calls))

    def test_wrong_repair_model_detected_by_grade_stops_before_review_or_next_case(self):
        def grade(root, runtime, tid):
            self.grade(root, runtime, tid)
            path = Path(root) / 'trials' / tid / 'grade.json'
            d = read_json(path)
            d.update(status='incomplete', gaps=['model_or_session_identity_unconfirmed'])
            path.write_text(json.dumps(d))
        with mock.patch('batch.grade', side_effect=grade):
            result = execute_batch(self.create(), self.config)
        self.assertEqual([s for s, c, t in self.calls], ['run', 'grade'])
        self.assertIn('runtime_identity_unconfirmed', result['batch_stop_reason'])

    def test_resume_carries_wall_budget(self):
        root = self.create()
        write_json(root / 'segments/old.started.json', {})
        write_json(root / 'segments/old.finished.json', {'elapsed_seconds': 601})
        result = execute_batch(root, self.config)
        self.assertEqual(self.calls, [])
        self.assertIn('wall-clock budget exhausted', result['batch_stop_reason'])

    def test_report_respects_access_frontmatter(self):
        root = self.create()
        write_new(root / 'summary.md', '---\nai_access: false\n---\nprivate\n')
        with self.assertRaises(EvalError):
            write_reports(root)


if __name__ == '__main__':
    unittest.main()
