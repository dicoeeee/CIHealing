"""A/B scheduling tests: local locks and mocked phases, never model calls."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import multiprocessing
from pathlib import Path
import queue
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ab_run
from common import write_json


def lease_probe(root, phase, label, ready, release):
    with ab_run.slot(root, phase, label):
        ready.put((phase, label))
        if not release.wait(10):
            raise RuntimeError("test lease was not released")


class ABRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.trial = self.root / "A/trials/main--case-001--r1"
        self.trial.mkdir(parents=True)
        self.row = {"id": self.trial.name, "trial": str(self.trial)}

    def tearDown(self):
        self.temp.cleanup()

    def test_frozen_registry_counts_each_version_and_only_declared_repeats(self):
        rows = ab_run.registry()
        counts = Counter((r["group"], r["scenario"]) for r in rows)
        self.assertEqual(len(rows), 42)
        self.assertEqual(len(counts), 30)
        self.assertEqual(len({r["id"] for r in rows}), 42)
        self.assertEqual({k for k, n in counts.items() if n == 3}, ab_run.CRITICAL)
        self.assertEqual(len(ab_run.CRITICAL), 6)
        self.assertTrue(all(n == (3 if k in ab_run.CRITICAL else 1) for k, n in counts.items()))
        plan = ab_run.freeze_plan(self.root)
        self.assertEqual(plan["executions"], len(rows) * len(plan["versions"]))
        self.assertEqual(plan["max_reviews"], 84)
        self.assertEqual(plan["global_icode_limit"], 6)
        self.assertEqual(ab_run.freeze_plan(self.root), plan)
        plan["global_icode_limit"] = 7
        (self.root / "plan.json").write_text(json.dumps(plan))
        with self.assertRaises(ValueError):
            ab_run.freeze_plan(self.root)

    def test_global_filelocks_cover_execution_and_review_across_processes(self):
        ctx = multiprocessing.get_context("fork")
        ready, release = ctx.Queue(), ctx.Event()
        workers = [ctx.Process(target=lease_probe,
                   args=(str(self.root), "execute" if n < 3 else "review", f"t{n}", ready, release))
                   for n in range(7)]
        try:
            for process in workers:
                process.start()
            acquired = [ready.get(timeout=5) for _ in range(6)]
            self.assertEqual({phase for phase, _ in acquired}, {"execute", "review"})
            with self.assertRaises(queue.Empty):
                ready.get(timeout=.3)
            release.set()
            ready.get(timeout=5)
            for process in workers:
                process.join(5)
                self.assertEqual(process.exitcode, 0)
            result = ab_run.summarize(self.root)
            self.assertEqual(result["observed_peak_icode_leases"], 6)
            self.assertEqual(result["unresolved_leases"], 0)
        finally:
            release.set()
            for process in workers:
                if process.is_alive():
                    process.terminate()
                process.join(5)
            ready.close()

    def test_lease_file_closes_when_acquire_or_release_audit_fails(self):
        for fail_at in (1, 2):
            handles = []
            real_open = Path.open

            def tracked_open(path, *args, **kwargs):
                handle = real_open(path, *args, **kwargs)
                if path.suffix == ".lock":
                    handles.append(handle)
                return handle

            calls = 0

            def broken_append(*args):
                nonlocal calls
                calls += 1
                if calls == fail_at:
                    raise OSError("synthetic audit storage failure")

            with mock.patch.object(Path, "open", tracked_open), mock.patch.object(ab_run, "append", broken_append):
                with self.assertRaises(OSError):
                    with ab_run.slot(self.root, "review", "test"):
                        pass
            self.assertTrue(handles)
            self.assertTrue(all(h.closed for h in handles))

    def test_execution_and_review_both_hold_shared_phase_lease(self):
        active, phases = [], []

        @contextmanager
        def lease(root, phase, label):
            active.append(phase); phases.append(phase)
            try:
                yield
            finally:
                active.pop()

        def execute(trial, config):
            self.assertEqual(active, ["execute"])
            write_json(trial / "run.json", {"status": "completed", "process": {"status": "completed", "returncode": 0}})

        def objective(trial, config):
            self.assertEqual(active, [])
            write_json(trial / "objective.json", {"status": "pass"})

        def review(directory, config, packet, timeout):
            self.assertEqual(active, ["review"])
            write_json(directory / "result.json", {"status": "completed", "verdict": "pass"})

        with mock.patch.object(ab_run, "slot", lease), \
             mock.patch.object(ab_run.ab_runtime, "execute", execute), \
             mock.patch.object(ab_run.ab_runtime, "objective", objective), \
             mock.patch.object(ab_run.ab_runtime, "packet", return_value={}), \
             mock.patch.object(ab_run.ab_review, "run", review):
            result = ab_run.one(self.root, "A", self.row, {})
        self.assertEqual(phases, ["execute", "review"])
        self.assertEqual(result["status"], "pass")

    def test_failed_started_execution_is_not_automatically_replayed(self):
        with mock.patch.object(ab_run.ab_runtime, "execute", side_effect=RuntimeError("do-not-export-secret")) as execute, \
             mock.patch.object(ab_run.ab_runtime, "objective") as objective, \
             mock.patch.object(ab_run.ab_review, "run") as review:
            first = ab_run.one(self.root, "A", self.row, {})
            second = ab_run.one(self.root, "A", self.row, {})
        self.assertEqual(execute.call_count, 1)
        objective.assert_not_called(); review.assert_not_called()
        self.assertEqual(first["status"], "harness_incomplete")
        self.assertNotIn("do-not-export-secret", json.dumps(first))
        self.assertEqual(second["reason"], "started_execution_not_replayed")

    def test_started_review_is_not_replayed(self):
        write_json(self.trial / "run.json", {"status": "completed", "process": {"status": "completed", "returncode": 0}})
        write_json(self.trial / "objective.json", {"status": "pass"})
        write_json(self.trial / "review-start.json", {})
        with mock.patch.object(ab_run.ab_runtime, "execute") as execute, mock.patch.object(ab_run.ab_review, "run") as review:
            result = ab_run.one(self.root, "A", self.row, {})
        execute.assert_not_called(); review.assert_not_called()
        self.assertEqual(result["reason"], "started_review_not_replayed")

    def test_concurrent_dispatch_of_same_trial_never_executes_twice(self):
        def execute(trial, config):
            write_json(trial / "run.json", {"status": "completed", "process": {"status": "completed", "returncode": 0}})

        with mock.patch.object(ab_run.ab_runtime, "execute", side_effect=execute) as run, \
             mock.patch.object(ab_run.ab_runtime, "objective", side_effect=lambda p, c: write_json(p / "objective.json", {"status": "pass"})):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(ab_run.one, self.root, "A", self.row, {}, False) for _ in range(2)]
                [future.result() for future in futures]
        self.assertEqual(run.call_count, 1)

    def test_derived_summary_can_refresh_without_rewriting_evidence(self):
        write_json(self.root / "A/manifest.json", {"trials": [self.row]})
        write_json(self.trial / "run.json", {"status": "completed", "process": {"status": "completed", "returncode": 0}})
        evidence = (self.trial / "run.json").read_bytes()
        first = ab_run.summarize(self.root)
        write_json(self.trial / "objective.json", {"status": "fail"})
        second = ab_run.summarize(self.root)
        self.assertEqual(first["versions"]["A"]["counts"]["incomplete"], 1)
        self.assertEqual(second["versions"]["A"]["counts"]["fail"], 1)
        self.assertEqual((self.trial / "run.json").read_bytes(), evidence)

    def test_partial_execution_or_review_cannot_pass_with_residual_verdict(self):
        run_path = self.trial / "run.json"
        review_path = self.trial / "review/result.json"
        write_json(self.trial / "objective.json", {"status": "pass"})
        for process_status, review_status in (("timeout", "completed"), ("completed", "incomplete")):
            run_path.write_text(json.dumps({"status": "completed", "process": {"status": process_status, "returncode": 0}}))
            review_path.parent.mkdir(exist_ok=True)
            review_path.write_text(json.dumps({"status": review_status, "verdict": "pass"}))
            self.assertEqual(ab_run.trial_status(self.trial, "synthetic")["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
