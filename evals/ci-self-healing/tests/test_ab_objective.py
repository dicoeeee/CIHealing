"""Host objective calibration only; synthetic traces are not Agent coverage."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import digest, snapshot, replace_json as write_json, write_new
from ab_runtime import objective
from repair_support import EXECUTION_CONTRACT
from test_eval import STOP_REPORT


class ABObjectiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ab-objective-")
        self.addCleanup(self.tmp.cleanup)
        self.trial = Path(self.tmp.name).resolve()
        self.config = {"python": sys.executable}
        self.audit_result = {"failures": [], "gaps": [], "builds": [], "resource_reads": []}
        self.audit = patch("skill_execution.audit_execution", return_value=self.audit_result).start()
        patch("generation_budget.generation_failure", return_value=None).start()
        self.addCleanup(patch.stopall)

    def fixture(self, group="main", formal=True):
        scenario = {"main": "case-003", "handoff": "empty", "keyword": "component-first", "enterprise": "output-independent"}[group]
        visible = self.trial / "visible"
        write_new(visible / "scene/sourcecode/input.txt", "unchanged")
        (visible / "output").mkdir()
        before = snapshot(visible)
        report = visible / "output" / ("analysis-fix.md" if formal else "result.md")
        body = STOP_REPORT if formal else "Ordinary diagnostic findings."
        write_new(report, body)
        events = []
        def call(name, args, result, changed=False):
            cid = str(len(events))
            events.append({"kind": "InvocationToolCallStart", "seq": len(events) + 1,
                           "payload": {"call_id": cid, "tool_name": name, "args": args}})
            payload = {"call_id": cid, "result": result}
            if changed:
                payload["changed_paths_since_previous_event"] = ["output/" + report.name]
            events.append({"kind": "InvocationToolCallResult", "seq": len(events) + 1, "payload": payload})
        call("write_file", {"path": str(report), "content": body},
             f"Written {len(body)} chars (1 lines) to {report}.", True)
        if formal:
            call("run_skill_script", {"skill_name": "ci-self-healing", "script_name": "scripts/report_check.py",
                 "arguments": ["--report", str(report), "--classification", "none", "--checks", "none"]},
                 json.dumps({"format_status": "valid", "sha256": digest(body.encode()), "errors": []}))
        call("read_file", {"path": str(report)}, body)
        write_json(self.trial / "ab-case.json", {"group": group, "scenario": scenario, "expected": "stop", "pipeline_report": formal})
        write_json(self.trial / "run.json", {"status": "completed", "process": {"returncode": 0, "cleanup": {"status": "confirmed"}},
                   "gateway_cleanup": {"status": "confirmed"}, "skill_execution_contract": EXECUTION_CONTRACT})
        write_json(self.trial / "public-trace.json", {"gaps": [], "events": events})
        write_json(self.trial / "before-run.json", before)
        write_json(self.trial / "after-run.json", snapshot(visible))
        write_new(self.trial / "case-access.jsonl", json.dumps({"kind": "search", "args": {"query": "401"}, "result": {"status": "ok", "items": []}}) + "\n")
        return report, events

    def test_all_groups_receive_native_audit_with_trusted_report_scope(self):
        for group, formal in (("main", True), ("handoff", True), ("keyword", False), ("enterprise", False)):
            with self.subTest(group=group), tempfile.TemporaryDirectory(dir=self.tmp.name) as directory:
                self.trial = Path(directory).resolve()
                self.fixture(group, formal)
                with patch("handoff_cases.measure_coverage", return_value={"status": "covered", "gaps": []}), \
                     patch("enterprise_cases.audit", return_value={"failures": [], "gaps": []}):
                    result = objective(self.trial, self.config)
                self.assertEqual(result["status"], "pass", result)
                self.assertIs(self.audit.call_args.kwargs["pipeline_report"], formal)
                self.assertEqual(self.audit.call_args.args[3], "stop")

    def test_report_presence_and_host_format_pass_do_not_replace_native_readback(self):
        _, events = self.fixture()
        events = events[:-2]
        write_json(self.trial / "public-trace.json", {"gaps": [], "events": events})
        result = objective(self.trial, self.config)
        self.assertTrue(any(c["returncode"] == 0 for c in result["report_check"]["attempts"]))
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("formal_report_final_check_or_readback_unconfirmed", result["gaps"])

    def test_parallel_final_check_and_readback_are_valid(self):
        _, events = self.fixture()
        events = events[:2] + [events[i] for i in (2, 4, 5, 3)]
        for seq, event in enumerate(events, 1):
            event["seq"] = seq
        write_json(self.trial / "public-trace.json", {"gaps": [], "events": events})
        self.assertEqual(objective(self.trial, self.config)["status"], "pass")

    def test_changed_final_bytes_invalidate_old_receipts(self):
        report, _ = self.fixture()
        report.write_text(STOP_REPORT + "\n")
        write_json(self.trial / "after-run.json", snapshot(self.trial / "visible"))
        result = objective(self.trial, self.config)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("formal_report_final_check_or_readback_unconfirmed", result["gaps"])

    def test_native_loading_violation_remains_hard_failure_in_ordinary_branch(self):
        self.fixture("keyword", False)
        self.audit_result["failures"].append({"check": "required_skill_resource_not_loaded_before_action:references/invocation-inputs.md"})
        result = objective(self.trial, self.config)
        self.assertEqual(result["status"], "fail")
        self.assertNotIn("report_check", result)

    def test_missing_ordinary_result_is_not_a_pass_or_formal_report_requirement(self):
        report, _ = self.fixture("keyword", False)
        report.unlink()
        write_json(self.trial / "after-run.json", snapshot(self.trial / "visible"))
        result = objective(self.trial, self.config)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("ordinary_result_missing_or_empty", result["gaps"])
        self.assertNotIn("formal_report_file_missing", result["gaps"])

    def test_unexposed_handoff_target_cannot_pass(self):
        self.fixture("handoff", True)
        with patch("handoff_cases.measure_coverage", return_value={"status": "not_covered", "gaps": []}):
            result = objective(self.trial, self.config)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("case_target_not_covered", result["gaps"])

    def test_timeout_retains_incomplete_even_with_complete_report(self):
        self.fixture()
        run = json.loads((self.trial / "run.json").read_text())
        run.update(status="timeout")
        run["process"]["returncode"] = 130
        write_json(self.trial / "run.json", run)
        self.assertEqual(objective(self.trial, self.config)["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
