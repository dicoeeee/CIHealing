"""Check the boundary between a recorded command, its child exit, and candidate identity."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.build_evidence import build_receipt


class BuildEvidenceTests(unittest.TestCase):
    cwd = "/trial/source"
    maven = "/opt/maven/bin/mvn"
    wrapper = ('cd /trial/source\nMVN=/opt/maven/bin/mvn\nRT=/trial/runtime\n'
               '"$MVN" -o -B -s "$RT/settings.xml" -Dmaven.repo.local="$RT/m2" compile 2>&1 | tail -20\n'
               'echo "EXIT=${PIPESTATUS[0]}"')

    def receipt(self, command, output):
        return build_receipt(command, output, cwd=self.cwd, source_dir=self.cwd, maven_paths={self.maven})

    def test_child_status_is_not_pipeline_status(self):
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                fact = self.receipt(self.wrapper, f"EXIT={exit_code}\n\n[exit_code: 0]")
                self.assertEqual(fact["exit_code"], exit_code)
                self.assertEqual(fact["program"], self.maven)
                self.assertEqual(fact["cwd"], self.cwd)
                self.assertEqual(fact["argv"], [self.maven, "-o", "-B", "-s", "/trial/runtime/settings.xml",
                                                "-Dmaven.repo.local=/trial/runtime/m2", "compile"])
        for cmd, out in [(self.wrapper, "BUILD SUCCESS\n[exit_code: 0]"),
                         (self.wrapper.rsplit("\n", 1)[0], "EXIT=0\n[exit_code: 0]")]:
            self.assertIsNone(self.receipt(cmd, out)["exit_code"])

    def test_uncertain_source_and_uninterpreted_commands_do_not_supply_receipts(self):
        for cmd in (self.wrapper.replace(self.maven, "/tmp/other/mvn"),
                    self.wrapper.replace(self.maven, "$(locate mvn)"),
                    self.wrapper.replace('"$MVN"', "'$MVN'"),
                    self.wrapper.replace("cd /trial/source", "cd /trial/other"),
                    self.wrapper.replace("compile", "--file /other/pom.xml compile"),
                    self.wrapper.replace('${PIPESTATUS[0]}', '0'),
                    "mvn --version", "mvn -s compile", "false && mvn compile", "echo mvn compile"):
            with self.subTest(command=cmd):
                self.assertIsNone(self.receipt(cmd, "[exit_code: 0]"))

    def test_quoted_literal_path_keeps_spaces_as_one_argument(self):
        command = self.wrapper.replace("RT=/trial/runtime", 'RT="/trial/local runtime"')
        fact = self.receipt(command, "EXIT=0\n[exit_code: 0]")
        self.assertIn("/trial/local runtime/settings.xml", fact["argv"])
        unquoted = command.replace('"$RT/settings.xml"', "$RT/settings.xml")
        self.assertIsNone(self.receipt(unquoted, "[exit_code: 0]"))
