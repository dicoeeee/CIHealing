"""Java scene/oracle bindings and frozen-catalog portability, without models."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
from harness.catalog import Catalog
from harness.core import EvaluationError
from harness import fixtures


class JavaCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ci-java-catalog-")
        self.addCleanup(self.temporary.cleanup)
        self.spec = Path(self.temporary.name) / "spec"
        self.spec.mkdir()
        for name in ("cases", "criteria", "behaviors", "legacy-case-map"):
            (self.spec / (name + ".json")).write_bytes((ENGINE / "spec" / (name + ".json")).read_bytes())
        self.document = json.loads((self.spec / "cases.json").read_text())
        self.cases = {row["id"]: row for row in self.document["cases"]}

    def load(self):
        (self.spec / "cases.json").write_text(json.dumps(self.document, ensure_ascii=False))
        return Catalog(self.spec, verify_sources=False)

    def test_current_java_cases_have_consistent_targets(self):
        catalog = self.load()
        self.assertEqual(sum(c["fixture"]["kind"] == "java_contract" for c in catalog.cases.values()), 9)

    def test_oracle_cannot_grade_a_different_target_from_the_scene(self):
        for identifier, other in (("repair-contract-preserve", "extend"),
                                  ("repair-contract-extend", "preserve")):
            with self.subTest(identifier=identifier):
                params = self.cases[identifier]["expected"]["behavior_oracle"]["parameters"]
                previous = params["target_contract"]
                params["target_contract"] = other
                with self.assertRaisesRegex(EvaluationError, "differs from fixture"):
                    self.load()
                params["target_contract"] = previous

    def test_ambiguous_scene_cannot_select_a_routing_oracle(self):
        case = self.cases["repair-goal-ambiguous"]
        case["expected"]["behavior_oracle"] = copy.deepcopy(
            self.cases["repair-contract-preserve"]["expected"]["behavior_oracle"])
        case["expected"]["behavior_oracle"]["parameters"]["target_contract"] = "ambiguous"
        with self.assertRaisesRegex(EvaluationError, "determinate Java target"):
            self.load()

    def test_legacy_interface_check_cannot_be_disabled_or_coerced(self):
        params = self.cases["repair-contract-extend"]["expected"]["behavior_oracle"]["parameters"]
        for value in (False, None, "true", 1):
            with self.subTest(value=value):
                params["require_legacy_interface"] = value
                with self.assertRaisesRegex(EvaluationError, "requires legacy compatibility"):
                    self.load()

    def test_revision_cannot_escape_the_java_template_directory(self):
        params = self.cases["repair-contract-preserve"]["fixture"]["parameters"]
        for value in ("../outside", "/tmp/outside", "x/../../outside", "x\\outside", "", None, 1):
            with self.subTest(value=value):
                params["source_revision"] = value
                with self.assertRaisesRegex(EvaluationError, "unsafe Java source_revision"):
                    self.load()

    def test_snapshot_catalog_survives_missing_current_template_but_prepare_cannot(self):
        params = self.cases["repair-contract-preserve"]["fixture"]["parameters"]
        params["source_revision"] = "archived-java-revision"
        self.assertEqual(self.load().cases["repair-contract-preserve"]["fixture"]["parameters"]["source_revision"],
                         "archived-java-revision")
        with patch.object(fixtures, "JAVA_FIXTURES", Path(self.temporary.name) / "absent-templates"):
            with self.assertRaisesRegex(EvaluationError, "invalid source directory"):
                fixtures._java_assets(params)


if __name__ == "__main__":
    unittest.main()
