"""Synthetic manifest contract and read-only CLI checks; no real repo edits."""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "file_change_check.py"
SPEC = importlib.util.spec_from_file_location("file_change_check", SCRIPT)
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)

ENTRY = {"file": "src/main/Example.java", "op": "modify",
         "repo": "example-repo", "desc": "补充导入，保持接口行为"}
BASE = {"version": "0.1", "changes": [ENTRY]}


def encode(document):
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


class FileChangeCheckTests(unittest.TestCase):
    def codes(self, document):
        return {error["code"] for error in check.validate(encode(document))}

    def cli(self, path):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--manifest", str(path)],
            text=True, capture_output=True, timeout=10,
        )
        return result.returncode, json.loads(result.stdout)

    def test_empty_changes_is_valid(self):
        self.assertEqual(check.validate(b'{"version":"0.1","changes":[]}'), [])

    def test_multi_repo_and_all_operations(self):
        items = [
            ENTRY,
            {**ENTRY, "file": "new.txt", "op": "create"},
            {**ENTRY, "file": "old.txt", "op": "delete"},
            {**ENTRY, "repo": "second-repo"},
        ]
        self.assertEqual(self.codes({"version": "0.1", "changes": items}), set())

    def test_rename_is_delete_and_create(self):
        items = [{**ENTRY, "file": "src/Old.java", "op": "delete"},
                 {**ENTRY, "file": "src/New.java", "op": "create"}]
        self.assertEqual(self.codes({"version": "0.1", "changes": items}), set())

    def test_json_escapes_and_unicode_names(self):
        item = {**ENTRY, "file": 'src/示例 file "quoted".java',
                "repo": "示例仓库", "desc": '修复 "参数" 传递\n保留旧接口'}
        self.assertEqual(self.codes({"version": "0.1", "changes": [item]}), set())

    def test_actual_subdirectory_can_share_repo_name(self):
        # Prefix ownership requires the actual Git root, not a string heuristic.
        item = {**ENTRY, "file": "example-repo/src/Foo.java"}
        self.assertEqual(self.codes({"version": "0.1", "changes": [item]}), set())

    def test_posix_colons_are_not_guessed_as_drive_prefixes(self):
        # These are legal POSIX names; actual Git-root ownership is a separate check.
        for path in ("a:b.java", "C:file", "C:/src/file"):
            with self.subTest(path=path):
                item = {**ENTRY, "file": path}
                self.assertEqual(self.codes({"version": "0.1", "changes": [item]}), set())

    def test_fixed_version_is_a_string(self):
        for value in (None, 0.1, 1, True, "0.2", "", {}, []):
            with self.subTest(value=value):
                self.assertIn("version", self.codes({**BASE, "version": value}))

    def test_top_level_and_array_shape(self):
        for value in (None, [], "", 1, True):
            with self.subTest(value=value):
                self.assertIn("object_required", self.codes(value))
        for value in (None, {}, "", 1, True):
            with self.subTest(value=value):
                self.assertIn("changes_array", self.codes({**BASE, "changes": value}))

    def test_required_fields_and_no_alias_or_extra_keys(self):
        for key in BASE:
            document = copy.deepcopy(BASE)
            del document[key]
            self.assertIn("fields", self.codes(document))
        self.assertIn("fields", self.codes({"version": "0.1", "changens": []}))
        self.assertIn("fields", self.codes({**BASE, "status": "passed"}))
        for key in ENTRY:
            item = dict(ENTRY)
            del item[key]
            self.assertIn("entry_fields", self.codes({**BASE, "changes": [item]}))
        self.assertIn("entry_fields", self.codes(
            {**BASE, "changes": [{**ENTRY, "baseline": "abc"}]}))

    def test_entries_are_objects(self):
        for value in (None, [], "", 1, True):
            with self.subTest(value=value):
                self.assertIn("entry_object", self.codes({**BASE, "changes": [value]}))

    def test_invalid_paths(self):
        paths = (None, 1, [], {}, "", " ", "/src/Foo.java", "//host/file",
                 "../file", "src/../file", "./file", "src/./file",
                 "src//file", "src/", ".", "..",
                 r"src\Foo.java", "src/\0bad")
        for value in paths:
            with self.subTest(path=value):
                document = {**BASE, "changes": [{**ENTRY, "file": value}]}
                self.assertIn("file_path", self.codes(document))

    def test_invalid_operations(self):
        for value in ("rename", "copy", "update", "MODIFY", "", None, [], {}, 1):
            with self.subTest(op=value):
                self.assertIn("operation", self.codes(
                    {**BASE, "changes": [{**ENTRY, "op": value}]}))

    def test_repository_name_and_description_required(self):
        for value in (None, "", " ", {}, 1, "/repo", "owner/repo", r"owner\repo",
                      "repo ", ".", ".."):
            with self.subTest(repo=value):
                self.assertIn("repository", self.codes(
                    {**BASE, "changes": [{**ENTRY, "repo": value}]}))
        for value in (None, "", " \n", {}, [], 1, "bad\0text"):
            with self.subTest(desc=value):
                self.assertIn("description", self.codes(
                    {**BASE, "changes": [{**ENTRY, "desc": value}]}))

    def test_duplicate_file_even_with_conflicting_operations(self):
        for second in (ENTRY, {**ENTRY, "op": "delete"}):
            self.assertIn("duplicate_file", self.codes(
                {**BASE, "changes": [ENTRY, second]}))

    def test_malformed_and_ambiguous_json(self):
        inputs = [
            b'', b'{"version":"0.1","changes":[],}', b'\xff',
            b'{"version":"0.1","changes":[],"changes":[]}',
            b'{"version":"0.1","changes":[{"file":"a","file":"b"}]}',
            b'{"version":"0.1","changes":[],"extra":NaN}',
            b'{"version":"0.1","changes":[],"extra":Infinity}',
            b'[' * 2000 + b']' * 2000,
        ]
        for data in inputs:
            with self.subTest(data=data[:60]):
                self.assertEqual(check.validate(data)[0]["code"], "invalid_json")

    def test_escaped_unpaired_surrogate_is_invalid_text(self):
        data = json.dumps({**BASE, "changes": [{**ENTRY, "desc": "\ud800"}]}).encode()
        self.assertIn("description", {e["code"] for e in check.validate(data)})

    def test_cli_valid_empty_and_multi_repo_leave_file_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file_change.json"
            for document in ({"version": "0.1", "changes": []},
                             {**BASE, "changes": [ENTRY, {**ENTRY, "repo": "other"}]}):
                data = encode(document)
                path.write_bytes(data)
                before = path.stat()
                code, result = self.cli(path)
                self.assertEqual((code, result["format_status"]), (0, "valid"))
                self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
                self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_cli_invalid_and_wrong_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file_change.json"
            path.write_text('{"version":"0.1","changens":[]}')
            code, result = self.cli(path)
            self.assertEqual((code, result["format_status"]), (1, "invalid"))
            other = path.with_name("other.json")
            other.write_bytes(encode(BASE))
            code, result = self.cli(other)
            self.assertEqual(code, 1)
            self.assertIn("filename", {e["code"] for e in result["errors"]})

    def test_missing_directory_symlink_and_fifo_are_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "file_change.json"
            self.assertEqual(self.cli(path)[0], 2)
            path.mkdir()
            self.assertEqual(self.cli(path)[0], 2)
            path.rmdir()
            target = root / "target.json"
            target.write_bytes(encode(BASE))
            path.symlink_to(target)
            self.assertEqual(self.cli(path)[0], 2)
            self.assertEqual(target.read_bytes(), encode(BASE))
            path.unlink()
            if hasattr(os, "mkfifo"):
                os.mkfifo(path)
                code, result = self.cli(path)
                self.assertEqual((code, result["format_status"]), (2, "incomplete"))

    def test_size_limit_does_not_validate_partial_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file_change.json"
            path.write_bytes(b" " * (check.MAX_BYTES + 1))
            code, result = self.cli(path)
            self.assertEqual((code, result["format_status"]), (2, "incomplete"))
            self.assertNotIn("sha256", result)

    def test_changes_during_read_are_not_reported_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file_change.json"
            path.write_bytes(encode(BASE))
            before = path.stat()
            values = {name: getattr(before, name) for name in
                      ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")}
            values["st_mtime_ns"] += 1
            with patch.object(check.os, "fstat", side_effect=[before, SimpleNamespace(**values)]):
                with self.assertRaises(check.CheckUnavailable):
                    check.read_bytes(path)

    def test_path_replacement_after_open_is_not_reported_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file_change.json"
            path.write_bytes(encode(BASE))
            before = path.stat()
            values = {name: getattr(before, name) for name in
                      ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")}
            values["st_ino"] += 1
            with patch.object(Path, "lstat", return_value=SimpleNamespace(**values)):
                with self.assertRaises(check.CheckUnavailable):
                    check.read_bytes(path)


if __name__ == "__main__":
    unittest.main()
