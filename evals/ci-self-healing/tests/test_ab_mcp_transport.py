"""MCP startup is independent of the model's Skill-registration gate."""
import asyncio
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ab_worker import model_request_filter
from repair_telemetry import transport_timing

try:
    import httpx
except ImportError:
    httpx = None


@unittest.skipUnless(httpx, "run with installed iCode Python for HTTPX")
class ABMCPTransportTests(unittest.TestCase):
    def test_origin_filter_matches_scheme_host_port_not_path(self):
        selected = model_request_filter("http://127.0.0.1:12345/v1")
        self.assertTrue(selected(httpx.Request("POST", "http://127.0.0.1:12345/v1/responses")))
        self.assertTrue(selected(httpx.Request("POST", "http://127.0.0.1:12345/other-model-path")))
        for url in ("http://127.0.0.1:12346/mcp", "https://127.0.0.1:12345/v1/responses", "http://localhost:12345/v1/responses"):
            self.assertFalse(selected(httpx.Request("POST", url)))
        self.assertTrue(model_request_filter("https://MODEL.invalid:443/v1")(httpx.Request("POST", "https://model.invalid/responses")))
        for url in ("file:///tmp/model", "http://user:secret@model.invalid", "http://model.invalid:0", "http://model.invalid?token=secret"):
            with self.assertRaises(ValueError):
                model_request_filter(url)

    def exercise_sync(self):
        events, received, gate_calls = [], [], []
        registered = False
        def gate():
            gate_calls.append(True)
            if not registered:
                raise RuntimeError("skill_not_registered")
        def handler(request):
            received.append(request.url.port)
            return httpx.Response(200, json={"ok": True})
        original = httpx.Client.send
        with transport_timing(lambda k, v: events.append((k, v)), before_send=gate,
                              request_filter=model_request_filter("http://127.0.0.1:12345/v1")):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                self.assertEqual(client.post("http://127.0.0.1:12346/mcp").status_code, 200)
                self.assertEqual((events, gate_calls), ([], []))
                with self.assertRaisesRegex(RuntimeError, "skill_not_registered"):
                    client.post("http://127.0.0.1:12345/v1/responses")
                self.assertEqual(received, [12346])
                registered = True
                self.assertEqual(client.post("http://127.0.0.1:12345/v1/responses").status_code, 200)
        self.assertIs(httpx.Client.send, original)
        self.assertEqual(received, [12346, 12345])
        self.assertEqual(len(gate_calls), 2)
        self.assertEqual([x for x, _ in events].count("transport.started"), 1)
        self.assertNotIn("127.0.0.1", json.dumps(events))

    def test_sync_mcp_before_registration_and_model_gate_remain_independent(self):
        self.exercise_sync()

    def test_async_mcp_before_registration_and_model_gate_remain_independent(self):
        events, received, gate_calls = [], [], []
        registered = False
        def gate():
            gate_calls.append(True)
            if not registered:
                raise RuntimeError("skill_not_registered")
        async def handler(request):
            received.append(request.url.port)
            return httpx.Response(200, json={"ok": True})
        async def request():
            nonlocal registered
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                self.assertEqual((await client.post("http://127.0.0.1:12346/mcp")).status_code, 200)
                self.assertEqual((events, gate_calls), ([], []))
                with self.assertRaisesRegex(RuntimeError, "skill_not_registered"):
                    await client.post("http://127.0.0.1:12345/v1/responses")
                self.assertEqual(received, [12346])
                registered = True
                self.assertEqual((await client.post("http://127.0.0.1:12345/v1/responses")).status_code, 200)
        original = httpx.AsyncClient.send
        with transport_timing(lambda k, v: events.append((k, v)), before_send=gate,
                              request_filter=model_request_filter("http://127.0.0.1:12345/v1")):
            asyncio.run(request())
        self.assertIs(httpx.AsyncClient.send, original)
        self.assertEqual(received, [12346, 12345])
        self.assertEqual(len(gate_calls), 2)
        self.assertEqual([x for x, _ in events].count("transport.started"), 1)
        self.assertNotIn("127.0.0.1", json.dumps(events))

    def test_default_filter_keeps_legacy_guard_behavior(self):
        invoked = []
        def gate():
            raise RuntimeError("legacy_guard")
        with transport_timing(lambda *args: None, before_send=gate):
            with httpx.Client(transport=httpx.MockTransport(lambda r: invoked.append(True) or httpx.Response(200))) as client:
                with self.assertRaisesRegex(RuntimeError, "legacy_guard"):
                    client.post("http://127.0.0.1:12346/mcp")
        self.assertEqual(invoked, [])


if __name__ == "__main__":
    unittest.main()
