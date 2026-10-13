"""No-model lifecycle, fail-closed classification and metadata-only diagnostics."""

import copy
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import EvalError, read_json, run_logged, write_new
from execution_outcome import classify_execution, classify_grade
from runtime_diagnostics import timing_summary, write_diagnostics


class ExecutionControlsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ci-execution-controls-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_timeout_requires_affirmative_cleanup_and_isolation(self):
        data = {"status": "timeout", "filesystem_status": "captured", "isolation_confirmed": True,
                "process": {"status": "timeout", "returncode": 0, "cleanup": {"status": "confirmed"}}}
        self.assertEqual(classify_execution(data, "run")["batch_action"], "continue")
        for key in ("isolation_confirmed", "filesystem_status"):
            changed = copy.deepcopy(data)
            changed.pop(key)
            self.assertEqual(classify_execution(changed, "run")["batch_action"], "stop")
        for status in (None, "unconfirmed"):
            changed = copy.deepcopy(data)
            changed["process"]["cleanup"]["status"] = status
            self.assertEqual(classify_execution(changed, "run")["batch_action"], "stop")
        data["gateway_cleanup_confirmed"] = False
        self.assertEqual(classify_execution(data, "run")["scope"], "safety")

    def test_global_faults_are_not_classified_as_case_timeouts(self):
        for code in (401, 403, 429, 502):
            data = {"process": {"status": "timeout", "returncode": -9, "cleanup": {"status": "confirmed"}},
                    "isolation_confirmed": True, "filesystem_status": "captured",
                    "runtime_diagnostics": {"gateway_statuses": [code]}}
            self.assertEqual(classify_execution(data, "run")["batch_action"], "stop")
        self.assertEqual(classify_execution({"process": {"returncode": 2}}, "run")["scope"], "unknown")
        self.assertEqual(classify_execution({"status": "incomplete", "process": {"returncode": 0}}, "review")["code"], "review_evidence_unconfirmed")
        self.assertIsNone(classify_execution({"status": "validated", "verdict": {"verdict": "fail"}, "process": {"returncode": 0}}, "review"))

    def test_malformed_receipts_stop_without_inventing_failure_scope(self):
        for data in (None, [], {"status": []}, {"process": []}, {"process": {"status": []}}, {"process": {"cleanup": None}},
                     {"runtime_diagnostics": None}, {"runtime_diagnostics": {"gateway_statuses": ["401"]}}):
            with self.subTest(data=data):
                result = classify_execution(data, "run")
                self.assertEqual(result["batch_action"], "stop")
                self.assertEqual(result["scope"], "unknown")

    def test_unknown_cleanup_overrides_a_zero_exit(self):
        result = classify_execution({"process": {"status": "completed", "returncode": 0, "cleanup": {"status": "unconfirmed"}}}, "run")
        self.assertEqual(result["code"], "process_cleanup_unconfirmed")

    def test_grade_distinguishes_missing_report_from_missing_identity(self):
        self.assertIsNone(classify_grade({"bindings": {"test": True}, "gaps": ["report_file_missing_fallback_requires_review"]}))
        self.assertEqual(classify_grade({"bindings": {"test": True}, "gaps": ["native_skill_registration_unconfirmed"]})["scope"], "environment")
        self.assertEqual(classify_grade({"gaps": ["arbitrary error"]})["scope"], "unknown")

    def test_stdout_closed_does_not_escape_deadline(self):
        code = "import os,time; os.close(1); os.close(2); time.sleep(20)"
        result = run_logged([sys.executable, "-c", code], self.root / "execution", .4)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["cleanup"]["status"], "confirmed")

    def test_successful_timeout_kill_is_not_sent_twice(self):
        # macOS may return EPERM for a second SIGKILL during teardown, even
        # though wait() and the later ESRCH prove that our group is gone.
        real = os.killpg
        kills = []
        def once(pgid, sig):
            if sig == signal.SIGKILL:
                kills.append(pgid)
                if len(kills) > 1:
                    raise PermissionError(1, "synthetic duplicate signal race")
            return real(pgid, sig)
        with mock.patch("common.os.killpg", side_effect=once):
            result = run_logged([sys.executable, "-c", "import time;time.sleep(20)"], self.root / "once", .3)
        self.assertEqual(result["cleanup"]["status"], "confirmed")
        self.assertEqual(len(kills), 1)

    def test_prior_kill_does_not_waive_reap_or_group_absence(self):
        import subprocess
        from common import stop_owned_group
        for reaped, absent in [(True, False), (False, True)]:
            process = mock.Mock(pid=123456, returncode=-9 if reaped else None)
            if not reaped:
                process.wait.side_effect = subprocess.TimeoutExpired("synthetic", 2)
            with mock.patch("common.os.killpg", side_effect=ProcessLookupError() if absent else None) as kill, \
                 mock.patch("common.time.monotonic", side_effect=[0, 2]):
                result = stop_owned_group(process, kill_already_sent=True)
            self.assertEqual(result["status"], "unconfirmed")
            self.assertTrue(all(call.args[1] == 0 for call in kill.call_args_list))

    def test_graceful_interrupt_retains_output_without_forging_success(self):
        code = ("import signal,time; "
                "signal.signal(signal.SIGINT, lambda *_: (print('checkpoint-saved',flush=True),exit(0))); "
                "print('started',flush=True); time.sleep(20)")
        result = run_logged([sys.executable, "-u", "-c", code], self.root / "execution", 1.0, grace_seconds=.4)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["returncode"], 0)
        self.assertTrue(result["shutdown"]["interrupt_sent"])
        self.assertEqual(result["termination"]["reason"], "timeout")
        self.assertEqual(result["termination"]["signals"][0]["signal"], "SIGINT")
        self.assertEqual(result["termination"]["signals"][0]["delivery"], "sent")
        self.assertLessEqual(result["termination"]["signals"][0]["requested_ns"], result["termination"]["signals"][0]["completed_ns"])
        self.assertEqual(result["cleanup"]["status"], "confirmed")
        self.assertIn("checkpoint-saved", (self.root / "execution/stdout.log").read_text())
        self.assertLess(result["duration_seconds"], 2)

    def test_ignored_interrupt_is_killed_and_diagnostics_are_not_agent_report(self):
        code = "import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); print('started',flush=True); time.sleep(20)"
        result = run_logged([sys.executable, "-u", "-c", code], self.root / "execution", .8, grace_seconds=.3)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["returncode"], -signal.SIGKILL)
        self.assertEqual(result["termination"]["signals"][-1]["signal"], "SIGKILL")
        report = write_diagnostics(self.root, self.root / "sessions", result, changed_paths=[])
        self.assertFalse(report["agent_report"])
        self.assertEqual(report["author"], "harness")
        self.assertFalse((self.root / "analysis-fix.md").exists())

    def test_operator_interrupt_persists_process_receipt_before_propagating(self):
        import selectors
        real = selectors.DefaultSelector
        def factory():
            selector = real()
            selector.select = mock.Mock(side_effect=KeyboardInterrupt())
            return selector
        with mock.patch("common.selectors.DefaultSelector", side_effect=factory), self.assertRaises(KeyboardInterrupt):
            run_logged([sys.executable, "-c", "import time;time.sleep(20)"], self.root / "execution", 2)
        result = read_json(self.root / "execution/process.json")
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["termination"]["reason"], "operator")
        self.assertEqual(result["termination"]["signals"][0]["reason"], "operator")

    def test_timing_exports_counts_not_reasoning_or_prompt_content(self):
        events = [
            {"event_type": "model.exchange.started", "occurred_at": "2026-01-01T00:00:00Z", "payload": {"prompt": "SECRET"}},
            {"event_type": "model.exchange.finished", "payload": {"duration_ms": 1000, "outcome": "success", "chunk_count": 20,
                "reasoning_text": "SECRET", "usage": {"normalized": {"reasoning": 19, "output_visible": 1, "output_total": 20, "secret": "SECRET"}}}},
            {"event_type": "event.segment", "payload": {"text": "SECRET"}},
            {"event_type": "model.exchange.started", "occurred_at": "2026-01-01T00:00:02Z", "payload": {}}]
        path = self.root / "sessions/session/trajectory/events.jsonl"
        write_new(path, "\n".join(json.dumps(r) for r in events) + "\n{partial")
        write_new(self.root / "public.log", 'CI_EVAL_EVENT ' + json.dumps({"kind": "InvocationToolCallResult", "payload": {"duration_ms": 12, "result": "SECRET"}}))
        result = timing_summary(self.root, {"ended_at": "2026-01-01T00:00:05+00:00"}, self.root / "public.log")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(result["exchanges"][0]["tokens"]["reasoning"], 19)
        self.assertEqual(result["exchanges"][1]["observed_until_process_end_ms"], 3000)
        self.assertFalse(result["exchanges"][1]["completed"])
        self.assertEqual(result["sum_tool_duration_ms"], 12)
        self.assertIn("partial_or_invalid_trajectory_line", result["gaps"])

    def test_gateway_status_uses_only_structured_numeric_fields(self):
        write_new(self.root / "gateway.log", '\n'.join([json.dumps({"upstream_status": 401, "body": "SECRET"}),
                                                       json.dumps({"upstream_status": "SECRET"}), 'invalid']))
        result = write_diagnostics(self.root, self.root / "missing", {}, gateway_log=self.root / "gateway.log")
        self.assertEqual(result["gateway_statuses"], [401])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_interrupted_exchange_ended_but_did_not_complete_successfully(self):
        events = [{"event_type": "model.exchange.started", "occurred_at": "2026-01-01T00:00:00Z", "payload": {}},
                  {"event_type": "model.exchange.finished", "payload": {"outcome": "interrupted", "chunk_count": 20}}]
        write_new(self.root / "sessions/s/trajectory/events.jsonl", "\n".join(json.dumps(e) for e in events))
        row = timing_summary(self.root, {})["exchanges"][0]
        self.assertTrue(row["exchange_ended"])
        self.assertFalse(row["completed"])
        self.assertEqual(row["outcome"], "interrupted")

    def test_timeout_is_primary_even_with_zero_exit_or_empty_stdout(self):
        from native_runner import review_process_failure
        self.assertEqual(review_process_failure({"status": "timeout", "returncode": 0}), "review_timeout")
        self.assertEqual(review_process_failure({"status": "interrupted", "returncode": 130}), "review_interrupted")
        self.assertIsNone(review_process_failure({"status": "completed", "returncode": 0}))

    def test_native_timeout_does_not_parse_empty_json_or_write_original_suite(self):
        from common import write_json
        from native_runner import native_review
        suite, out = self.root / "suite", self.root / "attempt"
        suite.mkdir()
        identity = {"profile_id": "test", "version": "synthetic"}
        config = {"timeout_seconds": 900, "max_packet_bytes": 524288, "endpoint_host": "example.invalid", "chrys": "/unused/chrys"}
        def prepare(*args, **kwargs):
            write_json(out / "binding.json", {"grade_sha256": "grade", "packet_sha256": "packet"})
            write_json(out / "input/packet.json", {})
            write_json(out / "invocation.json", {"argv": ["chrys", "run"]})
        def run(*args, **kwargs):
            write_new(out / "execution/stdout.log", "")
            process = {"status": "timeout", "returncode": 130, "cleanup": {"status": "confirmed"}}
            write_json(out / "execution/process.json", process)
            write_new(out / "tmp/transient.bin", "generated")
            return process
        with mock.patch("native_runner.native_identity", return_value=identity), \
             mock.patch("native_runner.prepare_native", side_effect=prepare), \
             mock.patch("native_runner.policy", return_value="synthetic"), \
             mock.patch("native_runner.run_logged", side_effect=run), \
             mock.patch("native_runner.strict_json") as parser:
            result = native_review(suite, "t1", out, config, identity, publish=False)
        parser.assert_not_called()
        self.assertEqual(result["failure_code"], "review_timeout")
        self.assertEqual(result["output_status"], "absent")
        self.assertFalse(result["suite_review_written"])
        self.assertEqual(result["resource_cleanup"]["status"], "cleaned")
        self.assertFalse((out / "tmp").exists())
        self.assertTrue((out / "execution/stdout.log").exists())
        self.assertEqual(list(suite.iterdir()), [])

    def test_absent_timing_is_reported_as_unknown_not_zero_model_activity(self):
        result = timing_summary(self.root / "absent", {})
        self.assertIn("native_timing_unavailable", result["gaps"])
        self.assertIsNone(result["tool_results"])
        self.assertIsNone(result["sum_tool_duration_ms"])

    def test_diagnostics_tampering_is_rejected(self):
        from common import digest
        from repair_support import RUNTIME_CONTRACT, validate_repair_artifacts
        from native_runner import validate_native_artifacts
        write_new(self.root / "runtime-diagnostics.json", "{}")
        receipt = {"runtime_contract": RUNTIME_CONTRACT, "diagnostics_directory": str(self.root),
                   "diagnostics_sha256": digest(b"old")}
        with self.assertRaisesRegex(EvalError, "diagnostics changed"):
            validate_repair_artifacts(self.root, receipt)
        with self.assertRaisesRegex(EvalError, "diagnostics changed"):
            validate_native_artifacts(receipt)


if __name__ == "__main__":
    unittest.main()
