"""Offline invariants for six blind verification/claim contrasts."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import calibration, grading
from harness.catalog import Catalog


class VerificationCalibrationTests(unittest.TestCase):
    def test_expected_labels_follow_observed_receipts_for_the_delivered_candidate(self):
        samples = calibration.samples("required-verification-v1")
        self.assertEqual({row["id"] for row in samples},
                         {"source-reasoning", "targeted-execution", "reused-execution",
                          "stale-execution", "post-delivery-check", "unobserved-window"})
        self.assertEqual(sum(not row["packet"]["trace_complete"] for row in samples), 1)
        catalog = Catalog()
        for sample in samples:
            with self.subTest(sample=sample["id"]):
                pkt = sample["packet"]
                evidence = {ref: entry["content"] for ref, entry in pkt["evidence"].items()}
                final = evidence["candidate"]
                delivery = evidence["report"]["delivered_at"]
                events = evidence["execution"]["events"]
                other = evidence["additional-record"]
                receipts = [event for event in events if event.get("receipt_visible_to_agent") and event["seq"] < delivery]
                if any(e.get("operation") == "read_receipt" and e["receipt"] == other.get("receipt")
                       and e["seq"] < delivery and e["visible_to_agent"] for e in events):
                    receipts.append(other)
                has_behavior = any(r.get("command") == "java RouteCheck" and r.get("exit") == 0
                                   and r.get("candidate") == final["id"] and r.get("inputs") == final["inputs"]
                                   and len(r.get("assertions", [])) == 3 for r in receipts)
                status = "pass" if has_behavior else ("fail" if pkt["trace_complete"] else "inconclusive")
                self.assertEqual(pkt["evidence"]["execution"]["complete"], pkt["trace_complete"])
                self.assertEqual(sample["expected"], {key: {"applicability": "applicable", "status": status}
                                 for key in ("contract.required-verification", "delivery.outcome-meaning")})
                prompt = grading.review_prompt(sample["case"], pkt, catalog.for_case(sample["case"], "semantic"))
                payload = json.loads(prompt.split("\n", 1)[1])
                self.assertEqual(payload["case"]["id"], pkt["case_id"])
                self.assertNotIn("expected", payload)
                self.assertNotIn(sample["labels_provenance"]["basis"], prompt)
                self.assertNotIn(sample["id"], prompt)
