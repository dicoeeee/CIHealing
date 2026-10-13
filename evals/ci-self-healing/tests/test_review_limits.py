"""Byte-boundary regressions: complete UTF-8 evidence, no model calls."""

import copy
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import EvalError, json_bytes
from review_limits import (DEFAULT_PACKET_BYTES, ENVELOPE_HEADROOM, MAX_PACKET_BYTES,
                           check_packet, encode_envelope, encode_json, encode_request, input_limits, read_envelope)
from review_responses import request_body


class ReviewLimitsTests(unittest.TestCase):
    def test_config_has_a_default_hard_ceiling_and_separate_layers(self):
        limits = input_limits()
        self.assertEqual(limits["packet_bytes"], 512 * 1024)
        self.assertGreater(limits["envelope_bytes"], limits["packet_bytes"])
        self.assertGreater(limits["request_bytes"], limits["envelope_bytes"])
        self.assertEqual(limits["truncation"], "disabled")
        for bad in (0, -1, True, 1.5, "524288", MAX_PACKET_BYTES + 1):
            with self.subTest(bad=bad), self.assertRaises(EvalError):
                input_limits(bad)

    def test_large_unicode_packet_is_preserved_and_exact_limit_is_inclusive(self):
        packet = {"events": [{"evidence_id": "E0001", "text": "完整证据\n" * 23000}], "files": []}
        before = copy.deepcopy(packet)
        raw = json_bytes(packet)
        self.assertGreater(len(raw), 256 * 1024)
        self.assertLess(len(raw), DEFAULT_PACKET_BYTES)
        self.assertEqual(check_packet(packet), raw)
        self.assertEqual(check_packet(packet, len(raw)), raw)
        with self.assertRaisesRegex(EvalError, "must not be truncated"):
            check_packet(packet, len(raw) - 1)
        self.assertEqual(packet, before)

    def test_worker_envelope_reader_is_bounded_and_uses_bytes(self):
        budget = 100
        limit = input_limits(budget)["envelope_bytes"]
        self.assertEqual(read_envelope(io.BytesIO(b"x" * limit), budget), b"x" * limit)
        stream = io.BytesIO(b"x" * (limit + 100))
        with self.assertRaisesRegex(EvalError, "envelope too large"):
            read_envelope(stream, budget)
        self.assertEqual(stream.tell(), limit + 1)
        with self.assertRaises(EvalError):
            encode_envelope({"packet": {}, "instructions": "中" * ENVELOPE_HEADROOM, "schema": {}}, budget)

    def test_escaping_and_unicode_roundtrip_in_measured_wire_bytes(self):
        packet = {"events": [{"evidence_id": "E0001", "text": ('中\n\\"\t' * 12000)}], "files": []}
        payload = {"instructions": "trusted rubric", "schema": {}, "packet": packet}
        profile = {"model_id": "fixture", "max_output_tokens": 16384, "stream": True}
        envelope = encode_envelope(payload)
        body = request_body(profile, json.loads(envelope))
        wire = encode_request(body)
        self.assertEqual(json.loads(json.loads(wire)["input"][0]["content"]), packet)
        self.assertEqual(json.loads(wire)["truncation"], "disabled")
        self.assertEqual(len(json.loads(wire)["input"]), 1)
        self.assertNotIn("context_management", body)

    def test_request_limit_is_independent_and_does_not_modify_the_body(self):
        body = {"input": "x" * 300000}
        before = copy.deepcopy(body)
        with self.assertRaisesRegex(EvalError, "HTTP request too large"):
            encode_request(body, 100)
        self.assertEqual(body, before)
        raw = encode_json(body)
        limit = (len(raw) + 1) // 2 - ENVELOPE_HEADROOM
        self.assertEqual(encode_request(body, limit), raw)
        with self.assertRaises(EvalError):
            encode_request(body, limit - 1)


if __name__ == "__main__":
    unittest.main()
