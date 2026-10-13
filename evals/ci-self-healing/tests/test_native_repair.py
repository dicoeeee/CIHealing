"""Offline native-backend wiring and permission tests; not real-model acceptance."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cases import materialize
from common import EvalError, read_json, read_regular, snapshot, write_json, write_new
from host_sandbox import environment, policy
from native_repair import load_config, probe_build, setup, model_profile, gateway, run_trials
from runtime import runtime_probe
from resource_cleanup import OwnedResources, validate_cleanup


class NativeRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native-repair-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.config = {"backend": "native", "python": "/opt/runtime/bin/python", "chrys": "/opt/runtime/bin/chrys",
                       "maven": "/opt/maven/bin/mvn", "java_home": "/opt/jdk", "rg": "/opt/tools/rg", "git": "/opt/git/bin/git",
                       "maven_repository": str(self.base / "cache"), "model_profile": str(self.base / "model.yaml"),
                       "endpoint_host": "gateway.invalid"}
        (self.base / "cache").mkdir()

    def test_native_fixture_contains_real_paths_not_container_paths(self):
        visible = self.base / "trial/visible"
        materialize(visible, "case-003", "opaque", backend="native")
        prompt = read_regular(visible / "task.txt").decode()
        build = read_json(visible / "scene/build_info.json")
        self.assertIn(str(visible / "scene/sourcecode"), prompt)
        self.assertIn(str(visible.parent / "runtime/skills/ci-self-healing"), prompt)
        self.assertNotIn("/tmp/m2", prompt)
        self.assertEqual(build["cwd"], str(visible / "scene/sourcecode"))
        self.assertIn("-gs", build["command"])
        self.assertNotIn("case-003", prompt)

    def test_sanitized_child_environment_preserves_home_but_not_secrets(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/operator", "OPENAI_API_KEY": "private", "HTTPS_PROXY": "private", "MAVEN_OPTS": "unsafe", "PYTHONPATH": "unsafe"}):
            env = environment(self.config, self.base / "runtime with spaces")
        self.assertEqual(env["HOME"], "/home/operator")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(env["JAVA_HOME"], "/opt/jdk")
        self.assertIn("'", env["MAVEN_OPTS"])
        self.assertEqual(env["PATH"].split(os.pathsep)[0], "/opt/git/bin")
        self.assertEqual(env["CI_EVAL_GIT"], "/opt/git/bin/git")
        self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)

    def test_policy_only_allows_selected_loopback_port_and_explicit_writes(self):
        with mock.patch("host_sandbox.platform.system", return_value="Darwin"):
            text = policy(self.config, read=[self.base / "skill"], write=[self.base / "visible"], port=12345)
        self.assertIn('(remote tcp "localhost:12345")', text)
        self.assertNotIn('*:443', text)
        self.assertIn('(deny appleevent-send)', text)
        self.assertNotIn('(subpath "' + str(Path.home()) + '")', text)
        self.assertNotIn(str(self.config["model_profile"]), text)
        self.assertIn('(deny file-write*)', text)
        self.assertIn('(deny pseudo-tty)', text)
        self.assertNotIn('(allow pseudo-tty)', text)
        self.assertNotIn('(subpath "/dev")', text)

    def test_pty_permission_requires_kernel_extension_and_explicit_opt_in(self):
        with mock.patch("host_sandbox.platform.system", return_value="Darwin"):
            text = policy(self.config, write=[self.base], pty=True)
        self.assertIn('(allow pseudo-tty)', text)
        self.assertIn('(literal "/dev/ptmx")', text)
        self.assertIn('(require-all (regex #"^/dev/ttys[0-9]+$") (extension "com.apple.sandbox.pty"))', text)
        self.assertNotIn('(subpath "/dev")', text)

    def test_no_unsandboxed_platform_fallback(self):
        with mock.patch("host_sandbox.platform.system", return_value="Linux"), self.assertRaises(EvalError):
            policy(self.config, write=[self.base])

    def test_runtime_dispatch_does_not_probe_docker(self):
        with mock.patch("native_repair.doctor", return_value={"backend": "native"}) as native, \
             mock.patch("runtime.docker_probe", side_effect=AssertionError("Docker is forbidden")):
            self.assertEqual(runtime_probe(self.config), {"backend": "native"})
        native.assert_called_once_with(self.config)

    def test_failed_native_doctor_stops_before_profile_or_gateway(self):
        with mock.patch("native_repair.load_suite", return_value={"backend": "native"}), \
             mock.patch("native_repair.doctor", side_effect=EvalError("shell failed")), \
             mock.patch("native_repair.model_profile") as profile, mock.patch("native_repair.gateway") as transport:
            with self.assertRaisesRegex(EvalError, "shell failed"):
                run_trials(self.base, self.config)
        profile.assert_not_called()
        transport.assert_not_called()

    def test_setup_never_exposes_maintenance_readme_or_shared_cache_writes(self):
        skill = self.base / "skill"
        write_new(skill / "SKILL.md", "---\nname: ci-self-healing\n---\n# Test")
        write_new(skill / "README.md", "---\nai_access: false\n---\nprivate")
        write_new(self.base / "cache/artifact.jar", b"synthetic")
        info = setup(self.config, self.base / "trial", skill)
        self.assertEqual(info["cli"], self.config["chrys"])
        self.assertFalse((Path(info["skill"]) / "README.md").exists())
        (self.base / "trial/runtime/m2/artifact.jar").write_bytes(b"changed")
        self.assertEqual(read_regular(self.base / "cache/artifact.jar"), b"synthetic")

    def test_icode_selection_is_forwarded_without_renaming_runtime_contracts(self):
        skill = self.base / "skill"
        write_new(skill / "SKILL.md", "---\nname: ci-self-healing\n---\n# Test")
        config = {**self.config, "chrys": "/opt/runtime/bin/icode"}
        info = setup(config, self.base / "trial", skill)
        self.assertEqual(info["cli"], "/opt/runtime/bin/icode")
        self.assertIn("CHRYS_MAX_TRANSIENT_RETRIES", environment(config, self.base / "runtime"))

    def test_build_uses_copy_sandbox_and_no_network(self):
        visible = self.base / "trial/visible"
        materialize(visible, "case-001", "opaque", backend="native")
        before = snapshot(visible)
        def fake(argv, directory, timeout, env, cwd):
            self.assertEqual(argv[0], "/usr/bin/sandbox-exec")
            self.assertIn("--native-root", argv)
            self.assertNotEqual(cwd, visible / "scene/sourcecode")
            self.assertNotIn("network-outbound", read_regular(argv[2]).decode())
            self.assertNotIn("(allow pseudo-tty)", read_regular(argv[2]).decode())
            self.assertFalse((cwd / ".git").exists())
            self.assertFalse((cwd / "target").exists())
            write_json(Path(directory) / "stdout.log", {"status": "passed", "checks": {}})
            process = {"status": "completed", "returncode": 0, "cleanup": {"status": "confirmed"}}
            write_json(Path(directory) / "process.json", process)
            return process
        with mock.patch("native_repair.run_logged", side_effect=fake), mock.patch("host_sandbox.platform.system", return_value="Darwin"):
            result = probe_build(self.config, visible / "scene/sourcecode", self.base / "baseline")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(snapshot(visible), before)
        self.assertEqual(result["resource_cleanup"]["status"], "cleaned")
        self.assertFalse((self.base / "baseline-workspace").exists())
        self.assertTrue((self.base / "baseline-evidence/source/pom.xml").exists())

    def test_profile_errors_do_not_echo_credentials(self):
        write_new(self.base / "model.yaml", "name: [\napi_key: SECRET_MARKER\n")
        with self.assertRaises(EvalError) as result:
            model_profile(self.config)
        self.assertNotIn("SECRET_MARKER", str(result.exception))

    def test_preflight_failure_reclaims_runtime_products_and_preserves_evidence(self):
        root = self.base / "suite"
        trial = root / "trials/t1"
        visible = trial / "visible"
        materialize(visible, "case-001", "t1", backend="native")
        write_json(trial / "initial.json", snapshot(visible))
        write_new(root / "skills/candidate/SKILL.md", "---\nname: ci-self-healing\ndescription: Test\n---\nTest")
        with OwnedResources(trial, "baseline-build") as resources:
            baseline_cleanup = resources.finish()
        baseline = {"status": "passed", "checks": {"maven": {"stdout": "original failure", "stderr": ""}},
                    "resource_cleanup": baseline_cleanup}
        row = {"id": "t1", "version": "candidate"}
        def preflight(argv, directory, *args, **kwargs):
            write_new(trial / "runtime/tmp/partial.bin", "temp")
            write_new(Path(directory) / "stdout.log", "preflight failed")
            result = {"status": "completed", "returncode": 1, "cleanup": {"status": "confirmed"}}
            write_json(Path(directory) / "process.json", result)
            return result
        with mock.patch("native_repair.load_suite", return_value={"backend": "native"}), \
             mock.patch("native_repair.select_trials", return_value=[row]), \
             mock.patch("native_repair.doctor", return_value={"backend": "native"}), \
             mock.patch("native_repair.model_profile", return_value=({"max_output_tokens": 100}, "profile")), \
             mock.patch("native_repair.probe_build", return_value=baseline), \
             mock.patch("native_repair.run_logged", side_effect=preflight), \
             mock.patch("host_sandbox.platform.system", return_value="Darwin"), \
             mock.patch("native_repair.gateway") as model_gateway:
            result = run_trials(root, self.config, "t1")
        model_gateway.assert_not_called()
        self.assertEqual(result[0]["status"], "preflight_failed")
        receipt = read_json(trial / "run.json")
        self.assertEqual(validate_cleanup(receipt["resource_cleanup"]), "cleaned")
        for name in ("tmp", "m2", "java-home"):
            self.assertFalse((trial / "runtime" / name).exists())
        self.assertTrue((trial / "preflight/stdout.log").exists())
        self.assertTrue((visible / "scene/sourcecode/target/preexisting.txt").exists())

    def test_gateway_bootstrap_is_one_line_and_owned_process_is_cleaned(self):
        captured = []
        class Pipe(io.BytesIO):
            def close(self):
                captured.append(self.getvalue())
                super().close()
        child = mock.Mock(pid=123456, stdin=Pipe())
        child.poll.return_value = None
        def start(*args, **kwargs):
            write_json(self.base / "gateway/ready.json", {"port": 43210})
            return child
        with mock.patch("native_repair.subprocess.Popen", side_effect=start), mock.patch("native_repair.stop_owned_group", return_value={"status": "confirmed"}) as cleanup:
            with gateway(self.config, self.base / "gateway", "profile-fingerprint") as (port, token):
                self.assertEqual(port, 43210)
                self.assertTrue(token)
        self.assertEqual(len(captured[0].splitlines()), 1)
        self.assertEqual(json.loads(captured[0])["profile_sha256"], "profile-fingerprint")
        self.assertEqual(json.loads(captured[0])["event_binding"], read_json(self.base / "gateway/binding.json"))
        cleanup.assert_called_once_with(child)
        self.assertEqual(read_json(self.base / "gateway/cleanup.json")["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
