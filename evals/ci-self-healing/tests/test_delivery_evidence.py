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
from delivery_evidence import artifact_checkpoint, audit_handoff_events, assess_delivery, readonly_shell, reviewable_timeout
from generation_budget import generation_failure
from grading import combined_status, grade_trial, normalize_trace
from repair_support import DELIVERY_CONTRACT, EXECUTION_CONTRACT, RUNTIME_CONTRACT, session_receipt, source_state
from review import make_packet
from suite import REPO, prepare, bundle_hash, LEGACY_CHECKER_FILES
from test_eval import STOP_REPORT, encode_trace


class DeliveryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="stop-delivery-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "suite"
        manifest = prepare(self.root, REPO / "skills/ci-self-healing", repeats=1, mode="single", case_ids=["case-003"], backend="native")
        if not getattr(self, "manifest_delivery", False):
            # This fixture exercises historical stop-artifact-v1 receipts. Keep
            # its suite envelope at the original two-file checker contract.
            manifest.pop("delivery_contracts", None)
            manifest.pop("checker_files", None)
            manifest["checker_sha256"] = bundle_hash({name: read_regular(self.root / "checker" / name)
                                                       for name in LEGACY_CHECKER_FILES})
            (self.root / "suite.json").write_text(json.dumps(manifest))
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
        inputs = read_regular(REPO / "skills/ci-self-healing/references/invocation-inputs.md")
        write_new(skill / "references/invocation-inputs.md", inputs)
        self.call("read_skill_resource", {"skill_name": "ci-self-healing", "resource_name": "references/invocation-inputs.md"}, inputs.decode())
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

    def test_check_and_readback_interleaving_preserves_delivery_audit(self):
        original = copy.deepcopy(self.events)
        # Both starts precede either result: native tools from one readonly batch.
        first = next(i for i, e in enumerate(original) if e["kind"] == "InvocationToolCallStart"
                     and e["payload"]["tool_name"] == "run_skill_script")
        for order in ((0, 2, 1, 3), (0, 2, 3, 1)):
            with self.subTest(order=order):
                self.events = copy.deepcopy(original)
                block = self.events[first:first + 4]
                self.events[first:first + 4] = [block[i] for i in order]
                for seq, event in enumerate(self.events, 1):
                    event.update(seq=seq, observed_ns=seq * 10)
                result = self.assess()
                self.assertEqual(result["gaps"], [])
                self.assertEqual(result["status"], "reviewable_stop_artifact")
                self.assertEqual(result["checkpoint_seq"], max(result["check_seq"], result["readback_seq"]))

    def test_check_and_readback_batch_does_not_waive_missing_result(self):
        start = next(e for e in self.events if e["kind"] == "InvocationToolCallStart"
                     and e["payload"]["tool_name"] == "read_file")
        for event in self.events:
            if event["kind"] == "InvocationToolCallResult" and event["payload"]["call_id"] == start["payload"]["call_id"]:
                event["kind"] = "omitted.result"
        result = self.assess()
        self.assertNotEqual(result["status"], "reviewable_stop_artifact")
        self.assertIn("unfinished_tool", result["gaps"])

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


class HandoffEvidenceTests(unittest.TestCase):
    """Focused audit checks are offline evidence, not semantic model judgments."""

    def setUp(self):
        self.events = []
        self.path = "/trial/visible/output/analysis-fix.md"
        self.body = b"line one\nline two\n"

    def event(self, kind, **payload):
        seq = len(self.events) + 1
        self.events.append({"seq": seq, "kind": kind, "payload": payload, "evidence_id": f"E{seq:04d}"})
        return seq

    def call(self, name, args, result, **extra):
        call_id = str(len(self.events))
        self.event("InvocationToolCallStart", call_id=call_id, tool_name=name, args=args)
        return self.event("InvocationToolCallResult", call_id=call_id, result=result, **extra)

    def write(self):
        content = self.body.decode()
        self.call("write_file", {"path": self.path, "content": content},
                  f"Written {len(content)} chars (2 lines) to {self.path}.",
                  changed_paths_since_previous_event=["output/analysis-fix.md"])

    def check(self, classification="none", valid=True, suffix=""):
        value = {"format_status": "valid" if valid else "invalid", "sha256": digest(self.body),
                 "errors": [] if valid else [{"code": "classification_none"}]}
        self.call("run_skill_script", {"skill_name": "ci-self-healing", "script_name": "scripts/report_check.py",
                  "arguments": ["--report", self.path, "--classification", classification]}, json.dumps(value) + suffix)

    def read(self, text=None):
        self.call("read_file", {"path": self.path}, text if text is not None else self.body.decode())

    def checkpoint(self):
        return artifact_checkpoint(self.events, self.path, self.body, "scripts/report_check.py", {"--classification": "none"})

    def audit(self, report="", bound=False, guide_seq=0, contract="handoff-evidence-v1"):
        return audit_handoff_events(self.events, report, case_source_path="/scene/case-source.txt",
                                    profile_path="/scene/profile.txt", profile_bound=bound,
                                    resource_reads=[{"resource": "references/case-retrieval.md", "seq": guide_seq, "complete": True}],
                                    contract=contract)

    def failures(self, **kwargs):
        return {x["check"] for x in self.audit(**kwargs)["failures"]}

    def original(self, case_id="C1", **extra):
        self.call("cases_read_case", {"case_id": case_id}, json.dumps({"status": "ok", "complete": True,
                  "case_id": case_id, "url": f"https://cases.invalid/{case_id}", **extra}))

    def semantic_audit(self, report=""):
        from handoff_judgment import CONTRACT
        return self.audit(report=report, contract=CONTRACT)

    def judgments(self, audit, report, case_status="pass", applicability="applicable"):
        """Hand-authored oracle input, not a model or a semantic classifier."""
        from handoff_judgment import CRITERIA
        rows = [{"id": p["id"], "applicability": applicability if p["id"].startswith("case:") else "applicable",
                 "status": case_status if p["id"].startswith("case:") else "pass",
                 "reason": "人工标注此边界用例的适用性与结果。", "support": [{"evidence_id": "F1", "quote": report}],
                 "counterevidence": [], "counterevidence_note": "此合成对照未提供其他反证。"} for p in audit["pending_judgments"]]
        checks = [{"criterion": c, "status": case_status if c == "case_citations" else "pass"} for c in CRITERIA]
        return {"judgments": rows, "checks": checks,
                "overall": {"pass": "pass", "fail": "fail", "unknown": "inconclusive"}[case_status]}

    def test_v2_case_use_is_a_question_not_an_id_based_failure(self):
        from handoff_judgment import adjudicate
        self.original(title="浏览器登录失败")
        cases = [
            ("C1 与本次流水线无关，已排除。", "not_applicable", "pass", "passed"),
            ("请管理员照 C1 轮换凭据。", "applicable", "fail", "failed"),
            ("未采用 C1 的修法，但以它排除机器凭据问题。", "applicable", "fail", "failed"),
            ("照《浏览器登录失败》的步骤处理。", "applicable", "fail", "failed"),
            ("借鉴刚读到的登录案例轮换凭据。", "applicable", "fail", "failed"),
        ]
        for text, applicability, status, expected in cases:
            with self.subTest(text=text):
                report = "## 1. 处理结论\n" + text
                audit = self.semantic_audit(report)
                self.assertEqual(audit["failures"], [])
                self.assertIn("case:C1", {p["id"] for p in audit["pending_judgments"]})
                result = adjudicate(audit, self.judgments(audit, report, status, applicability), [{"id": "F1", "content": report}])
                self.assertEqual(result["status"], expected, result)

    def test_v2_timestamp_mentions_and_negation_are_only_observations(self):
        self.original(read_at="2026-10-26T09:00:00Z", updated_at="2026-10-25T09:00:00Z")
        for text in ("C1 读取时间未知，不能使用 2026-10-25T09:00:00Z。",
                     "C1 读取时间：2026-10-25T09:00:00Z"):
            audit = self.semantic_audit("## 6. 剩余问题与交接\n" + text)
            self.assertEqual(audit["failures"], [])
            self.assertFalse(audit["observations"]["time_literals"][0]["matches_receipt_time"])
        audit = self.semantic_audit("## 6. 剩余问题与交接\nC1 版本时间：2026-10-25T09:00:00Z")
        self.assertEqual(audit["observations"]["time_literals"], [])
        audit = self.semantic_audit("## 6. 剩余问题与交接\nC1 读取时间：2026-10-26T17:00:00+08:00")
        self.assertTrue(audit["observations"]["time_literals"][0]["matches_receipt_time"])

    def test_v2_query_notes_are_extracted_without_assuming_the_last_message_is_the_note(self):
        self.call("cases_search_cases", {"query": "401"}, "{}")
        self.event("InvocationMessage", text="下次查询：机器凭据 401")
        self.event("InvocationMessage", text="本次仍保持停止。")
        self.call("cases_search_cases", {"query": "机器凭据 401"}, "{}")
        audit = self.semantic_audit()
        self.assertEqual(audit["failures"], [])
        self.assertTrue(audit["observations"]["queries"][1]["literal_query_in_notes"])
        self.assertIn("criterion:query_explanation", {p["id"] for p in audit["pending_judgments"]})

    def test_v2_hard_failures_remain_hard(self):
        for n in range(4):
            self.event("InvocationMessage", text=f"下一条 query{n}")
            self.call("cases_search_cases", {"query": f"query{n}"}, "{}")
            self.original(case_id=f"C{n}")
        self.call("cases_read_case", {"case_id": "C0"}, json.dumps({"status": "ok", "complete": True, "case_id": "wrong"}))
        audit = self.semantic_audit()
        self.assertEqual(audit["status"], "failed")
        self.assertTrue({"case_search_budget_exceeded", "case_original_budget_exceeded", "original_identity_mismatch"}
                        <= {f["check"] for f in audit["failures"]})

    def test_v2_resource_order_is_still_a_hard_failure(self):
        from handoff_judgment import CONTRACT
        self.call("read_file", {"path": "/scene/case-source.txt"}, "source")
        audit = self.audit(contract=CONTRACT, guide_seq=3)
        self.assertIn("case_source_before_complete_guide", {f["check"] for f in audit["failures"]})

    def test_v2_matching_query_text_does_not_establish_relevance(self):
        self.call("cases_search_cases", {"query": "Runner 磁盘压力"}, "{}")
        self.event("InvocationMessage", text="下一次查询：浏览器登录 401")
        self.call("cases_search_cases", {"query": "浏览器登录 401"}, "{}")
        audit = self.semantic_audit()
        self.assertEqual(audit["status"], "passed")
        self.assertTrue(audit["observations"]["queries"][1]["literal_query_in_notes"])
        self.assertIn("criterion:query_explanation", {p["id"] for p in audit["pending_judgments"]})

    def test_v2_unclear_keyword_does_not_resolve_conflicting_root_cause_claims(self):
        from handoff_judgment import adjudicate
        report = "## 1. 处理结论\n根因 unclear。\n## 2. 诊断\n编译通过，因此完全排除源码与构建配置原因。"
        audit = self.semantic_audit(report)
        answer = self.judgments(audit, report)
        row = next(r for r in answer["judgments"] if r["id"] == "criterion:root_cause_precision")
        row.update(status="fail", reason="前部声明未知不能支持后文排除所有源码与构建配置原因。",
                   counterevidence=[{"evidence_id": "F1", "quote": "根因 unclear。"}],
                   counterevidence_note="已考虑前部保留意见，但后文仍过度确定。")
        next(c for c in answer["checks"] if c["criterion"] == "root_cause_precision")["status"] = "fail"
        answer["overall"] = "fail"
        result = adjudicate(audit, answer, [{"id": "F1", "content": report}])
        self.assertEqual(audit["failures"], [])
        self.assertEqual(result["status"], "failed", result)

    def test_v2_unknown_or_missing_judgments_never_pass(self):
        from handoff_judgment import adjudicate
        self.original()
        report = "## 1. 处理结论\nC1 是否适用尚不能确定。"
        audit = self.semantic_audit(report)
        evidence = [{"id": "F1", "content": report}]
        self.assertEqual(adjudicate(audit, {"overall": "pass"}, evidence)["status"], "incomplete")
        verdict = self.judgments(audit, report, "unknown", "unknown")
        result = adjudicate(audit, verdict, evidence)
        self.assertEqual(result["status"], "incomplete", result)
        self.assertEqual(result["unresolved"], ["case:C1"])

    def test_v2_invalid_quotes_and_inconsistent_summaries_are_not_resolutions(self):
        from handoff_judgment import adjudicate
        self.original()
        report = "## 1. 处理结论\n参考 C1。"
        audit = self.semantic_audit(report)
        evidence = [{"id": "F1", "content": report}]
        for defect in ("quote", "reference", "duplicate", "foreign", "summary", "applicability", "counterevidence"):
            with self.subTest(defect=defect):
                verdict = self.judgments(audit, report, "fail")
                row = verdict["judgments"][-1]
                if defect == "quote": row["support"][0]["quote"] = "文件中没有这个句子"
                elif defect == "reference": row["support"][0]["evidence_id"] = "F404"
                elif defect == "duplicate": row["id"] = verdict["judgments"][0]["id"]
                elif defect == "foreign": row["id"] = "case:C404"
                elif defect == "summary": verdict["overall"] = "pass"
                elif defect == "applicability": row["applicability"] = "not_applicable"
                else: row["counterevidence_note"] = ""
                result = adjudicate(audit, verdict, evidence)
                self.assertEqual(result["status"], "incomplete", result)
                self.assertTrue(result["gaps"])

    def test_v2_aggregation_keeps_hard_failure_and_unknown_boundaries(self):
        from handoff_judgment import aggregate
        for semantic in ("passed", "failed", "incomplete"):
            self.assertEqual(aggregate("failed", semantic, "unknown"), "failed")
        self.assertEqual(aggregate("passed", "failed", "not_covered"), "failed")
        self.assertEqual(aggregate("passed", "passed", "covered"), "passed")
        self.assertEqual(aggregate("passed", "passed", "not_covered"), "incomplete")
        self.assertEqual(aggregate("passed", "incomplete", "covered"), "incomplete")

    def test_final_bytes_need_check_and_complete_readback(self):
        self.write(); self.check()
        self.assertEqual(self.checkpoint()["status"], "incomplete")
        self.read("1|line one")
        self.assertEqual(self.checkpoint()["status"], "incomplete")
        self.read("2|line two")
        self.assertEqual(self.checkpoint()["status"], "passed")

    def test_changed_report_needs_new_check_and_readback(self):
        self.write(); self.check(); self.read()
        self.body = b"line one\ncorrected\n"
        self.call("edit_file", {"path": self.path, "old_string": "line two", "new_string": "corrected"},
                  f"Replaced 1 occurrence(s) in {self.path}.", changed_paths_since_previous_event=["output/analysis-fix.md"])
        self.assertEqual(self.checkpoint()["status"], "incomplete")
        self.check(); self.read()
        self.assertEqual(self.checkpoint()["status"], "passed")

    def test_failed_write_or_check_receipt_never_passes(self):
        self.write(); self.check(); self.read()
        self.events[1]["payload"]["result"] = "write failed"
        self.assertEqual(self.checkpoint()["status"], "incomplete")
        self.events.clear(); self.write(); self.check(suffix="\n[exit_code: 1]"); self.read()
        self.assertEqual(self.checkpoint()["status"], "incomplete")
        self.events.clear(); self.write(); self.check(valid=False, suffix="\n[exit_code: 1]"); self.read()
        self.assertEqual(self.checkpoint()["status"], "failed")

    def test_flags_cannot_be_changed_to_bypass_requirement(self):
        self.write(); self.check(classification="required"); self.read()
        self.assertEqual(self.checkpoint()["status"], "incomplete")
        self.check()
        self.assertEqual(self.checkpoint()["status"], "passed")

    def test_source_order_and_unbound_profile_are_observed(self):
        self.call("read_file", {"path": "/scene/case-source.txt"}, "source")
        self.call("read_file", {"path": "/scene/profile.txt"}, "profile")
        self.assertEqual(self.failures(guide_seq=2), {"case_source_before_complete_guide", "unbound_profile_read"})
        self.assertEqual(self.audit(bound=True)["status"], "passed")

    def test_next_query_uses_public_text_and_previous_result(self):
        self.call("cases_search_cases", {"query": "runner disk pressure"}, '{"results": []}')
        self.event("InvocationMessage", text="无结果。本次查询「runner unavailable」，查找 Runner 不可用的处置。")
        self.call("cases_search_cases", {"query": "runner unavailable"}, '{"results": []}')
        self.assertEqual(self.audit()["status"], "passed")
        self.events[-2]["payload"]["args"]["query"] = "upload authentication"
        self.assertIn("query_text_differs_from_pre_search_note", self.failures())
        self.events = [self.events[0], self.events[-2]]
        self.assertIn("case_search_before_previous_result", self.failures())

    def test_search_and_original_budgets_do_not_reset(self):
        for i in range(4):
            self.event("InvocationMessage", text=f"query {i}")
            self.call("cases_search_cases", {"query": f"query {i}"}, '{}')
            self.original(f"C{i}")
        self.assertTrue({"case_search_budget_exceeded", "case_original_budget_exceeded"} <= self.failures())

    def test_original_link_and_read_time_must_have_actual_source(self):
        self.original()
        report = "## 1. 处理结论\n请管理员参考 C1。\n## 6. 剩余问题与交接\nC1 读取时间：2026-10-26T08:48:05Z"
        self.assertEqual(self.failures(report=report), {"case_original_link_missing_in_summary", "case_read_time_not_bound_to_original"})
        report = report.replace("参考 C1", "参考 [C1](https://cases.invalid/C1)").replace("2026-10-26T08:48:05Z", "未知：工具未返回")
        self.assertEqual(self.audit(report)["status"], "passed")
        self.assertIn("root-cause claims across all sections", self.audit(report)["semantic_review_required"])

    def test_timestamp_is_per_original_not_run_or_publication_time(self):
        stamp = "2026-10-26T08:48:05Z"
        self.original(read_at=stamp)
        report = f"## 6. 剩余问题与交接\n| ID | 版本时间 | 读取时间 |\n| C1 | 2020-01-01T00:00:00Z | {stamp} |"
        self.assertEqual(self.audit(report)["status"], "passed")
        self.original("C2", updated_at=stamp)
        report += f"\n| C2 | {stamp} | 读取时间：{stamp} |"
        self.assertIn("case_read_time_not_bound_to_original", self.failures(report=report))

    def test_original_identity_mismatch_is_not_usable_evidence(self):
        self.call("cases_read_case", {"case_id": "C1"}, '{"status":"ok","complete":true,"case_id":"C2"}')
        self.assertIn("original_identity_mismatch", self.failures())
        self.assertEqual(self.audit()["complete_original_ids"], [])


if __name__ == "__main__":
    unittest.main()
