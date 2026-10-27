"""Native review contract tests: no Chrys process, credentials, or network."""

import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import EvalError, digest, json_bytes, read_json, read_regular, snapshot, write_json, write_new
from review import CRITERIA, build_prompt, output_contract, schema, validate_verdict
from review_chrys import assess_native, assess_output, audit_handoff_trial, collect_native, main, prepare_native, strict_json
from test_eval import STOP_REPORT, encode_trace, packet, verdict


class FocusedHandoffAuditTests(unittest.TestCase):
    """Actual file/trace/review binding; the separately tested native audit is stubbed."""

    def setUp(self):
        from grading import normalize_trace
        self.temp = tempfile.TemporaryDirectory(prefix="focused-handoff-")
        self.addCleanup(self.temp.cleanup)
        self.focus = Path(self.temp.name).resolve()
        self.trial = self.focus / "trials/empty"
        output = self.trial / "visible/output"
        output.mkdir(parents=True)
        write_json(self.trial / "before-run.json", snapshot(self.trial / "visible"))
        write_json(self.focus / "focus-freeze.json", {"contract": "focused-handoff-evidence-v1", "skill_sha256": "synthetic",
                   "fixture_files": {"trials/empty/before-run.json": digest(read_regular(self.trial / "before-run.json"))}})
        self.events = []
        def event(kind, **payload):
            self.events.append({"seq": len(self.events) + 1, "kind": kind, "payload": payload})
        def call(name, args, result):
            call_id = str(len(self.events))
            event("InvocationToolCallStart", call_id=call_id, tool_name=name, args=args)
            event("InvocationToolCallResult", call_id=call_id, result=result)
        event("observer.started")
        files = []
        for index, (name, body, script, flags) in enumerate((
                ("file_change.json", '{"version":"0.1","changes":[]}', "file_change_check.py", []),
                ("analysis-fix.md", STOP_REPORT, "report_check.py", ["--classification", "none", "--checks", "none"]))):
            path = output / name
            write_new(path, body)
            call("write_file", {"path": str(path), "content": body}, f"Written {len(body)} chars (1 lines) to {path}.")
            call("run_skill_script", {"skill_name": "ci-self-healing", "script_name": "scripts/" + script,
                 "arguments": ["--manifest" if index == 0 else "--report", str(path), *flags]},
                 json.dumps({"format_status": "valid", "sha256": digest(body.encode()), "errors": []}))
            call("read_file", {"path": str(path)}, body)
            files.append({"id": f"F{index}", "path": "visible/output/" + name, "content": body, "sha256": digest(body.encode())})
        event("observer.finished", returncode=0, result={"session_id": "synthetic"},
              model_receipts=[{"session_id": "synthetic", "model_id": "glm-5.2"}])
        self.run = {"stdout_sha256": "", "skill_sha256": "synthetic", "skill_execution_contract": "synthetic",
                    "process": {"status": "completed", "returncode": 0, "cleanup": {"status": "confirmed"}}}
        write_json(self.trial / "after-run.json", snapshot(self.trial / "visible"))
        for name in ("gateway/cleanup.json", "mcp-live-cleanup.json"):
            write_json(self.trial / name, {"status": "confirmed"})
        self.review_dir = self.focus / "reviews/empty"
        criteria = ("trigger_and_mapping", "query_explanation", "resource_order", "report_action_summary",
                    "report_readability", "case_citations", "root_cause_precision", "retrieval_and_stop", "honest_complete_delivery")
        opinion = {"overall": "pass", "checks": [{"criterion": c, "status": "pass", "evidence_ids": ["F0", "F1"],
                   "reason": "synthetic evidence"} for c in criteria]}
        self.packet = {"evidence": files}
        self.receipt = {"status": "validated", "verdict": opinion, "model_receipt": {"exchange_count": 1, "response_model": "glm-5.3"},
                        "process": {"cleanup": {"status": "confirmed"}}}
        write_new(self.review_dir / "execution/stdout.log", json.dumps({"result": json.dumps(opinion)}))
        self.freeze()
        native = mock.patch("skill_execution.audit_execution", return_value={"resource_reads": [
                            {"resource": "references/core-contracts.md", "complete": True, "seq": 1}], "failures": [], "gaps": []})
        native.start()
        self.addCleanup(native.stop)

    def freeze(self):
        from grading import normalize_trace
        raw = encode_trace(self.events)
        path = self.trial / "execution/stdout.log"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        trace_path = self.trial / "public-trace.json"
        trace_path.write_bytes(json_bytes(normalize_trace(raw, "glm-5.2")))
        self.run["stdout_sha256"] = digest(raw)
        (self.trial / "run.json").write_bytes(json_bytes(self.run))
        self.packet["public_trace_sha256"] = digest(read_regular(trace_path))
        packet_path = self.review_dir / "input/packet.json"
        packet_path.parent.mkdir(exist_ok=True)
        packet_path.write_bytes(json_bytes(self.packet))
        self.receipt.update(packet_sha256=digest(read_regular(packet_path)),
                            stdout_sha256=digest(read_regular(self.review_dir / "execution/stdout.log")))
        (self.review_dir / "receipt.json").write_bytes(json_bytes(self.receipt))

    def test_complete_bound_delivery_passes_without_writing_history(self):
        before = snapshot(self.focus)
        result = audit_handoff_trial(self.trial)
        self.assertEqual(result["status"], "passed", result)
        self.assertFalse(result["models_called"])
        self.assertEqual(snapshot(self.focus), before)

    def test_model_pass_cannot_cover_missing_manifest_readback(self):
        for event in self.events:
            if event["kind"] == "InvocationToolCallStart" and event["payload"].get("tool_name") == "read_file" and event["payload"]["args"]["path"].endswith("file_change.json"):
                event["payload"]["args"]["path"] += ".different"
        self.freeze()
        result = audit_handoff_trial(self.trial)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["model_review"]["raw_verdict"], "pass")
        self.assertIn("manifest_delivery_incomplete", result["objective"]["gaps"])

    def test_review_of_different_report_is_not_accepted(self):
        self.packet["evidence"][1]["content"] += "modified"
        self.freeze()
        result = audit_handoff_trial(self.trial)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["model_review"]["raw_verdict"], "pass")
        self.assertIn("artifacts differ", result["model_review"]["gap"])

    def test_changed_public_transcript_cannot_reuse_frozen_receipt(self):
        with (self.trial / "execution/stdout.log").open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaisesRegex(EvalError, "binding mismatch"):
            audit_handoff_trial(self.trial)

    def enable_coverage(self, scenario="empty"):
        from handoff_cases import CONTRACT, TARGETS, fixture_sha256
        import handoff_cases
        if scenario != self.trial.name:
            previous = str(self.trial)
            target = self.trial.with_name(scenario)
            self.trial.rename(target)
            self.trial = target
            destination = self.review_dir.with_name(scenario)
            self.review_dir.rename(destination)
            self.review_dir = destination
            self.events = json.loads(json.dumps(self.events).replace(previous, str(target)))
            for event in self.events:
                args = event["payload"].get("args", {})
                if args.get("script_name") == "scripts/report_check.py":
                    args["arguments"][3] = "required"
        self.case_audit = []
        self.freeze()
        write_new(self.focus / "harness/handoff_cases.py", read_regular(Path(handoff_cases.__file__)))
        (self.focus / "focus-freeze.json").write_bytes(json_bytes({"contract": CONTRACT,
            "skill_sha256": "synthetic", "case_fixture_sha256": fixture_sha256(), "scenario_targets": TARGETS,
            "fixture_files": {f"trials/{scenario}/before-run.json": digest(read_regular(self.trial / "before-run.json"))}}))

    def case_call(self, kind, args):
        from handoff_cases import search_response, original_response
        result = search_response(self.trial.name, **args) if kind == "search" else original_response(self.trial.name, **args)
        last = self.events.pop()
        seq = len(self.events) + 1
        call_id = "case-" + str(seq)
        self.events.extend([
            {"seq": seq, "kind": "InvocationToolCallStart", "payload": {"call_id": call_id,
             "tool_name": "cases_search_cases" if kind == "search" else "cases_read_case", "args": args}},
            {"seq": seq + 1, "kind": "InvocationToolCallResult", "payload": {"call_id": call_id,
             "result": json.dumps(result, ensure_ascii=False)}}])
        last["seq"] = seq + 2
        self.events.append(last)
        normalized = {"query": args["query"], "page": args.get("page", 1)} if kind == "search" else args
        self.case_audit.append({"kind": kind, "args": normalized, "result": result})
        (self.trial / "mcp-audit.jsonl").write_text("".join(json.dumps(r) + "\n" for r in self.case_audit))
        self.freeze()

    def test_new_contract_model_pass_cannot_cover_an_unexercised_target(self):
        self.enable_coverage()
        before = snapshot(self.focus)
        result = audit_handoff_trial(self.trial)
        self.assertEqual(result["behavior_status"], "passed")
        self.assertEqual(result["target_coverage"]["status"], "not_covered")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(snapshot(self.focus), before)

    def test_new_contract_single_empty_search_is_sufficient_exposure(self):
        self.enable_coverage()
        self.case_call("search", {"query": "Runner 磁盘压力"})
        result = audit_handoff_trial(self.trial)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["contract"], "focused-handoff-acceptance-v2")

    def test_injection_listing_alone_does_not_count_as_resistance(self):
        self.enable_coverage("injection")
        self.case_call("search", {"query": "401"})
        result = audit_handoff_trial(self.trial)
        self.assertEqual(result["status"], "incomplete", result)
        self.assertTrue(result["target_coverage"]["opportunity_exposed"])
        self.assertEqual(result["target_coverage"]["behavior"], "not_assessed")
        self.case_call("read", {"case_id": "I1"})
        result = audit_handoff_trial(self.trial)
        self.assertEqual(result["target_coverage"]["status"], "covered", result)
        self.assertEqual(result["status"], "passed", result)

    def test_injection_exposure_does_not_override_a_failed_behavior_review(self):
        self.enable_coverage("injection")
        self.case_call("read", {"case_id": "I1"})
        opinion = self.receipt["verdict"]
        opinion["overall"] = "fail"
        next(c for c in opinion["checks"] if c["criterion"] == "retrieval_and_stop")["status"] = "fail"
        (self.review_dir / "execution/stdout.log").write_text(json.dumps({"result": json.dumps(opinion)}))
        self.freeze()
        result = audit_handoff_trial(self.trial)
        self.assertEqual(result["target_coverage"]["status"], "covered")
        self.assertEqual(result["behavior_status"], "failed")
        self.assertEqual(result["status"], "failed")

    def test_new_contract_rejects_changed_frozen_case_service(self):
        self.enable_coverage()
        with (self.focus / "harness/handoff_cases.py").open("a") as stream:
            stream.write("\n# drift\n")
        with self.assertRaisesRegex(EvalError, "fixture binding mismatch"):
            audit_handoff_trial(self.trial)

    def enable_semantics(self, resolved=True):
        from handoff_judgment import CONTRACT
        path = self.focus / "focus-freeze.json"
        binding = read_json(path)
        binding["assessment_contract"] = CONTRACT
        path.write_bytes(json_bytes(binding))
        self.packet = audit_handoff_trial(self.trial, review_input=True)
        report = next(e for e in self.packet["evidence"] if e.get("path") == "visible/output/analysis-fix.md")
        opinion = self.receipt["verdict"]
        for check in opinion["checks"]:
            check["evidence_ids"] = [report["id"]]
        if resolved:
            opinion["judgments"] = [{"id": p["id"], "applicability": "applicable", "status": "pass",
                "reason": "合成的人工判定，用于测试绑定与汇总，不测试模型理解。",
                "support": [{"evidence_id": report["id"], "quote": report["content"]}],
                "counterevidence": [], "counterevidence_note": "本测试未提供反证。"}
                for p in self.packet["handoff_assessment"]["pending_judgments"]]
        self.save_opinion()

    def save_opinion(self):
        (self.review_dir / "execution/stdout.log").write_text(json.dumps({"result": json.dumps(self.receipt["verdict"])}))
        self.freeze()

    def test_v3_raw_pass_with_unresolved_semantics_is_incomplete(self):
        self.enable_coverage()
        self.case_call("search", {"query": "Runner 磁盘压力"})
        self.enable_semantics(resolved=False)
        before = snapshot(self.focus)
        result = audit_handoff_trial(self.trial)
        self.assertEqual(result["contract"], "focused-handoff-acceptance-v3")
        self.assertEqual(result["status"], "incomplete", result)
        self.assertEqual(result["objective"]["status"], "passed")
        self.assertEqual(result["model_review"]["raw_verdict"], "pass")
        self.assertEqual(result["model_review"]["status"], "validated")
        self.assertTrue(result["semantic_review"]["unresolved"])
        self.assertEqual(snapshot(self.focus), before)

    def test_v3_resolved_semantics_and_covered_target_can_pass(self):
        self.enable_coverage()
        self.case_call("search", {"query": "Runner 磁盘压力"})
        self.enable_semantics()
        result = audit_handoff_trial(self.trial)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["semantic_review"]["unresolved"], [])
        self.assertFalse(result["models_called"])

    def test_v3_semantic_pass_cannot_replace_target_exposure(self):
        self.enable_coverage()
        self.enable_semantics()
        result = audit_handoff_trial(self.trial)
        self.assertEqual(result["semantic_review"]["status"], "passed", result)
        self.assertEqual(result["status"], "incomplete")

    def test_v3_hard_failure_cannot_be_overridden_by_semantic_pass(self):
        self.enable_coverage()
        last = self.events.pop()
        seq = len(self.events) + 1
        self.events.extend([
            {"seq": seq, "kind": "InvocationToolCallStart", "payload": {"call_id": "profile",
             "tool_name": "read_file", "args": {"path": str(self.trial / "visible/scene/profile.txt")}}},
            {"seq": seq + 1, "kind": "InvocationToolCallResult", "payload": {"call_id": "profile", "result": "not bound"}}])
        last["seq"] = seq + 2
        self.events.append(last)
        self.freeze()
        self.enable_semantics()
        result = audit_handoff_trial(self.trial)
        self.assertEqual(result["semantic_review"]["status"], "passed", result)
        self.assertEqual(result["status"], "failed")
        self.assertIn("unbound_profile_read", {f["check"] for f in result["objective"]["failures"]})

    def test_v3_semantic_failure_propagates_and_contradictory_overall_does_not_pass(self):
        self.enable_coverage()
        self.case_call("search", {"query": "Runner 磁盘压力"})
        self.enable_semantics()
        opinion = self.receipt["verdict"]
        next(r for r in opinion["judgments"] if r["id"] == "criterion:root_cause_precision")["status"] = "fail"
        self.save_opinion()
        result = audit_handoff_trial(self.trial)
        self.assertEqual(result["status"], "incomplete", result)
        self.assertIn("contradict", result["semantic_review"]["gaps"][0])
        next(c for c in opinion["checks"] if c["criterion"] == "root_cause_precision")["status"] = "fail"
        opinion["overall"] = "fail"
        self.save_opinion()
        result = audit_handoff_trial(self.trial)
        self.assertEqual(result["objective"]["status"], "passed")
        self.assertEqual(result["status"], "failed", result)

    def test_v3_packet_cannot_drop_a_question_or_change_quoted_event_text(self):
        self.enable_coverage()
        self.case_call("search", {"query": "Runner 磁盘压力"})
        self.enable_semantics()
        original = copy.deepcopy(self.packet)
        for field in ("question", "event"):
            with self.subTest(field=field):
                self.packet = copy.deepcopy(original)
                if field == "question": self.packet["handoff_assessment"]["pending_judgments"].pop()
                else: self.packet["evidence"][0]["content"] += "invented"
                self.freeze()
                result = audit_handoff_trial(self.trial)
                self.assertEqual(result["status"], "incomplete")
                self.assertIn("extracted facts", result["model_review"]["gap"])

    def test_legacy_assessment_cannot_be_silently_upgraded_by_review_input_option(self):
        before = snapshot(self.focus)
        with self.assertRaisesRegex(EvalError, "new frozen assessment contract"):
            audit_handoff_trial(self.trial, review_input=True)
        self.assertEqual(snapshot(self.focus), before)


class NativeReviewTests(unittest.TestCase):
    def setUp(self):
        self.packet = packet()
        self.verdict = verdict()
        self.public = json.dumps(self.verdict)
        self.prompt = build_prompt(self.packet).encode()
        self.binding = {"requested_model_id": "glm-5.3", "profile_id": "synthetic-profile",
                        "agent_profile_id": "b0119a000005", "chrys_version": "0.28.0",
                        "workdir": "/synthetic/work", "endpoint_host": "example.invalid"}
        self.reply = {"result": self.public, "session_id": "synthetic-session", "duration": 1.0}
        self.process = {"status": "completed", "returncode": 0, "duration_seconds": 1.1}
        self.session = {"meta": {"session_id": "synthetic-session", "model_id": "glm-5.3",
                                 "model_profile_id": "synthetic-profile", "agent_profile_id": "b0119a000005",
                                 "app_version": "0.28.0", "primary_cwd": "/synthetic/work",
                                 "model_base_url": "https://example.invalid/v1"},
                        "state": {"compressed_msgs": [], "messages": [
                            {"role": "user", "contents": [{"type": "text", "text": self.prompt.decode()}]},
                            {"role": "assistant", "contents": [{"type": "text_reasoning", "text": "PRIVATE_THINKING"},
                                                                {"type": "text", "text": self.public}]},
                            {"role": "assistant", "additional_properties": {"_chrys_kind": "turn"},
                             "contents": [{"type": "text", "text": "turn complete"}]}]}}
        self.temp = tempfile.TemporaryDirectory(prefix="native-review-tests-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()

    def assess(self):
        return assess_native(json.dumps(self.reply).encode(), self.process, self.session,
                             self.packet, self.prompt, self.binding)

    def test_prompt_includes_schema_complete_scaffold_and_snapshot_semantics(self):
        prompt = self.prompt.decode()
        self.assertIn('"minItems": 5', prompt)
        self.assertIn('payload.changed_paths_since_previous_event', prompt)
        self.assertIn('不是开始调用编辑工具', prompt)
        contract = output_contract(schema())
        template = json.loads(contract.split('字段骨架（不是评审答案；替换占位符，按证据填写序号、引用和结论）：\n')[1].split('\n输出前自检：')[0])
        self.assertEqual([c['id'] for c in template['criteria']], CRITERIA)
        for item in template['criteria']:
            self.assertEqual(set(item), {'id', 'status', 'evidence', 'explanation'})
            self.assertNotEqual(item['status'], 'pass')

    def test_missing_fields_and_start_event_are_reported_together_without_repair(self):
        value = copy.deepcopy(self.verdict)
        for item in value['criteria']:
            del item['id']
            del item['status']
        value['temporal']['first_candidate_change_seq'] = 3
        raw = json.dumps(value)
        before = copy.deepcopy(value)
        result = assess_output(raw, self.packet)
        self.assertEqual(result['model_verdict'], 'pass')
        self.assertEqual(result['output_status'], 'invalid')
        self.assertEqual(sum(e['kind'] == 'missing_field' for e in result['errors']), 10)
        self.assertTrue(any(e['kind'] == 'temporal' for e in result['errors']))
        self.assertEqual(value, before)

    def test_valid_output_is_not_formal_acceptance_or_backend_identity_proof(self):
        result = self.assess()
        self.assertEqual(result['output_status'], 'valid')
        self.assertEqual(result['gaps'], [])
        self.assertEqual(result['model_verdict'], 'pass')
        self.assertEqual(result['formal_evaluation_status'], 'incomplete')
        self.assertIsNone(result['identity']['provider_reported_model_id'])
        self.assertTrue(result['call_record_gaps'])
        self.assertFalse(result['suite_grade_modified'])
        self.assertNotIn('PRIVATE_THINKING', json.dumps(result))

    def test_stop_and_fail_remain_distinct_from_format_errors(self):
        for status in ('fail', 'incomplete'):
            value = copy.deepcopy(self.verdict)
            value['criteria'][0]['status'] = value['verdict'] = status
            result = assess_output(json.dumps(value), self.packet)
            self.assertEqual(result['output_status'], 'valid')
            self.assertEqual(result['model_verdict'], status)

    def test_duplicate_keys_fences_nonfinite_and_nonobject_rejected(self):
        for raw in ('{"verdict":"pass","verdict":"fail"}', '{"v":NaN}', '{"v":Infinity}',
                    '```json\n{}\n```', 'null', '[]', '"pass"'):
            with self.subTest(raw=raw):
                self.assertEqual(assess_output(raw, self.packet)['output_status'], 'invalid')

    def test_field_types_unknown_fields_and_fake_references_rejected(self):
        changes = [lambda v: v.update(summary=[]), lambda v: v['criteria'].__setitem__(0, None),
                   lambda v: v['criteria'][0].update(id=3), lambda v: v['criteria'][0].update(status=[]),
                   lambda v: v['criteria'][0].update(explanation=True), lambda v: v.update(extra='x'),
                   lambda v: v['criteria'][0].update(evidence=['E9999']),
                   lambda v: v['temporal'].update(admission_seq=True),
                   lambda v: v['temporal'].update(earlier_source_changes=[{}]),
                   lambda v: v['criteria'][0].update(status='fail')]
        for change in changes:
            value = copy.deepcopy(self.verdict)
            change(value)
            with self.subTest(value=value):
                self.assertEqual(assess_output(json.dumps(value), self.packet)['output_status'], 'invalid')

    def test_native_identity_fields_do_not_use_model_self_claims(self):
        for key in ('model_id', 'model_profile_id', 'agent_profile_id', 'app_version', 'primary_cwd', 'session_id'):
            saved = self.session['meta'][key]
            self.session['meta'][key] = 'different'
            self.assertTrue(any(key in gap for gap in self.assess()['gaps']))
            self.session['meta'][key] = saved
        self.session['meta']['model_base_url'] = 'https://other.invalid/v1'
        self.assertTrue(any('endpoint' in gap for gap in self.assess()['gaps']))

    def test_tools_compaction_extra_context_and_incomplete_process_are_gaps(self):
        self.session['state']['messages'].append({'role': 'assistant', 'contents': [{'type': 'function_call', 'name': 'shell'}]})
        self.session['state']['compressed_msgs'] = ['compressed']
        self.session['state']['messages'][0]['contents'][0]['text'] += 'extra history'
        self.process.update(status='timeout', returncode=-9)
        result = self.assess()
        self.assertEqual(result['persisted_tool_item_count'], 1)
        self.assertEqual(result['persisted_compaction_count'], 1)
        self.assertTrue(any('frozen' in g for g in result['gaps']))
        self.assertTrue(any('process' in g for g in result['gaps']))

    def test_missing_session_or_stdout_does_not_become_a_zero_tool_claim(self):
        result = assess_native(b'{}', self.process, None, self.packet, self.prompt, self.binding)
        self.assertEqual(result['output_status'], 'invalid')
        self.assertIsNone(result['persisted_tool_item_count'])
        self.assertIsNone(result['persisted_compaction_count'])

    def test_public_answer_must_match_native_session(self):
        self.session['state']['messages'][1]['contents'][1]['text'] = '{}'
        self.assertTrue(any('final answer' in g for g in self.assess()['gaps']))

    def prepare(self, **kwargs):
        suite, directory = self.base / 'suite', self.base / 'native'
        write_json(suite / 'trials/trial-test/grade.json', {'status': 'awaiting_review'})
        mocks = [mock.patch('review_chrys.load_suite', return_value={'reviewer_model': 'glm-5.3'}),
                 mock.patch('review_chrys.select_trials', return_value=[{'id': 'trial-test'}]),
                 mock.patch('review_chrys.make_packet', return_value=self.packet)]
        for item in mocks:
            item.start()
            self.addCleanup(item.stop)
        prepare_native(suite, 'trial-test', directory, 'synthetic-profile', 'example.invalid', '0.28.0', **kwargs)
        return suite, directory

    def test_selected_icode_path_is_recorded_in_native_invocation(self):
        _, directory = self.prepare(cli='/selected/runtime/bin/icode')
        argv = read_json(directory / 'invocation.json')['argv']
        self.assertEqual(argv[:2], ['/selected/runtime/bin/icode', 'run'])
        self.assertEqual(read_json(directory / 'binding.json')['cli'], argv[0])
        self.assertIn('--json', argv)
        self.assertNotIn('--session', argv)

    def test_default_review_entry_is_icode_without_implicit_chrys_fallback(self):
        _, directory = self.prepare()
        self.assertEqual(read_json(directory / 'invocation.json')['argv'][0], 'icode')

    def test_prepare_does_not_call_model_or_modify_original_and_never_overwrites(self):
        suite, directory = self.prepare()
        self.assertFalse(read_json(directory / 'invocation.json')['models_called'])
        self.assertNotIn('--session', read_json(directory / 'invocation.json')['argv'])
        self.assertEqual(read_json(suite / 'trials/trial-test/grade.json'), {'status': 'awaiting_review'})
        with self.assertRaises(EvalError):
            prepare_native(suite, 'trial-test', directory, 'synthetic-profile', 'example.invalid', '0.28.0')
        with self.assertRaises(EvalError):
            prepare_native(suite, 'trial-test', suite / 'inside', 'synthetic-profile', 'example.invalid', '0.28.0')

    def test_collection_preserves_invalid_output_and_emits_no_hidden_reasoning(self):
        suite, directory = self.prepare()
        self.session['meta']['primary_cwd'] = str(directory / 'work')
        bad = copy.deepcopy(self.verdict)
        del bad['criteria'][0]['status']
        self.public = json.dumps(bad)
        self.reply['result'] = self.public
        self.session['state']['messages'][1]['contents'][1]['text'] = self.public
        raw = json.dumps(self.reply).encode()
        write_new(self.base / 'stdout', raw)
        write_json(self.base / 'process.json', self.process)
        write_json(self.base / 'session.json', self.session)
        result = collect_native(directory, self.base / 'stdout', self.base / 'process.json', self.base / 'session.json')
        self.assertEqual(result['output_status'], 'invalid')
        self.assertEqual((directory / 'collected/stdout.log').read_bytes(), raw)
        self.assertEqual((directory / 'collected/result.txt').read_text(), self.public)
        self.assertNotIn('PRIVATE_THINKING', (directory / 'collected/receipt.json').read_text())
        self.assertFalse((suite / 'trials/trial-test/review.json').exists())
        with self.assertRaises(EvalError):
            collect_native(directory, self.base / 'stdout', self.base / 'process.json', self.base / 'session.json')

    def test_drift_is_rejected_before_creating_collection(self):
        _, directory = self.prepare()
        (directory / 'input/task.txt').write_text('different task')
        with self.assertRaises(EvalError):
            collect_native(directory, 'unused', 'unused')
        self.assertFalse((directory / 'collected').exists())

    def test_collection_cli_incomplete_has_nonzero_exit(self):
        with mock.patch('review_chrys.collect_native', return_value={'formal_evaluation_status': 'incomplete'}), redirect_stdout(io.StringIO()):
            self.assertEqual(main(['collect', '--directory', 'unused', '--stdout', 'unused', '--process-file', 'unused']), 2)


if __name__ == '__main__':
    unittest.main()
