"""Offline runtime contract tests; no paid calls, Docker or user profiles."""

import asyncio
import dataclasses
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chrys_observer import Recorder
from chrys_preflight import check_search, check_shell, check_skills, selected_skills
from common import EvalError, digest, read_json, read_regular, run_logged, write_json, write_new
from grading import grade_trial, normalize_trace
from repair_support import (ADAPTER_FILES, RUNTIME_CONTRACT, SESSION_MOUNT, SKILL_MOUNT,
                            SKILL_NAME, budget_notice, session_receipt, validate_repair_artifacts)
from repair_telemetry import transport_timing
from runtime import run_trials
from suite import prepare

try:
    import httpx
    import yaml
except ImportError:
    httpx = yaml = None


class RepairRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="repair-runtime-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_budget_is_neutral_bounded_and_has_utc_wrapup(self):
        text = budget_notice(600, datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertIn("00:09:00+00:00", text)
        self.assertIn("00:10:00+00:00", text)
        self.assertNotIn("case-", text)
        self.assertNotIn("condition", text)
        self.assertIn("不得伪造成功", text)

    @unittest.skipUnless(yaml, "use the existing Chrys Python for PyYAML")
    def test_profile_rejects_old_mount_and_implicit_discovery(self):
        folder = self.root / "profiles/agents"
        folder.mkdir(parents=True)
        skills = {"paths": [SKILL_MOUNT], "auto_load_user_agents_skills": False,
                  "auto_load_cwd_agents_skills": False}
        path = folder / "EvalCode.yaml"
        for change in ({}, {"paths": ["/skill"]}, {"auto_load_cwd_agents_skills": True}, {"inline": [{"name": "extra"}]}):
            path.write_text(yaml.safe_dump({"name": "EvalCode", "skills": {**skills, **change}}))
            if not change:
                self.assertEqual(selected_skills(folder.parent, "EvalCode"), skills)
            else:
                with self.assertRaises(EvalError):
                    selected_skills(folder.parent, "EvalCode")

    def test_actual_session_catalog_checked_without_other_runtime_details(self):
        @dataclasses.dataclass
        class SessionReady:
            skill_names: list
            runtime_details: object
        stream = io.StringIO()
        recorder = Recorder(stream, self.root)
        private = types.SimpleNamespace(skill_sources={SKILL_MOUNT: [SKILL_NAME]}, model="private-data")
        recorder.observe(SessionReady([SKILL_NAME], private))
        self.assertTrue(recorder.skill_registration_confirmed)
        self.assertNotIn("private-data", stream.getvalue())
        with self.assertRaises(EvalError):
            recorder.observe(SessionReady([], types.SimpleNamespace(skill_sources={})))
        self.assertFalse(recorder.skill_registration_confirmed)
        with self.assertRaises(EvalError):
            recorder.require_registration()

    def test_new_trace_requires_registration_without_rewriting_legacy(self):
        rows = [{"seq": 1, "kind": "observer.started", "payload": {"runtime_contract": RUNTIME_CONTRACT}}]
        encode = lambda: ("CI_EVAL_EVENT " + json.dumps(rows[0])).encode()
        self.assertIn("native_skill_registration_unconfirmed", normalize_trace(encode(), "glm-5.2")["gaps"])
        rows[0]["payload"] = {}
        self.assertNotIn("native_skill_registration_unconfirmed", normalize_trace(encode(), "glm-5.2")["gaps"])

    def test_native_checkpoint_survives_forced_timeout_and_exports_only_meta(self):
        sessions = self.root / "native-session"
        sessions.mkdir(mode=0o700)
        payload = {"meta": {"session_id": "synthetic", "model_id": "glm-5.2"},
                   "messages": [{"reasoning": "private-not-for-receipt"}]}
        # Real subprocess writes a checkpoint, then gets killed by the harness.
        code = "import pathlib,time,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2]); time.sleep(10)"
        process = run_logged([sys.executable, "-c", code, str(sessions / "session.json"), json.dumps(payload)],
                             self.root / "execution", 0.5)
        self.assertEqual(process["status"], "timeout")
        receipt = session_receipt(sessions)
        self.assertEqual(receipt["status"], "retained")
        self.assertEqual(receipt["sessions"][0]["meta"]["session_id"], "synthetic")
        self.assertNotIn("private-not-for-receipt", json.dumps(receipt))

    def test_missing_partial_and_symlink_sessions_fail_closed(self):
        sessions = self.root / "native-session"
        sessions.mkdir()
        self.assertIn("session_checkpoint_missing", session_receipt(sessions)["gaps"])
        write_new(sessions / "session.json", '{"meta":')
        self.assertIn("invalid_session_checkpoint", session_receipt(sessions)["gaps"])
        (sessions / "outside").symlink_to(self.root / "not-a-session")
        self.assertEqual(session_receipt(sessions)["status"], "incomplete")

    def test_private_artifact_drift_is_rejected(self):
        write_json(self.root / "native-session/session.json", {"meta": {"session_id": "s"}})
        write_json(self.root / "session-receipt.json", session_receipt(self.root / "native-session"))
        write_new(self.root / "adapter/task.txt", "task")
        run = {"runtime_contract": RUNTIME_CONTRACT, "task_sha256": digest(b"task"),
               "session_receipt_sha256": digest(read_regular(self.root / "session-receipt.json"))}
        validate_repair_artifacts(self.root, run)
        (self.root / "native-session/session.json").write_text('{}')
        with self.assertRaises(EvalError):
            validate_repair_artifacts(self.root, run)

    def test_native_search_probe_does_not_trust_path_rg_alone(self):
        grep = mock.AsyncMock(return_value="Found 1 match(es)\nCI_EVAL_SEARCH_READY")
        search = types.ModuleType("chrys.service.tools.builtins.search")
        search._find_rg = mock.Mock(return_value="/vendor/rg")
        search._grep_impl = grep
        with mock.patch.dict(sys.modules, {search.__name__: search}), mock.patch("chrys_preflight.subprocess.run") as command:
            result = asyncio.run(check_search())
            self.assertTrue(result["native_grep"])
            self.assertEqual(command.call_args.args[0], ["/vendor/rg", "--version"])
            grep.assert_awaited_once()
            grep.return_value = "Error: Exec format error"
            with self.assertRaises(EvalError):
                asyncio.run(check_search())

    def shell_probe(self, results):
        function = mock.AsyncMock(side_effect=results)
        model = types.ModuleType("chrys.foundation.models.session_env")
        model.SessionEnvironment = mock.Mock()
        shell = types.ModuleType("chrys.service.tools.builtins.shell")
        shell.ShellTools = mock.Mock()
        shell.ShellTools.return_value.tools.return_value = [function]
        with mock.patch.dict(sys.modules, {model.__name__: model, shell.__name__: shell}):
            result = asyncio.run(check_shell())
        self.assertEqual(shell.ShellTools.call_args.args, (model.SessionEnvironment.capture.return_value,))
        return result, function

    def test_native_shell_probe_uses_actual_tool_and_both_exit_paths(self):
        rows = [f"CI_EVAL_SHELL_STDOUT\nCI_EVAL_SHELL_STDERR\n[exit_code: {code}]" for code in (0, 7)]
        result, function = self.shell_probe(rows)
        self.assertTrue(result["native_shell"])
        self.assertEqual(result["exit_codes"], [0, 7])
        self.assertEqual(function.await_count, 2)
        for call in function.await_args_list:
            self.assertIn("shell-probe.txt", call.kwargs["command"])
            self.assertEqual(call.kwargs["timeout"], 5)
            self.assertFalse(Path(call.kwargs["working_dir"]).exists())

    def test_native_shell_probe_rejects_pty_error_and_incomplete_output(self):
        good = "CI_EVAL_SHELL_STDOUT\nCI_EVAL_SHELL_STDERR\n[exit_code: 0]"
        for rows in (["Error: failed to execute command — out of pty devices"],
                     ["CI_EVAL_SHELL_STDOUT\n[exit_code: 0]"], [good, good], [None]):
            with self.subTest(results=rows), self.assertRaises(EvalError):
                self.shell_probe(rows)

    def test_empty_inventory_is_also_bound(self):
        (self.root / "native-session").mkdir()
        write_json(self.root / "session-receipt.json", session_receipt(self.root / "native-session"))
        write_new(self.root / "adapter/task.txt", "task")
        run = {"runtime_contract": RUNTIME_CONTRACT, "task_sha256": digest(b"task"),
               "session_receipt_sha256": digest(read_regular(self.root / "session-receipt.json"))}
        validate_repair_artifacts(self.root, run)
        write_new(self.root / "native-session/added.json", "{}")
        with self.assertRaises(EvalError):
            validate_repair_artifacts(self.root, run)

    def execute_fake_trial(self, preflight_ok):
        skill = self.root / "skill"
        write_new(skill / "SKILL.md", "---\nname: ci-self-healing\ndescription: Synthetic test\n---\n# Test\n")
        suite = self.root / "suite"
        manifest = prepare(suite, skill, mode="single", case_ids=["case-003"], repeats=1)
        row = manifest["trials"][0]
        trial = suite / "trials" / row["id"]
        profiles = self.root / "profiles"
        profiles.mkdir()
        config = {"image": "sha256:" + "a" * 64, "network": "test-internal", "agent": "EvalCode",
                  "expected_model_id": "glm-5.2", "chrys_config_dir": str(profiles),
                  "profile_snapshot_sha256": "fake", "timeout_seconds": 600, "max_tool_calls": 80}
        invocations = []
        def run(argv, directory, timeout, name):
            invocations.append(argv)
            if "--profiles" in argv:
                self.assertIn("none", argv)
                self.assertNotIn("CHRYS_EVAL_API_KEY", argv)
                return {"status": "completed", "returncode": 0 if preflight_ok else 2}
            self.assertTrue(any("dst=" + SKILL_MOUNT + ",readonly" in a for a in argv))
            self.assertTrue(any("dst=" + SESSION_MOUNT in a and "readonly" not in a for a in argv))
            self.assertFalse(any("dst=/skill," in a for a in argv))
            for filename in ADAPTER_FILES:
                self.assertTrue((trial / "adapter" / filename).is_file())
            write_new(Path(directory) / "stdout.log", "synthetic trace")
            write_json(trial / "native-session/session.json", {"meta": {"session_id": "s", "model_id": "glm-5.2"}})
            return {"status": "timeout", "returncode": -9}
        with mock.patch("runtime.load_runtime", return_value=config), \
             mock.patch("runtime.docker_probe", return_value={"image_id": config["image"], "network_id": "n"}), \
             mock.patch("runtime.probe_build", return_value={"status": "passed", "checks": {"maven": {"stdout": "failed build", "stderr": ""}}}), \
             mock.patch("runtime.run_container", side_effect=run), \
             mock.patch.dict(os.environ, {"CHRYS_EVAL_API_KEY": "synthetic"}):
            run_trials(suite, self.root / "runtime.json")
        return trial, row, invocations

    def test_failed_registration_does_not_launch_model(self):
        trial, row, invocations = self.execute_fake_trial(False)
        self.assertEqual(len(invocations), 1)
        self.assertEqual(read_json(trial / "run.json")["status"], "preflight_failed")
        self.assertEqual(grade_trial(trial.parents[1], row, None)["status"], "incomplete")
        self.assertFalse((trial / "execution").exists())

    def test_mounts_and_timeout_receipt_are_bound(self):
        trial, row, invocations = self.execute_fake_trial(True)
        self.assertEqual(len(invocations), 2)
        run = read_json(trial / "run.json")
        self.assertEqual(run["status"], "timeout")
        validate_repair_artifacts(trial, run)
        self.assertEqual(read_json(trial / "session-receipt.json")["status"], "retained")
        self.assertIn("宿主执行预算", read_regular(trial / "adapter/task.txt").decode())
        self.assertNotIn("宿主执行预算", read_regular(trial / "visible/task.txt").decode())

    @unittest.skipUnless(httpx, "use the existing Chrys Python for HTTPX")
    def test_transport_timing_sync_stream_has_no_content_or_credentials(self):
        events = []
        class Stream(httpx.SyncByteStream):
            def __iter__(self):
                yield b"private-response"
        original = httpx.Client.send
        with transport_timing(lambda k, v: events.append((k, v))):
            with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Stream()))) as client:
                with client.stream("POST", "https://private.invalid/secret", headers={"Authorization": "secret"}, content=b"private-prompt") as response:
                    self.assertEqual(response.read(), b"private-response")
        self.assertIs(httpx.Client.send, original)
        self.assertEqual([e[0] for e in events], ["transport.started", "transport.headers_received", "transport.first_body_bytes", "transport.body_finished"])
        self.assertNotIn("private", json.dumps(events))
        self.assertNotIn("secret", json.dumps(events))

    @unittest.skipUnless(httpx, "use the existing Chrys Python for HTTPX")
    def test_transport_async_failure_and_restore(self):
        events = []
        async def request():
            async def handler(request):
                raise httpx.ReadTimeout("private-upstream-error")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await client.get("https://private.invalid/")
        original = httpx.AsyncClient.send
        with self.assertRaises(httpx.ReadTimeout), transport_timing(lambda k, v: events.append((k, v))):
            asyncio.run(request())
        self.assertIs(httpx.AsyncClient.send, original)
        self.assertEqual(events[-1][1]["error_type"], "ReadTimeout")
        self.assertNotIn("private", json.dumps(events))

    @unittest.skipUnless(httpx, "use the existing Chrys Python for HTTPX")
    def test_transport_async_stream_completion(self):
        events = []
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"private-body"
        async def request():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Stream()))) as client:
                async with client.stream("POST", "https://private.invalid/") as response:
                    self.assertEqual(await response.aread(), b"private-body")
        with transport_timing(lambda k, v: events.append((k, v))):
            asyncio.run(request())
        self.assertEqual(events[-1][0], "transport.body_finished")
        self.assertEqual(events[-1][1]["bytes"], 12)
        self.assertNotIn("private", json.dumps(events))

    @unittest.skipUnless(httpx, "use the existing Chrys Python for HTTPX")
    def test_unregistered_session_is_blocked_before_transport(self):
        recorder = Recorder(io.StringIO(), self.root)
        handler = mock.Mock(return_value=httpx.Response(200))
        original = httpx.Client.send
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(EvalError), transport_timing(recorder.emit, before_send=recorder.require_registration):
                client.post("https://fixture.invalid/responses")
        handler.assert_not_called()
        self.assertIs(httpx.Client.send, original)


if __name__ == "__main__":
    unittest.main()
