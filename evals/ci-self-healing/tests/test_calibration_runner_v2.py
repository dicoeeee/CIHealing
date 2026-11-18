"""Offline calibration orchestration; fake judges do not establish semantic accuracy."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
from harness import calibration, cli, grading, runner
from harness.core import EvaluationError, read_allowed, read_json, sha


class FakeJudge:
    """Return supplied labels to test the runner, never an actual model judgment."""
    def __init__(self, suite, *, change=None, barrier=None):
        self.samples = {row["case"]["id"]: row for row in calibration.samples(suite)}
        self.change = change
        self.barrier = barrier
        self.lock = threading.Lock()
        self.calls = []
        self.preflights = 0
        self.active = 0
        self.maximum_active = 0

    def preflight(self, runtime):
        self.preflights += 1
        return {"status": "passed", "synthetic": True, "models_called": False}

    def execute(self, *args):
        raise AssertionError("calibration must never execute repair tasks")

    def judge(self, directory, runtime, prompt, seconds, *, review_contract=None):
        payload = json.loads(prompt.split("\n", 1)[1])
        identifier = payload["case"]["id"]
        sample = self.samples[identifier]
        if review_contract != payload['allowed_ids']:
            raise AssertionError("judge contract differs from prompt allowlist")
        if sample["id"] in prompt or "labels_provenance" in prompt or '"expected"' in prompt:
            raise AssertionError("judge prompt contains hidden labels")
        with self.lock:
            self.calls.append((identifier, seconds, directory, runtime))
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            if self.barrier:
                self.barrier.wait(timeout=10)
            checks = [{"criterion_id": criterion_id, **label,
                       "evidence_ids": [next(iter(sample["packet"]["evidence"]))],
                       "reason": "Synthetic adapter checks orchestration only."}
                      for criterion_id, label in sample["expected"].items()]
            response = {"status": "completed", "trace_complete": True,
                        "models_called": False, "answer": json.dumps({"checks": checks})}
            if self.change:
                self.change(identifier, checks, response, directory)
            return response
        finally:
            with self.lock:
                self.active -= 1


class CalibrationPreparationTests(unittest.TestCase):
    def test_plans_keep_default_and_select_new_suite_without_runtime_or_output(self):
        with patch("harness.native.preflight", side_effect=AssertionError("no runtime allowed")):
            default = runner.calibration_plan()
            selected = runner.calibration_plan("evidence-semantics-v1")
        self.assertEqual(default["suite"], "initial-v1")
        self.assertEqual(default["criteria_ids"], ["delivery.outcome-meaning", "untrusted.no-authority"])
        self.assertEqual(selected["criteria_ids"], ["contract.target-evidence", "delivery.observation-honesty", "delivery.outcome-meaning"])
        for plan in (default, selected):
            self.assertEqual((plan["planned_sessions"], plan["maximum_sessions"], plan["concurrency"]), (6, 6, 3))
            self.assertEqual((plan["wall_seconds"], plan["stage_seconds"]), (1800, 900))
            self.assertFalse(plan["models_called"])

    def test_prepare_freezes_blind_prompts_criteria_implementation_and_separate_labels(self):
        suite = "evidence-semantics-v1"
        samples = calibration.samples(suite)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "prepared"
            with patch("harness.native.preflight", side_effect=AssertionError("no runtime allowed")):
                result = runner.prepare_calibration(root, suite)
            self.assertEqual((result["status"], result["exit_code"], result["sessions_reserved"]), ("prepared", 0, 0))
            self.assertFalse(result["models_called"])
            self.assertFalse((root / "runtime.json").exists())
            self.assertFalse((root / "ledger.json").exists())
            frozen = runner.Catalog(root / "spec", verify_sources=False)
            unlabeled = read_json(root / "samples.json")["samples"]
            labels = read_json(root / "labels.json")["labels"]
            self.assertEqual(len(unlabeled), 6)
            self.assertEqual(len(labels), 6)
            for index, (sample, blind, label) in enumerate(zip(samples, unlabeled, labels), 1):
                self.assertEqual(blind["id"], f"c{index:03d}")
                self.assertNotIn("expected", blind)
                self.assertNotIn("provenance", blind)
                self.assertEqual(label["expected"], sample["expected"])
                self.assertEqual(label["provenance"], sample["labels_provenance"])
                prompt = (root / blind["prompt_file"]).read_text()
                self.assertEqual(prompt, grading.review_prompt(sample["case"], sample["packet"], frozen.for_case(sample["case"], "semantic")))
                self.assertEqual(sha(prompt.encode()), blind["prompt_sha256"])
                self.assertNotIn(sample["id"], prompt)
                self.assertNotIn(sample["labels_provenance"]["basis"], prompt)
                self.assertNotIn('"expected"', prompt)
                self.assertNotIn("labels_provenance", prompt)
            for relative, digest in read_json(root / "implementation.json").items():
                self.assertEqual(sha((root / "implementation" / relative).read_bytes()), digest)
            self.assertIn("fixtures/calibration/evidence-semantics-v1/samples.json", read_json(root / "implementation.json"))
            self.assertEqual(sha((root / "input-seal.json").read_bytes()), result["input_seal_sha256"])
            runner._assert_calibration_inputs(root)

    def test_existing_directory_is_never_overwritten_even_before_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "historical.txt"
            marker.write_bytes(b"preserved")
            adapter = FakeJudge("initial-v1")
            for invoke in (lambda: runner.prepare_calibration(root), lambda: runner.calibrate(root, {}, adapter=adapter)):
                with self.assertRaisesRegex(EvaluationError, "exists"):
                    invoke()
            self.assertEqual(marker.read_bytes(), b"preserved")
            self.assertEqual(list(root.iterdir()), [marker])
            self.assertEqual(adapter.preflights, 0)

    def test_changed_or_added_prepared_inputs_cannot_pass_the_seal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "prepared"
            runner.prepare_calibration(root, "evidence-semantics-v1")
            for relative in ("labels.json", "prompts/c001.txt", "contracts/c001.json", "spec/criteria.json"):
                path = root / relative
                before = path.read_bytes()
                path.write_bytes(before + b" ")
                with self.assertRaisesRegex(EvaluationError, "inputs or labels changed"):
                    runner._assert_calibration_inputs(root)
                path.write_bytes(before)
            added = root / "prompts/unsealed.txt"
            added.write_text("not frozen")
            with self.assertRaisesRegex(EvaluationError, "inputs or labels changed"):
                runner._assert_calibration_inputs(root)
            added.unlink()
            runner._assert_calibration_inputs(root)

    def test_rule_snapshot_is_independent_of_later_source_changes_and_is_sealed(self):
        catalog = runner.Catalog()
        rows = catalog.documents["criteria"]["source_snapshot"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "prepared"
            source_root = Path(temporary) / "isolated-repo"
            for row in rows:
                path = source_root / row["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(read_allowed(runner.REPO / row["path"]))
            with patch.object(runner, "REPO", source_root):
                runner.prepare_calibration(root, "evidence-semantics-v1")
                relative = rows[0]["path"]
                frozen = root / "requirements" / relative
                initial = read_allowed(frozen)
                (source_root / relative).write_bytes(initial + b"\nsource changed after freezing\n")
                self.assertEqual(read_allowed(frozen), initial)
                runner._assert_calibration_inputs(root)
                with self.assertRaisesRegex(EvaluationError, "requirement changed before freezing"):
                    runner.prepare_calibration(Path(temporary) / "drifted", "evidence-semantics-v1")
            expected = {"requirements/" + row["path"]: row["sha256"] for row in rows}
            actual = {name: digest for name, digest in read_json(root / "input-seal.json")["files"].items()
                      if name.startswith("requirements/")}
            self.assertEqual(actual, expected)
            frozen.write_bytes(initial + b"\nsnapshot changed\n")
            with self.assertRaisesRegex(EvaluationError, "inputs or labels changed"):
                runner._assert_calibration_inputs(root)

    def test_suite_and_strict_budget_validation_precede_preflight(self):
        invalid = [dict(suite="other"), dict(maximum_sessions=5), dict(maximum_sessions=7),
                   dict(maximum_sessions=True), dict(wall_seconds=0), dict(wall_seconds=1801),
                   dict(wall_seconds=1.5), dict(stage_seconds=0), dict(stage_seconds=901),
                   dict(concurrency=0), dict(concurrency=7), dict(concurrency=True)]
        with tempfile.TemporaryDirectory() as temporary:
            for options in invalid:
                with self.subTest(options=options):
                    adapter = FakeJudge("initial-v1")
                    with self.assertRaises(EvaluationError):
                        runner.calibrate(Path(temporary) / "new", {}, adapter=adapter, **options)
                    self.assertEqual(adapter.preflights, 0)
                    self.assertFalse((Path(temporary) / "new").exists())

    def test_new_suite_requires_six_distinct_opaque_samples_and_all_three_pairs(self):
        originals = calibration.samples("evidence-semantics-v1")
        variants = []
        variants.append(originals[:-1])
        duplicated = copy.deepcopy(originals)
        duplicated[-1]["id"] = duplicated[0]["id"]
        variants.append(duplicated)
        disclosed = copy.deepcopy(originals)
        disclosed[0]["case"]["id"] = disclosed[0]["id"]
        variants.append(disclosed)
        same_label = copy.deepcopy(originals)
        first = same_label[0]
        criterion = first["case"]["criteria_ids"][0]
        first["expected"][criterion]["status"] = "pass" if first["expected"][criterion]["status"] == "fail" else "fail"
        variants.append(same_label)
        unknown = copy.deepcopy(originals)
        criterion = unknown[0]["case"]["criteria_ids"][0]
        unknown[0]["expected"][criterion] = {"status": "inconclusive", "applicability": "unknown"}
        variants.append(unknown)
        missing = copy.deepcopy(originals)
        missing[0]["expected"] = {}
        variants.append(missing)
        for rows in variants:
            with self.subTest(rows=rows[0]["id"]), patch.object(calibration, "samples", return_value=rows):
                with self.assertRaises(EvaluationError):
                    runner.calibration_plan("evidence-semantics-v1")


class CalibrationExecutionTests(unittest.TestCase):
    def test_explicit_six_way_calibration_does_not_expand_repair_concurrency(self):
        suite = "evidence-semantics-v1"
        with tempfile.TemporaryDirectory() as temporary:
            adapter = FakeJudge(suite, barrier=threading.Barrier(6))
            root = Path(temporary) / "run"
            result = runner.calibrate(root, {}, suite=suite, adapter=adapter, concurrency=6)
            self.assertEqual(result["status"], "pending_rationale")
            self.assertEqual(result["automatic_status"], "passed")
            self.assertFalse(result["phase2_permitted"])
            self.assertFalse(result["models_called"])
            self.assertEqual(adapter.maximum_active, 6)
            self.assertEqual(len(adapter.calls), 6)
            self.assertEqual(result["sessions_reserved"], 6)
            self.assertEqual(result["concurrency"], 6)
            self.assertEqual(read_json(root / "plan.json")["concurrency"], 6)
        self.assertEqual(runner.calibration_plan(concurrency=1)["concurrency"], 1)
        with self.assertRaisesRegex(EvaluationError, "invalid concurrency; maximum 3"):
            runner.make_plan(None, None, concurrency=6)

    def test_fake_judge_six_calls_bounded_parallelism_and_both_label_fields(self):
        for suite in calibration.SUITES:
            with self.subTest(suite=suite), tempfile.TemporaryDirectory() as temporary:
                adapter = FakeJudge(suite, barrier=threading.Barrier(3))
                root = Path(temporary) / "run"
                result = runner.calibrate(root, {"synthetic": True}, suite=suite, adapter=adapter)
                self.assertEqual(result["status"], "pending_rationale")
                self.assertEqual(result["automatic_status"], "passed")
                self.assertFalse(result["phase2_permitted"])
                self.assertFalse(result["models_called"])
                self.assertEqual(adapter.preflights, 1)
                self.assertEqual(adapter.maximum_active, 3)
                self.assertEqual(len(adapter.calls), 6)
                self.assertEqual({c[0] for c in adapter.calls}, set(adapter.samples))
                self.assertTrue(all(1 <= c[1] <= 900 for c in adapter.calls))
                self.assertTrue(all(c[3]["_frozen_identity"]["synthetic"] for c in adapter.calls))
                self.assertEqual(len(read_json(root / "ledger.json")["sessions"]), 6)
                for row in result["samples"]:
                    self.assertEqual(row["actual"], row["expected"])
                    self.assertTrue(all(set(label) == {"status", "applicability"} for label in row["actual"].values()))

    def test_status_and_applicability_disagreements_are_never_passed(self):
        for field in ("status", "applicability"):
            def change(identifier, checks, response, directory):
                target = "c001" if field == "status" else "c006"
                if identifier != target:
                    return
                checks[0][field] = "fail" if field == "status" else "applicable"
                response["answer"] = json.dumps({"checks": checks})
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                adapter = FakeJudge("initial-v1", change=change)
                result = runner.calibrate(Path(temporary) / "run", {}, adapter=adapter)
                self.assertNotEqual(result["status"], "passed")
                self.assertEqual(sum(row["status"] == "failed" for row in result["samples"]), 1)
                self.assertEqual(sum(row["status"] == "passed" for row in result["samples"]), 5)
                self.assertEqual(len(adapter.calls), 6)

    def test_timeout_exception_invalid_schema_and_trace_gaps_do_not_retry(self):
        def change(identifier, checks, response, directory):
            if identifier == "c001": response["status"] = "timeout"
            if identifier == "c002": raise RuntimeError("synthetic provider failure")
            if identifier == "c003": response["answer"] = '{"checks":[]}'
            if identifier == "c004": response["trace_complete"] = False
        with tempfile.TemporaryDirectory() as temporary:
            adapter = FakeJudge("evidence-semantics-v1", change=change)
            result = runner.calibrate(Path(temporary) / "run", {}, suite="evidence-semantics-v1", adapter=adapter)
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual([row["status"] for row in result["samples"]], ["incomplete"] * 4 + ["passed"] * 2)
            self.assertEqual(result["sessions_reserved"], 6)
            self.assertEqual(len(adapter.calls), 6)
            self.assertIsNone(result["models_called"])
            self.assertNotIn("synthetic provider failure", json.dumps(result))

    def test_labels_changed_during_review_invalidate_results(self):
        def change(identifier, checks, response, directory):
            if identifier == "c001":
                label_path = directory.parent / "labels.json"
                label_path.write_bytes(label_path.read_bytes() + b" ")
        with tempfile.TemporaryDirectory() as temporary:
            adapter = FakeJudge("evidence-semantics-v1", change=change, barrier=threading.Barrier(3))
            result = runner.calibrate(Path(temporary) / "run", {}, suite="evidence-semantics-v1", adapter=adapter)
            self.assertNotEqual(result["status"], "passed")
            first = result["samples"][0]
            self.assertEqual(first["status"], "incomplete")
            self.assertIn("inputs or labels changed", first["error"]["message"])
            self.assertLessEqual(result["sessions_reserved"], 6)


class CalibrationCliTests(unittest.TestCase):
    def invoke(self, args):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = cli.main(args)
        return code, output.getvalue(), error.getvalue()

    def test_plan_and_prepare_ignore_runtime_and_never_launch_models(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "new"
            args = ["calibrate", "--suite", "evidence-semantics-v1", "--output", str(root),
                    "--runtime", "/does/not/exist.json", "--concurrency", "6"]
            with patch.object(cli, "_runtime", side_effect=AssertionError("runtime was accessed")), \
                 patch("harness.native.preflight", side_effect=AssertionError("no model runtime")):
                code, output, error = self.invoke(args)
                self.assertEqual((code, error), (0, ""))
                self.assertEqual(json.loads(output)["suite"], "evidence-semantics-v1")
                self.assertEqual(json.loads(output)["concurrency"], 6)
                self.assertFalse(root.exists())
                code, output, error = self.invoke(args + ["--prepare-only"])
                self.assertEqual((code, error), (0, ""))
                self.assertEqual(json.loads(output)["status"], "prepared")
                self.assertEqual(json.loads(output)["concurrency"], 6)
                self.assertFalse(json.loads(output)["models_called"])
            self.assertTrue((root / "labels.json").is_file())

    def test_live_cli_passes_explicit_concurrency_to_adapter_orchestration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "new"
            args = ["calibrate", "--suite", "evidence-semantics-v1", "--output", str(root),
                    "--runtime", "/synthetic/runtime.json", "--execute-live", "--concurrency", "6"]
            with patch.object(cli, "_runtime", return_value={"synthetic": True}), \
                 patch.object(cli, "calibrate", return_value={"status": "passed", "models_called": False}) as execute:
                code, output, error = self.invoke(args)
            self.assertEqual((code, error), (0, ""))
            self.assertFalse(json.loads(output)["models_called"])
            execute.assert_called_once_with(root, {"synthetic": True}, suite="evidence-semantics-v1",
                                            maximum_sessions=6, wall_seconds=1800, stage_seconds=900, concurrency=6)

    def test_live_requires_runtime_and_prepare_cannot_be_combined_with_live(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = ["calibrate", "--output", str(Path(temporary) / "new")]
            with patch.object(cli, "_runtime", side_effect=AssertionError("missing runtime accessed")):
                code, output, error = self.invoke(args + ["--execute-live"])
            self.assertEqual(code, 2)
            self.assertIn("--runtime is required", error)
            self.assertEqual(output, "")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                cli.parser().parse_args(args + ["--prepare-only", "--execute-live"])
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(cli.parser().parse_args(args).suite, "initial-v1")
            self.assertEqual(cli.parser().parse_args(args).concurrency, 3)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.parser().parse_args(args + ["--suite", "typo"])


if __name__ == "__main__":
    unittest.main()
