"""Offline E2 protocol and provenance controls; no CLI or model invocations."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ab_review
from ab_review_collection import collect, collect_record, resource_aliases, strict_json
from common import digest, json_bytes, read_regular, replace_json, write_json, write_new
from native_runner import exchange_receipt
from test_native_responses import responses_trace


def packet():
    origin = {"session_id": "executor-session", "invocation_id": "executor-turn"}
    return {"evidence_ids": ["E0001", "E0002"], "shared_resources": {"SKILL.md": "Full body\n"},
            "events": [
                {"seq": 1, "kind": "InvocationToolCallStart", "evidence_id": "E0001", "payload": {
                    "call_id": "load", "origin": origin, "tool_name": "load_skill", "args": {"skill_name": "ci-self-healing"}}},
                {"seq": 2, "kind": "InvocationToolCallResult", "evidence_id": "E0002", "payload": {
                    "call_id": "load", "origin": copy.deepcopy(origin), "result": {"shared_resource": "SKILL.md",
                    "matched_full_body": True, "sha256": digest(b"Full body\n")}}}]}


def opinion(status="pass", reference="E0002"):
    return {"checks": [{"id": key, "status": status, "evidence": [reference], "reason": "Frozen reason"}
                       for key in ab_review.CRITERIA],
            "summary": "Frozen summary", "verdict": "pass" if status == "not_applicable" else status}


class ReviewCollectionTests(unittest.TestCase):
    def resolve(self, value, evidence=None):
        return collect(value if isinstance(value, str) else json.dumps(value), packet() if evidence is None else evidence)

    def assertRejected(self, value, evidence=None, message=None):
        result = self.resolve(value, evidence)
        self.assertEqual(result["status"], "incomplete")
        self.assertIsNone(result["verdict"])
        self.assertNotIn("opinion", result)
        if message:
            self.assertIn(message, "; ".join(result["gaps"]))
        return result

    def test_canonical_output_is_unchanged_for_every_valid_status(self):
        for status in ("pass", "fail", "incomplete", "not_applicable"):
            with self.subTest(status=status):
                value = opinion(status)
                result = self.resolve(value)
                self.assertEqual(result["status"], "accepted")
                self.assertEqual(result["opinion"], value)
                self.assertEqual(result["normalizations"], [])

    def test_only_literal_tab_encoding_changes_and_decoded_text_is_retained(self):
        value = opinion()
        value["checks"][0]["reason"] = 'Quoted "reason" with\ttab and \\t literal'
        text = json.dumps(value).replace("with\\ttab", "with\ttab")
        result = self.resolve(text)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["opinion"], value)
        self.assertEqual(result["raw_text_sha256"], digest(text.encode()))
        self.assertEqual(result["normalizations"][0]["character_offsets"], [text.index("\t")])
        # JSON whitespace outside strings is already legal, and is not rewritten.
        self.assertEqual(self.resolve("\t" + json.dumps(value))["normalizations"], [])

    def test_raw_non_tab_controls_and_invalid_escapes_stay_rejected(self):
        text = json.dumps(opinion())
        for char in ("\n", "\r", "\x00", "\x08", "\x1f", "\\\t"):
            with self.subTest(character=repr(char)):
                self.assertRejected(text.replace("Frozen reason", "reason" + char, 1))
        self.assertRejected(text + "\x00")

    def test_no_fences_truncation_trailing_prose_or_multiple_objects_are_rescued(self):
        text = json.dumps(opinion())
        for invalid in ("```json\n" + text + "\n```", text[:-1], text + "{}", "Here: " + text, text + " done"):
            with self.subTest(invalid=invalid[:30]):
                self.assertRejected(invalid)

    def test_duplicate_keys_at_any_depth_and_non_json_constants_are_rejected(self):
        text = json.dumps(opinion())
        for invalid in (text.replace('"verdict": "pass"', '"verdict":"fail", "verdict":"pass"'),
                        text.replace('"status": "pass"', '"status":"fail", "status":"pass"', 1),
                        text.replace('"summary": "Frozen summary"', '"summary":NaN'),
                        text.replace('"summary": "Frozen summary"', '"summary":Infinity')):
            with self.subTest(invalid=invalid[-100:]):
                self.assertRejected(invalid)

    def test_complete_map_preserves_all_judgments_reasons_and_overall_opinion(self):
        value = opinion("fail")
        expected = copy.deepcopy(value)
        value["checks"] = {row.pop("id"): row for row in value["checks"]}
        original = copy.deepcopy(value)
        result = self.resolve(value)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["opinion"], expected)
        self.assertEqual(result["original_opinion"], original)
        self.assertEqual(value, original)
        # Repeating the same explicit id is safe; conflicting identities are not.
        value["checks"]["task_scope"]["id"] = "task_scope"
        self.assertEqual(self.resolve(value)["opinion"], expected)
        value["checks"]["task_scope"]["id"] = "truthful_delivery"
        self.assertRejected(value, message="key/id conflict")

    def test_missing_extra_duplicate_and_invalid_check_entries_are_not_filled(self):
        value = opinion()
        value["checks"].pop()
        self.assertRejected(value, message="truthful_delivery")
        value = opinion()
        value["checks"][-1] = copy.deepcopy(value["checks"][0])
        self.assertRejected(value, message="duplicate")
        value = opinion()
        value["checks"] = {row.pop("id"): row for row in value["checks"]}
        del value["checks"]["truthful_delivery"]
        self.assertRejected(value, message="truthful_delivery")
        value["checks"]["invented"] = {"status": "pass"}
        self.assertRejected(value, message="invented")
        for row in (None, [], "pass", {"id": []}, {"id": "task_scope", "status": []}):
            value = opinion()
            value["checks"][0] = row
            with self.subTest(row=row):
                self.assertRejected(value)

    def test_evidence_note_remains_visible_and_is_never_discarded(self):
        value = opinion()
        value["evidence_note"] = "Evidence limitation retained verbatim."
        result = self.resolve(value)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["opinion"], value)
        for invalid in (None, "", "  ", {"verdict": "fail"}, []):
            value["evidence_note"] = invalid
            with self.subTest(note=invalid):
                self.assertRejected(value, message="evidence_note")

    def test_unknown_fields_empty_summary_and_inconsistent_verdict_stay_rejected(self):
        for key, invalid in (("summary", None), ("summary", " "), ("verdict", "fail"), ("verdict", []),
                             ("override", "pass")):
            value = opinion(); value[key] = invalid
            with self.subTest(field=key, value=invalid):
                self.assertRejected(value)
        value = opinion(); value["checks"][0]["extra"] = "ignored?"
        self.assertRejected(value)

    def test_exact_aliases_keep_every_semantic_value_and_inputs_unchanged(self):
        for status in ("pass", "fail", "incomplete", "not_applicable"):
            value, evidence = opinion(status, "SKILL.md"), packet()
            originals = copy.deepcopy((value, evidence))
            result = self.resolve(value, evidence)
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(result["opinion"], opinion(status))
            self.assertEqual((value, evidence), originals)
            self.assertEqual(result["mapping"]["SKILL.md"]["evidence_id"], "E0002")

    def test_named_resource_read_is_supported_without_prefix_matching(self):
        evidence = packet()
        name = "references/example.md"
        evidence["shared_resources"] = {name: "Full body\n"}
        evidence["events"][0]["payload"].update(tool_name="read_skill_resource",
            args={"skill_name": "ci-self-healing", "resource_name": name})
        evidence["events"][1]["payload"]["result"]["shared_resource"] = name
        self.assertEqual(self.resolve(opinion(reference=name), evidence)["status"], "accepted")
        self.assertRejected(opinion(reference="example.md"), evidence)

    def test_unknown_refs_are_never_dropped_guessed_or_case_folded(self):
        for reference in ("E0099", "skill.md", "./SKILL.md", "SKILL.md:1", " SKILL.md ", {}, [], None):
            value = opinion(); value["checks"][0]["evidence"].append(reference)
            with self.subTest(reference=reference):
                result = self.assertRejected(value, message="unknown review evidence")
                self.assertEqual(result["unresolved_references"], [{"check_id": "task_scope", "reference": reference}])

    def test_incomplete_full_read_bindings_and_ambiguous_calls_cannot_create_aliases(self):
        def mutate(evidence, case):
            start, end = evidence["events"]
            if case == "partial": end["payload"]["result"]["matched_full_body"] = False
            elif case == "changed_body": evidence["shared_resources"]["SKILL.md"] += "changed"
            elif case == "wrong_hash": end["payload"]["result"]["sha256"] = "0" * 64
            elif case == "absent_body": evidence["shared_resources"] = {}
            elif case == "wrong_name": end["payload"]["result"]["shared_resource"] = "elsewhere.md"
            elif case == "wrong_call": end["payload"]["call_id"] = "other"
            elif case == "wrong_origin": end["payload"]["origin"]["session_id"] = "other"
            elif case == "no_origin": start["payload"].pop("origin")
            elif case == "wrong_skill": start["payload"]["args"]["skill_name"] = "other"
            elif case == "wrong_tool": start["payload"]["tool_name"] = "read_file"
            elif case == "reversed": start["seq"] = 3
            elif case == "bool_sequence": start["seq"] = True
            elif case == "unregistered_event": evidence["evidence_ids"].remove("E0002")
            elif case == "duplicate_start": evidence["events"].append(copy.deepcopy(start))
            elif case == "duplicate_result": evidence["events"].append(copy.deepcopy(end))
            elif case == "duplicate_event_id": evidence["events"].append({"kind": "observer", "evidence_id": "E0002", "payload": {}})
        for case in ("partial", "changed_body", "wrong_hash", "absent_body", "wrong_name", "wrong_call", "wrong_origin",
                     "no_origin", "wrong_skill", "wrong_tool", "reversed", "bool_sequence", "unregistered_event",
                     "duplicate_start", "duplicate_result", "duplicate_event_id"):
            evidence = packet(); mutate(evidence, case)
            with self.subTest(case=case):
                self.assertRejected(opinion(reference="SKILL.md"), evidence)

    def test_prompt_lists_real_resource_ids_and_all_required_criteria(self):
        text = ab_review.prompt(packet())
        self.assertIn('"SKILL.md": "E0002"', text)
        for key in ab_review.CRITERIA:
            self.assertIn('"' + key + '"', text)
        evidence = packet(); evidence["events"] = []
        self.assertEqual(resource_aliases(evidence), {})


class ReviewRecordTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name).resolve() / "trial/review"
        model = ab_review.MODEL_ID
        rows = responses_trace()
        rows[5]["payload"]["request_model"] = model
        rows[7]["payload"]["response_model"] = ab_review.RESPONSE_MODEL_ID
        trajectory = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
        self.exchange = exchange_receipt(trajectory, "session-1", model,
                                        expected_response_model=ab_review.RESPONSE_MODEL_ID)
        process = {"status": "completed", "returncode": 0}
        self.value = opinion(reference="SKILL.md")
        public = {"session_id": "session-1", "result": json.dumps(self.value)}
        frozen = packet(); task = "Frozen review prompt"
        session = {"meta": {"model_id": model, "session_id": "session-1"}, "state": {}}
        self.files = {
            "result.json": {"status": "incomplete", "verdict": None, "gaps": ["unknown review evidence"],
                "process": process, "model": {"model_id": model, "profile_sha256": "a" * 64}},
            "binding.json": {"model_id": model, "profile_sha256": "a" * 64,
                "packet_sha256": digest(json_bytes(frozen)), "prompt_sha256": digest(task.encode())},
            "execution/process.json": process, "public-response.json": public, "execution/stdout.log": public,
            "input/packet.json": frozen, "input/model-packet.json": frozen,
            "session-receipt.json": {"sessions": [{"meta": session["meta"], "path": "s/session.json",
                "sha256": digest(json_bytes(session))}]},
            "sessions/s/session.json": session, "exchange-receipt.json": self.exchange}
        for name, value in self.files.items():
            write_json(self.folder / name, value)
        write_new(self.folder / "input/task.txt", task)
        write_new(self.folder / "sessions/s/trajectory/events.jsonl", trajectory)

    def test_record_collection_preserves_sources_and_verifies_native_provenance(self):
        before = {str(p): digest(read_regular(p)) for p in self.folder.rglob("*") if p.is_file()}
        result = collect_record(self.folder)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["provenance"], "validated")
        self.assertEqual(result["original_opinion"], self.value)
        self.assertEqual(result["opinion"], opinion())
        self.assertEqual(before, {str(p): digest(read_regular(p)) for p in self.folder.rglob("*") if p.is_file()})

    def test_each_frozen_identity_or_content_mismatch_is_rejected(self):
        for name, key, replacement in [
            ("binding.json", "profile_sha256", "b" * 64),
            ("binding.json", "packet_sha256", "b" * 64),
            ("binding.json", "prompt_sha256", "b" * 64),
            ("public-response.json", "result", json.dumps(opinion("fail"))),
            ("execution/process.json", "returncode", 1),
            ("input/model-packet.json", "events", []),
            ("sessions/s/session.json", "state", {"changed": True}),
            ("exchange-receipt.json", "response_model", "similar-model"),
            ("exchange-receipt.json", "trajectory_sha256", "b" * 64),
        ]:
            changed = copy.deepcopy(self.files[name]); changed[key] = replacement
            replace_json(self.folder / name, changed)
            with self.subTest(name=name, field=key):
                result = collect_record(self.folder)
                self.assertEqual(result["status"], "incomplete")
                self.assertEqual(result["stage"], "provenance")
                self.assertNotIn("opinion", result)
            replace_json(self.folder / name, self.files[name])

    def test_timeout_is_not_rescued_by_well_formed_final_text(self):
        changed = copy.deepcopy(self.files["result.json"])
        changed["process"] = {"status": "timeout", "returncode": 0}
        replace_json(self.folder / "result.json", changed)
        result = collect_record(self.folder)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("did not complete", result["gaps"][0])

    def test_session_path_traversal_and_symlinks_are_rejected(self):
        for path in ("../session.json", "/tmp/session.json"):
            changed = copy.deepcopy(self.files["session-receipt.json"])
            changed["sessions"][0]["path"] = path
            replace_json(self.folder / "session-receipt.json", changed)
            self.assertEqual(collect_record(self.folder)["status"], "incomplete")
        replace_json(self.folder / "session-receipt.json", self.files["session-receipt.json"])
        source = self.folder / "input/packet.json"
        source.unlink(); source.symlink_to(self.folder / "input/model-packet.json")
        self.assertEqual(collect_record(self.folder)["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
