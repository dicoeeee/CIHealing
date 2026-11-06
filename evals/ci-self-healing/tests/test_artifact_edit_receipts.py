"""Native receipt regressions: no model calls or changes to frozen evidence."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import digest
from delivery_evidence import artifact_checkpoint


class ArtifactEditReceiptTests(unittest.TestCase):
    def setUp(self):
        self.path = "/trial/visible/output/[报告].md"
        self.events = []
        self.original = "first\nold\nlast\n"
        self.final = "first\nnew\nlast\n"

    def call(self, tool, args, reply, *, mutation=False):
        seq = len(self.events) + 1
        call = {"call_id": str(seq), "tool_name": tool, "args": args}
        result = {"call_id": str(seq), "result": reply}
        if mutation:
            result["changed_paths_since_previous_event"] = ["output/" + Path(self.path).name]
        self.events.extend([
            {"seq": seq, "kind": "InvocationToolCallStart", "payload": call},
            {"seq": seq + 1, "kind": "InvocationToolCallResult", "payload": result},
        ])

    def write(self):
        self.call("write_file", {"path": self.path, "content": self.original},
                  f"Written {len(self.original)} chars (3 lines) to {self.path}.", mutation=True)

    def receipt(self, count=1, path=None):
        plural = "" if count == 1 else "s"
        return (f"Replaced {count} occurrence{plural} in {path or self.path}.\n"
                "Before:\n  1|first\n  2|old\n  3|last\n"
                "After:\n  1|first\n  2|new\n  3|last")

    def edit(self, reply=None, **args):
        self.call("edit_file", {"path": self.path, "old_string": "old", "new_string": "new", **args},
                  self.receipt() if reply is None else reply, mutation=True)

    def check(self, *, classification="none", body=None):
        result = {"format_status": "valid", "sha256": digest((self.final if body is None else body).encode()), "errors": []}
        self.call("run_skill_script", {"skill_name": "ci-self-healing", "script_name": "scripts/report_check.py",
                  "arguments": ["--report", self.path, "--classification", classification, "--checks", "none"]}, json.dumps(result))

    def read(self, body=None):
        self.call("read_file", {"path": self.path}, self.final if body is None else body)

    def audit(self):
        return artifact_checkpoint(self.events, self.path, self.final.encode(), "scripts/report_check.py",
                                   {"--classification": "none", "--checks": "none"})

    def test_actual_native_single_edit_replays_exact_final_bytes(self):
        self.write(); self.edit(); self.check(); self.read()
        self.assertEqual(self.audit()["status"], "passed")

    def test_plural_replace_all_preview_is_not_authoritative_file_content(self):
        self.original = "old old\n"; self.final = "new new\n"
        self.write()
        # The native tool previews only the first replacement, even with replace_all.
        self.edit(f"Replaced 2 occurrences in {self.path}.\nBefore:\n  1|old old\nAfter:\n  1|new old", replace_all=True)
        self.check(); self.read()
        self.assertEqual(self.audit()["status"], "passed")

    def test_legacy_header_only_format_remains_supported(self):
        self.write(); self.edit(f"Replaced 1 occurrence(s) in {self.path}."); self.check(); self.read()
        self.assertEqual(self.audit()["status"], "passed")

    def test_elided_native_preview_is_supported_without_becoming_readback(self):
        self.write()
        self.edit(f"Replaced 1 occurrence in {self.path}.\nBefore:\n  1|first\n"
                  "  [... 20 lines omitted (2-21) ...]\n  22|old\nAfter:\n  1|new")
        self.check()
        self.assertIn("artifact_complete_readback_missing", self.audit()["gaps"])
        self.read()
        self.assertEqual(self.audit()["status"], "passed")

    def test_empty_after_preview_still_allows_literal_replay(self):
        self.original = "old"; self.final = ""
        self.write(); self.edit(f"Replaced 1 occurrence in {self.path}.\nBefore:\n  1|old\nAfter:\n", new_string="")
        self.check(); self.read()
        result = self.audit()
        self.assertNotIn("artifact_write_receipt_unconfirmed", result["gaps"])
        self.assertNotIn("artifact_final_bytes_not_replayed", result["gaps"])

    def test_wrong_path_error_truncation_and_unknown_suffix_are_rejected(self):
        bad = [self.receipt(path=self.path + ".other"), "edit failed", self.receipt() + "\n[exit_code: 1]",
               self.receipt().split("\n", 1)[0], self.receipt().split("\nAfter:", 1)[0],
               self.receipt().replace("Before:", "Unknown:"), self.receipt().replace("After:\n", "After: error\n"),
               self.receipt().replace("Replaced 1 occurrence", "Replaced 0 occurrences"),
               self.receipt().replace("Replaced 1 occurrence", "Replaced 1 occurrences"),
               self.receipt().replace("  2|new", "tool_error: refused")]
        for reply in bad:
            with self.subTest(reply=reply):
                self.events.clear(); self.write(); self.edit(reply); self.check(); self.read()
                self.assertEqual(self.audit()["status"], "incomplete")
                self.assertIn("artifact_write_receipt_unconfirmed", self.audit()["gaps"])

    def test_replacement_count_and_arguments_must_be_replayable(self):
        cases = [(self.receipt(2), {}), (self.receipt(), {"old_string": "missing"}),
                 (self.receipt(), {"old_string": ""}), (self.receipt(), {"new_string": None}),
                 (self.receipt(), {"replace_all": "false"})]
        for reply, args in cases:
            with self.subTest(args=args, reply=reply):
                self.events.clear(); self.write(); self.edit(reply, **args); self.check(); self.read()
                self.assertIn("artifact_edit_not_replayable", self.audit()["gaps"])
                self.assertNotEqual(self.audit()["status"], "passed")
        self.events.clear(); self.original = "old old\n"; self.final = "new old\n"
        self.write(); self.edit(); self.check(); self.read()
        self.assertIn("artifact_edit_not_replayable", self.audit()["gaps"])

    def test_valid_receipt_cannot_mask_final_content_or_checker_requirement_mismatch(self):
        self.write(); self.edit(); self.final = "different final bytes\n"; self.check(); self.read()
        self.assertIn("artifact_final_bytes_not_replayed", self.audit()["gaps"])
        self.events.clear(); self.final = "first\nnew\nlast\n"
        self.write(); self.edit(); self.check(classification="required"); self.read()
        self.assertIn("artifact_check_requirement_mismatch", self.audit()["gaps"])

    def test_pre_edit_checks_and_reads_do_not_satisfy_final_delivery(self):
        self.write(); self.check(body=self.original); self.read(self.original); self.edit()
        result = self.audit()
        self.assertIn("artifact_final_check_missing", result["gaps"])
        self.assertIn("artifact_complete_readback_missing", result["gaps"])
        self.check(); self.read("2|new")
        self.assertIn("artifact_complete_readback_missing", self.audit()["gaps"])
        self.read()
        self.assertEqual(self.audit()["status"], "passed")


if __name__ == "__main__":
    unittest.main()
