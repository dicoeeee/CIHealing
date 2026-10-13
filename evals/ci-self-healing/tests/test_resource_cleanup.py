"""Real isolated filesystem cleanup and failure injection; no historical data."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import EvalError, read_json, snapshot, write_json, write_new
from resource_cleanup import OwnedResources, cleanup_complete, validate_cleanup


class ResourceCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="owned-cleanup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        write_new(self.root / "evidence/result.txt", "keep")
        write_new(self.root / "original/target/preexisting.txt", "job owned")
        self.protected = snapshot(self.root / "original")

    def scope(self):
        return OwnedResources(self.root, "build", evidence=("evidence", "original"))

    def test_all_owned_products_removed_and_existing_evidence_preserved(self):
        with self.scope() as scope:
            for name in ("build-copy", "runtime/tmp", "runtime/m2"):
                root = scope.mkdir(name)
                write_new(root / ".hidden/arbitrary.ext", "temp")
                write_new(root / "target/classes/X.class", b"compiled")
                (root / "empty").mkdir()
            binding = scope.finish()
            self.assertEqual(binding["status"], "cleaned")
            self.assertEqual(validate_cleanup(binding), "cleaned")
            self.assertEqual(read_json(binding["receipt"])["remaining"], [])
            for name in ("build-copy", "runtime/tmp", "runtime/m2"):
                self.assertFalse((self.root / name).exists())
        self.assertEqual(snapshot(self.root / "original"), self.protected)
        self.assertEqual((self.root / "evidence/result.txt").read_text(), "keep")

    def test_preexisting_directory_cannot_be_adopted_or_deleted(self):
        with self.scope() as scope:
            with self.assertRaises(FileExistsError):
                scope.mkdir("original")
            self.assertEqual(scope.finish()["status"], "not_applicable")
        self.assertEqual(snapshot(self.root / "original"), self.protected)

    def test_escaping_and_overlapping_paths_rejected(self):
        with self.scope() as scope:
            scope.mkdir("scratch")
            for path in ("../original", "/tmp/unowned", "scratch/child", "resource-cleanup"):
                with self.subTest(path=path), self.assertRaises(EvalError):
                    scope.mkdir(path)

    def test_child_symlinks_unlinked_without_following_targets(self):
        with self.scope() as scope:
            root = scope.mkdir("scratch")
            (root / "external").symlink_to(self.root / "original", target_is_directory=True)
            (root / "broken").symlink_to(self.root / "missing")
            self.assertEqual(scope.finish()["status"], "cleaned")
        self.assertEqual(snapshot(self.root / "original"), self.protected)

    def test_replaced_root_is_retained_and_incomplete(self):
        with self.scope() as scope:
            root = scope.mkdir("scratch")
            root.rename(self.root / "displaced")
            root.mkdir()
            write_new(root / "other-owner.txt", "preserve")
            binding = scope.finish()
        self.assertEqual(binding["status"], "incomplete")
        self.assertTrue((root / "other-owner.txt").exists())
        self.assertTrue((self.root / "displaced").exists())

    def test_root_replaced_by_link_never_deletes_target(self):
        with self.scope() as scope:
            root = scope.mkdir("scratch")
            root.rmdir()
            root.symlink_to(self.root / "original", target_is_directory=True)
            self.assertEqual(scope.finish()["status"], "incomplete")
        self.assertEqual(snapshot(self.root / "original"), self.protected)

    def test_missing_or_unconfirmed_writer_receipt_blocks_deletion(self):
        for status in (None, "unconfirmed"):
            stage = "missing" if status is None else status
            with OwnedResources(self.root, stage) as scope:
                root = scope.mkdir(stage + "-scratch")
                receipt = self.root / stage / "process.json"
                scope.watch(receipt)
                if status:
                    write_json(receipt, {"cleanup": {"status": status}})
                self.assertEqual(scope.finish()["status"], "incomplete")
                self.assertTrue(root.exists())

    def test_failed_and_timed_out_processes_clean_after_confirmed_exit(self):
        for status in ("completed", "timeout", "interrupted", "execution_error"):
            with OwnedResources(self.root, status) as scope:
                root = scope.mkdir(status + "-scratch")
                receipt = self.root / status / "process.json"
                write_json(receipt, {"status": status, "returncode": 1, "cleanup": {"status": "confirmed"}})
                scope.watch(receipt)
                self.assertEqual(scope.finish()["status"], "cleaned")
                self.assertFalse(root.exists())

    def test_initialization_exception_still_cleans_created_resources(self):
        scope = self.scope()
        with self.assertRaisesRegex(ValueError, "original failure"):
            with scope:
                scope.mkdir("scratch")
                raise ValueError("original failure")
        self.assertEqual(scope.binding["status"], "cleaned")

    def test_evidence_publication_failure_preserves_scratch(self):
        with self.scope() as scope:
            root = scope.mkdir("scratch")
            with mock.patch("resource_cleanup.write_json", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    scope.finish()
            self.assertTrue(root.exists())

    def test_delete_failure_is_incomplete_and_does_not_touch_originals(self):
        with self.scope() as scope:
            root = scope.mkdir("scratch")
            write_new(root / "x", "temp")
            with mock.patch("resource_cleanup.os.unlink", side_effect=PermissionError("denied")):
                binding = scope.finish()
            self.assertEqual(binding["status"], "incomplete")
            self.assertFalse(cleanup_complete({"resource_cleanup": binding}))
        self.assertEqual(snapshot(self.root / "original"), self.protected)

    def test_successful_delete_return_does_not_override_residue(self):
        with self.scope() as scope:
            scope.mkdir("scratch")
            with mock.patch("resource_cleanup.os.rmdir", return_value=None):
                self.assertEqual(scope.finish()["status"], "incomplete")

    def test_retained_evidence_drift_invalidates_receipt(self):
        with self.scope() as scope:
            scope.mkdir("scratch")
            binding = scope.finish()
        (self.root / "evidence/result.txt").write_text("changed")
        with self.assertRaises(EvalError):
            validate_cleanup(binding)

    def test_evidence_overlap_prevents_deletion(self):
        with OwnedResources(self.root, "overlap", evidence=("scratch/log.txt",)) as scope:
            root = scope.mkdir("scratch")
            write_new(root / "log.txt", "necessary evidence")
            self.assertEqual(scope.finish()["status"], "incomplete")
            self.assertTrue((root / "log.txt").exists())

    def test_missing_required_evidence_retains_products(self):
        with self.scope() as scope:
            root = scope.mkdir("scratch")
            scope.required.append("evidence/missing.log")
            self.assertEqual(scope.finish()["status"], "incomplete")
            self.assertTrue(root.exists())

    def test_symlink_parent_cannot_create_or_remove_outside_scope(self):
        with self.scope() as scope:
            (self.root / "alias").symlink_to(self.root / "original", target_is_directory=True)
            with self.assertRaises(OSError):
                scope.mkdir("alias/never-created/tmp")
            self.assertFalse((self.root / "original/never-created").exists())

    def test_budget_exhaustion_does_not_report_complete(self):
        with self.scope() as scope:
            root = scope.mkdir("scratch")
            write_new(root / "file", "temp")
            with mock.patch("resource_cleanup.time.monotonic", side_effect=[0, 100]):
                self.assertEqual(scope.finish()["status"], "incomplete")

    def test_mount_boundary_is_not_traversed(self):
        from resource_cleanup import erase_contents
        root = self.root / "mount-test"
        root.mkdir()
        write_new(root / "keep", "unchanged")
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaisesRegex(EvalError, "mount boundary"):
                erase_contents(fd, root.stat().st_dev + 1, float("inf"), [100])
        finally:
            os.close(fd)
        self.assertTrue((root / "keep").exists())

    def test_reappeared_path_invalidates_receipt_without_redeleting(self):
        with self.scope() as scope:
            root = scope.mkdir("scratch")
            binding = scope.finish()
            self.assertEqual(scope.finish(), binding)
        root.mkdir()
        with self.assertRaises(EvalError):
            validate_cleanup(binding)
        self.assertTrue(root.exists())

    def test_budget_and_special_file_fail_closed(self):
        with self.scope() as scope:
            root = scope.mkdir("scratch")
            os.mkfifo(root / "pipe")
            self.assertEqual(scope.finish()["status"], "incomplete")

    def test_legacy_receipts_do_not_require_new_cleanup_fields(self):
        self.assertTrue(cleanup_complete({"status": "completed"}))


if __name__ == "__main__":
    unittest.main()
