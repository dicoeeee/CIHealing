"""Grade/review handoff controls; build and model execution are explicitly mocked.

Real capture/check recovery proof is exercised in test_skill_execution.py.
These tests cover contract dispatch and ensure eligibility is not acceptance.
"""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import digest, read_json, read_regular, snapshot, write_json, write_new
from grading import combined_status, grade_trial
from repair_support import EXECUTION_CONTRACT, LEGACY_EXECUTION_CONTRACT
from review import make_packet
from review_packet import project_packet
from suite import REPO, prepare
from test_eval import STOP_REPORT, encode_trace, trace_rows


class RecoveryGradingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="recovery-grading-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.skill = self.base / "skill"
        for name in ("SKILL.md", "scripts/report_check.py", "assets/analysis-fix.template.md"):
            write_new(self.skill / name, read_regular(REPO / "skills/ci-self-healing" / name))

    def fixture(self, contract):
        root = self.base / contract
        manifest = prepare(root, self.skill, repeats=1, mode="single")
        row = next(r for r in manifest["trials"] if r["case"] == "case-001")
        trial = root / "trials" / row["id"]
        write_json(root / "runtime-binding.json", {"synthetic": True})
        write_json(trial / "before-run.json", snapshot(trial / "visible"))
        path = trial / "visible/scene/sourcecode/src/main/java/example/Feature.java"
        path.write_text(path.read_text().replace("route(payload, condition, slot)", "route(payload)"))
        report = STOP_REPORT.replace("未声明或执行检查；未创建候选。", """| 检查或命令 | 执行位置/目录或运行身份 | 结果 | 覆盖范围与未覆盖内容 | 证据 |
| --- | --- | --- | --- | --- |
| compile | fixture | passed | synthetic orchestration only | fixture receipt |""")
        write_new(trial / "visible/output/analysis-fix.md", report)
        write_json(trial / "after-run.json", snapshot(trial / "visible"))
        raw = encode_trace(trace_rows())
        write_new(trial / "execution/stdout.log", raw)
        write_json(trial / "run.json", {"status": "completed", "process": {"returncode": 0},
                   "backend": "native", "environment": {}, "expected_model_id": "glm-5.2",
                   "stdout_sha256": digest(raw), "skill_execution_contract": contract})
        return root, row, trial

    def test_v2_recovery_unblocks_build_and_review_but_not_acceptance(self):
        root, row, trial = self.fixture(EXECUTION_CONTRACT)
        incident = {"check": "build_without_confirmed_native_capture", "seq": 10, "paths": []}
        audit = {"contract": EXECUTION_CONTRACT, "failures": [], "gaps": [],
                 "violations": [incident], "recoveries": [{"violation": incident, "check_seq": 20}]}
        with mock.patch("grading.audit_execution", return_value=audit) as check, \
                mock.patch("native_repair.tool_identity", return_value={}), \
                mock.patch("grading.probe_build", return_value={"status": "passed"}) as build:
            result = grade_trial(root, row, {"backend": "native"})
        build.assert_called_once()
        self.assertEqual(check.call_args.kwargs, {"contract": EXECUTION_CONTRACT, "trace_gaps": []})
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual(combined_status(result, None), "incomplete")
        self.assertEqual(result["report_check"]["status"], "passed")
        write_json(trial / "grade.json", result)
        packet = project_packet(make_packet(root, row))
        self.assertEqual(packet["deterministic"]["execution_checks"]["violations"], [incident])
        self.assertEqual(packet["deterministic"]["execution_checks"]["recoveries"], audit["recoveries"])
        rejected = {"status": "validated", "verdict": {"verdict": "fail"}}
        self.assertEqual(combined_status(result, rejected), "failed")

    def test_v1_dispatch_and_other_failure_still_block_build(self):
        for contract, failure in ((LEGACY_EXECUTION_CONTRACT, "build_without_confirmed_native_capture"),
                                  (EXECUTION_CONTRACT, "skill_native_tool_bypassed")):
            with self.subTest(contract=contract):
                root, row, _ = self.fixture(contract)
                audit = {"failures": [{"check": failure, "paths": []}], "gaps": []}
                with mock.patch("grading.audit_execution", return_value=audit) as check, \
                        mock.patch("grading.probe_build") as build:
                    result = grade_trial(root, row, None)
                self.assertEqual(check.call_args.kwargs["contract"], contract)
                self.assertEqual(result["status"], "failed")
                build.assert_not_called()

    def test_unknown_contract_is_not_silently_ignored(self):
        root, row, _ = self.fixture("skill-execution-unknown")
        with mock.patch("grading.audit_execution") as audit:
            result = grade_trial(root, row, None)
        audit.assert_not_called()
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("unsupported_skill_execution_contract", result["gaps"])


if __name__ == "__main__":
    unittest.main()
