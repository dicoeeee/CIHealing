"""Native adapter boundaries, using synthetic public events and local files only."""
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import native, worker


def event(name, **values):
    value = type(name, (), {})()
    for key, item in values.items():
        setattr(value, key, item)
    return value


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = io.StringIO()
        self.layout = {"role": "execute", "visible": str(self.root), "skill": str(self.root / "skill"), "max_tool_calls": 2}

    def tearDown(self):
        self.temp.cleanup()

    def rows(self):
        return [json.loads(line[len(worker.PREFIX):]) for line in self.output.getvalue().splitlines()]

    def test_private_content_is_not_emitted_or_accumulated(self):
        observer = worker.Observer(self.output, self.layout)
        for name in ("InvocationContentDelta", "InvocationContentFinished", "InvocationMessage"):
            observer.observe(event(name, channel="reasoning", text="HIDDEN_REASONING", origin={"session_id": "s"}))
        observer.observe(event("InvocationContentDelta", channel="answer", text="public answer", origin={"session_id": "s"}, segment_id="a"))
        observer.observe(event("InvocationContentFinished", channel="answer", origin={"session_id": "s"}, segment_id="a", status="completed"))
        self.assertEqual(self.rows()[0]["payload"]["text"], "public answer")
        self.assertNotIn("HIDDEN_REASONING", self.output.getvalue())
        self.assertEqual(observer.answers, {})

    def test_tool_boundary_changes_reconstruct_real_file_state(self):
        path = self.root / "source.java"
        path.write_text("original")
        observer = worker.Observer(self.output, self.layout)
        observer.observe(event("InvocationToolCallStart", tool_name="edit_file", call_id="c", args={"path": str(path)}))
        path.write_text("changed")
        observer.observe(event("InvocationToolCallResult", tool_name="edit_file", call_id="c", result="done"))
        changed = self.rows()[1]["payload"]["path_changes"]["source.java"]
        self.assertNotEqual(changed["before"]["sha256"], changed["after"]["sha256"])
        self.assertEqual(observer.pending, {})
        self.assertEqual(observer.gaps, [])

    def test_changes_between_calls_are_observed_before_next_tool(self):
        observer = worker.Observer(self.output, self.layout)
        (self.root / "interleaved.txt").write_text("new")
        observer.observe(event("InvocationToolCallStart", tool_name="read_file", call_id="r", args={}))
        change = self.rows()[0]["payload"]["path_changes"]["interleaved.txt"]
        self.assertIsNone(change["before"])
        self.assertEqual(change["after"]["size"], 3)

    def test_judge_tool_attempt_remains_failure_even_if_cli_catches_error(self):
        observer = worker.Observer(self.output, {**self.layout, "role": "judge"})
        with self.assertRaisesRegex(RuntimeError, "judge_attempted_tool"):
            observer.observe(event("InvocationToolCallStart", tool_name="bash", call_id="bad", args={"command": "echo forbidden"}))
        self.assertIn("judge_attempted_tool", observer.gaps)
        self.assertEqual(self.rows()[0]["kind"], "InvocationToolCallStart")

    def test_max_tools_is_enforced_before_dispatch(self):
        observer = worker.Observer(self.output, {**self.layout, "max_tool_calls": 1})
        observer.observe(event("InvocationToolCallStart", tool_name="read_file", call_id="one", args={}))
        with self.assertRaisesRegex(RuntimeError, "tool_budget_exhausted"):
            observer.observe(event("InvocationToolCallStart", tool_name="read_file", call_id="two", args={}))
        self.assertIn("tool_budget_exhausted", observer.gaps)

    def test_observer_redacts_only_injected_token_without_exporting_config(self):
        with mock.patch.dict("os.environ", {"CI_EVAL_MODEL_TOKEN": "synthetic-token"}):
            observer = worker.Observer(self.output, self.layout)
        observer.emit("InvocationMessage", {"text": "synthetic-token"})
        self.assertNotIn("synthetic-token", self.output.getvalue())
        self.assertIn("[REDACTED]", self.output.getvalue())

    def test_snapshot_does_not_follow_symlink_to_hidden_oracle(self):
        with tempfile.TemporaryDirectory() as outside:
            oracle = Path(outside) / "oracle.txt"
            oracle.write_text("hidden answer")
            (self.root / "link").symlink_to(oracle)
            state = worker.snapshot(self.root)
        self.assertEqual(state["link"]["kind"], "symlink")
        self.assertNotIn("hidden answer", json.dumps(state))

    def test_snapshot_restricted_markdown_only_records_metadata(self):
        (self.root / "restricted.md").write_text("---\nai_access: false\n---\nPRIVATE_BODY\n")
        state = worker.snapshot(self.root)
        self.assertEqual(state["restricted.md"]["kind"], "restricted")
        self.assertNotIn("sha256", state["restricted.md"])


class NativeTests(unittest.TestCase):
    def binding(self):
        profile = {"requested_model_id": "same-model", "profile_sha256": "frozen-profile"}
        tools = {"icode": {"version": "same-version", "executable_sha256": "frozen-bin"}, "maven_cache_sha256": "frozen-cache"}
        return {"_frozen_identity": {"execution_model": profile, "review_model": profile, "runtime_identity": tools}}, profile, tools

    def test_same_model_id_with_changed_profile_is_refused_before_gateway(self):
        runtime, frozen, tools = self.binding()
        changed = {**frozen, "profile_sha256": "different-profile"}
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp).resolve() / "judge"
            with mock.patch.object(native, "_config", return_value=runtime), \
                 mock.patch.object(native, "_profile", return_value=({}, changed)), \
                 mock.patch.object(native, "_prepare_runtime"), \
                 mock.patch.object(native, "_tool_identity", return_value=tools), \
                 mock.patch.object(native, "_gateway") as gateway:
                with self.assertRaisesRegex(ValueError, "identity_drift"):
                    native.judge(directory, runtime, "synthetic evidence", 30)
                gateway.assert_not_called()

    def test_cache_or_executable_drift_is_refused_with_unchanged_profile(self):
        runtime, profile, tools = self.binding()
        for changed in ({**tools, "maven_cache_sha256": "different-cache"},
                        {**tools, "icode": {"version": "same-version", "executable_sha256": "different-bin"}}):
            with self.assertRaisesRegex(ValueError, "identity_drift"):
                native._assert_binding(runtime, "execute", profile, changed)
        native._assert_binding(runtime, "execute", profile, tools)

    def test_incomplete_frozen_identity_is_not_silently_ignored(self):
        _, profile, tools = self.binding()
        with self.assertRaisesRegex(ValueError, "malformed"):
            native._assert_binding({"_frozen_identity": None}, "judge", profile, tools)

    def test_post_execution_drift_does_not_turn_completed_process_into_pass(self):
        runtime, profile, tools = self.binding()
        result = {"status": "completed", "trace_complete": True, "gaps": []}
        changed = {**profile, "profile_sha256": "changed-during-session"}
        with mock.patch.object(native, "_profile", return_value=({}, changed)), \
             mock.patch.object(native, "_tool_identity", return_value=tools):
            native._finish_binding(result, runtime, "judge", profile, tools, Path("/private/tmp"), 1)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["trace_complete"])
        self.assertIn("identity_drift", result["gaps"])
        self.assertEqual(result["identity_binding"]["status"], "drift")

    def test_standalone_call_still_checks_its_own_before_after_identity(self):
        _, profile, tools = self.binding()
        result = {"status": "completed", "trace_complete": True, "gaps": []}
        with mock.patch.object(native, "_profile", return_value=({}, profile)), \
             mock.patch.object(native, "_tool_identity", return_value=tools):
            native._finish_binding(result, {}, "execute", profile, tools, Path("/private/tmp"), 1)
        self.assertTrue(result["trace_complete"])
        self.assertEqual(result["identity_binding"]["scope"], "session-local")
        self.assertEqual(result["identity_binding"]["status"], "stable")

    def test_unavailable_end_identity_is_a_gap_without_exception_text(self):
        runtime, profile, tools = self.binding()
        result = {"status": "completed", "trace_complete": True, "gaps": []}
        with mock.patch.object(native, "_profile", side_effect=ValueError("SECRET_NOT_FOR_LOGS")):
            native._finish_binding(result, runtime, "judge", profile, tools, Path("/private/tmp"), 1)
        self.assertFalse(result["trace_complete"])
        self.assertIn("identity_recheck_incomplete", result["gaps"])
        self.assertNotIn("SECRET_NOT_FOR_LOGS", json.dumps(result))

    def test_isolated_profile_never_contains_upstream_credential_or_endpoint(self):
        profile = {"id": "p", "name": "model", "provider": "openai", "api_style": "responses", "model_id": "m",
                   "api_key": "UPSTREAM_SECRET", "base_url": "https://private.example/v1", "max_output_tokens": 100,
                   "http_headers": {"secret": "must-not-copy"}}
        selected = native._isolated_model(profile, 12345)
        self.assertEqual(selected["api_key"], "{{CI_EVAL_MODEL_TOKEN}}")
        self.assertEqual(selected["http_max_retries"], 0)
        self.assertNotIn("UPSTREAM_SECRET", json.dumps(selected))
        self.assertNotIn("private.example", json.dumps(selected))
        self.assertNotIn("http_headers", selected)

    def test_only_judge_gets_owned_schema_and_no_profile_chat_options(self):
        from harness.review_schema import response_format
        injected = {"response_format": {"type": "json_object"}, "temperature": 0.9,
                    "instructions": "UNTRUSTED_PROFILE_OPTIONS", "text": {"format": {"type": "text"}}}
        profile = {"id": "p", "name": "synthetic", "chat_options": json.dumps(injected),
                   "api_key": "SYNTHETIC_UPSTREAM_SECRET"}
        execution = native._isolated_model(profile, 12345)
        review = native._isolated_model(profile, 12345, role="judge")
        self.assertNotIn("chat_options", execution)
        self.assertEqual(json.loads(review["chat_options"]), {"response_format": response_format()})
        self.assertNotIn("UNTRUSTED_PROFILE_OPTIONS", json.dumps(review))
        self.assertNotIn("SYNTHETIC_UPSTREAM_SECRET", json.dumps(review))
        self.assertEqual(profile["chat_options"], json.dumps(injected))

    def test_timeout_cannot_exceed_approved_stage_limit(self):
        for value in (0, -1, 901, True, "900"):
            with self.assertRaises(ValueError):
                native._seconds(value)
        self.assertEqual(native._seconds(900), 900)

    def test_fixture_paths_cannot_escape_or_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            trial = Path(temp).resolve()
            (trial / "link").symlink_to("/private/tmp")
            for path in ("../outside", "/private/tmp", "link/file"):
                with self.assertRaises(ValueError):
                    native._within(trial, path)
            self.assertEqual(native._within(trial, "visible/output"), trial / "visible/output")

    def write_session(self, root, response_model="provider-alias"):
        session = root / "one"
        (session / "trajectory").mkdir(parents=True)
        (session / "session.json").write_text(json.dumps({"meta": {"session_id": "s", "model_id": "requested"}}))
        row = {"event_type": "model.exchange.finished", "payload": {"response_model": response_model, "outcome": "success",
                                                                    "finish_reason": None, "response_id": "r"}}
        (session / "trajectory/events.jsonl").write_text(json.dumps(row) + "\n")

    def test_response_alias_must_be_explicit_not_inferred_from_output(self):
        with tempfile.TemporaryDirectory() as temp:
            sessions = Path(temp).resolve()
            self.write_session(sessions)
            inferred = native._session_identity(sessions, "requested")
            pinned = native._session_identity(sessions, "requested", "provider-alias")
        self.assertFalse(inferred["response_model_confirmed"])
        self.assertIn("response_model_identity_unconfirmed", inferred["gaps"])
        self.assertTrue(pinned["response_model_confirmed"])

    def test_success_exit_without_public_trace_cannot_be_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            log = root / "process"
            log.mkdir()
            (log / "stdout.log").write_text("")
            sessions = root / "sessions"
            sessions.mkdir()
            self.write_session(sessions)
            result = native._collect({"status": "completed", "returncode": 0, "duration_seconds": 1,
                                      "cleanup": {"status": "confirmed"}}, log, sessions,
                                     {"requested_model_id": "requested"}, "judge",
                                     {"review_response_model_id": "provider-alias"}, root)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["trace_complete"])
        self.assertIn("public_trace_incomplete", result["gaps"])


@unittest.skipUnless(os.environ.get("CI_EVAL_V2_NATIVE_RUNTIME"), "explicit installed-runtime path required; no external models")
class InstalledWorkerProtocolTests(unittest.TestCase):
    """Real installed iCode against a fake local Responses server, never a model."""
    def test_gateway_process_cleanup_without_upstream_request(self):
        import urllib.request
        with tempfile.TemporaryDirectory(prefix="ci-eval-v2-gateway-", dir="/private/tmp") as temp:
            directory = Path(temp).resolve() / "gateway"
            profile = {"base_url": "https://not-contacted.example.invalid/v1", "api_key": "SYNTHETIC_UPSTREAM_SECRET",
                       "model_id": "synthetic-model", "max_output_tokens": 100}
            with native._gateway(profile, directory) as (port, token):
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
                    self.assertEqual(response.status, 200)
            cleanup = json.loads((directory / "cleanup.json").read_text())
            self.assertEqual(cleanup["status"], "confirmed")
            self.assertTrue(cleanup["group_absent"])
            for path in directory.iterdir():
                if path.is_file():
                    self.assertNotIn("SYNTHETIC_UPSTREAM_SECRET", path.read_text())
                    self.assertNotIn(token, path.read_text())

    def run_role(self, role, *, rejection_status=None, review_contract=None, command_probe=None, artifact_root=None):
        from environment.gateway import allowed_request
        from harness.review_schema import response_schema
        runtime = json.loads(Path(os.environ["CI_EVAL_V2_NATIVE_RUNTIME"]).read_text())
        calls = []
        requested_model = "glm-5.2" if role == "execute" else "deepseek-v4.1-flash"
        response_model = requested_model if role == "execute" else "deepseek-v4-1-flash-261017"
        answer = "SYNTHETIC_PUBLIC_FINAL" if role == "execute" else json.dumps({"checks": [{
            "criterion_id": "synthetic.criterion", "applicability": "applicable", "status": "pass",
            "evidence_ids": ["synthetic-e1"], "reason": "Synthetic protocol fixture; no model was called."}]})
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(body)
                if not allowed_request(body, requested_model):
                    self.send_response(400); self.end_headers(); return
                if rejection_status is not None:
                    encoded = json.dumps({"error": {"message": "SYNTHETIC_SCHEMA_UNSUPPORTED",
                        "type": "invalid_request_error", "code": "unsupported_response_format"}}).encode()
                    self.send_response(rejection_status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers(); self.wfile.write(encoded); return
                if role == "execute" and len(calls) == 1:
                    names = [item.get("name") for item in body.get("tools", [])]
                    if "load_skill" not in names:
                        self.send_response(500); self.end_headers(); return
                    output = [{"type": "function_call", "id": "fc_synthetic", "call_id": "call_synthetic",
                               "name": "load_skill", "arguments": '{"skill_name":"ci-self-healing"}', "status": "completed"}]
                elif role == 'execute' and command_probe and len(calls) == 2:
                    shell = next((tool['name'] for tool in body.get('tools', []) if tool.get('name') in worker.SHELL_TOOLS), None)
                    if shell is None:
                        self.send_response(500); self.end_headers(); return
                    args = {'command': '/bin/cp input.txt output.txt' if command_probe == 'copy' else '/bin/sleep 2',
                            'reason': 'Fixed local protocol probe.', 'timeout': 5 if command_probe == 'copy' else 1}
                    output = [{'type': 'function_call', 'id': 'fc_command', 'call_id': 'call_command',
                               'name': shell, 'arguments': json.dumps(args), 'status': 'completed'}]
                elif role == 'execute' and command_probe == 'copy' and len(calls) == 3:
                    output = [{'type': 'function_call', 'id': 'fc_read', 'call_id': 'call_read',
                               'name': 'read_file', 'arguments': '{"path":"output.txt"}', 'status': 'completed'}]
                else:
                    output = [{"type": "message", "id": "msg_synthetic", "role": "assistant", "status": "completed",
                               "content": [{"type": "output_text", "text": answer, "annotations": []}]}]
                response = {"id": "resp_synthetic_" + str(len(calls)), "object": "response", "created_at": 1,
                            "status": "completed", "model": response_model, "output": output,
                            "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
                            "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20}}
                encoded = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers(); self.wfile.write(encoded)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="ci-eval-v2-protocol-", dir="/private/tmp") as temp:
                directory = Path(temp).resolve()
                native._prepare_runtime(directory / "runtime", runtime, copy_cache=False)
                own = directory / "runtime"
                visible = directory / "visible"
                cwd = visible / "scene/sourcecode"
                cwd.mkdir(parents=True)
                if command_probe:
                    (cwd / 'input.txt').write_text('synthetic new content\n')
                    (cwd / 'output.txt').write_text('synthetic old content\n')
                task = visible / "task.txt"
                task.write_text("Use the explicitly selected ci-self-healing Skill for this synthetic task.")
                skill = own / "skills/ci-self-healing"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("---\nname: ci-self-healing\ndescription: Local protocol fixture.\n---\n\nRead the supplied synthetic task.\n")
                port = server.server_address[1]
                profile = {"id": "synthetic", "name": "synthetic",
                           "provider": "openai" if role == "execute" else "deepseek-openai", "api_style": "responses",
                           "model_id": requested_model, "max_context_tokens": 32768,
                           "max_output_tokens": 1024, "stream": False,
                           "chat_options": json.dumps({"response_format": {"type": "json_object"},
                                                       "instructions": "UNTRUSTED_PROFILE_OPTIONS"})}
                layout = {"role": role, "cli": runtime["chrys"], "visible": str(visible), "skill": str(skill),
                          "config": str(own / "config"), "sessions": str(own / "sessions"),
                          "cwd": str(cwd), "task": str(task), "max_tool_calls": 3, "model_port": port,
                          "model": native._isolated_model(profile, port, role=role, review_contract=review_contract)}
                native.write_json(own / "adapter/layout.json", layout)
                sb = native.policy(runtime, read=[visible, skill, own / "adapter"],
                                   write=[*[own / n for n in ("config", "sessions", "tmp", "java-home")],
                                          *([visible] if command_probe else [])], port=port, pty=bool(command_probe))
                native.write_new(directory / "run.sb", sb)
                process = native.run_logged(["/usr/bin/sandbox-exec", "-f", str(directory / "run.sb"), runtime["python"], "-B",
                                             str(own / "adapter/worker.py"), str(own / "adapter/layout.json")],
                                            directory / "process", 45, cwd=cwd,
                                            env={**native.environment(runtime, own), "CI_EVAL_MODEL_TOKEN": "synthetic-token"})
                result = native._collect(process, directory / "process", own / "sessions",
                                         {"requested_model_id": requested_model}, role,
                                         {"review_response_model_id": response_model}, directory)
                self.assertNotIn("UNTRUSTED_PROFILE_OPTIONS", json.dumps(calls))
                self.assertTrue(calls, "installed CLI never reached the local fixture")
                if role == "judge":
                    expected_format = {"type": "json_schema", "name": "ci_review_v2" if review_contract is not None else "ci_review_v1", "strict": True,
                                       "schema": response_schema(review_contract)}
                    self.assertEqual(calls[0].get("text", {}).get("format"), expected_format)
                    schema = calls[0]["text"]["format"]["schema"]
                    self.assertIs(schema["additionalProperties"], False)
                    item = schema["properties"]["checks"]["items"]
                    self.assertIs(item["additionalProperties"], False)
                    self.assertEqual(set(item["required"]), {"criterion_id", "applicability", "status", "evidence_ids", "reason"})
                    self.assertEqual(set(item["properties"]), set(item["required"]))
                else:
                    self.assertTrue(all("format" not in call.get("text", {}) for call in calls))
                if rejection_status is not None:
                    self.assertEqual(len(calls), 1, "a rejected schema must not retry or fall back to unconstrained output")
                    self.assertNotEqual(result["status"], "completed")
                    self.assertFalse(result["trace_complete"])
                    self.assertEqual(result["answer"], "")
                    return
                if not result["trace_complete"]:
                    # Only test-local evidence and no upstream credentials exist here.
                    diagnostic = (directory / "process/stderr.log").read_text()
                    self.fail(json.dumps({"result": result, "stderr": diagnostic, "requests": len(calls)}))
                self.assertEqual(result["answer"], answer)
                rows = [json.loads(line) for line in Path(result["events_file"]).read_text().splitlines()]
                tool_events = [row for row in rows if row["kind"] == "InvocationToolCallStart"]
                expected_tools = 3 if command_probe == 'copy' else 2 if command_probe else 1 if role == 'execute' else 0
                self.assertEqual(len(tool_events), expected_tools)
                self.assertEqual(len(calls), expected_tools + 1)
                if command_probe:
                    from harness import command_checks
                    from harness.catalog import Catalog
                    for row in rows: row['evidence_id'] = 'e' + str(row['seq'])
                    packet = {'observation_complete': True, 'trace_complete': True,
                              'facts': {'events': rows}, 'evidence': {}}
                    catalog = Catalog()
                    checks = [command_checks.check(catalog.criteria[key], packet) for key in command_checks.PAIRS.values()]
                    terminal = [row['payload']['command_receipt'] for row in rows if 'command_receipt' in row.get('payload', {})]
                    self.assertEqual(len(terminal), 1)
                    self.assertTrue(terminal[0]['metadata']['collected'])
                    self.assertFalse(terminal[0]['metadata'].get('errored'), json.dumps([
                        row['payload'].get('result') for row in rows if 'command_receipt' in row.get('payload', {})]))
                    if command_probe == 'copy':
                        self.assertEqual([c['status'] for c in checks], ['pass', 'not_applicable', 'pass'], json.dumps({'terminal': terminal, 'checks': checks}))
                        self.assertTrue(checks[-1]['operations'][0]['result']['target_matches_expected'])
                    else:
                        self.assertTrue(terminal[0]['metadata'].get('shell_timed_out') or terminal[0]['metadata'].get('shell_background_task'), json.dumps(terminal))
                        self.assertEqual(checks[1]['status'], 'inconclusive')
                    if artifact_root is not None:
                        destination = Path(artifact_root) / command_probe
                        destination.mkdir(parents=True, exist_ok=False)
                        native.write_json(destination / 'packet.json', packet)
                        native.write_json(destination / 'checks.json', {'checks': checks, 'models_called': False,
                            'synthetic_provider': True, 'tool_execution': 'installed_iCode_native_tools', 'requests_to_local_fixture': len(calls)})
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_judge_real_cli_with_local_fake_response(self):
        self.run_role("judge")

    def test_execute_real_skill_tool_then_local_fake_completion(self):
        self.run_role("execute")

    def test_judge_real_cli_preserves_bound_ids_on_wire(self):
        self.run_role("judge", review_contract={"criterion_ids": ["synthetic.criterion"],
                                                "evidence_ids": ["synthetic-e1"]})

    def test_native_copy_metadata_and_readback_with_local_fixed_responses(self):
        self.run_role('execute', command_probe='copy')

    def test_native_timeout_or_background_metadata_is_not_normal_completion(self):
        self.run_role('execute', command_probe='timeout')

    def test_schema_rejection_is_not_retried_or_downgraded_by_real_cli(self):
        # The harness gateway maps a non-200 upstream response to 502. Cover
        # that production status as well as a provider's direct schema 400.
        for status in (400, 502):
            with self.subTest(status=status):
                self.run_role("judge", rejection_status=status)

if __name__ == "__main__":
    unittest.main()
