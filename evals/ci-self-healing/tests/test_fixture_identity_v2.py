"""Controlled Java template identity, snapshot fidelity, and drift rejection."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import runner
from harness.core import EvaluationError, Ledger, read_json, save_json, sha


class FixtureIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ci-fixture-identity-")
        self.root = Path(self.temporary.name).resolve()
        self.engine = self.root / "engine"
        self.files = {
            "harness/engine.py": b"SYNTHETIC_ENGINE = True\n",
            "common.py": b"# synthetic process primitives\n",
            "host_sandbox.py": b"# synthetic sandbox primitives\n",
            "environment/gateway.py": b"# synthetic gateway primitives\n",
            "fixtures/java/routing-arity-v1/source/Router.java": b"interface Router {}\n",
            "fixtures/java/routing-arity-v1/oracle/BehaviorOracle.java": b"class BehaviorOracle {}\n",
            "fixtures/java/routing-arity-v1/pom.xml": b"<project/>\n",
        }
        for relative, body in self.files.items():
            path = self.engine / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        self.oracle = self.engine / "fixtures/java/routing-arity-v1/oracle/BehaviorOracle.java"
        self.engine_patch = patch.object(runner, "ENGINE", self.engine)
        self.engine_patch.start()
        self.addCleanup(self.engine_patch.stop)
        self.addCleanup(self.temporary.cleanup)

    def test_identity_includes_source_build_and_hidden_oracle_bytes(self):
        self.assertEqual(runner.implementation_identity(), {name: sha(body) for name, body in self.files.items()})

    def test_template_modification_addition_and_deletion_each_change_identity(self):
        before = runner.implementation_identity()
        original = self.oracle.read_bytes()
        self.oracle.write_bytes(b"class DifferentOracle {}\n")
        self.assertNotEqual(runner.implementation_identity(), before)
        self.oracle.write_bytes(original)
        added = self.oracle.with_name("AdditionalOracle.java")
        added.write_text("class AdditionalOracle {}\n")
        self.assertNotEqual(runner.implementation_identity(), before)
        added.unlink()
        self.oracle.unlink()
        self.assertNotEqual(runner.implementation_identity(), before)

    def test_snapshot_matches_manifest_and_never_changes_with_current_templates(self):
        before = runner.implementation_identity()
        frozen = self.root / "frozen"
        self.assertEqual(runner.freeze_implementation(frozen, before), before)
        self.assertEqual({str(path.relative_to(frozen)): sha(path.read_bytes())
                          for path in frozen.rglob("*") if path.is_file()}, before)
        self.oracle.write_text("changed after freezing")
        self.assertEqual((frozen / self.oracle.relative_to(self.engine)).read_bytes(),
                         self.files[str(self.oracle.relative_to(self.engine))])
        with self.assertRaises(FileExistsError):
            runner.freeze_implementation(frozen)

    def test_changed_expected_manifest_refuses_snapshot_before_output_creation(self):
        before = runner.implementation_identity()
        self.oracle.unlink()
        output = self.root / "refused"
        with self.assertRaisesRegex(EvaluationError, "changed before freezing"):
            runner.freeze_implementation(output, before)
        self.assertFalse(output.exists())

    def test_fixture_symlink_and_restricted_markdown_are_not_frozen(self):
        linked = self.oracle.with_name("LinkedOracle.java")
        linked.symlink_to(self.oracle)
        with self.assertRaises(EvaluationError):
            runner.implementation_identity()
        linked.unlink()
        restricted = self.oracle.with_name("private.md")
        restricted.write_text("---\nai_access: false\n---\nThis body must not be consumed.\n")
        with self.assertRaises(EvaluationError):
            runner.implementation_identity()

    def trial(self, *, change_before=False, change_during_execute=False):
        root = self.root / "run"
        root.mkdir()
        save_json(root / "implementation.json", runner.implementation_identity())
        ledger = Ledger(root / "ledger.json", 2, 30)
        row = {"id": "t0001", "version": "candidate", "repeat": 1}
        case = {"id": "synthetic-controlled-java"}
        def prepare(trial, *_):
            (trial / "visible").mkdir(parents=True)
        def execute(*_):
            if change_during_execute:
                self.oracle.write_text("changed while fake execution ran")
            return {"status": "completed", "trace_complete": True}
        fixtures = SimpleNamespace(prepare=Mock(side_effect=prepare),
                                   baseline=Mock(return_value={"status": "passed"}), verify=Mock())
        grading = SimpleNamespace(collect=Mock())
        adapter = SimpleNamespace(execute=Mock(side_effect=execute), judge=Mock())
        if change_before:
            self.oracle.write_text("changed before fixture preparation")
        with patch.dict(sys.modules, {"harness.fixtures": fixtures, "harness.grading": grading}), \
             patch("harness.fixtures", fixtures, create=True), patch("harness.grading", grading, create=True):
            result = runner._trial(root, row, case, self.root / "unused-skill", {}, Mock(), ledger, 10, adapter)
        return result, fixtures, adapter, ledger

    def test_changed_template_prevents_any_fixture_or_model_start(self):
        result, fixtures, adapter, ledger = self.trial(change_before=True)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("implementation changed", result["error"]["message"])
        fixtures.prepare.assert_not_called()
        adapter.execute.assert_not_called()
        adapter.judge.assert_not_called()
        self.assertEqual(ledger.data["sessions"], [])

    def test_changed_oracle_during_execution_prevents_verification_and_judge(self):
        result, fixtures, adapter, ledger = self.trial(change_during_execute=True)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["failure_origin"], "environment_or_engine")
        fixtures.verify.assert_not_called()
        adapter.judge.assert_not_called()
        self.assertEqual(len(ledger.data["sessions"]), 1)

    def test_run_inventory_seals_added_templates_without_retrofitting_old_runs(self):
        root = self.root / "old-run"
        root.mkdir()
        for name in ("plan.json", "runtime.json", "runtime-identity.json", "implementation.json", "trials.json",
                     "ledger.json", "summary.json"):
            save_json(root / name, {})
        (root / "summary.md").write_text("Synthetic old report.\n")
        for name in ("implementation", "skills", "spec", "trials"):
            (root / name).mkdir()
        old = runner._run_inventory(root)
        # Adding a current template does not alter an old run's inventory.
        self.oracle.with_name("NewOracle.java").write_text("class NewOracle {}\n")
        self.assertEqual(runner._run_inventory(root), old)
        snapshot = root / "implementation/fixtures/java/routing-arity-v1/oracle/BehaviorOracle.java"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text("class BehaviorOracle {}\n")
        recorded = runner._run_inventory(root)
        self.assertIn(str(snapshot.relative_to(root)), recorded)
        snapshot.write_text("changed frozen oracle")
        self.assertNotEqual(runner._run_inventory(root), recorded)


if __name__ == "__main__":
    unittest.main()
