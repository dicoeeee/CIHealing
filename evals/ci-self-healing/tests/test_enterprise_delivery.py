"""Offline v4 delivery boundary checks; not evidence of real Agent compliance."""

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import EvalError, read_json, write_json, write_new
from grading import grade_trial
from delivery_evidence import assess_delivery
from repair_support import (ENTERPRISE_DELIVERY_CONTRACT, PIPELINE_DELIVERY_CONTRACT, MANIFEST_DELIVERY_CONTRACT,
                            delivery_contract, delivery_contract_matches, enterprise_outputs_unconfigured)
from suite import REPO, LEGACY_CHECKER_FILES, load_suite, prepare
import test_delivery_evidence as fixtures


class EnterpriseDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DeliveryEvidenceTests()
        self.fixture.fixture_contract = ENTERPRISE_DELIVERY_CONTRACT
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture

    def test_unconfigured_v4_delivers_report_without_manifest(self):
        self.assertFalse((self.f.visible / "output/file_change.json").exists())
        self.assertFalse((self.f.root / "checker/scripts/file_change_check.py").exists())
        suite = load_suite(self.f.root)
        self.assertEqual(suite["delivery_contracts"], {"candidate": ENTERPRISE_DELIVERY_CONTRACT})
        self.assertEqual(suite["checker_files"], list(LEGACY_CHECKER_FILES))
        result = self.f.assess()
        self.assertEqual((result["status"], result["gaps"]), ("reviewable_stop_artifact", []), result)
        self.assertNotIn("manifest_check", result)

    def test_completed_report_only_run_is_not_failed_for_missing_manifest(self):
        f = self.f
        f.run["status"] = "completed"
        f.run["process"].update(status="completed", returncode=0)
        f.events[-1]["payload"]["returncode"] = 0
        gateway = f.run["runtime_diagnostics"]["gateway_evidence"]["events"]
        gateway[-2].update(status="completed", reason="none", output_tokens=10)
        gateway[-1]["outcome"] = "completed"
        # Replace the cancelled synthetic exchange with a fully observed response.
        gateway[:] = [e for e in gateway if e["kind"] != "downstream_disconnect"]
        for seq, event in enumerate(gateway, 1):
            event["seq"] = seq
        f.persist()
        f.run["delivery_assessment"] = assess_delivery(f.trial, f.run)
        write_json(f.trial / "run.json", f.run)
        result = grade_trial(f.root, f.row, None)
        self.assertEqual(result["status"], "awaiting_review", result)
        self.assertEqual(result["report_check"]["status"], "passed")
        self.assertEqual(result["enterprise_output_check"]["status"], "not_required")
        self.assertNotIn("file_change_check", result)

    def test_configured_missing_or_ambiguous_requirements_cannot_pass_as_report_only(self):
        path = self.f.root / "skills/candidate/SKILL.md"
        original = path.read_text()
        row = "| 企业输出规范 | 未配置 | 未配置 |"
        for replacement in ("| 企业输出规范 | product-a | references/missing-output-spec.md |",
                            "", row + "\n" + row, "| 企业输出规范 | product-a | 未配置 |"):
            with self.subTest(binding=replacement):
                path.write_text(original.replace(row, replacement))
                result = self.f.assess()
                self.assertEqual(result["status"], "unconfirmed")
                self.assertIn("enterprise_output_review_required", result["gaps"])
        path.write_text(original)

    def test_unconfigured_v4_does_not_authorize_arbitrary_extra_writes(self):
        write_new(self.f.visible / "output/extra.json", "{}")
        result = self.f.assess()
        self.assertNotEqual(result["status"], "reviewable_stop_artifact")
        self.assertIn("not_report_only_change", result["gaps"])

    def test_inactive_or_conflicting_bindings_require_delivery_review(self):
        path = self.f.root / "skills/candidate/SKILL.md"
        original = path.read_text()
        start = original.index("### 自定义资源绑定\n")
        end = original.index("\n## ", start)
        commented = original[:start] + "<!--\n" + original[start:end] + "\n-->\n" + original[end:]
        row = "| 企业输出规范 | 未配置 | 未配置 |"
        conflicted = original.replace(row, row + "\n | 企业输出规范 | product-a | references/product.md |")
        for entry in (commented, conflicted):
            with self.subTest(entry=entry[start:start + 80]):
                path.write_text(entry)
                result = self.f.assess()
                self.assertEqual(result["status"], "unconfirmed", result)
                self.assertIn("enterprise_output_review_required", result["gaps"])
        path.write_text(original)

    def test_v4_requires_explicit_matching_run_contract(self):
        self.assertFalse(delivery_contract_matches(ENTERPRISE_DELIVERY_CONTRACT, {}))
        self.assertFalse(delivery_contract_matches(ENTERPRISE_DELIVERY_CONTRACT,
                                                 {"delivery_contract": MANIFEST_DELIVERY_CONTRACT}))
        self.assertTrue(delivery_contract_matches(ENTERPRISE_DELIVERY_CONTRACT,
                                                {"delivery_contract": ENTERPRISE_DELIVERY_CONTRACT}))


class EnterpriseBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="enterprise-output-binding-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_only_explicit_active_unconfigured_binding_is_recognized(self):
        row = "| 企业输出规范 | 未配置 | 未配置 |"
        prefix = "# Skill\n\n### 自定义资源绑定\n\n| 资源类型 | 固定 ID | 接入入口 |\n| --- | --- | --- |\n"
        for body, expected in ((row, True), ("", False), (row + "\n" + row, False),
                               ("```text\n" + row + "\n```", False),
                               ("<!--\n" + row + "\n-->", False),
                               ("    " + row, False),
                               ("| 企业输出规范 | 未配置 | references/example.md |", False),
                               ("| 企业输出规范 | product-a | references/example.md |", False)):
            with self.subTest(body=body):
                (self.root / "SKILL.md").write_text(prefix + body + "\n\n## Next\n")
                self.assertEqual(enterprise_outputs_unconfigured(self.root), expected)
        for marker in ("```", "~~~~"):
            with self.subTest(fenced_heading=marker):
                (self.root / "SKILL.md").write_text(marker + "markdown\n" + prefix + row + "\n" + marker + "\n")
                self.assertFalse(enterprise_outputs_unconfigured(self.root))

    def test_current_package_declares_v6_and_cannot_silently_fall_back(self):
        source = REPO / "skills/ci-self-healing"
        self.assertEqual(delivery_contract(source), PIPELINE_DELIVERY_CONTRACT)
        self.assertTrue(enterprise_outputs_unconfigured(source))
        shutil.copytree(source, self.root / "skill")
        (self.root / "skill/assets/delivery-resources.json").unlink()
        with self.assertRaises(EvalError):
            delivery_contract(self.root / "skill")

    def test_comments_do_not_supply_an_active_binding(self):
        binding = "### 自定义资源绑定\n\n| 企业输出规范 | 未配置 | 未配置 |\n"
        commented = "<!--\n" + binding + "```text\n-->\n"
        for entry, expected in ((commented, False), (commented + binding, True),
                                ("<!--\n" + binding, False),
                                ("<!-- example -->\n" + binding, True),
                                ("<!-- example -->" + binding, False),
                                (binding + "<!-- unfinished", False),
                                ("prefix " + binding, False)):
            with self.subTest(entry=entry):
                (self.root / "SKILL.md").write_text(entry)
                self.assertEqual(enterprise_outputs_unconfigured(self.root), expected)

    def test_table_indentation_preserves_conflict_detection(self):
        prefix = "### 自定义资源绑定\n\n"
        row = "| 企业输出规范 | 未配置 | 未配置 |"
        configured = "| 企业输出规范 | product-a | references/product.md |"
        for spaces in range(4):
            for body, expected in ((" " * spaces + row, True),
                                   (row + "\n" + " " * spaces + configured, False),
                                   (row + "\n" + " " * spaces + row, False),
                                   (row + "\n" + " " * spaces + configured[:-1], False)):
                with self.subTest(body=body):
                    (self.root / "SKILL.md").write_text(prefix + body + "\n")
                    self.assertEqual(enterprise_outputs_unconfigured(self.root), expected)
        for indent in ("    ", "\t"):
            with self.subTest(ambiguous_indent=repr(indent)):
                (self.root / "SKILL.md").write_text(prefix + row + "\n" + indent + configured + "\n")
                self.assertFalse(enterprise_outputs_unconfigured(self.root))

    def test_v3_v6_comparison_preserves_v3_checker_without_forcing_it_on_v6(self):
        legacy = self.root / "v3"
        shutil.copytree(REPO / "skills/ci-self-healing", legacy)
        declaration = legacy / "assets/delivery-resources.json"
        value = read_json(declaration)
        value["contract"] = MANIFEST_DELIVERY_CONTRACT
        declaration.write_text(json.dumps(value))
        output = self.root / "suite"
        manifest = prepare(output, REPO / "skills/ci-self-healing", baseline_dir=legacy,
                           repeats=1, mode="compare", case_ids=["case-003"], backend="native")
        self.assertEqual(manifest["delivery_contracts"], {"before": MANIFEST_DELIVERY_CONTRACT,
                                                        "after": PIPELINE_DELIVERY_CONTRACT})
        self.assertTrue((output / "checker/scripts/file_change_check.py").exists())
        self.assertEqual(load_suite(output), manifest)


if __name__ == "__main__":
    unittest.main()
