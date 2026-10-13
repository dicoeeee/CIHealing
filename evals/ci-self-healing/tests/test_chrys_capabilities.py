"""Version-agnostic admission: capabilities required, release numbers not pinned."""

import dataclasses
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from chrys_capabilities import observer_capabilities, require_cli_options
from common import EvalError, write_new
from native_runner import load_native
from runtime import docker_probe


def synthetic_api(version):
    modules = {name: types.ModuleType(name) for name in (
        "chrys", "chrys.foundation.events", "chrys.foundation.events.bus", "chrys.app.cli.app")}
    modules["chrys"].__version__ = version
    async def publish(self, event, *, raise_handler_errors=False):
        pass
    modules["chrys.foundation.events.bus"].EventBus = type("EventBus", (), {"publish": publish})
    modules["chrys.app.cli.app"].main = lambda: 0
    shapes = {
        "SessionReady": ("skill_names", "runtime_details"),
        "AgentRuntimeUpdated": ("skill_names", "runtime_details"),
        "InvocationMessage": ("origin", "text", "is_final"),
        "InvocationToolCallStart": ("origin", "tool_name", "args", "call_id"),
        "InvocationToolCallResult": ("origin", "tool_name", "result", "call_id"),
    }
    modules["chrys.foundation.events"].types = types.SimpleNamespace(**{
        name: dataclasses.make_dataclass(name, [(field, object) for field in fields]) for name, fields in shapes.items()})
    return modules


class ChrysCapabilitiesTests(unittest.TestCase):
    def test_observer_accepts_any_reported_version_with_required_capabilities(self):
        for version in ("0.25.0", "0.29.0", "1.99.0-dev", "enterprise-build"):
            with self.subTest(version=version), mock.patch.dict(sys.modules, synthetic_api(version)):
                self.assertEqual(observer_capabilities(), {"chrys_version": version, "observer_event_api": True})

    def test_known_version_cannot_bypass_missing_event_fields(self):
        modules = synthetic_api("0.29.0")
        modules["chrys.foundation.events"].types.InvocationToolCallResult = dataclasses.make_dataclass("Incomplete", [("result", str)])
        with mock.patch.dict(sys.modules, modules), self.assertRaisesRegex(EvalError, "InvocationToolCallResult"):
            observer_capabilities()

    def test_incompatible_event_publisher_is_rejected(self):
        modules = synthetic_api("new-version")
        async def publish(self, event):
            pass
        modules["chrys.foundation.events.bus"].EventBus.publish = publish
        with mock.patch.dict(sys.modules, modules), self.assertRaisesRegex(EvalError, "incompatible"):
            observer_capabilities()

    def test_cli_probe_requires_whole_options(self):
        require_cli_options("--agent NAME --model NAME --json", ("--agent", "--model", "--json"))
        with self.assertRaises(EvalError):
            require_cli_options("--json-format", ("--json",))

    def test_reviewer_config_has_no_release_pin(self):
        config = {"chrys": "/fixture/chrys", "python": "/fixture/python", "profile": "/fixture/profile.yaml", "endpoint_host": "fixture.invalid"}
        wanted = load_native(config)
        self.assertNotIn("version", wanted)
        for version in ("old", "future", "0.29.0"):
            self.assertEqual(load_native({**config, "version": version}), wanted)

    def test_doctor_records_actual_version_instead_of_a_constant(self):
        calls = [b"server", b'[{"Id":"sha256:fixture"}]', b'[{"Id":"n", "Internal":true}]']
        def probe(argv, directory, timeout, name):
            write_new(Path(directory) / "stdout.log", json.dumps({"status": "passed", "chrys_version": "future-build",
                      "observer_event_api": True, "headless_cli": True, "search": {"native_grep": True}}))
            return {"status": "completed", "returncode": 0}
        with mock.patch("runtime.command", side_effect=calls), mock.patch("runtime.run_container", side_effect=probe), \
             mock.patch("runtime.os.getuid", return_value=501):
            self.assertEqual(docker_probe({"image": "sha256:fixture", "network": "isolated"})["chrys_version"], "future-build")


if __name__ == "__main__":
    unittest.main()
