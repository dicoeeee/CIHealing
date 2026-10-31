"""Offline version isolation and pipeline report checks; no model behavior claims."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import EvalError, read_json, write_json
from repair_support import ENTERPRISE_DELIVERY_CONTRACT, FOUR_OUTCOME_DELIVERY_CONTRACT, PIPELINE_DELIVERY_CONTRACT
from suite import REPO, prepare, load_suite, report_checker
from test_eval import STOP_REPORT
import test_delivery_evidence as fixtures


class ReportContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="report-versions-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.legacy = self.base / "legacy"
        shutil.copytree(REPO / "skills/ci-self-healing", self.legacy)
        path = self.legacy / "assets/delivery-resources.json"
        value = read_json(path); value["contract"] = ENTERPRISE_DELIVERY_CONTRACT; path.write_text(json.dumps(value))
        self.root = self.base / "suite"
        self.manifest = prepare(self.root, REPO / "skills/ci-self-healing", baseline_dir=self.legacy,
                                repeats=1, case_ids=["case-003"], backend="native")

    def test_mixed_versions_use_different_host_checkers(self):
        self.assertEqual(self.manifest["report_contracts"], {"before": "v1", "after": "v3"})
        rows = {r["version"]: r for r in self.manifest["trials"]}
        report = self.base / "analysis-fix.md"
        report.write_text(STOP_REPORT.replace("| 处理结果 | unresolved |", "| 处理结果 | outer_ci_passed |"))
        for version, expected in (("before", 0), ("after", 1)):
            checked = subprocess.run([sys.executable, "-B", str(report_checker(self.root, rows[version])),
                "--report", str(report), "--classification", "none", "--checks", "none"], capture_output=True)
            self.assertEqual(checked.returncode, expected, checked.stdout)
        self.assertEqual(load_suite(self.root), self.manifest)

    def test_contract_map_cannot_be_downgraded_or_removed(self):
        for versions in (None, {"before": "v1", "after": "v1"},
                         {"before": "v1", "after": "v2"}, {"before": "v2", "after": "v3"}):
            with self.subTest(versions=versions):
                value = dict(self.manifest)
                if versions is None: value.pop("report_contracts")
                else: value["report_contracts"] = versions
                (self.root / "suite.json").write_text(json.dumps(value))
                with self.assertRaises(EvalError): load_suite(self.root)
        (self.root / "suite.json").write_text(json.dumps(self.manifest))

    def test_candidate_checker_cannot_replace_v3_contract(self):
        row = next(r for r in self.manifest["trials"] if r["version"] == "after")
        self.assertEqual(report_checker(self.root, row).read_bytes(), (ROOT / "contracts/v3/scripts/report_check.py").read_bytes())
        checker = report_checker(self.root, row)
        checker.write_text("print('always pass')")
        with self.assertRaises(EvalError): load_suite(self.root)

    def test_live_report_resources_match_new_frozen_contract(self):
        for name in ("scripts/report_check.py", "assets/analysis-fix.template.md"):
            self.assertEqual((REPO / "skills/ci-self-healing" / name).read_bytes(), (ROOT / "contracts/v3" / name).read_bytes())

    def test_four_and_three_outcome_contracts_keep_distinct_checks_and_review_targets(self):
        from common import read_regular
        from review import make_packet
        from unittest.mock import patch

        declaration = self.legacy / "assets/delivery-resources.json"
        value = read_json(declaration)
        value["contract"] = FOUR_OUTCOME_DELIVERY_CONTRACT
        declaration.write_text(json.dumps(value))
        for name in ("scripts/report_check.py", "assets/analysis-fix.template.md"):
            (self.legacy / name).write_bytes(read_regular(ROOT / "contracts/v2" / name))
        root = self.base / "v2-v3"
        manifest = prepare(root, REPO / "skills/ci-self-healing", baseline_dir=self.legacy,
                           repeats=1, case_ids=["outcome-not-applicable"], backend="native")
        self.assertEqual(manifest["report_contracts"], {"before": "v2", "after": "v3"})
        report = self.base / "analysis-fix.md"
        report.write_text(STOP_REPORT.replace("| 处理结果 | unresolved |", "| 处理结果 | not_applicable |"))
        for row in manifest["trials"]:
            legacy = row["version"] == "before"
            checked = subprocess.run([sys.executable, "-B", str(report_checker(root, row)),
                "--report", str(report), "--classification", "none", "--checks", "none"], capture_output=True)
            self.assertEqual(checked.returncode, 0 if legacy else 1, checked.stdout)
            if not legacy:
                self.assertIn("outcome_enum", {e["code"] for e in json.loads(checked.stdout)["errors"]})
            write_json(root / "trials" / row["id"] / "grade.json", {"trace": {"events": []}})
            with patch("review.validate_grade_binding"):
                packet = make_packet(root, row)
            self.assertEqual(packet["report_evaluation"], {
                "contract": "v2" if legacy else "v3",
                "fixture_outcome": "not_applicable" if legacy else "unresolved"})
        self.assertEqual(load_suite(root), manifest)

    def test_focused_cases_keep_default_batch_unchanged(self):
        from cases import default_cases, CASES, materialize
        self.assertEqual(default_cases(), ["case-001", "case-002", "case-003"])
        for name, outcome in (("outcome-advised", "repair_advised"), ("outcome-not-applicable", "unresolved")):
            visible = self.base / name
            materialize(visible, name, "test-invocation", backend="native")
            task = (visible / "task.txt").read_text()
            self.assertIn("企业策略禁止本轮修改", task)
            self.assertNotIn("允许修改本仓 Java 源码", task)
            self.assertNotIn(outcome, task)
            self.assertEqual(CASES[name]["report_outcome"], outcome)

    def test_v6_timeout_and_grade_use_pipeline_contract(self):
        from grading import grade_trial
        f = fixtures.DeliveryEvidenceTests(); f.fixture_contract = PIPELINE_DELIVERY_CONTRACT; f.setUp()
        self.addCleanup(f.doCleanups)
        assessment = f.assess()
        self.assertEqual(assessment["status"], "reviewable_stop_artifact", assessment)
        f.run["delivery_assessment"] = assessment
        write_json(f.trial / "run.json", f.run)
        result = grade_trial(f.root, f.row, None)
        self.assertEqual(result["status"], "awaiting_review", result)
        self.assertEqual(result["enterprise_output_check"]["status"], "not_required")

    def test_case_advice_pair_keeps_history_fixed_and_answers_host_only(self):
        from cases import CASES, HISTORICAL_ROUTING_CASE, default_cases, materialize
        from common import read_regular

        names = ("outcome-case-adapted", "outcome-case-unconfirmed")
        expected = ("repair_advised", "unresolved")
        self.assertEqual(default_cases(), ["case-001", "case-002", "case-003"])
        manifest = prepare(self.base / "case-suite", REPO / "skills/ci-self-healing",
                           repeats=1, mode="single", case_ids=list(names), backend="native")
        for name, outcome in zip(names, expected):
            with self.subTest(case=name):
                visible = self.base / name
                materialize(visible, name, "case-advice-pair", backend="native")
                context = read_regular(visible / "scene/mr_context.md").decode()
                task = read_regular(visible / "task.txt").decode()
                self.assertTrue(context.endswith(HISTORICAL_ROUTING_CASE + "\n"))
                self.assertTrue(CASES[name]["focused"])
                for enum in ("local_verified", "repair_advised", "not_applicable", "unresolved"):
                    self.assertNotIn(enum, context + task)
                self.assertEqual(manifest["case_definitions"][name]["report_outcome"], outcome)
                self.assertEqual(next(r for r in manifest["trials"] if r["case"] == name)["expected"], "stop")
                self.assertEqual("禁止本轮修改" in task, name == "outcome-case-adapted")

    def test_case_evidence_controls_preserve_policy_and_current_contract(self):
        from cases import CASES, HISTORICAL_ROUTING_CASE, default_cases
        from common import read_regular

        names = ("outcome-case-adapted", "outcome-case-unconfirmed-blocked",
                 "outcome-case-counterevidence")
        contracts = ("case-001", "case-003", "case-002")
        expected = ("repair_advised", "unresolved", "repair_advised")
        root = self.base / "evidence-controls"
        manifest = prepare(root, REPO / "skills/ci-self-healing", repeats=1,
                           mode="single", case_ids=list(names), backend="native")
        self.assertEqual(default_cases(), ["case-001", "case-002", "case-003"])
        task_bodies = []
        for name, contract, outcome in zip(names, contracts, expected):
            row = next(r for r in manifest["trials"] if r["case"] == name)
            visible = root / "trials" / row["id"] / "visible"
            context = read_regular(visible / "scene/mr_context.md").decode()
            self.assertEqual(context, "# MR 说明\n\n" + CASES[contract]["contract"] + HISTORICAL_ROUTING_CASE + "\n")
            task = read_regular(visible / "task.txt").decode()
            # Random per-trial paths/identities must not obscure input equality.
            task_bodies.append(task.replace(row["id"], "TRIAL_ID"))
            self.assertEqual(row["expected"], "stop")
            self.assertEqual(manifest["case_definitions"][name]["report_outcome"], outcome)
            for enum in ("local_verified", "repair_advised", "unresolved", "not_applicable"):
                self.assertNotIn(enum, context + task)
        self.assertEqual(task_bodies[0], task_bodies[1])
        self.assertEqual(task_bodies[0], task_bodies[2])
        self.assertEqual(load_suite(root), manifest)


if __name__ == "__main__":
    unittest.main()
