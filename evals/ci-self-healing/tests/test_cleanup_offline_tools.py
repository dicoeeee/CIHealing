"""Opt-in real Maven/Javac test. Requires an explicitly prepared offline cache.

CI_CLEANUP_TEST_CACHE, CI_CLEANUP_TEST_JAVA_HOME select read-only inputs.
No model, cache download, shared cache modification or existing trial cleanup.
"""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cases import materialize
from common import read_json, snapshot
from native_repair import probe_build
from resource_cleanup import validate_cleanup


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("CI_CLEANUP_TEST_CACHE")
                     and os.environ.get("CI_CLEANUP_TEST_JAVA_HOME"), "explicit offline toolchain/cache required")
class OfflineCleanupTests(unittest.TestCase):
    def test_real_maven_javac_clean_and_host_reclamation_preserve_inputs(self):
        cache = Path(os.environ["CI_CLEANUP_TEST_CACHE"]).resolve()
        config = {"python": sys.executable, "maven": str(Path(shutil.which("mvn")).resolve()),
                  "java_home": str(Path(os.environ["CI_CLEANUP_TEST_JAVA_HOME"]).resolve()),
                  "maven_repository": str(cache)}
        original_cache = snapshot(cache, skip_git=False)
        with tempfile.TemporaryDirectory(prefix="real-cleanup-tools-") as temp:
            root = Path(temp).resolve()
            materialize(root / "visible", "case-001", "offline-cleanup-test", backend="native")
            source = root / "visible/scene/sourcecode"
            original = snapshot(source, skip_git=False)
            baseline = probe_build(config, source, root / "baseline")
            self.assertEqual(baseline["status"], "passed", baseline)
            self.assertNotEqual(baseline["checks"]["maven"]["returncode"], 0)
            self.assertEqual(snapshot(source, skip_git=False), original)
            feature = source / "src/main/java/example/Feature.java"
            feature.write_text(feature.read_text().replace("route(payload, condition, slot)", "route(payload)"))
            candidate_before = snapshot(source, skip_git=False)
            candidate = probe_build(config, source, root / "candidate", "case-001")
            self.assertEqual(candidate["status"], "passed", candidate)
            self.assertTrue(all(v["returncode"] == 0 for v in candidate["checks"].values()))
            self.assertEqual(snapshot(source, skip_git=False), candidate_before)
            self.assertEqual(snapshot(cache, skip_git=False), original_cache)
            for name, result in (("baseline", baseline), ("candidate", candidate)):
                self.assertEqual(result["native_clean"]["status"], "completed", result)
                self.assertEqual(validate_cleanup(result["resource_cleanup"]), "cleaned")
                self.assertFalse((root / (name + "-workspace")).exists())
                self.assertEqual(read_json(result["resource_cleanup"]["receipt"])["remaining"], [])
                self.assertTrue((root / (name + "-evidence/source/pom.xml")).exists())
                self.assertTrue((root / (name + "-clean/stdout.log")).exists())
            self.assertEqual(list(root.rglob("*.class")), [])
            self.assertTrue((source / "target/preexisting.txt").exists())


if __name__ == "__main__":
    unittest.main()
