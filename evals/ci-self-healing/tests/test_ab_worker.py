"""Offline worker boundaries: public content, profiles, and reviewer tool refusal."""
import asyncio
import copy
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ab_worker


@dataclass
class Origin:
    session_id: str
    invocation_id: str


def event(kind, **attrs):
    item = type(kind, (), {})()
    for key, value in attrs.items():
        setattr(item, key, value)
    return item


class ABWorkerTests(unittest.TestCase):
    def worker_layout(self):
        return {"config": "/private/tmp/ab-test/runtime/config", "sessions": "/private/tmp/ab-test/sessions",
                "model": {"base_url": "http://127.0.0.1:1234/v1", "name": "offline-model"},
                "task": "offline synthetic task", "max_tools": 4}

    def test_ordinary_execute_does_not_import_enterprise_helper(self):
        layout = self.worker_layout()
        observer = mock.Mock(main=mock.Mock(return_value=17))
        with mock.patch.object(sys, "argv", ["worker", "/private/tmp/ab-test/adapter/layout.json"]), \
             mock.patch.object(ab_worker, "read_json", return_value=layout), \
             mock.patch.object(ab_worker, "bootstrap"), mock.patch.object(ab_worker, "profiles", return_value="ABCode"), \
             mock.patch.object(ab_worker, "public_answer_observer", return_value=observer), \
             mock.patch.dict(sys.modules, {"ab_skill_host": None}):
            self.assertEqual(ab_worker.main(), 17)
        observer.main.assert_called_once()

    def test_reviewer_does_not_import_enterprise_helper_even_if_flag_is_present(self):
        layout = {**self.worker_layout(), "role": "review", "enterprise_resource_registration": True,
                  "cli": "synthetic-cli", "workdir": "/private/tmp/ab-test/work"}
        cli = ModuleType("chrys.app.cli.app")
        cli.main = mock.Mock(return_value=23)
        with mock.patch.object(sys, "argv", ["worker", "/private/tmp/ab-test/adapter/layout.json"]), \
             mock.patch.object(ab_worker, "read_json", return_value=layout), \
             mock.patch.object(ab_worker, "bootstrap"), mock.patch.object(ab_worker, "profiles", return_value="ABReviewer"), \
             mock.patch.object(ab_worker, "no_tool_reviewer", return_value=nullcontext()), \
             mock.patch.dict(sys.modules, {"ab_skill_host": None, "chrys.app.cli.app": cli}):
            self.assertEqual(ab_worker.main(), 23)
        cli.main.assert_called_once()

    def test_enterprise_preflight_finishes_before_observer_and_context_spans_execution(self):
        layout = {**self.worker_layout(), "enterprise_resource_registration": True}
        order = []
        helper = ModuleType("ab_skill_host")

        class Host:
            async def preflight(self):
                order.append("preflight")
                return {"status": "passed", "model_calls": 0}

        @contextmanager
        def selected(actual_layout, source):
            self.assertIs(actual_layout, layout)
            order.append("enter")
            try:
                yield Host()
            finally:
                order.append("exit")

        helper.enterprise_skill_host = selected
        observer = mock.Mock(main=lambda: order.append("observer"))
        with mock.patch.object(sys, "argv", ["worker", "/private/tmp/ab-test/adapter/layout.json"]), \
             mock.patch.object(ab_worker, "read_json", return_value=layout), \
             mock.patch.object(ab_worker, "bootstrap"), mock.patch.object(ab_worker, "profiles", return_value="ABCode"), \
             mock.patch.object(ab_worker, "public_answer_observer", return_value=observer), \
             mock.patch.object(ab_worker, "write_new", side_effect=lambda *args: order.append("receipt")), \
             mock.patch.dict(sys.modules, {"ab_skill_host": helper}):
            ab_worker.main()
        self.assertEqual(order, ["enter", "preflight", "receipt", "observer", "exit"])

    def observer(self):
        class Recorder:
            def __init__(self):
                self.emitted = []
                self.forwarded = []

            def observe(self, item):
                self.forwarded.append(item)

            def emit(self, kind, payload):
                self.emitted.append((kind, payload))

        module = ModuleType("chrys_observer")
        module.Recorder = Recorder
        with mock.patch.dict(sys.modules, {"chrys_observer": module}):
            self.assertIs(ab_worker.public_answer_observer(), module)
            patched = Recorder.observe
            ab_worker.public_answer_observer()
            self.assertIs(Recorder.observe, patched)
        return Recorder()

    def test_public_delta_aggregation_is_scoped_and_never_captures_reasoning(self):
        recorder = self.observer()
        a, b = Origin("s", "a"), Origin("s", "b")
        for origin, text in [(a, "第一"), (b, "another"), (a, "段")]:
            recorder.observe(event("InvocationContentDelta", origin=origin, agent_name="Code", segment_id="same", channel="answer", text=text))
        for kind in ("InvocationContentDelta", "InvocationContentFinished", "InvocationMessage"):
            recorder.observe(event(kind, origin=a, segment_id="private", channel="reasoning", text="PRIVATE_REASONING"))
        for origin in (b, a):
            recorder.observe(event("InvocationContentFinished", origin=origin, agent_name="Code", segment_id="same", channel="answer", status="completed"))
        self.assertEqual([p["text"] for _, p in recorder.emitted], ["another", "第一段"])
        self.assertEqual(recorder.forwarded, [])
        self.assertNotIn("PRIVATE_REASONING", json.dumps(recorder.emitted))
        self.assertEqual(recorder._ab_answers, {})
        self.assertEqual(recorder.emitted[1][1]["origin"]["invocation_id"], "a")

    def test_abandoned_answer_is_not_published_as_current_evidence(self):
        recorder = self.observer()
        origin = Origin("s", "a")
        recorder.observe(event("InvocationContentDelta", origin=origin, agent_name="Code", segment_id="old", channel="answer", text="discarded"))
        recorder.observe(event("InvocationContentAbandoned", origin=origin, agent_name="Code", segment_ids=("old",)))
        recorder.observe(event("InvocationContentFinished", origin=origin, agent_name="Code", segment_id="old", channel="answer", status="abandoned"))
        self.assertEqual(recorder.emitted, [])
        self.assertEqual(recorder._ab_answers, {})

    def test_reviewer_refuses_implicit_tools_even_if_cli_catches_error(self):
        published = []

        class Bus:
            async def publish(self, item, *, raise_handler_errors=False):
                published.append(item)
                return "ok"

        module = ModuleType("chrys.foundation.events.bus")
        module.EventBus = Bus
        original = Bus.publish
        with mock.patch.dict(sys.modules, {"chrys.foundation.events.bus": module}):
            with self.assertRaisesRegex(RuntimeError, "reviewer_tool_dispatch_forbidden"):
                with ab_worker.no_tool_reviewer():
                    self.assertEqual(asyncio.run(Bus().publish(event("InvocationMessage", text="public"))), "ok")
                    with self.assertRaisesRegex(RuntimeError, "reviewer_tool_dispatch_forbidden"):
                        asyncio.run(Bus().publish(event("InvocationToolCallStart", tool_name="sleep")))
            self.assertIs(Bus.publish, original)
        self.assertEqual(len(published), 1)

    def test_reviewer_guard_allows_normal_model_response_and_restores_bus(self):
        class Bus:
            async def publish(self, item, *, raise_handler_errors=False):
                return "model response preserved"

        module = ModuleType("chrys.foundation.events.bus")
        module.EventBus = Bus
        original = Bus.publish
        with mock.patch.dict(sys.modules, {"chrys.foundation.events.bus": module}):
            with ab_worker.no_tool_reviewer():
                result = asyncio.run(Bus().publish(event("InvocationMessage", text='{"verdict":"pass"}')))
            self.assertEqual(result, "model response preserved")
            self.assertIs(Bus.publish, original)

    def test_profile_mcp_is_tools_mcp_and_reviewer_has_no_enabled_resources(self):
        # A tiny serializer stub keeps this test independent of the CLI install;
        # the installed schema is separately checked without launching iCode.
        builtin = {"name": "Code", "tools": {"builtins": ["danger"], "mcp": [{"name": "old"}]},
                   "skills": {"paths": ["old"]}, "memory": {"files": ["old"]}, "sub_agents": {"agents": ["old"]}}
        yaml = ModuleType("yaml")
        yaml.safe_load = lambda data: copy.deepcopy(builtin)
        yaml.safe_dump = lambda value, **kwargs: json.dumps(value)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            chrys = ModuleType("chrys")
            chrys.__file__ = str(root / "chrys/__init__.py")
            profile_root = root / "chrys/service/profiles/agents/builtins"
            profile_root.mkdir(parents=True)
            for name in ("Code.yaml", "QA.yaml"):
                (profile_root / name).write_text("synthetic fixture")
            mcp = [{"name": "synthetic_cases", "transport": "http", "url": "http://127.0.0.1:1/mcp",
                    "allowed_tools": ["search_cases", "read_case"], "tool_name_prefix": "cases"}]
            with mock.patch.dict(sys.modules, {"chrys": chrys, "yaml": yaml}):
                for role, name in (("execute", "ABCode"), ("review", "ABReviewer")):
                    config = root / role
                    self.assertEqual(ab_worker.profiles({"role": role, "config": str(config), "skill": "/synthetic/skill",
                                                        "mcp": mcp, "model": {"name": "synthetic-model"}}), name)
                    actual = json.loads((config / "agents" / f"{name}.yaml").read_text())
                    self.assertEqual(actual["tools"]["mcp"], mcp if role == "execute" else [])
                    self.assertNotIn("mcp_servers", actual)
                    self.assertEqual(actual["memory"], {"files": []})
                    self.assertEqual(actual["sub_agents"], {"agents": []})
                    self.assertFalse(actual["skills"]["auto_load_user_agents_skills"])
                    if role == "review":
                        self.assertEqual(actual["tools"]["builtins"], [])
                        self.assertEqual(actual["skills"]["paths"], [])


if __name__ == "__main__":
    unittest.main()
