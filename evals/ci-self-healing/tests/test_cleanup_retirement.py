"""Offline v8 boundary checks; no model execution or historical-result writes."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import read_regular, snapshot, write_new
from grading import final_state_checks
from repair_support import EXECUTION_CONTRACT, MODULAR_EXECUTION_CONTRACT, execution_contract
from review import RUBRIC, rubric_for
from ab_review import CRITERIA, criteria_for
from review_packet import CANDIDATE_SECTIONS, MODULAR_SECTIONS, sections
from suite import skill_from_dir
import test_skill_execution as legacy_execution


class CleanupRetirementTests(unittest.TestCase):
    def setUp(self):
        # Reuse synthetic tool-boundary helpers, not the legacy test assertions.
        self.f = legacy_execution.SkillExecutionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.before = snapshot(self.f.visible)
        shutil.rmtree(self.f.skill)  # Owned TemporaryDirectory created above.
        self.current = skill_from_dir(ROOT.parents[1] / "skills/ci-self-healing")
        for name, body in self.current.items():
            write_new(self.f.skill / name, body)
        self.f.events = []
        for name in ("SKILL.md", "references/invocation-inputs.md", "references/diagnosis-and-repair.md",
                     "references/repair-admission.md", "references/enterprise-policy.md",
                     "references/candidate-verification.md", "references/output-delivery.md"):
            self.f.load_resource(name, self.current[name])

    def audit(self):
        return self.f.audit(contract=EXECUTION_CONTRACT)

    def repaired_build(self):
        file = self.f.repo / "src/main/java/Example.java"
        self.f.call("edit_file", {"path": str(file)}, "edited",
                    lambda: file.write_text("class Example { int repaired; }\n"))
        self.f.build()

    def test_build_with_retained_products_needs_no_cleanup_evidence(self):
        self.repaired_build()
        result = self.audit()
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["gaps"], [])
        self.assertEqual(result["dimensions"]["cleanup_evidence"]["status"], "not_required")
        self.assertEqual(result["workspace_checks"], [])
        self.assertTrue((self.f.repo / "target/generated-sources/annotations/Example.class").is_file())
        after = snapshot(self.f.visible)
        self.assertEqual(final_state_checks(self.before, after, "repair_caller",
                                           contract=EXECUTION_CONTRACT)["failures"], [])
        historical = final_state_checks(self.before, after, "repair_caller",
                                        contract=MODULAR_EXECUTION_CONTRACT)
        self.assertIn("protected_inputs_or_cleanup", [f["check"] for f in historical["failures"]])

    def test_original_job_files_are_still_protected_inside_output_directory(self):
        self.repaired_build()
        original = self.f.repo / "target/preexisting.txt"
        original.write_text("overwritten\n")
        for operation in ("overwrite", "delete"):
            if operation == "delete":
                original.unlink()
            result = final_state_checks(self.before, snapshot(self.f.visible), "repair_caller",
                                        contract=EXECUTION_CONTRACT)
            self.assertIn("scene/sourcecode/target/preexisting.txt",
                          [p for f in result["failures"] for p in f["paths"]])

    def test_output_exception_does_not_allow_unknown_paths_or_symlinks(self):
        self.repaired_build()
        write_new(self.f.repo / "unowned.bin", b"not a declared fixture output")
        (self.f.repo / "target/outside-link").symlink_to(self.f.out)
        result = final_state_checks(self.before, snapshot(self.f.visible), "repair_caller",
                                    contract=EXECUTION_CONTRACT)
        self.assertIn("scene/sourcecode/unowned.bin", [p for f in result["failures"] for p in f["paths"]])
        self.assertIn("unsafe_file_types", [f["check"] for f in result["failures"]])

    def test_missing_verification_and_required_guides_still_fail(self):
        self.assertIn("agent_build_not_observed", [f["check"] for f in self.audit()["failures"]])
        self.f.truncate_resource("references/candidate-verification.md")
        self.repaired_build()
        self.assertIn("required_skill_resource_not_loaded_before_action:references/candidate-verification.md",
                      [f["check"] for f in self.audit()["failures"]])

    def test_retired_resources_not_shipped_and_archive_bytes_unchanged(self):
        archive = ROOT / "legacy/cleanup-v7"
        manifest = json.loads(read_regular(archive / "manifest.json"))
        for name, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256(read_regular(archive / "skill" / name)).hexdigest(), digest)
        for name in ("scripts/workspace_check.py", "references/workspace-cleanup.md",
                     "references/workspace-recovery.md"):
            self.assertNotIn(name, self.current)
        self.assertEqual(execution_contract(self.f.skill), EXECUTION_CONTRACT)
        self.assertEqual(execution_contract(archive / "skill"), MODULAR_EXECUTION_CONTRACT)
        # Both historical and current review clause maps remain usable.
        for resource, headings in MODULAR_SECTIONS.items():
            self.assertEqual([x["heading"] for x in sections(
                read_regular(archive / "skill" / resource).decode(), headings)], headings)
        for resource, headings in CANDIDATE_SECTIONS.items():
            self.assertEqual([x["heading"] for x in sections(self.current[resource].decode(), headings)], headings)

    def test_review_contracts_keep_historical_meaning(self):
        self.assertEqual(rubric_for(MODULAR_EXECUTION_CONTRACT), RUBRIC)
        self.assertEqual(criteria_for(MODULAR_EXECUTION_CONTRACT), CRITERIA)
        current = rubric_for(EXECUTION_CONTRACT)
        self.assertNotIn("必要验证/清理全部完成", current)
        self.assertNotIn("最终有效 match", current)
        self.assertIn("最终源码", current)
        self.assertIn("普通编译产物", criteria_for(EXECUTION_CONTRACT)["workspace"])
        self.assertEqual(set(criteria_for(EXECUTION_CONTRACT)), set(CRITERIA))


if __name__ == "__main__":
    unittest.main()
