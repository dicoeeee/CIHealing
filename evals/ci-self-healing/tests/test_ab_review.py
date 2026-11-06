"""Strict review JSON calibration with synthetic evidence; no model or CLI calls."""
import ast
import copy
import inspect
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ab_review


class ABReviewValidationTests(unittest.TestCase):
    EVIDENCE = {"request:synthetic", "event:001", "artifact:final"}

    def result(self, status="pass"):
        return {
            "checks": [{"id": criterion, "status": status,
                        "evidence": [] if status == "incomplete" else ["event:001"],
                        "reason": "Synthetic evidence supports this criterion."}
                       for criterion in ab_review.CRITERIA],
            "verdict": "pass" if status == "not_applicable" else status,
            "summary": "Synthetic calibration; no real execution or model judgment.",
        }

    def test_complete_known_evidence_pass_is_accepted(self):
        self.assertEqual(ab_review.validate(self.result(), self.EVIDENCE), "pass")

    def test_missing_top_level_field_is_rejected(self):
        for field in ("checks", "verdict", "summary"):
            with self.subTest(field=field):
                value = self.result()
                del value[field]
                with self.assertRaises(ValueError):
                    ab_review.validate(value, self.EVIDENCE)

    def test_missing_criterion_and_duplicate_id_are_rejected(self):
        missing = self.result()
        missing["checks"].pop()
        duplicate = self.result()
        duplicate["checks"][-1]["id"] = duplicate["checks"][0]["id"]
        for value in (missing, duplicate):
            with self.subTest(ids=[row["id"] for row in value["checks"]]):
                with self.assertRaisesRegex(ValueError, "criteria missing or duplicate"):
                    ab_review.validate(value, self.EVIDENCE)

    def test_unknown_criterion_cannot_replace_a_required_id(self):
        value = self.result()
        value["checks"][0]["id"] = "not-a-contract-criterion"
        with self.assertRaises(ValueError):
            ab_review.validate(value, self.EVIDENCE)

    def test_missing_or_extra_criterion_fields_are_rejected(self):
        for field in ("status", "evidence", "reason"):
            value = self.result()
            del value["checks"][0][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                ab_review.validate(value, self.EVIDENCE)
        value = self.result()
        value["checks"][0]["self_reported_pass"] = True
        with self.assertRaises(ValueError):
            ab_review.validate(value, self.EVIDENCE)

    def test_unknown_evidence_is_rejected_even_for_incomplete(self):
        for status in ("pass", "fail", "not_applicable", "incomplete"):
            value = self.result(status)
            value["checks"][0]["evidence"] = ["invented:999"]
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "unknown review evidence"):
                ab_review.validate(value, self.EVIDENCE)

    def test_assertions_require_nonempty_evidence_lists(self):
        for status in ("pass", "fail", "not_applicable"):
            value = self.result(status)
            value["checks"][0]["evidence"] = []
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "assertion lacks evidence"):
                ab_review.validate(value, self.EVIDENCE)
        value = self.result()
        value["checks"][0]["evidence"] = "event:001"
        with self.assertRaisesRegex(ValueError, "unknown review evidence"):
            ab_review.validate(value, self.EVIDENCE)

    def test_all_incomplete_accepts_no_references_but_requires_reason(self):
        value = self.result("incomplete")
        self.assertEqual(ab_review.validate(value, set()), "incomplete")
        for reason in (None, "", "  "):
            bad = copy.deepcopy(value)
            bad["checks"][0]["reason"] = reason
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, "missing review reasoning"):
                ab_review.validate(bad, set())

    def test_not_applicable_with_evidence_is_accepted(self):
        value = self.result("not_applicable")
        self.assertEqual(ab_review.validate(value, self.EVIDENCE), "pass")
        value["checks"][0]["reason"] = ""
        with self.assertRaises(ValueError):
            ab_review.validate(value, self.EVIDENCE)

    def test_fail_has_precedence_over_incomplete(self):
        value = self.result("incomplete")
        value["checks"][0].update(status="fail", evidence=["event:001"])
        value["verdict"] = "fail"
        self.assertEqual(ab_review.validate(value, self.EVIDENCE), "fail")
        for incorrect in ("pass", "incomplete"):
            value["verdict"] = incorrect
            with self.subTest(verdict=incorrect), self.assertRaisesRegex(ValueError, "verdict differs"):
                ab_review.validate(value, self.EVIDENCE)

    def test_incomplete_prevents_pass_and_cannot_be_relabelled_fail(self):
        value = self.result()
        value["checks"][0].update(status="incomplete", evidence=[])
        value["verdict"] = "incomplete"
        self.assertEqual(ab_review.validate(value, self.EVIDENCE), "incomplete")
        for incorrect in ("pass", "fail"):
            value["verdict"] = incorrect
            with self.subTest(verdict=incorrect), self.assertRaises(ValueError):
                ab_review.validate(value, self.EVIDENCE)

    def test_all_pass_cannot_have_contradictory_overall_verdict(self):
        for verdict in ("fail", "incomplete", "not_applicable", "PASS"):
            value = self.result()
            value["verdict"] = verdict
            with self.subTest(verdict=verdict), self.assertRaises(ValueError):
                ab_review.validate(value, self.EVIDENCE)

    def test_unknown_status_is_rejected(self):
        value = self.result()
        value["checks"][0]["status"] = "not_run"
        with self.assertRaisesRegex(ValueError, "invalid review criterion"):
            ab_review.validate(value, self.EVIDENCE)

    def test_exact_request_and_response_identity_are_bound_to_receipt(self):
        self.assertEqual(ab_review.MODEL_ID, "deepseek-v4.1-flash")
        self.assertEqual(ab_review.RESPONSE_MODEL_ID, "deepseek-v4-1-flash-261017")
        self.assertEqual(ab_review.PROFILE_NAME, "huoshan-ds-v4.1-flash")
        # Inspect wiring without entering run(), reading credentials, launching
        # iCode, or replacing any frozen runner implementation.
        tree = ast.parse(inspect.getsource(ab_review.run))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "exchange_receipt"]
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertIsInstance(call.args[2], ast.Name)
        self.assertEqual(call.args[2].id, "MODEL_ID")
        expected = [item.value for item in call.keywords if item.arg == "expected_response_model"]
        self.assertEqual(len(expected), 1)
        self.assertIsInstance(expected[0], ast.Name)
        self.assertEqual(expected[0].id, "RESPONSE_MODEL_ID")


if __name__ == "__main__":
    unittest.main()
