"""Literal keyword fixture and actual CLI recording tests; no model calls."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from keyword_cases import SCENARIOS, audit, prepare, read, search


class KeywordSearchTests(unittest.TestCase):
    def ids(self, scenario, query, page=1):
        return [r["case_id"] for r in search(scenario, query, page)["items"]]

    def test_priority_pair_has_identical_corpus_and_results(self):
        for query in ("边缘扫描器 服务身份", "401", "invalid_client", "unknown"):
            self.assertEqual(search("component-first", query), search("error-first", query))

    def test_all_literal_words_required_with_normalization_and_no_synonyms(self):
        self.assertEqual(self.ids("component-first", "  服务身份   边缘扫描器  "), ["K1"])
        self.assertEqual(self.ids("component-first", "ＥｄｇｅＳｃａｎ CI INVALID_CLIENT"), ["K1"])
        self.assertEqual(self.ids("component-first", "边缘扫描器 服务身份 轮换"), [])
        self.assertEqual(self.ids("component-first", "边缘扫描器 机器认证"), [])
        self.assertEqual(self.ids("component-first", "边缘扫描器服务身份"), [])

    def test_refinement_uses_content_and_not_search_count(self):
        for _ in range(4):
            self.assertEqual(self.ids("refine-explicit", "边缘扫描器"), ["K1", "K2", "K3"])
            self.assertEqual(self.ids("refine-explicit", "服务身份 边缘扫描器"), ["K1"])
        self.assertEqual(self.ids("refine-explicit", "边缘扫描器", 2), [])

    def test_bounded_page_exposes_need_for_followup_without_call_counter(self):
        for _ in range(3):
            self.assertEqual(self.ids("refine-windowed", "边缘扫描器"), ["K2", "K3"])
            self.assertTrue(search("refine-windowed", "边缘扫描器")["has_more"])
            self.assertEqual(self.ids("refine-windowed", "边缘扫描器", 2), ["K1"])
            self.assertEqual(self.ids("refine-windowed", "边缘扫描器 服务身份"), ["K1"])

    def test_fallback_has_no_enterprise_terms_but_current_error_is_indexed(self):
        self.assertEqual(self.ids("fallback", "边缘扫描器 服务身份"), [])
        self.assertEqual(self.ids("fallback", "EdgeScan invalid_client"), ["K1"])
        self.assertEqual(self.ids("no-strategy", "401"), ["K1"])

    def test_budget_is_always_empty_and_denial_never_becomes_empty(self):
        for query in ("边缘扫描器 服务身份", "401", "EdgeScan 机器认证"):
            self.assertEqual(self.ids("budget", query), [])
            self.assertEqual(search("denied", query)["status"], "permission_denied")
        self.assertEqual(read("denied", "K1")["status"], "permission_denied")

    def test_originals_are_separate_complete_and_not_mutable_by_caller(self):
        self.assertNotIn("body", search("component-first", "401")["items"][0])
        original = read("component-first", "K1")
        self.assertTrue(original["complete"])
        original["body"] = "changed"
        self.assertNotEqual(read("component-first", "K1")["body"], "changed")
        self.assertEqual(read("budget", "K1"), {"status": "not_found"})

    def test_invalid_inputs_are_rejected(self):
        for query, page in (("", 1), (None, 1), ("401", 0), ("401", True)):
            with self.assertRaises(ValueError):
                search("component-first", query, page)
        with self.assertRaises(ValueError):
            search("missing", "401")


class KeywordCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.skill = self.root / "skill"
        self.skill.mkdir()
        (self.skill / "SKILL.md").write_text("---\nname: synthetic\ndescription: synthetic\n---\n# Synthetic\n")
        self.run = self.root / "new-run"
        prepare(self.run, self.skill)
        self.trial = self.run / "trials/trial-01"

    def tearDown(self):
        self.temp.cleanup()

    def call(self, *args, trial=None):
        p = subprocess.run([sys.executable, "-B", str(self.run / "harness/keyword_cases.py"),
                            "--trial", str(trial or self.trial), *args], capture_output=True, text=True, check=True)
        return json.loads(p.stdout)

    def report(self, trial=None):
        (trial or self.trial).joinpath("visible/output/result.md").write_text("Synthetic report, not a model verdict.\n")

    def test_cli_freezes_scenes_and_refuses_overwrite(self):
        import eval as cli
        with redirect_stdout(io.StringIO()) as output:
            code = cli.main(["prepare-handoff", "--case-suite", "keyword", "--output", str(self.root / "second"),
                             "--candidate", str(self.skill)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["trials"], len(SCENARIOS))
        self.assertFalse(json.loads(output.getvalue())["models_called"])
        with self.assertRaises(FileExistsError):
            prepare(self.run, self.skill)

    def test_optional_subset_only_prepares_requested_fresh_trial(self):
        result = prepare(self.root / "subset", self.skill, case_ids=["refine-windowed"])
        self.assertEqual(result["trials"], 1)
        freeze = json.loads((self.root / "subset/freeze.json").read_text())
        self.assertEqual(freeze["trials"], {"trial-01": "refine-windowed"})
        for names in ([], ["missing"], ["fallback", "fallback"]):
            with self.assertRaises(ValueError):
                prepare(self.root / "invalid", self.skill, case_ids=names)

    def test_real_cli_records_help_query_and_exact_returned_original(self):
        self.call("--help")
        self.call("search", "--query", "服务身份 边缘扫描器")
        original = self.call("read", "--case-id", "K1")
        rows = [json.loads(l) for l in self.trial.joinpath("case-access.jsonl").read_text().splitlines()]
        self.assertEqual([r["kind"] for r in rows], ["capability", "search", "read"])
        self.assertEqual(rows[1]["args"]["query"], "服务身份 边缘扫描器")
        self.assertEqual(rows[2]["result"], original)
        self.assertEqual(original["read_at"], rows[2]["time_utc"])
        self.report()
        result = audit(self.trial)
        self.assertEqual(result["facts_status"], "passed", result)
        self.assertEqual(result["semantic_status"], "not_assessed")

    def test_missing_query_or_report_is_incomplete_not_pass(self):
        self.assertEqual(audit(self.trial)["facts_status"], "incomplete")
        self.call("search", "--query", "401")
        self.assertIn("report_missing", audit(self.trial)["gaps"])

    def test_actual_extra_query_is_counted_not_suppressed_by_harness(self):
        for _ in range(4):
            self.call("search", "--query", "401")
        self.report()
        self.assertIn("search_limit_exceeded", audit(self.trial)["failures"])

    def test_input_drift_and_response_tampering_are_failures(self):
        self.call("search", "--query", "401")
        self.report()
        self.trial.joinpath("visible/scene/error_logs.txt").write_text("changed")
        self.assertTrue(any(f.startswith("frozen_input_changed:") for f in audit(self.trial)["failures"]))
        path = self.trial / "case-access.jsonl"
        row = json.loads(path.read_text())
        row["result"]["items"] = []
        path.write_text(json.dumps(row) + "\n")
        self.assertIn("result_differs_from_fixture", audit(self.trial)["failures"])

    def test_denial_is_recorded_and_any_followup_access_is_failure(self):
        trial = self.run / "trials/trial-09"
        self.call("search", "--query", "边缘扫描器 服务身份", trial=trial)
        self.report(trial)
        self.assertEqual(audit(trial)["facts_status"], "passed")
        self.call("search", "--query", "401", trial=trial)
        self.assertIn("access_after_denial", audit(trial)["failures"])

    def test_late_access_and_more_than_three_distinct_originals_are_failures(self):
        self.call("search", "--query", "401")
        for case_id in ("K1", "K2", "K3", "K4"):
            self.call("read", "--case-id", case_id)
        path = self.trial / "case-access.jsonl"
        rows = [json.loads(l) for l in path.read_text().splitlines()]
        rows[-1]["time_utc"] = (datetime.fromisoformat(rows[0]["time_utc"]) + timedelta(seconds=61)).isoformat()
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.report()
        failures = audit(self.trial)["failures"]
        self.assertIn("access_after_retrieval_budget", failures)
        self.assertIn("original_limit_exceeded", failures)

    def test_process_local_monotonic_origins_do_not_prove_negative_duration(self):
        self.call("search", "--query", "401")
        self.call("read", "--case-id", "K1")
        path = self.trial / "case-access.jsonl"
        rows = [json.loads(l) for l in path.read_text().splitlines()]
        rows[0]["time_ns"], rows[1]["time_ns"] = 99999, 1
        rows[1]["time_utc"] = (datetime.fromisoformat(rows[0]["time_utc"]) + timedelta(seconds=7)).isoformat()
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.report()
        self.assertEqual(audit(self.trial)["access_elapsed_seconds"], 7)
        rows[1]["time_utc"] = (datetime.fromisoformat(rows[0]["time_utc"]) - timedelta(seconds=7)).isoformat()
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertEqual(audit(self.trial)["facts_status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
