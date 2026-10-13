"""Offline gateway policy tests; run using Chrys's Python environment."""

import copy
import unittest

from gateway import allowed_request


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.body = {"model": "fixture-model", "input": "READY", "stream": True, "store": False}

    def test_text_and_local_function_tools(self):
        self.body["tools"] = [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}]
        self.assertTrue(allowed_request(self.body, "fixture-model"))

    def test_model_and_storage_are_restricted(self):
        for patch in ({"model": "other"}, {"store": True}, {"previous_response_id": "private"}, {"base_url": "https://elsewhere"}):
            with self.subTest(patch=patch):
                self.assertFalse(allowed_request({**self.body, **patch}, "fixture-model"))

    def test_hosted_tools_are_rejected(self):
        for kind in ("web_search", "mcp", "file_search", "code_interpreter"):
            self.assertFalse(allowed_request({**self.body, "tools": [{"type": kind}]}, "fixture-model"))

    def test_nullable_tools_and_invalid_tool_types(self):
        self.assertTrue(allowed_request({**self.body, "tools": None}, "fixture-model"))
        for value in (False, 0, "function", {}):
            self.assertFalse(allowed_request({**self.body, "tools": value}, "fixture-model"))

    def test_function_schema_nullable_type_is_not_a_media_reference(self):
        body = {**self.body, "tools": [{"type": "function", "name": "read_file", "parameters": {
            "type": "object", "properties": {"line_range": {"type": ["array", "null"]}}}}]}
        self.assertTrue(allowed_request(body, "fixture-model"))

    def test_remote_media_is_rejected(self):
        for kind in ("input_image", "input_file", "input_audio"):
            body = copy.deepcopy(self.body)
            body["input"] = [{"role": "user", "content": [{"type": kind, "url": "https://example.invalid"}]}]
            self.assertFalse(allowed_request(body, "fixture-model"))

    def test_body_must_be_object(self):
        for body in (None, [], "text"):
            self.assertFalse(allowed_request(body, "fixture-model"))


if __name__ == "__main__":
    unittest.main()
