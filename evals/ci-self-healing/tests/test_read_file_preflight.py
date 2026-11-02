"""Capability-based pagination checks retain negative validation probes."""
import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chrys_preflight import check_read_file
from common import EvalError


class SyntheticRead:
    def __init__(self, style, ignore=False, accept_invalid=False):
        self.style, self.ignore, self.accept_invalid = style, ignore, accept_invalid

    def parameters(self):
        keys = ["line_range"] if self.style == "line_range" else ["offset", "limit"]
        return {"properties": {key: {} for key in keys}}

    async def invoke(self, *, arguments, skip_parsing):
        lines = Path(arguments["path"]).read_text().splitlines()
        if self.style == "line_range":
            value = arguments.get("line_range", [1, -1])
            valid = isinstance(value, list)
            start, end = value if valid else (1, -1)
        else:
            start, length = arguments.get("offset", 1), arguments.get("limit", 3)
            valid = type(start) is int and type(length) is int and start > 0 and length > 0
            end = start + length - 1 if valid else -1
        if not valid:
            if self.accept_invalid:
                return "ignored invalid arguments"
            raise TypeError("Invalid arguments for 'read_file'")
        return "\n".join(lines if self.ignore else lines[start - 1:None if end == -1 else end])


class ReadFilePreflightTests(unittest.TestCase):
    def test_both_native_pagination_shapes(self):
        for style in ("line_range", "offset_limit"):
            with self.subTest(style=style):
                result = asyncio.run(check_read_file(SyntheticRead(style)))
                self.assertEqual(result["pagination"], style)
                self.assertTrue(result["invalid_pagination_rejected"])

    def test_ignored_pagination_cannot_pass(self):
        for style in ("line_range", "offset_limit"):
            with self.subTest(style=style), self.assertRaisesRegex(EvalError, "ignored pagination"):
                asyncio.run(check_read_file(SyntheticRead(style, ignore=True)))

    def test_invalid_arguments_must_still_be_rejected(self):
        for style in ("line_range", "offset_limit"):
            with self.subTest(style=style), self.assertRaisesRegex(EvalError, "accepted invalid"):
                asyncio.run(check_read_file(SyntheticRead(style, accept_invalid=True)))


if __name__ == "__main__":
    unittest.main()
