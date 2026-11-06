"""Synthetic offline aggregation checks, not additional Agent executions."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ab_compare import CRITERIA, MODEL_ID, RESPONSE_MODEL_ID, compare, lease_summary, usage_metadata, write_outputs
from common import digest, read_regular, replace_json, write_json, write_new


class ABComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.post_audit = patch("ab_compare.audit_trial", return_value={"status": "pass", "violations": []}).start()
        self.addCleanup(patch.stopall)

    def plan(self, rows):
        write_json(self.root / "plan.json", {"versions": ["A", "B"], "trials_per_version": rows, "executions": len(rows) * 2})

    def row(self, name="example", repeat=1):
        return {"id": f"keyword--{name}--r{repeat}", "group": "keyword", "scenario": name, "repeat": repeat}

    def completed(self, version, row, status="pass"):
        trial = self.root / version / "trials" / row["id"]
        write_json(trial / "run.json", {"status": "completed", "process": {"status": "completed", "returncode": 0, "duration_seconds": 12.5}})
        write_json(trial / "objective.json", {"status": status, "gaps": [], "failures": [], "pipeline_report": False,
            "execution_checks": {"dimensions": {"native_protocol": {"status": "passed"}}}})
        checks = [{"id": key, "status": "pass", "evidence": ["D1"]} for key in CRITERIA]
        write_json(trial / "review/verdict.json", {"verdict": "pass", "checks": checks})
        write_json(trial / "review/result.json", {"status": "completed", "verdict": "pass", "process": {"status": "completed", "returncode": 0, "duration_seconds": 5.5}})
        write_json(trial / "review/execution/process.json", {"status": "completed", "returncode": 0, "duration_seconds": 5.5})
        write_json(trial / "review/exchange-receipt.json", {"status": "validated", "request_model": MODEL_ID,
            "response_model": RESPONSE_MODEL_ID, "exchange_count": 1})
        write_json(trial / "public-trace.json", {"events": [{"kind": "InvocationToolCallStart", "payload": {
            "tool_name": "read_skill_resource", "args": {"skill_name": "ci-self-healing", "resource_name": "references/example.md"}}}] * 2})
        return trial

    def test_all_84_slots_exist_before_any_version_prepared(self):
        rows = [self.row("s" + str(i), repeat) for i in range(30) for repeat in range(1, 4 if i < 6 else 2)]
        self.plan(rows)
        result = compare(self.root)
        self.assertEqual(result["expected"], {"unique_scenarios": 30, "trials": 84, "declared_executions": 84})
        self.assertEqual(result["pair_counts"], {"undetermined": 42})
        self.assertEqual(result["versions"]["A"]["stages"]["execution"], {"not_started": 42})
        self.assertIsNone(result["versions"]["A"]["usage"]["execute_usage"]["tokens"]["input_total"]["sum"])

    def test_paired_categories_and_repeated_identity_are_independent(self):
        rows = [self.row("same", i) for i in range(1, 5)]
        self.plan(rows)
        for row, a, b in zip(rows, ("fail", "pass", "fail", "pass"), ("fail", "fail", "pass", "pass")):
            self.completed("A", row, a); self.completed("B", row, b)
        result = compare(self.root)
        self.assertEqual([p["comparison"] for p in result["pairs"]], ["existing_failure", "new_regression", "improved", "stable_pass"])
        self.assertEqual(result["trials"][0]["performance"]["read_skill_resource_calls"], 2)
        self.assertEqual(result["trials"][0]["performance"]["distinct_resource_count"], 1)

    def test_missing_review_receipt_or_criterion_cannot_pass(self):
        row = self.row(); self.plan([row])
        a = self.completed("A", row); b = self.completed("B", row)
        (a / "review/exchange-receipt.json").unlink()
        verdict = json.loads((b / "review/verdict.json").read_text()); verdict["checks"].pop()
        replace_json(b / "review/verdict.json", verdict)
        result = compare(self.root)
        self.assertEqual(result["pair_counts"], {"undetermined": 1})
        self.assertTrue(all(r["status"] == "incomplete" and r["stages"]["loading"] == "incomplete" for r in result["trials"]))

    def test_one_successful_repeat_does_not_pass_unique_scenario(self):
        rows = [self.row("repeated", i) for i in range(1, 4)]
        self.plan(rows); self.completed("A", rows[0])
        result = compare(self.root)
        self.assertEqual(result["unique_scenario_status"]["A"], {"incomplete": 1})
        self.assertEqual(result["scenarios"][0]["versions"]["A"]["status"], {"pass": 1, "incomplete": 2})

    def test_timeout_remains_incomplete_even_with_passed_assessments(self):
        row = self.row(); self.plan([row])
        trial = self.completed("A", row)
        replace_json(trial / "run.json", {"status": "timeout", "process": {"status": "timeout", "returncode": -15}})
        result = compare(self.root)
        self.assertEqual(result["trials"][0]["status"], "incomplete")
        self.assertIn("execution:timeout", result["trials"][0]["uncovered"])

    def test_usage_exports_only_metadata_and_retains_missing_field_denominators(self):
        trial = self.root / "trial"
        write_json(trial / "session-receipt.json", {"sessions": [{"path": "s/session.json", "meta": {
            "session_id": "s", "model_id": "alias", "model_profile_id": "profile-1", "app_version": "fixture"}}]})
        rows = [{"event_type": "model.exchange.started", "operation_id": "x", "payload": {"request_model": "alias"}},
                {"event_type": "model.exchange.finished", "operation_id": "x", "payload": {"response_model": "dated-model",
                    "usage": {"normalized": {"input_total": 100, "output_total": 20, "reasoning": 8}}, "reasoning": "DO_NOT_EXPORT"}},
                {"event_type": "trajectory.runtime.finished", "payload": {"reason": "graceful_shutdown"}}]
        write_new(trial / "native-session/s/trajectory/events.jsonl", "\n".join(json.dumps({"session_id": "s", "runtime_id": "r", **r}) for r in rows))
        result = usage_metadata(trial, "execute")
        self.assertNotIn("DO_NOT_EXPORT", json.dumps(result))
        self.assertEqual(result["normalized_tokens"]["output_total"]["sum"], 20)
        self.assertIsNone(result["normalized_tokens"]["cache_read"]["sum"])
        self.assertEqual(result["models"], [{"request_model": "alias", "response_model": "dated-model"}])
        self.assertEqual(result["native_exchange_starts"], 1)

    def test_lease_overlap_cannot_be_hidden_by_same_slot_set(self):
        rows = [{"event": "acquired", "slot": 0, "owner_pid": p, "phase": "execute", "trial": str(p)} for p in (1, 2)]
        write_new(self.root / "icode-concurrency.jsonl", "\n".join(json.dumps(r) for r in rows))
        result = lease_summary(self.root)
        self.assertEqual(result["status"], "violation")
        self.assertIn("overlapping_slot", result["gaps"])

    def test_rerun_only_replaces_derived_outputs(self):
        row = self.row(); self.plan([row]); trial = self.completed("A", row)
        before = digest(read_regular(trial / "review/verdict.json"))
        for _ in range(2):
            write_outputs(self.root, compare(self.root))
        self.assertEqual(digest(read_regular(trial / "review/verdict.json")), before)
        report = read_regular(self.root / "analysis/report.md").decode()
        self.assertTrue(report.startswith("---\nai_generated: true\nreviewed: false\n---"))
        self.assertIn("不是生产故障统计", report)

    def disposition(self, trial):
        write_json(trial / "harness-disposition.json", {"classification": "invalid_harness_fixture",
            "reason": "confirmed host defect", "replacement_required": True})

    def replacement(self, row, source, target, revision=2):
        return {"version": "A", "id": row["id"], "original_trial": str(source), "replacement_trial": str(target),
                "reason": "confirmed host defect", "harness_revision": revision}

    def test_invalid_harness_fail_and_pass_are_both_excluded_without_replacement(self):
        rows = [self.row("failed"), self.row("green")]; self.plan(rows)
        for row, status in zip(rows, ("fail", "pass")):
            trial = self.completed("A", row, status); self.disposition(trial)
            self.completed("B", row)
        result = compare(self.root)
        self.assertEqual(result["versions"]["A"]["status"], {"invalid_harness": 2})
        self.assertEqual(result["pair_counts"], {"undetermined": 2})
        self.assertEqual({r["recorded_status"] for r in result["harness_history"]["invalid_trials"]}, {"pass", "fail"})
        self.assertEqual(result["harness_history"]["invalid_attempts_accounting"]["execution_attempts_started"], 2)
        self.assertEqual(result["versions"]["A"]["performance"]["execution_seconds"]["observed"], 0)
        self.assertEqual(result["harness_history"]["invalid_attempts_accounting"]["process_wall_time"]["execution_seconds"]["sum"], 25)

    def test_explicit_replacement_uses_primary_slot_but_preserves_extra_cost(self):
        row = self.row(); self.plan([row])
        original = self.completed("A", row, "fail"); self.disposition(original)
        target = self.completed("replacement-r2", row); self.completed("B", row)
        write_json(self.root / "replacements.json", {"entries": [self.replacement(row, original, target)]})
        result = compare(self.root)
        self.assertEqual(result["pair_counts"], {"stable_pass": 1})
        self.assertEqual(len(result["trials"]), 2)
        self.assertEqual(result["trials"][0]["trial_path"], str(target))
        history = result["harness_history"]
        self.assertEqual(history["registered_total_slots"], 3)
        self.assertEqual(history["registered_extra_slots"], 1)
        self.assertEqual(history["all_attempts_accounting"]["execution_attempts_started"], 3)
        self.assertEqual(history["invalid_trials"][0]["recorded_status"], "fail")
        self.assertEqual(history["all_attempts_accounting"]["process_wall_time"]["execution_seconds"]["sum"], 37.5)

    def test_incomplete_replacement_does_not_turn_original_fail_into_regression(self):
        row = self.row(); self.plan([row])
        original = self.completed("A", row, "fail"); self.disposition(original)
        target = self.completed("replacement-r2", row, "fail"); self.completed("B", row)
        (target / "review/exchange-receipt.json").unlink()
        write_json(self.root / "replacements.json", {"entries": [self.replacement(row, original, target)]})
        result = compare(self.root)
        self.assertEqual(result["trials"][0]["status"], "fail")
        self.assertFalse(result["trials"][0]["assessment_complete"])
        self.assertEqual(result["pair_counts"], {"undetermined": 1})

    def test_replacement_requires_disposition_and_is_not_arbitrary_replay(self):
        row = self.row(); self.plan([row])
        original = self.completed("A", row, "fail"); target = self.completed("replacement-r2", row)
        write_json(self.root / "replacements.json", {"entries": [self.replacement(row, original, target)]})
        with self.assertRaisesRegex(ValueError, "disposition"):
            compare(self.root)

    def test_multirevision_chain_retains_both_invalid_attempts(self):
        row = self.row(); self.plan([row])
        original = self.completed("A", row, "fail"); self.disposition(original)
        second = self.completed("replacement-r2", row, "fail"); self.disposition(second)
        third = self.completed("replacement-r3", row); self.completed("B", row)
        write_json(self.root / "replacements.json", {"entries": [self.replacement(row, original, second),
            self.replacement(row, second, third, revision=3)]})
        result = compare(self.root)
        self.assertEqual(result["pair_counts"], {"stable_pass": 1})
        self.assertEqual(result["harness_history"]["registered_total_slots"], 4)
        self.assertEqual(len(result["harness_history"]["invalid_trials"]), 2)

    def test_host_skip_result_is_not_a_model_review_start(self):
        row = self.row(); self.plan([row])
        trial = self.root / "A/trials" / row["id"]
        write_json(trial / "review/result.json", {"status": "not_run_harness_invalid", "verdict": None})
        self.disposition(trial)
        result = compare(self.root)
        self.assertEqual(result["harness_history"]["invalid_attempts_accounting"]["review_attempts_started"], 0)
        self.assertEqual(result["harness_history"]["invalid_attempts_accounting"]["host_reviews_skipped"], 1)
        self.assertEqual(result["overall_model_cost"]["review_attempts_started"], 0)

    def test_uniform_postaudit_hard_failure_overrides_model_pass_without_editing_it(self):
        row = self.row(); self.plan([row]); trial = self.completed("A", row)
        verdict_before = read_regular(trial / "review/verdict.json")
        self.post_audit.return_value = {"status": "fail", "violations": [{"action_seq": 10}]}
        result = compare(self.root)
        a = result["trials"][0]
        self.assertEqual(a["status"], "fail")
        self.assertEqual(a["stages"]["loading"], "fail")
        self.assertEqual(a["stages"]["review"], "pass")
        self.assertEqual(a["before_post_audit_status"], "pass")
        self.assertEqual(read_regular(trial / "review/verdict.json"), verdict_before)

    def test_collection_status_and_effective_opinion_are_separate(self):
        row = self.row(); self.plan([row]); trial = self.completed("A", row)
        opinion = json.loads(read_regular(trial / "review/verdict.json"))
        replace_json(trial / "review/result.json", {"status": "incomplete", "verdict": None, "gaps": ["unknown review evidence"]})
        correction = {"status": "corrected", "original_opinion": opinion, "effective_opinion": opinion,
                      "effective_review": {"status": "completed", "verdict": "pass"}}
        with patch("ab_compare.resolve_collection", return_value=correction):
            result = compare(self.root)
        a = result["trials"][0]
        self.assertEqual(a["original_review_status"], "incomplete")
        self.assertIsNone(a["recorded_review_verdict"])
        self.assertEqual(a["effective_review"]["verdict"], "pass")
        self.assertEqual(a["original_model_opinion"], "pass")
        self.assertEqual(a["status"], "pass")

    def test_known_failure_with_uncollected_review_is_not_a_paired_regression(self):
        row = self.row(); self.plan([row]); self.completed("A", row)
        trial = self.completed("B", row, "fail")
        for name in ("result.json", "verdict.json", "exchange-receipt.json"):
            (trial / "review" / name).unlink()
        result = compare(self.root)
        b = result["trials"][1]
        self.assertEqual(b["status"], "fail")  # Keep the confirmed objective failure.
        self.assertEqual(b["lifecycle"], "review_finished_missing_result")
        self.assertFalse(b["assessment_complete"])
        self.assertFalse(b["decision_complete"])
        self.assertEqual(result["pair_counts"], {"undetermined": 1})

    def test_missing_required_delivery_does_not_hide_a_decidable_failure(self):
        row = self.row(); self.plan([row]); self.completed("A", row)
        trial = self.completed("B", row, "fail")
        objective = json.loads(read_regular(trial / "objective.json"))
        objective.update(gaps=["enterprise_output_missing:handoff.json"], failures=[{"check": "protected_output_changed"}])
        replace_json(trial / "objective.json", objective)
        opinion = json.loads(read_regular(trial / "review/verdict.json"))
        opinion["checks"][0]["status"] = "fail"; opinion["verdict"] = "fail"
        replace_json(trial / "review/verdict.json", opinion)
        review = json.loads(read_regular(trial / "review/result.json")); review["verdict"] = "fail"
        replace_json(trial / "review/result.json", review)
        self.post_audit.side_effect = lambda p, *_args, **_kwargs: {
            "status": "fail" if Path(p).parent.parent.name == "B" else "pass", "violations": []}
        result = compare(self.root)
        b = result["trials"][1]
        self.assertEqual(b["status"], "fail")
        self.assertFalse(b["assessment_complete"])
        self.assertTrue(b["decision_complete"])
        self.assertEqual(set(b["decision_failure_basis"]), {"objective_failure", "model_failure", "post_loading_failure"})
        self.assertEqual(result["pair_counts"], {"new_regression": 1})

    def test_corrupt_receipt_does_not_gain_decision_completeness_from_model_fail(self):
        row = self.row(); self.plan([row]); self.completed("A", row)
        trial = self.completed("B", row, "fail")
        opinion = json.loads(read_regular(trial / "review/verdict.json"))
        opinion["checks"][0]["status"] = "fail"; opinion["verdict"] = "fail"
        replace_json(trial / "review/verdict.json", opinion)
        review = json.loads(read_regular(trial / "review/result.json")); review["verdict"] = "fail"
        replace_json(trial / "review/result.json", review)
        (trial / "objective.json").write_text("invalid JSON")
        result = compare(self.root)
        b = result["trials"][1]
        self.assertEqual(b["status"], "fail")
        self.assertFalse(b["decision_complete"])
        self.assertEqual(result["pair_counts"], {"undetermined": 1})

    def test_unstarted_and_finished_evidence_gap_have_distinct_lifecycles(self):
        rows = [self.row("pending"), self.row("gap")]; self.plan(rows)
        trial = self.completed("A", rows[1])
        replace_json(trial / "objective.json", {"status": "incomplete", "gaps": ["final_readback_missing"]})
        result = compare(self.root)
        pending, gap = result["trials"][:2]
        self.assertEqual(pending["lifecycle"], "not_started")
        self.assertEqual(gap["lifecycle"], "finished_with_evidence_gaps")
        self.assertEqual(gap["status"], "incomplete")

    def test_process_receipt_preserves_cost_when_final_run_receipt_is_missing(self):
        row = self.row(); self.plan([row])
        trial = self.root / "A/trials" / row["id"]
        write_json(trial / "execution/process.json", {"status": "completed", "returncode": 0, "duration_seconds": 18})
        result = compare(self.root)
        a = result["trials"][0]
        self.assertEqual(a["lifecycle"], "execution_finished_missing_run_evidence")
        self.assertEqual(a["performance"]["execution_seconds"], 18)
        self.assertEqual(result["harness_history"]["all_attempts_accounting"]["execution_attempts_started"], 1)
        self.assertEqual(result["overall_model_cost"]["execution_wall_time"]["sum"], 18)

    def test_wrong_return_model_cannot_keep_a_collected_pass(self):
        row = self.row(); self.plan([row]); trial = self.completed("A", row)
        receipt = json.loads(read_regular(trial / "review/exchange-receipt.json"))
        receipt["response_model"] = "wrong-model"
        replace_json(trial / "review/exchange-receipt.json", receipt)
        result = compare(self.root)
        self.assertEqual(result["trials"][0]["status"], "incomplete")
        self.assertEqual(result["trials"][0]["recorded_review_verdict"], "pass")
        self.assertFalse(result["trials"][0]["review_evidence_valid"])

    def test_duplicate_session_receipt_does_not_double_count_tokens(self):
        trial = self.root / "trial"
        session = {"path": "s/session.json", "meta": {"session_id": "s"}}
        write_json(trial / "session-receipt.json", {"sessions": [session, session]})
        events = [{"event_type": "model.exchange.started", "operation_id": "x", "payload": {"request_model": "alias"}},
                  {"event_type": "model.exchange.finished", "operation_id": "x", "payload": {"response_model": "dated",
                   "usage": {"normalized": {"input_total": 10, "output_total": 2}}}},
                  {"event_type": "trajectory.runtime.finished", "payload": {"reason": "graceful_shutdown"}}]
        write_new(trial / "native-session/s/trajectory/events.jsonl", "\n".join(json.dumps({"session_id": "s", "runtime_id": "r", **e}) for e in events))
        usage = usage_metadata(trial, "execute")
        self.assertEqual(usage["session_count"], 1)
        self.assertEqual(usage["normalized_tokens"]["input_total"]["sum"], 10)
        self.assertIn("duplicate_session_receipt", usage["gaps"])

    def test_84_primary_and_12_replacements_do_not_become_96_started_trials(self):
        rows = [self.row("s" + str(i), repeat) for i in range(30) for repeat in range(1, 4 if i < 6 else 2)]
        self.plan(rows)
        entries = []
        for row in rows[:12]:
            original = self.completed("A", row, "fail"); self.disposition(original)
            replacement = self.completed("supplemental", row)
            entries.append(self.replacement(row, original, replacement))
        write_json(self.root / "replacements.json", {"entries": entries})
        result = compare(self.root)
        history = result["harness_history"]
        self.assertEqual(len(result["trials"]), 84)
        self.assertEqual(history["registered_total_slots"], 96)
        self.assertEqual(history["registered_extra_slots"], 12)
        self.assertEqual(history["all_attempts_accounting"]["execution_attempts_started"], 24)
        self.assertEqual(history["all_attempts_accounting"]["existing_trial_directories"], 24)
        self.assertEqual(result["pair_counts"], {"undetermined": 42})

    def test_calibration_counts_four_processes_not_reused_result_rows(self):
        row = self.row(); self.plan([row])
        names = ("honest-pass", "false-success", "missing-evidence", "missing-report-evidence-v2")
        for name in names:
            target = self.root / "calibration" / name
            write_json(target / "execution/process.json", {"status": "completed", "duration_seconds": 2})
            write_json(target / "result.json", {"status": "completed", "verdict": "pass"})
            write_json(target / "session-receipt.json", {"sessions": [{"path": "s/session.json", "meta": {"session_id": name}}]})
            events = [{"event_type": "model.exchange.started", "operation_id": "x", "payload": {"request_model": "alias"}},
                {"event_type": "model.exchange.finished", "operation_id": "x", "payload": {"response_model": "dated",
                    "usage": {"normalized": {"input_total": 10, "output_total": 5}}}},
                {"event_type": "trajectory.runtime.finished", "payload": {"reason": "graceful_shutdown"}}]
            write_new(target / "sessions/s/trajectory/events.jsonl", "\n".join(json.dumps({"session_id": name, "runtime_id": "r", **e}) for e in events))
        for filename, selected in (("calibration-result.json", names[:3]), ("calibration-result-v2.json", (names[0], names[1], names[3]))):
            write_json(self.root / filename, {"cases": [{"name": name, "actual": "pass"} for name in selected]})
        # No-model diagnostic directories are outside the calibration process inventory.
        write_json(self.root / "diagnostics/mcp-canary/result.json", {"status": "passed"})
        result = compare(self.root)
        self.assertEqual(result["calibration"]["review_processes_started"], 4)
        self.assertEqual(result["overall_model_cost"]["review_attempts_started"], 4)
        self.assertEqual(result["calibration"]["usage"]["tokens"]["input_total"]["sum"], 40)
        self.assertEqual(result["overall_model_cost"]["review_usage"]["tokens"]["output_total"]["sum"], 20)
        self.assertEqual(result["expected"]["trials"], 2)
        self.assertEqual(result["versions"]["A"]["status"], {"incomplete": 1})


if __name__ == "__main__":
    unittest.main()
