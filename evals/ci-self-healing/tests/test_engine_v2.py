"""Offline contracts: concurrency, failed stages, evidence drift and access boundaries."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
from harness.catalog import Catalog
from harness.core import EvaluationError, Ledger, derive_status, freeze_tree, read_allowed, read_json, save_json
from harness.runner import make_plan, report, run, summarize, verify_seal


class CatalogTests(unittest.TestCase):
    def test_all_old_material_has_disposition_and_all_criteria_have_cases(self):
        catalog = Catalog()
        self.assertEqual(catalog.summary()["legacy_materials"], 24)
        self.assertEqual(catalog.summary()["criteria_without_cases"], [])
        self.assertEqual(len(catalog.select("smoke")), 8)

    def test_duplicate_case_rejected_and_changed_requirement_invalidates_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            freeze_tree(ENGINE / "spec", root / "spec")
            cases = read_json(root / "spec/cases.json")
            cases["cases"].append(copy.deepcopy(cases["cases"][0]))
            save_json(root / "spec/cases.json", cases)
            with self.assertRaisesRegex(EvaluationError, "duplicate"):
                Catalog(root / "spec")
            cases["cases"].pop()
            save_json(root / "spec/cases.json", cases)
            criteria = read_json(root / "spec/criteria.json")
            criteria["source_snapshot"][0]["sha256"] = "0" * 64
            save_json(root / "spec/criteria.json", criteria)
            with self.assertRaisesRegex(EvaluationError, "sources changed"):
                Catalog(root / "spec")

    def test_comparison_requires_explicit_sufficient_budget(self):
        catalog = Catalog()
        skill = ENGINE.parents[1] / "skills/ci-self-healing"
        with self.assertRaisesRegex(EvaluationError, "32 sessions"):
            make_plan(catalog, skill, baseline=skill)
        plan = make_plan(catalog, skill, baseline=skill, maximum_sessions=32)
        self.assertEqual(plan["planned_sessions"], 32)


class BudgetTests(unittest.TestCase):
    def test_concurrent_reservations_never_overspend_or_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.json"
            ledger = Ledger(path, 6, 30)
            barrier = threading.Barrier(12)
            def reserve(index):
                barrier.wait()
                try:
                    return ledger.reserve("execute", str(index))
                except EvaluationError:
                    return None
            with ThreadPoolExecutor(max_workers=12) as pool:
                result = list(pool.map(reserve, range(12)))
            self.assertEqual(len([r for r in result if r is not None]), 6)
            self.assertEqual(len(read_json(path)["sessions"]), 6)
            with self.assertRaisesRegex(EvaluationError, "replayed"):
                Ledger(path, 6, 30)

    def test_incomplete_and_uncovered_are_not_passes(self):
        self.assertEqual(derive_status([{"status": "pass"}], covered=False), "incomplete")
        self.assertEqual(derive_status([{"status": "not_applicable"}]), "incomplete")
        self.assertEqual(derive_status([{"status": "fail"}, {"status": "inconclusive"}]), "failed")

    def test_restricted_markdown_body_is_never_consumed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private.md"
            path.write_text("---\nai_access: false\n---\nDO NOT READ BODY\n")
            with patch.object(Path, "read_bytes", side_effect=AssertionError("body consumed")):
                with self.assertRaises(EvaluationError):
                    read_allowed(path)

    def test_symlink_cannot_escape_frozen_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source").mkdir()
            (root / "source/escape").symlink_to(root / "outside")
            with self.assertRaisesRegex(EvaluationError, "symlink"):
                freeze_tree(root / "source", root / "frozen")


class PipelineTests(unittest.TestCase):
    def test_new_failure_is_visible_inside_an_already_failing_case(self):
        def row(version, target, coverage):
            return {"id": version, "version": version, "case_id": "repair-contract-preserve",
                    "repeat": 1, "status": "failed", "coverage": {"status": "covered"},
                    "checks": [{"criterion_id": key, "status": status, "evidence_ids": [version]}
                               for key, status in (("precondition.diagnosis-read-order", "fail"),
                                                   ("contract.behavior-oracle", target),
                                                   ("contract.compatibility-oracle", coverage))]}
        plan = {"versions": {"baseline": "old", "candidate": "new"}, "catalog_sha256": "synthetic"}
        baseline = row("baseline", "pass", "pass")
        for status, expected in [("fail", True), ("inconclusive", False), ("not_applicable", False)]:
            with self.subTest(status=status):
                candidate = row("candidate", status, "pass")
                pair, = summarize(None, [baseline, candidate], plan, write=False)["comparisons"]
                self.assertEqual(pair["regression"], expected)
                change, = pair["criterion_changes"]
                self.assertEqual((change["criterion_id"], change["baseline"], change["candidate"]),
                                 ("contract.behavior-oracle", "pass", status))
                self.assertEqual(change["candidate_evidence"], ["candidate"])

    def test_fixed_baseline_failure_is_improvement_but_missing_baseline_is_not_comparable(self):
        def row(version, status):
            return {"id": version, "case_id": "one", "repeat": 1, "version": version,
                    "status": status, "coverage": {"status": "covered"}}
        plan = {"versions": {"baseline": "old", "candidate": "new"}, "catalog_sha256": "synthetic"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            improved = summarize(root, [row("baseline", "failed"), row("candidate", "passed")], plan)
            self.assertEqual(improved["exit_code"], 0)
            self.assertFalse(improved["comparisons"][0]["regression"])
            unknown = summarize(root, [row("baseline", "incomplete"), row("candidate", "passed")], plan)
            self.assertEqual(unknown["exit_code"], 2)
            regressed = summarize(root, [row("baseline", "passed"), row("candidate", "failed")], plan)
            self.assertTrue(regressed["comparisons"][0]["regression"])
            self.assertEqual(regressed["exit_code"], 1)

    def test_parallel_fake_backend_evidence_seal_and_one_failure_isolation(self):
        """Fake backend tests scheduling only; it never demonstrates model behavior."""
        catalog = Catalog()
        barrier = threading.Barrier(2)
        stages = []
        lock = threading.Lock()
        def prepare(trial, case, skill, runtime):
            (trial / "visible").mkdir(parents=True)
            (trial / "visible/source.txt").write_text("original")
            save_json(trial / "fixture.json", {"case_id": case["id"]})
        fixtures = types.SimpleNamespace(prepare=prepare, baseline=lambda *a: {"status": "passed"},
                                         verify=lambda *a: {"status": "passed"})
        def deterministic(case, packet):
            return [{"criterion_id": "fact", "status": "fail" if case["id"] == "repair-contract-extend" else "pass"}]
        grading = types.SimpleNamespace(collect=lambda *a: {"evidence": {"synthetic-e1": {"kind": "fixture", "complete": True}}}, deterministic=deterministic,
            review_prompt=lambda *a: "synthetic grading input", validate_review=lambda *a: [{"criterion_id": "meaning", "status": "pass"}],
            coverage=lambda *a: {"status": "covered"})
        class Adapter:
            @staticmethod
            def preflight(runtime): return {"status": "passed", "synthetic": True}
            @staticmethod
            def execute(trial, runtime, seconds):
                with lock: stages.append((trial.name, "execute"))
                barrier.wait(timeout=10)
                return {"status": "completed", "trace_complete": True, "answer": "synthetic"}
            @staticmethod
            def judge(directory, runtime, prompt, seconds, *, review_contract=None):
                if review_contract is None or review_contract['evidence_ids'] != ['synthetic-e1']:
                    raise AssertionError('runner did not bind the supplied evidence IDs')
                with lock: stages.append((directory.parent.name, "review"))
                return {"status": "completed", "trace_complete": True, "answer": "synthetic"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            plan = make_plan(catalog, ENGINE.parents[1] / "skills/ci-self-healing",
                             cases=["repair-contract-preserve", "repair-contract-extend"], maximum_sessions=4)
            with patch.dict(sys.modules, {"harness.fixtures": fixtures, "harness.grading": grading}), \
                 patch("harness.fixtures", fixtures, create=True), patch("harness.grading", grading, create=True):
                summary = run(root, {}, plan, catalog=catalog, adapter=Adapter)
            self.assertEqual(summary["counts"], {"passed": 1, "failed": 1, "incomplete": 0})
            self.assertEqual(len(read_json(root / "ledger.json")["sessions"]), 4)
            self.assertEqual(report(root)["status"], "failed")
            self.assertEqual([s for t, s in stages if t == "t0001"], ["execute", "review"])
            trial = root / "trials/t0001"
            verify_seal(trial)
            saved = (root / "summary.json").read_bytes()
            tampered = read_json(root / "summary.json")
            tampered.update(status="passed", exit_code=0, trials=[])
            save_json(root / "summary.json", tampered)
            with self.assertRaisesRegex(EvaluationError, "summary changed"):
                report(root)
            (root / "summary.json").write_bytes(saved)
            (trial / "visible/new.txt").write_text("unrecorded")
            with self.assertRaisesRegex(EvaluationError, "changed"):
                verify_seal(trial)
            (trial / "visible/new.txt").unlink()
            (trial / "visible/source.txt").write_text("drift")
            with self.assertRaisesRegex(EvaluationError, "changed"):
                verify_seal(trial)


if __name__ == "__main__":
    unittest.main()
