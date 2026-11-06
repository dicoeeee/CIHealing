"""Synthetic ordering calibration; no actual model or service invocation."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ab_loading_audit import RULES, audit_events, audit_trial, fixture_bindings
from common import read_regular, write_json, write_new


class LoadingPostAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.trial = Path(self.temp.name).resolve() / "trial"
        self.row = {"group": "handoff", "scenario": "adopt"}
        self.bindings = fixture_bindings(self.trial, self.row)
        self.skill = Path(self.bindings["skill_root"])
        write_new(self.skill / "SKILL.md", "---\nai_generated: true\nreviewed: false\n---\n" + "\n".join(r["clause"] for r in RULES.values()))
        for rule in RULES.values():
            write_new(self.skill / rule["guide"], "# Synthetic guide\n" + rule["guide"] + "\n")
        self.events = []

    def event(self, kind, payload):
        n = len(self.events) + 1
        self.events.append({"seq": n, "evidence_id": f"E{n:04}", "kind": kind, "payload": payload})

    def start_guide(self, rule, call="guide"):
        name = RULES[rule]["guide"]
        self.event("InvocationToolCallStart", {"call_id": call, "tool_name": "read_skill_resource",
            "args": {"skill_name": "ci-self-healing", "resource_name": name}})

    def finish_guide(self, rule, call="guide", full=True):
        body = read_regular(self.skill / RULES[rule]["guide"]).decode()
        self.event("InvocationToolCallResult", {"call_id": call, "result": body if full else body[:10]})

    def read(self, path, tool="read_file"):
        self.event("InvocationToolCallStart", {"call_id": "action", "tool_name": tool, "args": {"path": str(path)}})

    def assess(self, contract="skill-execution-v7", **kwargs):
        return audit_events(self.events, self.skill, self.bindings, execution_contract=contract, **kwargs)

    def test_complete_profile_guide_before_bound_read_passes(self):
        rule = "classification_before_bound_profile"
        self.start_guide(rule); self.finish_guide(rule)
        self.read(self.bindings["profiles"][0])
        result = self.assess()
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["actions"][0]["earliest_complete_result_seq"], 2)

    def test_same_batch_dependent_read_is_early_even_when_result_comes_later(self):
        rule = "classification_before_bound_profile"
        self.start_guide(rule); self.read(self.bindings["profiles"][0]); self.finish_guide(rule)
        result = self.assess()
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["violations"][0]["action_seq"], 2)
        self.assertIsNone(result["violations"][0]["earliest_complete_result_seq"])

    def test_prior_complete_read_can_be_reused_during_redundant_parallel_read(self):
        rule = "case_guide_before_bound_source"
        self.start_guide(rule, "first"); self.finish_guide(rule, "first")
        self.start_guide(rule, "redundant"); self.read(self.bindings["case_sources"][0]); self.finish_guide(rule, "redundant")
        result = self.assess()
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["actions"][0]["earliest_complete_result_seq"], 2)

    def test_no_branch_and_unbound_same_named_profile_do_not_require_guide(self):
        self.bindings = fixture_bindings(self.trial, {"group": "handoff", "scenario": "empty"})
        self.read(self.trial / "visible/scene/profile.txt")
        result = self.assess()
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["rules"]["classification_before_bound_profile"]["status"], "not_applicable")
        self.assertEqual(result["rules"]["case_guide_before_bound_source"]["status"], "not_applicable")

    def test_original_or_host_read_entry_cannot_substitute_agent_full_return(self):
        rule = "case_guide_before_bound_source"
        self.start_guide(rule); self.finish_guide(rule, full=False); self.read(self.bindings["case_sources"][0])
        result = self.assess(resource_reads=[{"resource": RULES[rule]["guide"], "complete": True, "seq": 1}])
        self.assertEqual(result["status"], "fail")

    def test_output_gate_is_identical_for_v6_and_v7(self):
        self.read(self.trial / "visible/output/result.md", tool="write_file")
        for contract in ("skill-execution-v6", "skill-execution-v7", "skill-execution-v8"):
            self.assertEqual(self.assess(contract)["status"], "fail")
        self.events = []
        self.start_guide("output_guide_before_output_action"); self.finish_guide("output_guide_before_output_action")
        self.read(self.trial / "visible/output/result.md", tool="write_file")
        for contract in ("skill-execution-v6", "skill-execution-v7", "skill-execution-v8"):
            self.assertEqual(self.assess(contract)["status"], "pass")

    def test_independent_guides_can_load_in_parallel_before_their_dependents(self):
        a, b = "classification_before_bound_profile", "case_guide_before_bound_source"
        self.start_guide(a, "a"); self.start_guide(b, "b")
        self.finish_guide(b, "b"); self.finish_guide(a, "a")
        self.read(self.bindings["profiles"][0]); self.read(self.bindings["case_sources"][0])
        self.assertEqual(self.assess()["status"], "pass")

    def test_trace_gap_cannot_prove_absence_of_an_earlier_read(self):
        self.read(self.bindings["profiles"][0])
        result = self.assess(trace_gaps=["missing_event"])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["violations"], [])

    def test_snapshot_is_immutable_and_keeps_original_artifacts_unchanged(self):
        self.read(self.bindings["profiles"][0])
        write_json(self.trial / "run.json", {"skill_execution_contract": "skill-execution-v6"})
        write_json(self.trial / "public-trace.json", {"events": self.events, "gaps": []})
        write_json(self.trial / "objective.json", {"status": "pass"})
        original = read_regular(self.trial / "objective.json")
        a = audit_trial(self.trial, self.row, snapshot_root=self.trial / "analysis")
        b = audit_trial(self.trial, self.row, snapshot_root=self.trial / "analysis")
        self.assertEqual(a["snapshot_path"], b["snapshot_path"])
        self.assertEqual(a["status"], "fail")
        self.assertEqual(read_regular(self.trial / "objective.json"), original)


if __name__ == "__main__":
    unittest.main()
