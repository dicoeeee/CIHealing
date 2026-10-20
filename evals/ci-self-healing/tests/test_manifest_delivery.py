"""Offline dual-artifact delivery and legacy compatibility; no model services."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import EvalError, digest, read_json, read_regular, snapshot, write_json, write_new
from delivery_evidence import assess_delivery, reviewable_timeout
from grading import grade_trial, summarize
from manifest_delivery import check_manifest
from repair_support import DELIVERY_CONTRACT, MANIFEST_DELIVERY_CONTRACT, delivery_contract, delivery_contract_matches
from suite import REPO, load_suite, prepare
import test_delivery_evidence as legacy_delivery
from test_eval import STOP_REPORT


EMPTY = '{"version":"0.1","changes":[]}'


class ManifestDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy_delivery.DeliveryEvidenceTests()
        self.fixture.manifest_delivery = True
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture
        self.add_manifest()

    def add_manifest(self, body=EMPTY):
        f = self.f
        marker = next(i for i, e in enumerate(f.events) if e["kind"] == "InvocationMessage")
        tail = copy.deepcopy(f.events[marker + 1:])
        f.events = f.events[:marker + 1]
        f.events[0]["payload"]["delivery_contract"] = MANIFEST_DELIVERY_CONTRACT
        f.run["delivery_contract"] = MANIFEST_DELIVERY_CONTRACT
        f.report.unlink()
        f.previous = snapshot(f.visible)
        path = f.visible / "output/file_change.json"
        f.call("write_file", {"path": str(path), "content": body}, "done", lambda: write_new(path, body))
        result = json.dumps({"format_status": "valid", "sha256": digest(body.encode()), "errors": []})
        f.call("run_skill_script", {"skill_name": "ci-self-healing", "script_name": "scripts/file_change_check.py",
                                   "arguments": ["--manifest", str(path)]}, result)
        f.call("read_file", {"path": str(path)}, body)
        for event in tail:
            p = event["payload"]
            if "call_id" in p:
                p["call_id"] = "report-" + p["call_id"]
            if event["kind"] == "InvocationToolCallResult" and p.get("tool_name") == "write_file":
                write_new(f.report, STOP_REPORT)
            f.event(event["kind"], p)

    def manifest_result(self, name):
        start = next(e for e in self.f.events if e["kind"] == "InvocationToolCallStart"
                     and e["payload"]["tool_name"] == name
                     and (e["payload"]["args"].get("script_name") == "scripts/file_change_check.py"
                          or e["payload"]["args"].get("path", "").endswith("file_change.json")))
        return next(e for e in self.f.events if e["kind"] == "InvocationToolCallResult"
                    and e["payload"]["call_id"] == start["payload"]["call_id"])

    def test_dual_artifact_timeout_is_reviewable_and_bound(self):
        result = self.f.assess()
        self.assertEqual(result["gaps"], [])
        self.assertEqual(result["status"], "reviewable_stop_artifact")
        self.assertEqual(result["contract"], MANIFEST_DELIVERY_CONTRACT)
        self.assertEqual(result["manifest_check"]["status"], "passed")
        manifest = result["manifest_delivery"]
        self.assertEqual(manifest["sha256"], digest(EMPTY.encode()))
        self.assertGreater(result["checkpoint_seq"], manifest["checkpoint_seq"])
        self.f.run["delivery_assessment"] = result
        self.assertTrue(reviewable_timeout(self.f.run))
        write_json(self.f.trial / "run.json", self.f.run)
        grade = grade_trial(self.f.root, self.f.row, None)
        self.assertEqual(grade["status"], "awaiting_review", grade)
        self.assertEqual(grade["file_change_check"]["status"], "passed")
        self.assertEqual(grade["execution_status"], "timeout")

    def test_missing_manifest_does_not_recover_timeout(self):
        (self.f.visible / "output/file_change.json").unlink()
        self.assertNotEqual(self.f.assess()["status"], "reviewable_stop_artifact")

    def test_stale_hash_and_wrong_check_target_are_rejected(self):
        original = copy.deepcopy(self.f.events)
        event = self.manifest_result("run_skill_script")
        value = json.loads(event["payload"]["result"])
        value["sha256"] = "0" * 64
        event["payload"]["result"] = json.dumps(value)
        self.assertIn("manifest_check_not_valid", self.f.assess()["gaps"])
        self.f.events = original
        event = next(e for e in self.f.events if e["kind"] == "InvocationToolCallStart"
                     and e["payload"].get("args", {}).get("script_name") == "scripts/file_change_check.py")
        event["payload"]["args"]["arguments"] = ["--manifest", "/another/file_change.json"]
        self.assertIn("manifest_check_not_valid", self.f.assess()["gaps"])

    def test_truncated_or_missing_readback_is_rejected(self):
        event = self.manifest_result("read_file")
        event["payload"]["result"] = '{"version":"0.1"'
        self.assertIn("stop_manifest_checkpoint_missing", self.f.assess()["gaps"])
        event["kind"] = "omitted.result"
        self.assertNotEqual(self.f.assess()["status"], "reviewable_stop_artifact")

    def test_fake_write_content_or_write_before_stop_is_rejected(self):
        event = next(e for e in self.f.events if e["kind"] == "InvocationToolCallStart"
                     and e["payload"]["tool_name"] == "write_file"
                     and e["payload"]["args"]["path"].endswith("file_change.json"))
        event["payload"]["args"]["content"] = "{}"
        self.assertIn("manifest_write_not_bound_to_stop_and_final_bytes", self.f.assess()["gaps"])

    def test_nonempty_manifest_without_source_changes_is_rejected(self):
        path = self.f.visible / "output/file_change.json"
        content = json.dumps({"version": "0.1", "changes": [
            {"file": "src/main/java/example/Router.java", "repo": "routing-feature", "op": "modify", "desc": "synthetic"}]})
        path.write_text(content)
        self.assertIn("file_change_manifest_does_not_match_source", self.f.assess()["gaps"])

    def test_extra_output_write_and_after_checkpoint_action_are_rejected(self):
        write_new(self.f.visible / "output/extra.json", "{}")
        self.assertIn("not_report_only_change", self.f.assess()["gaps"])
        (self.f.visible / "output/extra.json").unlink()
        self.f.events[-2]["kind"] = "InvocationMessage"
        self.f.events[-2]["payload"].update(text="new direction")
        self.assertIn("actions_or_decisions_after_checkpoint", self.f.assess()["gaps"])

    def test_contract_mismatch_and_v1_remain_fail_closed(self):
        self.f.run["delivery_contract"] = DELIVERY_CONTRACT
        self.assertNotEqual(self.f.assess()["status"], "reviewable_stop_artifact")
        self.f.events[0]["payload"]["delivery_contract"] = DELIVERY_CONTRACT
        result = self.f.assess()
        self.assertIn("not_report_only_change", result["gaps"])
        self.assertIn("non_report_script", result["gaps"])

    def completed_grade(self):
        f = self.f
        f.run["status"] = "completed"
        f.run["process"].update(status="completed", returncode=0)
        f.events[-1]["payload"]["returncode"] = 0
        # A completed synthetic second response is used only for offline grading.
        gateway = f.run["runtime_diagnostics"]["gateway_evidence"]["events"]
        gateway[-2].update(status="completed", reason="none", output_tokens=10)
        gateway[-1]["outcome"] = "completed"
        f.persist()
        f.run["delivery_assessment"] = assess_delivery(f.trial, f.run)
        (f.trial / "run.json").write_text(json.dumps(f.run))
        return grade_trial(f.root, f.row, None)

    def test_completed_run_requires_manifest_and_does_not_report_verified_when_missing(self):
        result = self.completed_grade()
        self.assertEqual(result["file_change_check"]["status"], "passed")
        path = self.f.visible / "output/file_change.json"
        path.unlink()
        result = self.completed_grade()
        self.assertEqual(result["file_change_check"]["status"], "incomplete")
        self.assertIn("file_change_manifest_unavailable", result["gaps"])
        (self.f.trial / "grade.json").write_text(json.dumps(result))
        summary = summarize(self.f.root)
        self.assertNotIn(summary["trials"][0]["delivery_status"],
                         {"report_format_verified", "report_and_manifest_verified"})

    def test_completed_invalid_format_or_contract_mismatch_is_not_accepted(self):
        path = self.f.visible / "output/file_change.json"
        path.write_text('{"version":"0.1","changens":[]}')
        result = self.completed_grade()
        self.assertIn("file_change_manifest_format", {item["check"] for item in result["failures"]})
        path.write_text(EMPTY)
        self.f.events[0]["payload"]["delivery_contract"] = DELIVERY_CONTRACT
        result = self.completed_grade()
        self.assertIn("delivery_contract_mismatch", result["gaps"])

    def test_completed_v2_suite_cannot_downgrade_or_omit_run_contract(self):
        original = copy.deepcopy(self.f.run)
        for contract in (DELIVERY_CONTRACT, None):
            with self.subTest(contract=contract):
                self.f.run = copy.deepcopy(original)
                if contract is None:
                    self.f.run.pop("delivery_contract")
                else:
                    self.f.run["delivery_contract"] = contract
                result = self.completed_grade()
                self.assertIn("delivery_contract_mismatch", result["gaps"])
                self.assertIn("file_change_check", result)

    def test_missing_symlink_or_malformed_repository_mapping_does_not_crash_grading(self):
        f = self.f
        mapping = f.visible / "scene/build_info.json"
        original = mapping.read_bytes()
        mapping.unlink()
        result = self.completed_grade()
        self.assertIn("file_change_repository_mapping_unavailable", result["gaps"])
        self.assertIn("protected_inputs_or_cleanup", {item["check"] for item in result["failures"]})
        target = f.trial / "mapping.json"
        target.write_bytes(original)
        mapping.symlink_to(target)
        checked = check_manifest(f.root, f.trial, f.before, snapshot(f.visible))
        self.assertIn("file_change_repository_mapping_unavailable", checked["gaps"])
        mapping.unlink()
        for body in ('[1]', '{"repositories":[null]}', '{"repositories":"bad"}', '{bad'):
            with self.subTest(body=body):
                mapping.write_text(body)
                # Preserve a malformed trusted input to test structure handling,
                # in addition to the ordinary changed-input check.
                before = copy.deepcopy(f.before)
                before["scene/build_info.json"]["sha256"] = digest(body.encode())
                checked = check_manifest(f.root, f.trial, before, snapshot(f.visible))
                self.assertIn("file_change_repository_mapping_unavailable", checked["gaps"])

    def test_independent_check_requires_paths_operations_and_repository_to_match(self):
        f = self.f
        source = f.visible / "scene/sourcecode/src/main/java/example/Router.java"
        source.write_text(source.read_text() + "// synthetic\n")
        item = {"file": "src/main/java/example/Router.java", "repo": "routing-feature", "op": "modify", "desc": "synthetic"}
        path = f.visible / "output/file_change.json"
        for entry, status in ((item, "passed"), ({**item, "op": "create"}, "failed"),
                              ({**item, "repo": "sourcecode"}, "failed"),
                              ({**item, "file": "routing-feature/" + item["file"]}, "failed")):
            with self.subTest(entry=entry):
                path.write_text(json.dumps({"version": "0.1", "changes": [entry]}))
                result = check_manifest(f.root, f.trial, f.before, snapshot(f.visible))
                self.assertEqual(result["status"], status)

    def test_final_rename_requires_both_delete_and_create(self):
        f = self.f
        source = f.visible / "scene/sourcecode/src/main/java/example/Router.java"
        source.rename(source.with_name("RenamedRouter.java"))
        items = [
            {"file": "src/main/java/example/Router.java", "repo": "routing-feature",
             "op": "delete", "desc": "移除旧路径"},
            {"file": "src/main/java/example/RenamedRouter.java", "repo": "routing-feature",
             "op": "create", "desc": "移入新路径"},
        ]
        path = f.visible / "output/file_change.json"
        for entries, status in ((items, "passed"), (items[:1], "failed"), (items[1:], "failed")):
            with self.subTest(entries=entries):
                path.write_text(json.dumps({"version": "0.1", "changes": entries}))
                checked = check_manifest(f.root, f.trial, f.before, snapshot(f.visible))
                self.assertEqual(checked["status"], status)

    def test_restored_edits_and_removed_new_file_leave_empty_manifest(self):
        f = self.f
        source = f.visible / "scene/sourcecode/src/main/java/example/Router.java"
        original = source.read_bytes()
        source.write_bytes(original + b"// synthetic\n")
        source.write_bytes(original)
        temporary = source.with_name("Temporary.java")
        temporary.write_text("// synthetic\n")
        temporary.unlink()
        checked = check_manifest(f.root, f.trial, f.before, snapshot(f.visible))
        self.assertEqual(checked["status"], "passed", checked)


class DeliveryContractSelectionTests(unittest.TestCase):
    def test_frozen_package_selects_contract_and_missing_declaration_cannot_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            write_new(root / "SKILL.md", "legacy Skill")
            self.assertEqual(delivery_contract(root), DELIVERY_CONTRACT)
            (root / "SKILL.md").write_text("deliver file_change.json")
            with self.assertRaises(EvalError):
                delivery_contract(root)
            write_new(root / "assets/delivery-resources.json", read_regular(REPO / "skills/ci-self-healing/assets/delivery-resources.json"))
            self.assertEqual(delivery_contract(root), MANIFEST_DELIVERY_CONTRACT)
            (root / "assets/delivery-resources.json").write_text('{"schema":"ci-skill-delivery-resources/v1","contract":"unknown"}')
            with self.assertRaises(EvalError):
                delivery_contract(root)

    def test_suite_freezes_manifest_checker_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "suite"
            manifest = prepare(root, REPO / "skills/ci-self-healing", repeats=1, mode="single", case_ids=["case-003"])
            self.assertEqual(manifest["delivery_contracts"], {"candidate": MANIFEST_DELIVERY_CONTRACT})
            load_suite(root)
            checker = root / "checker/scripts/file_change_check.py"
            checker.write_text(checker.read_text() + "\n# changed\n")
            with self.assertRaisesRegex(EvalError, "checker changed"):
                load_suite(root)

    def test_mixed_suite_preserves_old_docker_receipt_without_weakening_v2(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            legacy = root / "legacy"
            write_new(legacy / "SKILL.md", "legacy Skill")
            suite = root / "suite"
            manifest = prepare(suite, REPO / "skills/ci-self-healing", baseline_dir=legacy,
                               repeats=1, mode="compare", case_ids=["case-003"])
            contracts = manifest["delivery_contracts"]
            self.assertEqual(contracts, {"before": DELIVERY_CONTRACT, "after": MANIFEST_DELIVERY_CONTRACT})
            load_suite(suite)
            self.assertTrue(delivery_contract_matches(contracts["before"], {}))
            self.assertTrue(delivery_contract_matches(contracts["before"], {"backend": "docker"}))
            self.assertFalse(delivery_contract_matches(contracts["before"], {"backend": "native"}))
            self.assertFalse(delivery_contract_matches(contracts["after"], {}))
            self.assertFalse(delivery_contract_matches(contracts["after"], {"delivery_contract": DELIVERY_CONTRACT}))

    def test_legacy_suite_still_uses_only_legacy_checker_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            skill = root / "old-skill"
            write_new(skill / "SKILL.md", "legacy Skill")
            suite = root / "suite"
            manifest = prepare(suite, skill, repeats=1, mode="single", case_ids=["case-003"])
            self.assertNotIn("checker_files", manifest)
            self.assertNotIn("delivery_contracts", manifest)
            self.assertFalse((suite / "checker/scripts/file_change_check.py").exists())
            load_suite(suite)


if __name__ == "__main__":
    unittest.main()
