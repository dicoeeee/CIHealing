"""Host build lifecycle tests; injected process results, actual files and receipts."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import EvalError, read_json, snapshot, write_json, write_new
from native_clean import CLEAN_VERSION, clean_build
from native_repair import probe_build, setup
from resource_cleanup import OwnedResources, validate_cleanup
from execution_outcome import classify_execution
from grading import combined_status


class BuildCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="build-cleanup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "original"
        write_new(self.source / "pom.xml", "<project/>")
        write_new(self.source / "src/X.java", "class X {}")
        write_new(self.source / "target/preexisting.txt", "job-owned")
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.config = {"python": "/usr/bin/python3", "maven": "/opt/maven/bin/mvn", "java_home": "/opt/jdk",
                       "maven_repository": str(self.cache), "chrys": "/unused/icode"}
        self.clean_status = "completed"
        self.build_status = "completed"
        self.build_returncode = 0
        self.stopped = True
        self.clean_calls = []
        self.stack = __import__('contextlib').ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch("native_repair.run_logged", side_effect=self.build))
        self.stack.enter_context(mock.patch("native_clean.run_logged", side_effect=self.clean))
        self.stack.enter_context(mock.patch("host_sandbox.platform.system", return_value="Darwin"))

    def pin_clean(self):
        folder = self.cache / "org/apache/maven/plugins/maven-clean-plugin" / CLEAN_VERSION
        for suffix in ("jar", "pom"):
            write_new(folder / f"maven-clean-plugin-{CLEAN_VERSION}.{suffix}", "test fixture; process mocked")

    def process(self, directory, status, code):
        result = {"status": status, "returncode": code, "cleanup": {"status": "confirmed" if self.stopped else "unconfirmed"}}
        write_json(Path(directory) / "process.json", result)
        return result

    def build(self, argv, directory, timeout, env, cwd):
        workspace = Path(cwd).parent
        write_new(Path(cwd) / "target/classes/X.class", "compiled")
        write_new(workspace / "classes/Oracle.class", "javac")
        write_new(workspace / "tmp/.hidden/x.bin", "temp")
        (workspace / "tmp/empty").mkdir()
        write_json(Path(directory) / "stdout.log", {"status": "passed" if self.build_returncode == 0 else "failed", "checks": {}})
        return self.process(directory, self.build_status, self.build_returncode)

    def clean(self, argv, directory, timeout, env, cwd):
        self.clean_calls.append(argv)
        self.assertIn("-o", argv)
        self.assertIn("-Dmaven.clean.fast=false", argv)
        self.assertEqual(argv[0], "/usr/bin/sandbox-exec")
        self.assertNotEqual(Path(cwd), self.source)
        self.assertNotIn(Path(argv[2]), list(Path(cwd).parent.rglob("*.sb")))
        write_new(Path(directory) / "stdout.log", "synthetic native clean result; leaves residue intentionally")
        return self.process(directory, self.clean_status, 0 if self.clean_status == "completed" else 1)

    def test_native_clean_preferred_but_success_does_not_hide_residue(self):
        self.pin_clean()
        before_source, before_cache = snapshot(self.source), snapshot(self.cache)
        result = probe_build(self.config, self.source, self.root / "candidate")
        self.assertEqual(len(self.clean_calls), 1)
        self.assertEqual(result["native_clean"]["status"], "completed")
        self.assertEqual(validate_cleanup(result["resource_cleanup"]), "cleaned")
        self.assertFalse((self.root / "candidate-workspace").exists())
        self.assertEqual(snapshot(self.source), before_source)
        self.assertEqual(snapshot(self.cache), before_cache)
        self.assertTrue((self.root / "candidate-clean/stdout.log").exists())
        self.assertTrue((self.root / "candidate-evidence/source/src/X.java").exists())

    def test_missing_plugin_uses_owned_cleanup_without_network_or_clean_call(self):
        result = probe_build(self.config, self.source, self.root / "candidate")
        self.assertEqual(result["native_clean"]["reason"], "pinned_clean_plugin_not_cached")
        self.assertEqual(self.clean_calls, [])
        self.assertEqual(result["resource_cleanup"]["status"], "cleaned")

    def test_failed_native_clean_keeps_observation_and_cleans_remaining_files(self):
        self.pin_clean()
        self.clean_status = "timeout"
        result = probe_build(self.config, self.source, self.root / "candidate")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["native_clean"]["status"], "failed")
        self.assertEqual(result["resource_cleanup"]["status"], "cleaned")
        self.assertEqual(read_json(self.root / "candidate-clean/process.json")["status"], "timeout")

    def test_failed_build_cleans_without_rewriting_failure(self):
        self.build_returncode = 1
        result = probe_build(self.config, self.source, self.root / "baseline")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["resource_cleanup"]["status"], "cleaned")

    def test_build_timeout_and_interrupt_do_not_leave_owned_products(self):
        for status in ("timeout", "interrupted"):
            self.build_status = status
            result = probe_build(self.config, self.source, self.root / status)
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["resource_cleanup"]["status"], "cleaned")

    def test_missing_writer_confirmation_preserves_workspace(self):
        self.stopped = False
        result = probe_build(self.config, self.source, self.root / "candidate")
        self.assertEqual(result["native_clean"]["reason"], "build_writers_unconfirmed")
        self.assertEqual(result["resource_cleanup"]["status"], "incomplete")
        self.assertTrue((self.root / "candidate-workspace/classes/Oracle.class").exists())

    def test_actual_interrupt_exception_cleans_and_preserves_interrupt(self):
        def interrupt(*args, **kwargs):
            self.build(*args, **kwargs)
            raise KeyboardInterrupt()
        with mock.patch("native_repair.run_logged", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            probe_build(self.config, self.source, self.root / "interrupted")
        self.assertFalse((self.root / "interrupted-workspace").exists())
        self.assertEqual(read_json(self.root / "resource-cleanup/interrupted/receipt.json")["status"], "cleaned")

    def test_clean_launch_error_preserves_build_result_and_reports_cleanup_gap(self):
        self.pin_clean()
        with mock.patch("native_clean.run_logged", side_effect=OSError("cannot launch")):
            result = probe_build(self.config, self.source, self.root / "candidate")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["native_clean"]["reason"], "clean_execution_error")
        self.assertEqual(result["resource_cleanup"]["status"], "incomplete")

    def test_missing_post_build_evidence_prevents_destructive_finalizer(self):
        original = write_json
        def fail_post(path, value):
            if Path(path).name == "post-build-source.json":
                original(path, {})  # A partial/stale-looking file is not sealed evidence.
                raise OSError("disk full")
            return original(path, value)
        with mock.patch("native_repair.write_json", side_effect=fail_post), self.assertRaises(OSError):
            probe_build(self.config, self.source, self.root / "candidate")
        self.assertTrue((self.root / "candidate-workspace/classes/Oracle.class").exists())
        self.assertEqual(read_json(self.root / "resource-cleanup/candidate/receipt.json")["status"], "incomplete")

    def test_verifier_changed_input_is_preserved_and_cannot_pass(self):
        def change(*args, **kwargs):
            result = self.build(*args, **kwargs)
            (Path(kwargs["cwd"]) / "src/X.java").write_text("changed by build")
            return result
        with mock.patch("native_repair.run_logged", side_effect=change):
            result = probe_build(self.config, self.source, self.root / "candidate")
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("src/X.java", result["verification_input_drift"])
        self.assertEqual((self.root / "candidate-evidence/modified-inputs/src/X.java").read_text(), "changed by build")
        self.assertEqual((self.source / "src/X.java").read_text(), "class X {}")

    def test_candidate_symlinks_are_rejected_before_host_copy(self):
        (self.source / "link").symlink_to(self.root / "cache", target_is_directory=True)
        with self.assertRaisesRegex(EvalError, "symlinks"):
            probe_build(self.config, self.source, self.root / "candidate")
        self.assertFalse((self.root / "candidate-workspace").exists())

    def test_clean_never_accepts_original_or_unregistered_directory(self):
        with OwnedResources(self.root, "reject") as scope:
            with self.assertRaises(EvalError):
                clean_build(self.config, self.source, self.root / "clean", scope)
        self.assertTrue((self.source / "target/preexisting.txt").exists())

    def test_partial_setup_cleans_only_registered_runtime_dirs(self):
        trial = self.root / "trial"
        trial.mkdir()
        skill = self.root / "skill"
        write_new(skill / "SKILL.md", "---\nname: ci-self-healing\ndescription: test\n---\nTest")
        with OwnedResources(trial, "run", evidence=("native-session", "runtime/config")) as scope:
            setup(self.config, trial, skill, scope)
            write_new(trial / "runtime/tmp/partial.bin", "temp")
            write_new(trial / "native-session/identity.json", "{}")
            self.assertEqual(scope.finish()["status"], "cleaned")
        self.assertTrue((trial / "native-session/identity.json").exists())
        for folder in ("runtime/tmp", "runtime/java-home", "runtime/m2"):
            self.assertFalse((trial / folder).exists())

    def test_cleanup_failure_blocks_overall_pass_without_overwriting_build_result(self):
        self.stopped = False
        result = probe_build(self.config, self.source, self.root / "candidate")
        result["process"]["cleanup"]["status"] = "confirmed"
        self.assertEqual(result["status"], "passed")
        self.assertEqual(classify_execution(result, "review")["code"], "resource_cleanup_incomplete")
        review = {"status": "validated", "verdict": {"verdict": "pass"}, "resource_cleanup": result["resource_cleanup"]}
        self.assertEqual(combined_status({"status": "awaiting_review"}, review), "incomplete")


if __name__ == "__main__":
    unittest.main()
