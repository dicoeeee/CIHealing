"""Concurrent fake stages and real local process cleanup; no provider/model calls."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from batch import execute_batch, load_config, make_plan, stage_paths
from common import (BudgetExhausted, EvalError, bind_json, read_json, remaining_timeout,
                    run_logged, snapshot, wall_budget, write_json)
from suite import load_suite
from test_batch import BatchFixture


class ParallelBatchTests(BatchFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config['max_parallel_trials'] = 3

    def test_three_distinct_trials_overlap_but_each_trial_keeps_stage_order(self):
        barriers = {s: threading.Barrier(3) for s in ('run', 'grade', 'review')}
        lock = threading.Lock()
        active, peak, observed = set(), {}, []

        def wrap(stage, operation):
            def invoke(*args):
                tid = args[1] if stage == 'review' else args[2]
                with lock:
                    self.assertNotIn(tid, active)
                    active.add(tid)
                    peak[stage] = max(peak.get(stage, 0), len(active))
                    observed.append((tid, stage))
                try:
                    barriers[stage].wait(timeout=5)
                    return operation(*args)
                finally:
                    with lock:
                        active.remove(tid)
            return invoke

        import batch
        original_report = batch.write_reports
        def stable_report(*args):
            self.assertFalse(active, 'summary must never read an in-flight artifact')
            return original_report(*args)

        root = self.create()
        with mock.patch('batch.run_trials', side_effect=wrap('run', self.fake_run)), \
             mock.patch('batch.grade', side_effect=wrap('grade', self.grade)), \
             mock.patch('batch.native_review', side_effect=wrap('review', self.review)), \
             mock.patch('batch.write_reports', side_effect=stable_report):
            result = execute_batch(root, self.config)
        self.assertEqual(result['counts']['candidate']['passed'], 3, result.get('batch_stop_reason'))
        self.assertEqual(peak, {'run': 3, 'grade': 3, 'review': 3})
        for row in result['trials']:
            self.assertEqual([s for tid, s in observed if tid == row['id']], ['run', 'grade', 'review'])
        starts = [read_json(p) for p in (root / 'journal').glob('*/*.started.json')]
        self.assertEqual(sorted(r['agent_invocation_ordinal'] for r in starts if 'agent_invocation_ordinal' in r), list(range(1, 7)))
        self.assertEqual({r['max_parallel_trials'] for r in starts}, {3})
        self.assertEqual(result['reserved_agent_invocations'], 6)
        before = list(self.calls)
        self.assertEqual(execute_batch(root, self.config)['exit_code'], 0)
        self.assertEqual(before, self.calls)

    def test_global_budget_smaller_than_parallel_width_is_not_overspent(self):
        self.config['max_agent_invocations'] = 2
        barrier = threading.Barrier(2)
        def run(*args):
            barrier.wait(timeout=5)
            return self.fake_run(*args)
        root = self.create()
        with mock.patch('batch.run_trials', side_effect=run):
            result = execute_batch(root, self.config)
        self.assertEqual(result['reserved_agent_invocations'], 2)
        self.assertEqual(sum(s == 'run' for s, _, _ in self.calls), 2)
        self.assertEqual(sum(s == 'grade' for s, _, _ in self.calls), 2)
        self.assertFalse(any(s == 'review' for s, _, _ in self.calls))
        self.assertEqual(result['counts']['candidate']['not_run'], 1)
        calls = list(self.calls)
        self.assertIn('budget', execute_batch(root, self.config)['batch_stop_reason'])
        self.assertEqual(calls, self.calls)

    def test_uncertain_reservation_counts_on_resume_and_is_never_replayed(self):
        self.config['max_agent_invocations'] = 5
        root = self.create()
        tid = load_suite(root / 'suite')['trials'][0]['id']
        write_json(stage_paths(root, tid, 'run')[0], {'stage': 'run', 'trial_id': tid})
        result = execute_batch(root, self.config)
        self.assertEqual(result['reserved_agent_invocations'], 5)
        self.assertEqual(result['counts']['candidate']['passed'], 2)
        self.assertFalse(any(t == tid for _, _, t in self.calls))
        before = list(self.calls)
        execute_batch(root, self.config)
        self.assertEqual(before, self.calls)

    def test_isolated_timeout_does_not_cancel_other_cases_or_get_replayed(self):
        root = self.create()
        first = load_suite(root / 'suite')['trials'][0]['id']
        barrier = threading.Barrier(3)
        def run(suite, runtime, tid):
            barrier.wait(timeout=5)
            if tid != first:
                return self.fake_run(suite, runtime, tid)
            self.calls.append(('run', self.row(suite, tid)['case'], tid))
            trial = Path(suite) / 'trials' / tid
            write_json(trial / 'after-run.json', snapshot(trial / 'visible'))
            write_json(trial / 'run.json', {'status': 'timeout', 'filesystem_status': 'captured', 'isolation_confirmed': True,
                'process': {'status': 'timeout', 'returncode': -9, 'cleanup': {'status': 'confirmed'}}})
        with mock.patch('batch.run_trials', side_effect=run):
            result = execute_batch(root, self.config)
        self.assertEqual(result['counts']['candidate'], {'passed': 2, 'failed': 0, 'incomplete': 1, 'not_run': 0})
        self.assertEqual(result['reserved_agent_invocations'], 5)
        self.assertIsNone(result['batch_stop_reason'])
        before = list(self.calls)
        execute_batch(root, self.config)
        self.assertEqual(before, self.calls)

    def test_shared_auth_failure_cancels_live_peers_and_keeps_process_receipts(self):
        root = self.create()
        ids = [r['id'] for r in load_suite(root / 'suite')['trials']]
        barrier = threading.Barrier(3)
        def run(suite, runtime, tid):
            self.calls.append(('run', self.row(suite, tid)['case'], tid))
            trial = Path(suite) / 'trials' / tid
            barrier.wait(timeout=5)
            if tid == ids[0]:
                deadline = time.monotonic() + 5
                while not all((Path(suite) / 'trials' / p / 'local-process/stdout.log').exists()
                              and b'ready' in (Path(suite) / 'trials' / p / 'local-process/stdout.log').read_bytes() for p in ids[1:]):
                    if time.monotonic() > deadline:
                        raise AssertionError('peer processes did not start')
                    time.sleep(.01)
                write_json(trial / 'run.json', {'status': 'completed', 'process': {'returncode': 1},
                                               'runtime_diagnostics': {'gateway_statuses': [401]}})
            else:
                run_logged([sys.executable, '-u', '-c', "import time; print('ready'); time.sleep(20)"],
                           trial / 'local-process', 20)
                raise AssertionError('peer should have been cancelled')
        with mock.patch('batch.run_trials', side_effect=run):
            result = execute_batch(root, self.config)
        self.assertIn('provider_auth_rejected', result['batch_stop_reason'])
        self.assertEqual(result['reserved_agent_invocations'], 3)
        self.assertFalse(any(s != 'run' for s, _, _ in self.calls))
        for tid in ids[1:]:
            trial = root / 'suite/trials' / tid
            process = read_json(trial / 'local-process/process.json')
            self.assertEqual(process['status'], 'interrupted')
            self.assertEqual(process['termination']['reason'], 'coordinator')
            self.assertEqual(process['cleanup']['status'], 'confirmed')
            stage = read_json(stage_paths(root, tid, 'run')[1])
            self.assertEqual(stage['failure']['code'], 'batch_cancelled')
            self.assertEqual(stage['failure']['scope'], 'coordinator')
        before = list(self.calls)
        execute_batch(root, self.config)
        self.assertEqual(before, self.calls)

    def test_operator_interrupt_drains_all_started_workers_before_segment_finishes(self):
        import batch
        root = self.create()
        ids = [r['id'] for r in load_suite(root / 'suite')['trials']]
        barrier = threading.Barrier(3)
        def run(suite, runtime, tid):
            self.calls.append(('run', self.row(suite, tid)['case'], tid))
            barrier.wait(timeout=5)
            run_logged([sys.executable, '-u', '-c', "import time; print('ready'); time.sleep(20)"],
                       Path(suite) / 'trials' / tid / 'local-process', 20)
        original_wait = batch.wait
        interrupted = []
        def interrupt_once(*args, **kwargs):
            if not interrupted:
                deadline = time.monotonic() + 5
                while not all((root / 'suite/trials' / tid / 'local-process/stdout.log').exists()
                              and b'ready' in (root / 'suite/trials' / tid / 'local-process/stdout.log').read_bytes() for tid in ids):
                    if time.monotonic() > deadline:
                        raise AssertionError('workers did not reach running state')
                    time.sleep(.01)
                interrupted.append(True)
                raise KeyboardInterrupt()
            return original_wait(*args, **kwargs)
        with mock.patch('batch.run_trials', side_effect=run), mock.patch('batch.wait', side_effect=interrupt_once):
            result = execute_batch(root, self.config)
        self.assertIn('operator interrupted', result['batch_stop_reason'])
        self.assertEqual(result['reserved_agent_invocations'], 3)
        for tid in ids:
            self.assertEqual(read_json(root / 'suite/trials' / tid / 'local-process/process.json')['cleanup']['status'], 'confirmed')
            self.assertEqual(read_json(stage_paths(root, tid, 'run')[1])['status'], 'interrupted')
        self.assertEqual(len(list((root / 'segments').glob('*.finished.json'))), 1)
        before = list(self.calls)
        execute_batch(root, self.config)
        self.assertEqual(before, self.calls)

    def test_final_binding_checks_after_review_failure_keep_the_original_stop_cause(self):
        root = self.create()
        first = load_suite(root / 'suite')['trials'][0]['id']
        barrier = threading.Barrier(3)
        def review(suite, tid, directory, config, identity):
            barrier.wait(timeout=5)
            self.review(suite, tid, directory, config, identity)
            if tid == first:
                path = Path(suite) / 'trials' / tid / 'review.json'
                data = read_json(path)
                data.update(process={'returncode': 1}, runtime_diagnostics={'gateway_statuses': [401]})
                path.write_text(json.dumps(data))
        # Real grade binding checks run read-only Git commands which consult
        # this execution context; a cancelled reporting scope used to hide the
        # shared-service fault behind an apparent operator interruption.
        with mock.patch('batch.native_review', side_effect=review), \
             mock.patch('grading.validate_grade_binding', side_effect=lambda *a: remaining_timeout(1)):
            result = execute_batch(root, self.config)
        self.assertIn('provider_auth_rejected', result['batch_stop_reason'])
        self.assertNotIn('operator', result['batch_stop_reason'])
        self.assertEqual(result['reserved_agent_invocations'], 6)
        self.assertTrue(all(r['evidence_invalidation'] is None for r in result['trials']))

    def test_environment_drift_between_waves_prevents_grade_or_review(self):
        barrier = threading.Barrier(3)
        root = self.create()
        def run(*args):
            barrier.wait(timeout=5)
            self.fake_run(*args)
            self.identity['changed'] = True
        with mock.patch('batch.run_trials', side_effect=run), \
             mock.patch('batch.native_preflight', return_value=dict(self.identity)):
            result = execute_batch(root, self.config)
        self.assertIn('configuration changed', result['batch_stop_reason'])
        self.assertEqual(sum(s == 'run' for s, _, _ in self.calls), 3)
        self.assertFalse(any(s != 'run' for s, _, _ in self.calls))

    def test_parallel_config_validation_and_model_free_plan(self):
        config = {'output_root': 'runs', 'runtime': 'runtime.json', 'reviewer':
                  {'chrys': '/a/icode', 'python': '/a/python', 'profile': '/a/model', 'endpoint_host': 'example.invalid'}}
        path = self.base / 'batch-config.json'
        with mock.patch('batch.load_native', side_effect=lambda x: x):
            path.write_text(json.dumps(config))
            loaded = load_config(path)
            self.assertEqual(loaded['max_parallel_trials'], 3)
            plan = make_plan(loaded, self.candidate)
            self.assertEqual(plan['maximum_agent_invocations'], 6)
            self.assertEqual(plan['max_parallel_trials'], 3)
            self.assertFalse(plan['models_called'])
            for value in (0, 4, True, 1.5, '3'):
                path.write_text(json.dumps({**config, 'max_parallel_trials': value}))
                with self.subTest(value=value), self.assertRaises(EvalError):
                    load_config(path)

    def test_parallel_workers_share_one_batch_deadline(self):
        self.config['max_wall_seconds'] = 1
        root = self.create()
        barrier = threading.Barrier(3)
        def run(suite, runtime, tid):
            self.calls.append(('run', self.row(suite, tid)['case'], tid))
            trial = Path(suite) / 'trials' / tid
            barrier.wait(timeout=5)
            process = run_logged([sys.executable, '-c', 'import time; time.sleep(3)'], trial / 'local-process', 10)
            write_json(trial / 'after-run.json', snapshot(trial / 'visible'))
            write_json(trial / 'run.json', {'status': process['status'], 'filesystem_status': 'captured',
                                          'isolation_confirmed': True, 'process': process})
        with mock.patch('batch.run_trials', side_effect=run):
            result = execute_batch(root, self.config)
        self.assertEqual(result['reserved_agent_invocations'], 3)
        self.assertEqual(result['counts']['candidate']['incomplete'], 3)
        for row in result['trials']:
            process = read_json(root / 'suite/trials' / row['id'] / 'local-process/process.json')
            self.assertEqual(process['status'], 'timeout')
            self.assertLess(process['duration_seconds'], 2)
            self.assertEqual(process['cleanup']['status'], 'confirmed')
        before = list(self.calls)
        execute_batch(root, self.config)
        self.assertEqual(before, self.calls)


class ConcurrentPrimitiveTests(unittest.TestCase):
    def test_cleanup_deadline_does_not_extend_peer_deadline(self):
        entered, release = threading.Event(), threading.Event()
        def cleanup():
            with wall_budget(10):
                entered.set()
                self.assertTrue(release.wait(5))
                self.assertGreater(remaining_timeout(10), 5)
        def peer():
            self.assertTrue(entered.wait(5))
            try:
                time.sleep(.12)
                with self.assertRaises(BudgetExhausted):
                    remaining_timeout(10)
            finally:
                release.set()
        with wall_budget(.08), ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(copy_context().run, cleanup)
            b = pool.submit(copy_context().run, peer)
            a.result(); b.result()

    def test_cleanup_can_finish_after_cancellation_without_resetting_peer_token(self):
        event = threading.Event()
        with wall_budget(10, cancellation=event):
            event.set()
            with self.assertRaises(KeyboardInterrupt):
                remaining_timeout(1)
            with wall_budget(1):
                self.assertGreater(remaining_timeout(1), 0)
            with self.assertRaises(KeyboardInterrupt):
                remaining_timeout(1)

    def test_shared_binding_is_created_once_and_conflicting_identity_is_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'binding.json'
            barrier = threading.Barrier(8)
            def bind():
                barrier.wait(timeout=5)
                bind_json(path, {'model': 'synthetic'}, 'drift')
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(bind) for _ in range(8)]
                for f in futures:
                    f.result()
            self.assertEqual(read_json(path), {'model': 'synthetic'})
            with self.assertRaisesRegex(EvalError, 'drift'):
                bind_json(path, {'model': 'different'}, 'drift')
            self.assertEqual(read_json(path), {'model': 'synthetic'})
            alias = path.parent / 'alias.json'
            alias.symlink_to(path)
            with self.assertRaises(EvalError):
                bind_json(alias, {}, 'drift')


if __name__ == '__main__':
    unittest.main()
