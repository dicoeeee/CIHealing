"""Real temporary-workspace and CLI checks; never clean a user's workspace."""

import contextlib
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "workspace_check.py"
SPEC = importlib.util.spec_from_file_location("workspace_check", SCRIPT)
workspace = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workspace)


@unittest.skipUnless(os.name == "posix", "POSIX no-follow implementation")
class WorkspaceCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ci-workspace-check-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "repo with spaces"
        self.root.mkdir()
        (self.root / "src").mkdir()
        (self.root / "src" / "Main.java").write_text("candidate repair\n")
        (self.root / ".gitignore").write_text("target/\n")
        (self.root / "original-untracked.txt").write_text("job-owned input\n")
        self.manifest = self.base / "before.json"
        self.capture_result = None

    def args(self, operation, roots=None, manifest=None, **options):
        values = [operation, "--manifest", str(manifest or self.manifest),
                  "--invocation-id", "invocation-1", "--candidate-id", "candidate-1"]
        for root in roots or [self.root]:
            values += ["--root", str(root)]
        if operation == "check":
            values += ["--manifest-sha256", self.capture_result["manifest_sha256"]]
        for key, value in options.items():
            values += ["--" + key.replace("_", "-"), str(value)]
        return values

    def run_tool(self, operation, **kwargs):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = workspace.main(self.args(operation, **kwargs))
        return code, json.loads(stream.getvalue())

    def capture(self, **kwargs):
        code, result = self.run_tool("capture", **kwargs)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["result"], "captured")
        self.capture_result = result
        return result

    def gaps(self, result):
        return {gap["code"] for gap in result["gaps"]}

    def fact_tree(self):
        result = {}
        for path in [self.root] + sorted(self.root.rglob("*")):
            info = path.lstat()
            value = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode))
            if path.is_symlink():
                value += (os.readlink(path),)
            elif path.is_file():
                value += (path.read_bytes(),)
            result[str(path.relative_to(self.root))] = value
        return result

    def test_capture_and_match_leave_workspace_unchanged(self):
        before = self.fact_tree()
        self.capture()
        code, result = self.run_tool("check")
        self.assertEqual((code, result["result"]), (0, "match"))
        self.assertEqual(result["counts"], {"added": 0, "missing": 0, "changed": 0})
        self.assertEqual(before, self.fact_tree())
        self.assertEqual(stat.S_IMODE(self.manifest.stat().st_mode), 0o600)

    def test_manifest_has_hashes_not_source_contents(self):
        self.capture()
        contents = self.manifest.read_text()
        self.assertNotIn("candidate repair", contents)
        entry = json.loads(contents)["roots"][0]["entries"]["src/Main.java"]
        self.assertEqual(entry["sha256"], hashlib.sha256(b"candidate repair\n").hexdigest())

    def test_new_ignored_and_hidden_artifacts_are_reported_not_deleted(self):
        self.capture()
        (self.root / "target").mkdir()
        (self.root / "target" / "Main.class").write_bytes(b"compiled")
        (self.root / ".build-cache").write_text("cache")
        before = self.fact_tree()
        code, result = self.run_tool("check")
        self.assertEqual((code, result["result"]), (1, "mismatch"))
        self.assertEqual(result["counts"]["added"], 3)
        self.assertEqual(before, self.fact_tree())

    def test_accidental_deletion_and_changed_original_are_both_reported(self):
        self.capture()
        (self.root / "src" / "Main.java").unlink()
        (self.root / "original-untracked.txt").write_text("unexpected")
        code, result = self.run_tool("check")
        self.assertEqual(code, 1)
        self.assertEqual(result["counts"], {"added": 0, "missing": 1, "changed": 1})

    def test_restore_to_candidate_matches_without_git_head_or_original_inode(self):
        self.capture()
        source = self.root / "src" / "Main.java"
        original_mode = stat.S_IMODE(source.stat().st_mode)
        source.unlink()
        source.write_text("candidate repair\n")
        source.chmod(original_mode)
        os.utime(source, (1, 1))
        self.assertEqual(self.run_tool("check")[0], 0)

    def test_content_hash_detects_same_size_edit_with_original_mtime(self):
        self.capture()
        source = self.root / "src" / "Main.java"
        old = source.stat()
        source.write_text("different repair\n")
        self.assertEqual(source.stat().st_size, old.st_size)
        os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertEqual(self.run_tool("check")[0], 1)

    def test_permission_and_file_type_changes(self):
        self.capture()
        source = self.root / "src" / "Main.java"
        source.chmod(0o700)
        original = self.root / "original-untracked.txt"
        original.unlink()
        original.mkdir()
        code, result = self.run_tool("check")
        self.assertEqual(code, 1)
        self.assertEqual(result["counts"]["changed"], 2)

    def test_symlink_target_checked_without_following_outside_file(self):
        outside = self.base / "secret.txt"
        outside.write_text("outside content")
        link = self.root / "link"
        link.symlink_to(outside)
        self.capture()
        outside.write_text("not part of scope")
        self.assertEqual(self.run_tool("check")[0], 0)
        link.unlink()
        link.symlink_to(self.base / "missing")
        self.assertEqual(self.run_tool("check")[0], 1)
        self.assertEqual(outside.read_text(), "not part of scope")

    def test_directory_replaced_by_symlink_does_not_traverse_external_tree(self):
        self.capture()
        source = self.root / "src"
        (source / "Main.java").unlink()
        source.rmdir()
        source.symlink_to(self.base, target_is_directory=True)
        code, result = self.run_tool("check")
        self.assertEqual(code, 1)
        self.assertEqual(result["counts"]["missing"], 1)

    def test_preexisting_build_products_are_not_declared_garbage(self):
        (self.root / "target").mkdir()
        product = self.root / "target" / "old.class"
        product.write_bytes(b"old")
        self.capture()
        self.assertEqual(self.run_tool("check")[1]["result"], "match")
        product.unlink()
        self.assertEqual(self.run_tool("check")[1]["counts"]["missing"], 1)

    def test_git_metadata_is_explicitly_excluded_but_gitignore_is_not(self):
        (self.root / ".git").mkdir()
        (self.root / ".git" / "index").write_bytes(b"index")
        self.capture()
        (self.root / ".git" / "index").write_bytes(b"refresh")
        self.assertEqual(self.run_tool("check")[0], 0)
        (self.root / ".gitignore").write_text("*")
        self.assertEqual(self.run_tool("check")[0], 1)

    def test_nested_repository_is_not_silently_excluded(self):
        (self.root / "src" / ".git").write_text("gitdir: elsewhere")
        code, result = self.run_tool("capture")
        self.assertEqual(code, 2)
        self.assertIn("nested_repository", self.gaps(result))
        self.assertFalse(self.manifest.exists())

    def test_special_file_fails_without_blocking_or_reading(self):
        os.mkfifo(self.root / "pipe")
        code, result = self.run_tool("capture")
        self.assertEqual(code, 2)
        self.assertIn("unsupported_entry", self.gaps(result))

    def test_multi_repository_capture_requires_same_complete_roots(self):
        other = self.base / "repo-b"
        other.mkdir()
        (other / "a.c").write_text("int main() {}")
        self.capture(roots=[self.root, other])
        self.assertEqual(self.run_tool("check", roots=[other, self.root])[0], 0)
        self.assertIn("scope_mismatch", self.gaps(self.run_tool("check")[1]))
        (other / "new.o").write_bytes(b"obj")
        self.assertEqual(self.run_tool("check", roots=[self.root, other])[0], 1)

    def test_overlapping_or_duplicate_roots_rejected(self):
        for roots in ([self.root, self.root], [self.root, self.root / "src"]):
            with self.subTest(roots=roots):
                code, result = self.run_tool("capture", roots=roots)
                self.assertEqual(code, 2)
                self.assertIn("overlapping_roots", self.gaps(result))

    def test_replaced_root_is_not_treated_as_same_workspace(self):
        self.capture()
        self.root.rename(self.base / "old-root")
        self.root.mkdir()
        code, result = self.run_tool("check")
        self.assertEqual((code, result["result"]), (2, "incomplete"))
        self.assertIn("root_identity_changed", self.gaps(result))

    def test_baseline_cannot_be_overwritten_or_rehashed_to_silently_accept_changes(self):
        self.capture()
        original = self.manifest.read_bytes()
        self.assertEqual(self.run_tool("capture")[0], 2)
        self.assertEqual(self.manifest.read_bytes(), original)
        manifest = json.loads(original)
        manifest["roots"][0]["entries"]["src/Main.java"]["sha256"] = "0" * 64
        self.manifest.write_text(json.dumps(manifest))
        code, result = self.run_tool("check")
        self.assertEqual(code, 2)
        self.assertIn("manifest_hash_mismatch", self.gaps(result))

    def test_binding_mismatch(self):
        self.capture()
        for options in ({"candidate_id": "candidate-2"}, {"invocation_id": "another-call"}):
            code, result = self.run_tool("check", **options)
            self.assertEqual(code, 2)
            self.assertIn("binding_mismatch", self.gaps(result))

    def test_missing_or_truncated_manifest_is_incomplete(self):
        self.capture()
        self.manifest.write_text('{"schema":')
        self.assertEqual(self.run_tool("check")[1]["result"], "incomplete")
        self.manifest.unlink()
        self.assertEqual(self.run_tool("check")[0], 2)

    def test_structurally_invalid_manifests_fail_closed_even_with_matching_digest(self):
        self.capture()
        original = json.loads(self.manifest.read_text())
        for bad_entries in ({".": []}, {".": {"kind": "directory", "mode": 0o755}, "../escape": {"kind": "directory", "mode": 0o755}},
                            {".": {"kind": "directory", "mode": 0o755}, "missing/child": {"kind": "directory", "mode": 0o755}}):
            changed = json.loads(json.dumps(original))
            changed["roots"][0]["entries"] = bad_entries
            self.manifest.write_text(json.dumps(changed))
            code, result = self.run_tool("check", manifest_sha256=workspace.digest(changed))
            self.assertEqual(code, 2)
            self.assertIn("invalid_manifest", self.gaps(result))

    def test_duplicate_manifest_keys_are_rejected(self):
        self.capture()
        self.manifest.write_text('{"schema":"a","schema":"b"}')
        self.assertIn("invalid_manifest", self.gaps(self.run_tool("check")[1]))

    def test_state_files_inside_root_or_via_parent_alias_are_rejected(self):
        code, result = self.run_tool("capture", manifest=self.root / "before.json")
        self.assertEqual(code, 2)
        self.assertIn("state_inside_workspace", self.gaps(result))
        alias = self.base / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.run_tool("capture", manifest=alias / "before.json")[0], 2)

    def test_existing_manifest_symlink_does_not_overwrite_target(self):
        target = self.base / "existing"
        target.write_text("preserve")
        self.manifest.symlink_to(target)
        self.assertEqual(self.run_tool("capture")[0], 2)
        self.assertEqual(target.read_text(), "preserve")

    def test_output_creation_and_no_overwrite_or_source_alias(self):
        self.capture()
        output = self.base / "result.json"
        code, result = self.run_tool("check", output=output)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.read_text()), result)
        original = output.read_bytes()
        self.assertEqual(self.run_tool("check", output=output)[0], 2)
        self.assertEqual(output.read_bytes(), original)
        alias = self.base / "output-alias"
        os.link(self.root / "src" / "Main.java", alias)
        self.assertEqual(self.run_tool("check", output=alias)[0], 2)
        self.assertEqual((self.root / "src" / "Main.java").read_text(), "candidate repair\n")

    def test_entry_and_byte_budgets_never_produce_partial_baseline(self):
        for options in ({"max_entries": 1}, {"max_bytes": 1}):
            code, result = self.run_tool("capture", **options)
            self.assertEqual((code, result["result"]), (2, "incomplete"))
            self.assertFalse(self.manifest.exists())

    def test_check_budget_failure_is_not_match(self):
        self.capture()
        self.assertEqual(self.run_tool("check", max_bytes=1)[1]["result"], "incomplete")

    def test_difference_output_limit_does_not_hide_mismatch_or_counts(self):
        self.capture()
        for index in range(10):
            (self.root / f"artifact-{index}").write_text("generated")
        code, result = self.run_tool("check", max_differences=2)
        self.assertEqual(code, 1)
        self.assertEqual(result["counts"]["added"], 10)
        self.assertEqual(len(result["differences"]), 2)
        self.assertTrue(result["differences_truncated"])

    def test_permission_failure_is_incomplete_not_missing(self):
        self.capture()
        with mock.patch.object(workspace.Scanner, "file_hash", side_effect=PermissionError("denied")):
            code, result = self.run_tool("check")
        self.assertEqual((code, result["result"]), (2, "incomplete"))

    def test_file_changed_during_hashing_is_incomplete(self):
        real_read = workspace.os.read
        did_change = []
        source = self.root / "original-untracked.txt"

        def read_and_mutate(fd, count):
            block = real_read(fd, count)
            if block == b"job-owned input\n" and not did_change:
                did_change.append(True)
                source.write_bytes(b"job-owned INPUT\n")
            return block

        with mock.patch.object(workspace.os, "read", read_and_mutate):
            code, result = self.run_tool("capture")
        self.assertEqual(code, 2)
        self.assertIn("source_changed", self.gaps(result))

    def test_manifest_write_failure_keeps_workspace_unchanged_and_returns_incomplete(self):
        before = self.fact_tree()
        with mock.patch.object(workspace, "write_new", side_effect=OSError("disk full")):
            code, result = self.run_tool("capture")
        self.assertEqual((code, result["result"]), (2, "incomplete"))
        self.assertEqual(before, self.fact_tree())

    def test_interrupt_during_scan_does_not_create_baseline(self):
        with mock.patch.object(workspace.Scanner, "scan", side_effect=KeyboardInterrupt):
            code, result = self.run_tool("capture")
        self.assertEqual(code, 2)
        self.assertIn("interrupted", self.gaps(result))
        self.assertFalse(self.manifest.exists())

    def test_unicode_and_newline_names_roundtrip(self):
        filename = self.root / "含 空格\n文件.txt"
        filename.write_text("body")
        self.capture()
        self.assertEqual(self.run_tool("check")[0], 0)
        filename.write_text("new body")
        code, result = self.run_tool("check")
        self.assertEqual(code, 1)
        self.assertEqual(result["differences"][0]["path"], filename.name)

    def test_changes_between_scan_passes_are_incomplete(self):
        scan = workspace.Scanner.scan
        calls = []

        def mutate(scanner, roots):
            result = scan(scanner, roots)
            calls.append(1)
            if len(calls) == 1:
                (self.root / "src" / "Main.java").write_text("changed during observation")
            return result

        with mock.patch.object(workspace.Scanner, "scan", mutate):
            code, result = self.run_tool("capture")
        self.assertEqual(code, 2)
        self.assertIn("source_changed", self.gaps(result))

    def test_markdown_access_false_or_ambiguous_is_not_hashed(self):
        note = self.root / "private.md"
        for header in ("ai_access: false", "'ai_access': false", "ai_access: *unknown", "{ai_access: false}"):
            note.write_text(f"---\n{header}\n---\nDO NOT HASH BODY\n")
            code, result = self.run_tool("capture")
            self.assertEqual(code, 2)
            self.assertIn("markdown_access_restricted", self.gaps(result))
            self.assertFalse(self.manifest.exists())

    def test_markdown_allowed_header_and_plain_text_are_supported(self):
        (self.root / "readme.md").write_text("---\nai_access: true\n---\nbody\n")
        (self.root / "plain.md").write_text("# Ordinary content\n")
        self.capture()
        self.assertEqual(self.run_tool("check")[0], 0)

    def test_real_cli_roundtrip_exit_codes_and_stdout_json(self):
        process = subprocess.run([sys.executable, "-B", str(SCRIPT), *self.args("capture")],
                                 capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.capture_result = json.loads(process.stdout)
        (self.root / "new.bin").write_bytes(b"artifact")
        process = subprocess.run([sys.executable, "-B", str(SCRIPT), *self.args("check")],
                                 capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertEqual(json.loads(process.stdout)["result"], "mismatch")

    def test_match_and_mismatch_reports_equal_stdout_and_keep_source(self):
        self.capture()
        for index, expected in enumerate(("match", "mismatch")):
            if index:
                (self.root / "artifact.bin").write_bytes(b"new")
            before = self.fact_tree()
            output = self.base / f"result-{index}.json"
            code, result = self.run_tool("check", output=output)
            self.assertEqual(code, index)
            self.assertEqual(result["result"], expected)
            self.assertEqual(json.loads(output.read_text()), result)
            self.assertEqual(before, self.fact_tree())
            self.assertEqual(list(self.base.glob(".workspace-check-*.tmp")), [])

    def test_fsync_failure_after_report_bytes_written_never_publishes_match(self):
        self.capture()
        before, baseline = self.fact_tree(), self.manifest.read_bytes()
        output = self.base / "result.json"
        observed = []

        def failed_fsync(fd):
            temporary = list(self.base.glob(".workspace-check-*.tmp"))
            self.assertEqual(len(temporary), 1)
            observed.append(json.loads(temporary[0].read_text())["result"])
            raise OSError(errno.EIO, "injected fsync failure after write")

        with mock.patch.object(workspace.os, "fsync", failed_fsync):
            code, result = self.run_tool("check", output=output)
        self.assertEqual(observed, ["match"])
        self.assertEqual((code, result["result"]), (2, "incomplete"))
        self.assertFalse(output.exists())
        self.assertEqual(result["gaps"][0]["phase"], "report_write")
        self.assertEqual(result["gaps"][0]["path"], str(output))
        self.assertEqual(result["gaps"][0]["errno"], errno.EIO)
        self.assertEqual(self.fact_tree(), before)
        self.assertEqual(self.manifest.read_bytes(), baseline)
        self.assertEqual(list(self.base.glob(".workspace-check-*.tmp")), [])

    def test_capture_fsync_failure_does_not_publish_baseline(self):
        before = self.fact_tree()
        with mock.patch.object(workspace.os, "fsync", side_effect=OSError(errno.EIO, "sync failed")):
            code, result = self.run_tool("capture")
        self.assertEqual(code, 2)
        self.assertEqual(result["gaps"][0]["phase"], "manifest_write")
        self.assertFalse(self.manifest.exists())
        self.assertEqual(self.fact_tree(), before)

    def test_publish_collision_preserves_rival_file(self):
        self.capture()
        before = self.fact_tree()
        output = self.base / "rival.json"
        real_link = workspace.os.link

        def race(src, dst, **kwargs):
            output.write_bytes(b"other invocation")
            return real_link(src, dst, **kwargs)

        with mock.patch.object(workspace.os, "link", race):
            code, result = self.run_tool("check", output=output)
        self.assertEqual((code, result["result"]), (2, "incomplete"))
        self.assertEqual(output.read_bytes(), b"other invocation")
        self.assertEqual(result["gaps"][0]["errno"], errno.EEXIST)
        self.assertEqual(list(self.base.glob(".workspace-check-*.tmp")), [])
        self.assertEqual(before, self.fact_tree())

    def test_post_publication_cleanup_failure_is_stderr_warning_only(self):
        self.capture()
        output = self.base / "result.json"
        before = self.fact_tree()
        warnings = io.StringIO()
        with mock.patch.object(workspace.os, "unlink", side_effect=OSError(errno.EACCES, "cleanup failed")), contextlib.redirect_stderr(warnings):
            code, result = self.run_tool("check", output=output)
        self.assertEqual((code, result["result"]), (0, "match"))
        self.assertEqual(json.loads(output.read_text()), result)
        self.assertEqual(result["gaps"], [])
        self.assertEqual(json.loads(warnings.getvalue())["phase"], "report_write_cleanup")
        temporary = list(self.base.glob(".workspace-check-*.tmp"))
        self.assertEqual(len(temporary), 1)
        self.assertEqual(output.read_bytes(), temporary[0].read_bytes())
        self.assertEqual(before, self.fact_tree())

    def test_post_publication_directory_close_failure_keeps_receipt(self):
        self.capture()
        output = self.base / "result.json"
        real_link, real_close = workspace.os.link, workspace.os.close
        published = []

        def link(*args, **kwargs):
            real_link(*args, **kwargs)
            published.append(True)

        def close(fd):
            real_close(fd)
            if published:
                published.pop()
                raise OSError(errno.EIO, "injected close failure")

        warnings = io.StringIO()
        with mock.patch.object(workspace.os, "link", link), mock.patch.object(workspace.os, "close", close), contextlib.redirect_stderr(warnings):
            code, result = self.run_tool("check", output=output)
        self.assertEqual((code, result["result"]), (0, "match"))
        self.assertEqual(json.loads(output.read_text()), result)
        self.assertIn("injected close failure", warnings.getvalue())

    def test_secondary_cleanup_failure_does_not_mask_primary_sync_error(self):
        self.capture()
        output = self.base / "result.json"
        warnings = io.StringIO()
        with mock.patch.object(workspace.os, "fsync", side_effect=OSError(errno.EIO, "primary sync failure")), \
                mock.patch.object(workspace.os, "unlink", side_effect=OSError(errno.EACCES, "secondary cleanup failure")), \
                contextlib.redirect_stderr(warnings):
            code, result = self.run_tool("check", output=output)
        self.assertEqual(code, 2)
        self.assertEqual(result["gaps"][0]["errno"], errno.EIO)
        self.assertIn("primary sync failure", result["gaps"][0]["message"])
        self.assertIn("secondary cleanup failure", warnings.getvalue())
        self.assertFalse(output.exists())

    def test_temporary_name_collision_does_not_remove_existing_state(self):
        self.capture()
        existing = self.base / ".workspace-check-fixed.tmp"
        existing.write_bytes(b"belongs to another call")
        output = self.base / "result.json"
        with mock.patch.object(workspace.secrets, "token_hex", return_value="fixed"):
            code, result = self.run_tool("check", output=output)
        self.assertEqual(code, 2)
        self.assertEqual(existing.read_bytes(), b"belongs to another call")
        self.assertFalse(output.exists())

    def test_unsupported_link_publication_has_no_direct_write_fallback(self):
        self.capture()
        output = self.base / "result.json"
        with mock.patch.object(workspace.os, "link", side_effect=NotImplementedError("no link support")):
            code, result = self.run_tool("check", output=output)
        self.assertEqual(code, 2)
        self.assertEqual(result["gaps"][0]["phase"], "report_write")
        self.assertFalse(output.exists())

    def test_short_writes_complete_before_publication(self):
        self.capture()
        output = self.base / "result.json"
        real_write = workspace.os.write
        with mock.patch.object(workspace.os, "write", lambda fd, data: real_write(fd, data[:17])):
            code, result = self.run_tool("check", output=output)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.read_text()), result)

    def test_markdown_descriptions_and_comments_do_not_become_access_rules(self):
        examples = (
            'description: "Checks ai_access permissions"',
            "description: Checks ai_access permissions",
            "description: 'Checks ai_access permissions'",
            'description: "The text ai_access: false is an example"',
            'description: "Checks # ai_access permissions"',
            'description: "Checks \\"quoted\\" ai_access permissions"',
            "title: safe # ai_access is documented elsewhere",
            "ai_access: true # ai_access is allowed",
        )
        note = self.root / "README.md"
        for index, header in enumerate(examples):
            with self.subTest(header=header):
                body = "---\n" + header + "\n---\nfull body\n"
                note.write_text(body)
                manifest = self.base / f"description-{index}.json"
                self.capture(manifest=manifest)
                entry = json.loads(manifest.read_text())["roots"][0]["entries"]["README.md"]
                self.assertEqual(entry["sha256"], hashlib.sha256(body.encode()).hexdigest())
                self.assertEqual(self.run_tool("check", manifest=manifest)[0], 0)

    def test_markdown_ordinary_text_does_not_hide_later_false_or_ambiguity(self):
        note = self.root / "private.md"
        examples = (
            'description: "ai_access is a field"\nai_access: false # do not read',
            'description: "ai_access is a field"\nai_access: *alias',
            'description: "ai_access is a field"\n{ai_access: false}',
            'description: {ai_access: false}',
            'ai_access: "true"',
            'description: "unterminated ai_access',
        )
        for index, header in enumerate(examples):
            with self.subTest(header=header):
                note.write_text("---\n" + header + "\n---\nprivate body\n")
                manifest = self.base / f"denied-{index}.json"
                code, result = self.run_tool("capture", manifest=manifest)
                self.assertEqual(code, 2)
                self.assertIn("markdown_access_restricted", self.gaps(result))
                self.assertFalse(manifest.exists())

    def test_restricted_markdown_never_reaches_content_hash_reads(self):
        note = self.root / "private.md"
        note.write_text("---\nai_access: false\n---\nprivate body\n")
        parent = workspace.open_directory(str(self.root))
        try:
            with mock.patch.object(workspace.os, "read") as read:
                with self.assertRaises(workspace.CheckError):
                    workspace.Scanner(100, 10000).file_hash(parent, note.name, note.stat(), str(note))
            read.assert_not_called()
        finally:
            os.close(parent)

    def test_fd_read_error_has_actual_source_path_phase_and_errno(self):
        self.capture()
        source = self.root / "src" / "Main.java"
        identity = source.stat().st_ino
        real_read = workspace.os.read

        def read(fd, count):
            if os.fstat(fd).st_ino == identity:
                raise OSError(errno.EIO, "source read failed")
            return real_read(fd, count)

        with mock.patch.object(workspace.os, "read", read):
            code, result = self.run_tool("check")
        gap = result["gaps"][0]
        self.assertEqual((code, result["result"]), (2, "incomplete"))
        self.assertEqual((gap["phase"], gap["path"], gap["errno"]), ("scan", str(source), errno.EIO))

    def test_directory_enumeration_error_is_not_reported_as_missing(self):
        self.capture()
        with mock.patch.object(workspace.os, "listdir", side_effect=OSError(errno.EACCES, "directory denied")) as listing:
            with mock.patch.object(workspace.os, "supports_fd", workspace.os.supports_fd | {listing}):
                code, result = self.run_tool("check")
        self.assertEqual((code, result["result"]), (2, "incomplete"))
        gap = result["gaps"][0]
        self.assertEqual((gap["phase"], gap["path"], gap["errno"]), ("scan", str(self.root), errno.EACCES))
        self.assertNotIn("counts", result)

    def test_manifest_read_error_is_located_and_does_not_create_output(self):
        self.capture()
        self.manifest.unlink()
        output = self.base / "result.json"
        code, result = self.run_tool("check", output=output)
        gap = result["gaps"][0]
        self.assertEqual((code, result["result"]), (2, "incomplete"))
        self.assertEqual((gap["phase"], gap["path"], gap["errno"]), ("manifest_read", str(self.manifest), errno.ENOENT))
        self.assertFalse(output.exists())

    def test_second_repository_read_failure_is_overall_incomplete(self):
        other = self.base / "z-second-repo"
        other.mkdir()
        target = other / "source.c"
        target.write_text("code")
        self.capture(roots=[self.root, other])
        real_read = workspace.os.read
        inode = target.stat().st_ino

        def read(fd, count):
            if os.fstat(fd).st_ino == inode:
                raise OSError(errno.EIO, "second repo read failed")
            return real_read(fd, count)

        with mock.patch.object(workspace.os, "read", read):
            code, result = self.run_tool("check", roots=[self.root, other])
        self.assertEqual((code, result["result"]), (2, "incomplete"))
        self.assertEqual(result["gaps"][0]["path"], str(target))

    def test_empty_and_chunk_boundary_files_hash_change_and_restore(self):
        blob = self.root / "blob"
        for size in (0, workspace.CHUNK - 1, workspace.CHUNK, workspace.CHUNK + 1):
            with self.subTest(size=size):
                original = b"a" * size
                blob.write_bytes(original)
                manifest = self.base / f"size-{size}.json"
                self.capture(manifest=manifest)
                self.assertEqual(self.run_tool("check", manifest=manifest)[0], 0)
                blob.write_bytes((b"b" + original[1:]) if size else b"b")
                code, result = self.run_tool("check", manifest=manifest)
                self.assertEqual(code, 1)
                self.assertEqual(result["counts"]["changed"], 1)
                blob.write_bytes(original)
                self.assertEqual(self.run_tool("check", manifest=manifest)[0], 0)

    def test_exact_byte_budget_matches_and_one_less_is_incomplete(self):
        total = sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())
        self.capture(max_bytes=total)
        self.assertEqual(self.run_tool("check", max_bytes=total)[0], 0)
        code, result = self.run_tool("check", max_bytes=total - 1)
        self.assertEqual(code, 2)
        self.assertIn("byte_limit", self.gaps(result))

    def test_directory_permissions_are_compared(self):
        self.capture()
        source = self.root / "src"
        source.chmod(0o700)
        code, result = self.run_tool("check")
        self.assertEqual(code, 1)
        self.assertEqual([(item["path"], item["change"]) for item in result["differences"]], [("src", "changed")])

    def test_type_changes_in_both_directions_do_not_follow_links(self):
        target = self.root / "switch"
        outside = self.base / "outside.txt"
        outside.write_text("preserved")
        for index, (before, after) in enumerate((("file", "directory"), ("directory", "file"), ("file", "symlink"), ("symlink", "file"))):
            with self.subTest(before=before, after=after):
                def create(kind):
                    if kind == "file":
                        target.write_text("inside")
                    elif kind == "directory":
                        target.mkdir()
                    else:
                        target.symlink_to(outside)
                def remove():
                    target.rmdir() if target.is_dir() and not target.is_symlink() else target.unlink()
                create(before)
                manifest = self.base / f"type-{index}.json"
                self.capture(manifest=manifest)
                remove()
                create(after)
                code, result = self.run_tool("check", manifest=manifest)
                self.assertEqual(code, 1)
                self.assertEqual(result["counts"]["changed"], 1)
                self.assertEqual(result["differences"][0]["path"], "switch")
                self.assertEqual(outside.read_text(), "preserved")
                remove()

    def test_real_cli_match_and_runtime_error_return_json(self):
        self.capture()
        for missing in (False, True):
            if missing:
                self.manifest.unlink()
            process = subprocess.run([sys.executable, "-B", str(SCRIPT), *self.args("check")],
                                     capture_output=True, text=True, timeout=20)
            result = json.loads(process.stdout)
            self.assertEqual(process.returncode, 2 if missing else 0)
            self.assertEqual(result["result"], "incomplete" if missing else "match")
            if missing:
                self.assertEqual(result["gaps"][0]["phase"], "manifest_read")


if __name__ == "__main__":
    unittest.main()
