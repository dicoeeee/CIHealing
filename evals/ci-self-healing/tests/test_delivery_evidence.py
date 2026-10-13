"""Synthetic lifecycle tests; no model calls and no historical result rewrites."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import changes, digest, json_bytes, read_json, read_regular, snapshot, write_json, write_new
from delivery_evidence import assess_delivery, readonly_shell, reviewable_timeout
from generation_budget import generation_failure
from grading import combined_status, grade_trial, normalize_trace
from repair_support import DELIVERY_CONTRACT, EXECUTION_CONTRACT, RUNTIME_CONTRACT, session_receipt, source_state
from review import make_packet
from suite import REPO, prepare
from test_eval import STOP_REPORT, encode_trace


class DeliveryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="stop-delivery-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "suite"
        manifest = prepare(self.root, REPO / "skills/ci-self-healing", repeats=1, mode="single", case_ids=["case-003"], backend="native")
        self.row = manifest["trials"][0]
        self.trial = self.root / "trials" / self.row["id"]
        self.visible = self.trial / "visible"
        self.before = snapshot(self.visible)
        self.previous = self.before
        write_json(self.trial / "before-run.json", self.before)
        write_json(self.root / "runtime-binding.json", {"synthetic": True})
        write_new(self.trial / "adapter/task.txt", "synthetic task")
        self.events = []
        self.event("observer.started", {"runtime_contract": RUNTIME_CONTRACT, "delivery_contract": DELIVERY_CONTRACT, "clock_id": "clock-test"})
        self.event("observer.skill_registration", {"confirmed": True})
        skill = self.trial / "runtime/skills/ci-self-healing"
        body = read_regular(REPO / "skills/ci-self-healing/SKILL.md")
        write_new(skill / "SKILL.md", body)
        write_new(skill / "assets/execution-resources.json", read_regular(REPO / "skills/ci-self-healing/assets/execution-resources.json"))
        self.call("load_skill", {"skill_name": "ci-self-healing"}, body.decode())
        self.event("InvocationMessage", {"text": "repair_admission:\n  decision: stop\n目标行为无法区分；无协作通道。"})
        self.report = self.visible / "output/analysis-fix.md"
        self.call("write_file", {"path": str(self.report), "content": STOP_REPORT}, "done",
                  lambda: write_new(self.report, STOP_REPORT))
        args = ["--report", str(self.report), "--classification", "none", "--checks", "none"]
        checked = subprocess.run([sys.executable, "-B", str(REPO / "skills/ci-self-healing/scripts/report_check.py"), *args], capture_output=True, text=True, timeout=10)
        self.assertEqual(checked.returncode, 0)
        self.call("run_skill_script", {"skill_name": "ci-self-healing", "script_name": "scripts/report_check.py", "arguments": args}, checked.stdout)
        self.call("read_file", {"path": str(self.report)}, "\n".join(f"{i}|{s}" for i, s in enumerate(STOP_REPORT.splitlines(), 1)))
        self.event("transport.started", {"request_seq": 2})
        self.event("observer.finished", {"returncode": 130, "result": {}, "observed_session_ids": ["session-test"],
                   "model_receipts": [{"session_id": "session-test", "model_id": "glm-5.2"}]})
        write_json(self.trial / "native-session/sessions/test/session.json", {"meta": {"session_id": "session-test", "model_id": "glm-5.2"}})
        write_json(self.trial / "session-receipt.json", session_receipt(self.trial / "native-session"))
        binding = {"trial_id": "trial-test", "gateway_id": "gateway-test", "clock_id": "clock-test"}
        raw = [("gateway_ready", 0, 1, {}), ("request_started", 1, 2, {}),
               ("generation_request", 1, 3, {"max_output_tokens": 131072}),
               ("upstream_headers", 1, 4, {"http_status": 200}),
               ("generation_response", 1, 5, {"status": "completed", "reason": "none", "output_tokens": 20}),
               ("request_finished", 1, 6, {"outcome": "completed"}),
               ("request_started", 2, 500, {}),
               ("generation_request", 2, 501, {"max_output_tokens": 131072}),
               ("upstream_headers", 2, 502, {"http_status": 200}),
               ("downstream_disconnect", 2, 801, {"phase": "downstream_write", "error_type": "BrokenPipeError"}),
               ("generation_response", 2, 802, {"status": "unconfirmed", "reason": "unconfirmed", "output_tokens": None}),
               ("request_finished", 2, 803, {"outcome": "downstream_disconnect"})]
        gateway = [{"schema": "ci-gateway-event-v1", **binding, "seq": i, "monotonic_ns": ns,
                    "request_id": rid, "kind": kind, **fields} for i, (kind, rid, ns, fields) in enumerate(raw, 1)]
        self.run = {"status": "timeout", "backend": "native", "runtime_contract": RUNTIME_CONTRACT,
                    "delivery_contract": DELIVERY_CONTRACT, "skill_execution_contract": EXECUTION_CONTRACT,
                    "expected_model_id": "glm-5.2", "filesystem_status": "captured", "isolation_confirmed": True,
                    "gateway_cleanup_confirmed": True, "task_sha256": digest(b"synthetic task"),
                    "session_receipt_sha256": digest(read_regular(self.trial / "session-receipt.json")),
                    "generation_budget": {"contract": "profile-generation-v1", "profile_max_output_tokens": 131072, "effective_max_output_tokens": 131072},
                    "process": {"status": "timeout", "returncode": 130, "cleanup": {"status": "confirmed"}, "clock_id": "clock-test",
                                "identity": {k: binding[k] for k in ("trial_id", "gateway_id")},
                                "termination": {"reason": "timeout", "pid": 10, "pgid": 10, "signals": [
                                    {"reason": "timeout", "signal": "SIGINT", "requested_ns": 800, "completed_ns": 801, "delivery": "sent"}]}},
                    "runtime_diagnostics": {"gateway_evidence": {"schema": "ci-gateway-evidence-v1", "binding": binding, "events": gateway, "gaps": []}}}
        self.persist()

    def event(self, kind, payload):
        current = snapshot(self.visible)
        payload.update(source_state_sha256=digest(json_bytes(source_state(current))),
                       changed_paths_since_previous_event=changes(self.previous, current))
        if kind.startswith("Invocation"):
            payload["origin"] = {"session_id": "session-test"}
        self.previous = current
        seq = len(self.events) + 1
        self.events.append({"seq": seq, "observed_ns": seq * 10, "kind": kind, "payload": payload})

    def call(self, name, args, result, action=None):
        cid = str(len(self.events))
        self.event("InvocationToolCallStart", {"tool_name": name, "args": args, "call_id": cid})
        if action:
            action()
        self.event("InvocationToolCallResult", {"tool_name": name, "result": result, "call_id": cid})

    def persist(self):
        # Only ephemeral test fixtures are rewritten here, never real runs.
        p = self.trial / "execution/stdout.log"; p.parent.mkdir(exist_ok=True)
        raw = encode_trace(self.events); p.write_bytes(raw)
        self.run["stdout_sha256"] = digest(raw)
        p = self.trial / "after-run.json"; p.write_text(json.dumps(snapshot(self.visible)))

    def assess(self):
        self.persist()
        return assess_delivery(self.trial, self.run)

    def test_complete_stop_artifact_is_reviewable_but_not_execution_success(self):
        assessment = self.assess()
        self.assertEqual(assessment["gaps"], [])
        self.assertEqual(assessment["status"], "reviewable_stop_artifact")
        self.run["delivery_assessment"] = assessment
        self.assertTrue(reviewable_timeout(self.run))
        self.assertEqual(generation_failure(self.run), "generation_budget_unconfirmed")
        write_json(self.trial / "run.json", self.run)
        result = grade_trial(self.root, self.row, None)
        self.assertEqual(result["status"], "awaiting_review", result)
        self.assertEqual(result["execution_status"], "timeout")
        self.assertIn("agent_execution_failed", result["trace"]["gaps"])
        self.assertEqual(result["gaps"], [])
        write_json(self.trial / "grade.json", result)
        packet = make_packet(self.root, self.row)
        self.assertEqual(packet["deterministic"]["review_scope"], "completed_stop_artifact_not_normal_execution")
        self.assertEqual(combined_status(result, {"status": "validated", "verdict": {"verdict": "pass"}}), "incomplete")
        self.assertEqual(combined_status(result, {"status": "validated", "verdict": {"verdict": "fail"}}), "failed")

    def test_missing_wrong_or_conflicting_identity_is_not_recovered(self):
        for mutation in (lambda: self.events[-1]["payload"].update(model_receipts=[]),
                         lambda: self.events[-1]["payload"]["model_receipts"][0].update(model_id="other"),
                         lambda: self.events[2]["payload"].update(origin={"session_id": "other"}),
                         lambda: self.events[-1]["payload"].update(result={"session_id": "other"})):
            original = copy.deepcopy(self.events); mutation()
            self.assertNotEqual(self.assess()["status"], "reviewable_stop_artifact")
            self.events = original

    def test_report_stop_and_tools_are_required(self):
        for kind, mutation in [
            ("InvocationMessage", lambda p: p.update(text="will write repair_admission later")),
            ("InvocationMessage", lambda p: p.update(presentation={"provisional": True})),
            ("InvocationToolCallResult", lambda p: p.update(result="truncated")),
        ]:
            original = copy.deepcopy(self.events)
            candidates = [e for e in self.events if e["kind"] == kind]
            mutation((candidates[-1] if kind.endswith("Result") else candidates[0])["payload"])
            self.assertNotEqual(self.assess()["status"], "reviewable_stop_artifact")
            self.events = original
        self.events[-3]["kind"] = "omitted.result"
        self.assertIn("unfinished_tool", self.assess()["gaps"])

    def test_source_mutation_even_restored_and_post_checkpoint_action_rejected(self):
        original = copy.deepcopy(self.events)
        self.events[5]["payload"]["source_state_sha256"] = "changed"
        self.assertIn("source_state_changed_during_run", self.assess()["gaps"])
        self.events = original
        self.events[-2]["kind"] = "InvocationMessage"
        self.events[-2]["payload"].update(text="new direction")
        self.assertIn("actions_or_decisions_after_checkpoint", self.assess()["gaps"])

    def test_stale_report_hash_or_final_state_is_not_accepted(self):
        self.report.write_text(STOP_REPORT + "changed\n")
        self.assertNotEqual(self.assess()["status"], "reviewable_stop_artifact")

    def test_cleanup_legacy_and_early_or_unbound_cancel_fail_closed(self):
        original = copy.deepcopy(self.run)
        for mutate in (lambda: self.run.pop("delivery_contract"),
                       lambda: self.run.update(gateway_cleanup_confirmed=False),
                       lambda: self.run["process"].update(clock_id="other"),
                       lambda: self.run["process"]["termination"]["signals"][0].update(requested_ns=900, completed_ns=901)):
            mutate(); self.assertNotEqual(self.assess()["status"], "reviewable_stop_artifact")
            self.run = copy.deepcopy(original)

    def test_interrupted_generation_before_checkpoint_is_not_waived(self):
        events = self.run["runtime_diagnostics"]["gateway_evidence"]["events"]
        events[6]["monotonic_ns"] = 100
        self.assertIn("generation_prefix_or_cancelled_tail_unconfirmed", self.assess()["gaps"])

    def test_prior_incomplete_generation_cannot_be_hidden_by_a_later_report(self):
        self.run["runtime_diagnostics"]["gateway_evidence"]["events"][4].update(status="incomplete", reason="max_output_tokens")
        self.assertIn("generation_prefix_or_cancelled_tail_unconfirmed", self.assess()["gaps"])

    def test_empty_or_unordered_public_trace_fails_closed(self):
        original = copy.deepcopy(self.events)
        for events in ([], list(reversed(original))):
            self.events = events
            self.assertEqual(self.assess()["status"], "unconfirmed")

    def test_missing_receipts_and_final_source_edit_fail_closed(self):
        source = self.visible / "scene/sourcecode/src/main/java/example/Router.java"
        source.write_text(source.read_text() + "// synthetic change\n")
        self.assertIn("not_report_only_change", self.assess()["gaps"])
        (self.trial / "session-receipt.json").unlink()
        self.assertEqual(self.assess()["status"], "unconfirmed")

    def test_shell_filter_rejects_write_and_opaque_commands(self):
        for text in ('git status --short && git log -3 --oneline', 'ls -la', 'rg needle src', 'cat source.java'):
            self.assertTrue(readonly_shell(text), text)
        for text in ('mvn compile', 'git diff --output=x', 'python3 -c "print(1)"', 'cat > src.java',
                     'echo $(touch file)', 'git -c core.pager=evil log', 'rg --pre=evil x', 'rm x; git status'):
            self.assertFalse(readonly_shell(text), text)


if __name__ == "__main__":
    unittest.main()
