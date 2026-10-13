"""Supplemental reviews never rerun repair, overwrite history or retry a pass."""
from contextlib import ExitStack
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import EvalError, digest, json_bytes, read_json, snapshot, write_json, write_new
from review_attempt import review_attempt
from test_eval import packet


class ReviewAttemptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="review-attempt-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.suite, self.source, self.output = self.root / "suite", self.root / "old", self.root / "new"
        self.packet = packet()
        write_json(self.suite / "trials/t1/grade.json", {"status": "awaiting_review"})
        write_json(self.source / "input/packet.json", self.packet)
        write_new(self.source / "input/task.txt", "old frozen task")
        self.binding = {"format": "ci-native-review-v1", "suite": str(self.suite), "trial_id": "t1", "max_packet_bytes": 524288,
            "packet_sha256": digest(json_bytes(self.packet)), "prompt_sha256": digest(b"old frozen task"),
            "grade_sha256": digest((self.suite / "trials/t1/grade.json").read_bytes())}
        write_json(self.source / "binding.json", self.binding)
        write_json(self.source / "receipt.json", {"status": "incomplete", "gap": "review_timeout",
            "packet_sha256": self.binding["packet_sha256"], "grade_sha256": self.binding["grade_sha256"]})
        self.config = {"timeout_seconds": 900}
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch("review_attempt.load_suite", return_value={"trials": [{"id": "t1"}]}))
        self.stack.enter_context(mock.patch("review_attempt.make_packet", return_value=self.packet))
        self.preflight = self.stack.enter_context(mock.patch("review_attempt.native_preflight", return_value={"model": "glm-5.3"}))
        self.stack.enter_context(mock.patch("review_attempt.native_identity", return_value={"model": "glm-5.3"}))
        self.native = self.stack.enter_context(mock.patch("review_attempt.native_review", return_value={"status": "validated", "verdict": {"verdict": "pass"}}))

    def test_plan_is_read_only_and_calls_no_model_or_preflight(self):
        before = snapshot(self.root)
        result = review_attempt(self.source, self.output, self.config)
        self.assertEqual(result["maximum_model_calls"], 1)
        self.assertFalse(result["models_called"])
        self.assertEqual(snapshot(self.root), before)
        self.native.assert_not_called(); self.preflight.assert_not_called()

    def test_execute_uses_exact_frozen_packet_without_overwriting_old_evidence(self):
        before_suite, before_source = snapshot(self.suite), snapshot(self.source)
        result = review_attempt(self.source, self.output, self.config, execute=True)
        self.native.assert_called_once()
        self.assertFalse(self.native.call_args.kwargs["publish"])
        self.assertEqual(self.native.call_args.kwargs["frozen_packet"], self.packet)
        self.assertEqual(snapshot(self.suite), before_suite)
        self.assertEqual(snapshot(self.source), before_source)
        self.assertEqual(result["verdict"], "pass")
        self.assertFalse(result["suite_modified"])

    def test_timeout_is_one_attempt_without_automatic_retry(self):
        self.native.return_value = {"status": "incomplete", "gap": "review_timeout"}
        result = review_attempt(self.source, self.output, self.config, execute=True)
        self.native.assert_called_once()
        self.assertEqual(result["gap"], "review_timeout")

    def test_modified_packet_refused_before_dispatch(self):
        p = self.source / "input/packet.json"; p.write_text("{}")
        with self.assertRaises(EvalError):
            review_attempt(self.source, self.output, self.config, execute=True)
        self.native.assert_not_called()

    def test_completed_verdict_is_not_eligible_for_pass_seeking_retry(self):
        p = self.source / "receipt.json"; r = read_json(p); r["status"] = "validated"; p.write_bytes(json_bytes(r))
        with self.assertRaises(EvalError):
            review_attempt(self.source, self.output, self.config, execute=True)
        self.native.assert_not_called()

    def test_source_drift_during_preflight_prevents_call(self):
        def drift(*args):
            (self.source / "input/task.txt").write_text("changed")
            return {"model": "glm-5.3"}
        self.preflight.side_effect = drift
        with self.assertRaises(EvalError):
            review_attempt(self.source, self.output, self.config, execute=True)
        self.native.assert_not_called()

    def test_existing_attempt_or_symlink_output_refused(self):
        self.output.mkdir()
        with self.assertRaises(EvalError):
            review_attempt(self.source, self.output, self.config, execute=True)
        self.native.assert_not_called()

    def test_nested_output_cannot_mutate_source_review(self):
        with self.assertRaises(EvalError):
            review_attempt(self.source, self.source / "nested", self.config, execute=True)
        self.native.assert_not_called()

    def test_post_dispatch_drift_cannot_be_accepted_as_pass(self):
        def drift(*args, **kwargs):
            (self.source / "input/task.txt").write_text("changed while reviewing")
            return {"status": "validated", "verdict": {"verdict": "pass"}}
        self.native.side_effect = drift
        result = review_attempt(self.source, self.output, self.config, execute=True)
        self.assertEqual(result["status"], "incomplete")
        self.assertIsNone(result["verdict"])
        self.assertFalse(read_json(self.output / "attempt-result.json")["source_evidence_unchanged"])


if __name__ == "__main__":
    unittest.main()
