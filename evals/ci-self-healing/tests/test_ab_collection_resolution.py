"""Offline evidence-registration tests; fake receipts are not model executions."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ab_collection_resolution import MODEL_ID, RESPONSE_MODEL_ID, resolve_collection
from ab_review import CRITERIA
from common import digest, json_bytes, read_regular, replace_json, write_json, write_new


class CollectionResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.trial = Path(self.temp.name).resolve() / "trial"
        self.folder = self.trial / "review"
        self.opinion = {"verdict": "pass", "summary": "Original model judgment",
            "checks": [{"id": key, "status": "pass", "evidence": ["SKILL.md"], "reason": "Original model reason"} for key in CRITERIA]}
        self.packet = {"shared_resources": {"SKILL.md": "# Frozen full skill body\n"}, "evidence_ids": ["E0002"],
            "events": [{"seq": 1, "kind": "InvocationToolCallStart", "evidence_id": "E0001",
                        "payload": {"call_id": "load", "tool_name": "load_skill", "args": {"skill_name": "ci-self-healing"}}},
                       {"seq": 2, "kind": "InvocationToolCallResult", "evidence_id": "E0002",
                        "payload": {"call_id": "load", "result": {"shared_resource": "SKILL.md", "matched_full_body": True,
                                    "sha256": digest(b"# Frozen full skill body\n")}}}]}
        self.original = {"status": "incomplete", "verdict": None, "gaps": ["unknown review evidence"],
            "process": {"status": "completed", "returncode": 0}, "model": {"model_id": MODEL_ID, "profile_sha256": "a" * 64}}
        self.exchange = {"status": "validated", "exchange_count": 1, "request_model": MODEL_ID, "response_model": RESPONSE_MODEL_ID,
            "session_id": "s", "response_id": "fixture-response", "trajectory_sha256": digest(b"synthetic trajectory metadata\n")}
        write_json(self.folder / "result.json", self.original)
        write_json(self.folder / "exchange-receipt.json", self.exchange)
        write_json(self.folder / "session-receipt.json", {"sessions": [{"path": "s/session.json", "meta": {"session_id": "s", "model_id": MODEL_ID}}]})
        write_new(self.folder / "sessions/s/trajectory/events.jsonl", b"synthetic trajectory metadata\n")
        self.save_packet(); self.save_opinion()

    def save_packet(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "input").mkdir(exist_ok=True)
        replace_json(self.folder / "input/packet.json", self.packet)
        replace_json(self.folder / "binding.json", {"model_id": MODEL_ID, "profile_sha256": "a" * 64,
                     "packet_sha256": digest(json_bytes(self.packet))})

    def save_opinion(self):
        raw = {"session_id": "s", "result": json.dumps(self.opinion)}
        (self.folder / "execution").mkdir(exist_ok=True)
        replace_json(self.folder / "public-response.json", raw)
        replace_json(self.folder / "execution/stdout.log", raw)

    def resolve(self):
        return resolve_collection(self.trial, self.original, self.exchange, snapshot_root=self.trial / "analysis")

    def test_exact_alias_collects_pass_and_fail_without_changing_semantics(self):
        for verdict in ("pass", "fail"):
            self.opinion["verdict"] = verdict
            self.opinion["checks"][0]["status"] = verdict
            self.save_opinion()
            result = self.resolve()
            self.assertEqual(result["status"], "corrected")
            self.assertEqual(result["mapping"], {"SKILL.md": "E0002"})
            self.assertEqual(result["effective_review"]["verdict"], verdict)
            self.assertEqual(result["original_opinion"], self.opinion)
            for a, b in zip(self.opinion["checks"], result["effective_opinion"]["checks"]):
                self.assertEqual(a["status"], b["status"])
                self.assertEqual(a["reason"], b["reason"])
                self.assertEqual(b["evidence"], ["E0002"])

    def test_unknown_id_is_not_accepted_as_similar_resource_name(self):
        self.opinion["checks"][0]["evidence"] = ["skill.md"]
        self.save_opinion()
        self.assertEqual(self.resolve()["status"], "rejected")

    def test_partial_or_unprovided_resource_cannot_be_registered(self):
        self.packet["events"][1]["payload"]["result"]["matched_full_body"] = False
        self.save_packet()
        self.assertEqual(self.resolve()["status"], "rejected")
        self.packet["events"][1]["payload"]["result"]["matched_full_body"] = True
        self.packet["shared_resources"] = {}
        self.save_packet()
        self.assertEqual(self.resolve()["status"], "rejected")

    def test_wrong_identity_extra_error_or_invalid_verdict_remains_incomplete(self):
        self.exchange["response_model"] = "unexpected-model"
        self.assertEqual(self.resolve()["status"], "rejected")
        self.exchange["response_model"] = RESPONSE_MODEL_ID
        self.original["gaps"].append("another-error")
        self.assertEqual(self.resolve()["status"], "not_applicable")
        self.original["gaps"] = ["unknown review evidence"]
        self.opinion["verdict"] = "fail"  # All check statuses still pass.
        self.save_opinion()
        self.assertEqual(self.resolve()["status"], "rejected")

    def test_full_body_marker_requires_a_real_paired_tool_event(self):
        self.packet["events"][0]["payload"]["call_id"] = "unrelated"
        self.save_packet()
        self.assertEqual(self.resolve()["status"], "rejected")

    def test_original_files_and_immutable_correction_snapshot_are_preserved(self):
        paths = [self.folder / name for name in ("result.json", "public-response.json", "exchange-receipt.json")]
        before = [read_regular(p) for p in paths]
        first, second = self.resolve(), self.resolve()
        self.assertEqual(first["snapshot_path"], second["snapshot_path"])
        self.assertEqual([read_regular(p) for p in paths], before)


if __name__ == "__main__":
    unittest.main()
