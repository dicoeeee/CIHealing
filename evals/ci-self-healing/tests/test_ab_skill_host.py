"""No-model tests for the revision-3 native enterprise registration boundary.

Run with the installed iCode Python to include the real provider canaries.
Ordinary Python still exercises the dependency-free path/access guards.
"""
import asyncio
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ab_skill_host as host
from common import EvalError


def package(directory, scenario="profile-policy-chain"):
    trial = Path(directory).resolve() / scenario
    root = trial / "runtime/skills/ci-self-healing"
    layout = {"skill": str(root), "visible": str(trial / "visible"),
              "config": str(trial / "runtime/config"),
              "enterprise_resource_registration": True, "enterprise_resource_scenario": scenario}
    root.mkdir(parents=True)
    (trial / "visible").mkdir()
    (trial / "runtime/config").mkdir()
    (trial / "adapter").mkdir()
    (root / "SKILL.md").write_text("---\nname: ci-self-healing\ndescription: Synthetic offline host canary.\n---\n\n# Entry\n")
    for name in host.REQUIRED[scenario]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nai_generated: true\nreviewed: false\n---\n\n# " + name + "\n\nSynthetic complete body.\n")
    source = trial / "prepared-layout.json"
    source.write_text(json.dumps(layout))
    return trial, root, layout, source


class RegistrationBoundaryTests(unittest.TestCase):
    def test_requires_exact_opt_in_execute_role_and_known_scenario(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, layout, source = package(directory)
            for patch in ({"enterprise_resource_registration": False}, {"enterprise_resource_registration": "true"},
                          {"role": "review"}, {"enterprise_resource_scenario": "unknown"}):
                with self.subTest(patch=patch), self.assertRaises(EvalError):
                    host.EnterpriseRegistration({**layout, **patch}, source)

    def test_trial_binding_rejects_outside_and_parent_traversal_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            trial, _, layout, source = package(directory)
            for patch in ({"skill": str(trial / "other/ci-self-healing")},
                          {"config": str(trial.parent / "other/config")},
                          {"skill": str(trial / "runtime/skills/../ci-self-healing")}):
                with self.subTest(patch=patch), self.assertRaises(EvalError):
                    host.EnterpriseRegistration({**layout, **patch}, source)
            with self.assertRaises(EvalError):
                host.EnterpriseRegistration(layout, trial.parent / "other/layout.json")

    def test_symlinks_are_rejected_before_scanner_including_below_scan_depth(self):
        with tempfile.TemporaryDirectory() as directory:
            trial, root, layout, source = package(directory)
            outside = trial / "outside.md"
            outside.write_text("must not be read")
            nested = root / "references/a/b/c/d/e/link.md"
            nested.parent.mkdir(parents=True)
            nested.symlink_to(outside)
            with self.assertRaisesRegex(EvalError, "symlink"):
                host.EnterpriseRegistration(layout, source)

    def test_restricted_frontmatter_is_rejected_without_reading_body(self):
        with tempfile.TemporaryDirectory() as directory:
            _, root, layout, source = package(directory)
            restricted = root / "references/blocked.md"
            restricted.write_text("---\nai_access: false\n---\nPRIVATE BODY\n")
            original = Path.open
            accessed = []

            class HeaderOnly(io.BytesIO):
                def read(self, *args):
                    accessed.append("body-read")
                    raise AssertionError("restricted body must not be read")

            def opened(path, *args, **kwargs):
                if path == restricted:
                    return HeaderOnly(b"---\nai_access: false\n---\nPRIVATE BODY\n")
                return original(path, *args, **kwargs)

            with mock.patch.object(Path, "open", opened):
                with self.assertRaisesRegex(EvalError, "restricted or ambiguous"):
                    host.EnterpriseRegistration(layout, source)
            self.assertEqual(accessed, [])

    def test_missing_policy_exception_is_only_the_explicit_negative_scenario(self):
        with tempfile.TemporaryDirectory() as directory:
            _, root, layout, source = package(directory)
            (root / host.POLICY).unlink()
            with self.assertRaisesRegex(EvalError, "required enterprise resource"):
                host.EnterpriseRegistration(layout, source)
            _, missing_root, negative, negative_source = package(directory, "profile-policy-missing")
            registration = host.EnterpriseRegistration(negative, negative_source)
            self.assertEqual(registration.expected_missing, (host.POLICY,))
            p = missing_root / host.POLICY
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("policy unexpectedly restored")
            with self.assertRaisesRegex(EvalError, "unexpectedly exists"):
                host.EnterpriseRegistration(negative, negative_source)

    def test_registration_rejects_package_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            _, root, layout, source = package(directory)
            registration = host.EnterpriseRegistration(layout, source)
            (root / host.RULES).write_text("changed rules")
            with self.assertRaisesRegex(EvalError, "changed after"):
                registration.check_unchanged()


@unittest.skipUnless(importlib.util.find_spec("chrys"), "installed iCode interpreter required for native canaries")
class NativeRegistrationTests(unittest.TestCase):
    def test_all_six_scenarios_use_real_provider_and_retain_native_denials(self):
        from chrys.service.skills import adapter
        from chrys.service.skills.loader import load_file_skill
        original_collect, original_discover = adapter._collect_skill_paths, adapter.discover_file_skills
        with tempfile.TemporaryDirectory() as directory:
            for scenario in host.REQUIRED:
                with self.subTest(scenario=scenario):
                    _, root, layout, source = package(directory, scenario)
                    default = load_file_skill(str(root), script_extensions=(".py",))
                    default_names = {r.name for r in default.resources}
                    if scenario.startswith("profile"):
                        self.assertNotIn(host.PROFILE, default_names)
                    if scenario.startswith("case"):
                        self.assertNotIn(host.CASE_SOURCES[0], default_names)
                    with mock.patch("socket.socket.connect", side_effect=AssertionError("network forbidden")), \
                         mock.patch("subprocess.Popen", side_effect=AssertionError("child process forbidden")):
                        with host.enterprise_skill_host(layout, source) as registration:
                            receipt = asyncio.run(registration.preflight())
                    self.assertEqual(receipt["status"], "passed")
                    self.assertEqual({r["resource"] for r in receipt["required_reads"]}, set(host.REQUIRED[scenario]))
                    self.assertEqual(receipt["expected_missing"], [host.POLICY] if scenario == "profile-policy-missing" else [])
                    self.assertEqual(receipt["model_calls"], 0)
                    self.assertEqual(receipt["script_calls"], 0)
                    self.assertTrue(all(r["status"] == "resource_not_found" for r in receipt["denied_reads"]))
                    self.assertIs(adapter._collect_skill_paths, original_collect)
                    self.assertIs(adapter.discover_file_skills, original_discover)

    def test_no_user_skill_collection_no_extra_root_and_no_nested_adaptation(self):
        from chrys.service.skills import adapter
        from chrys.service.profiles.agents.schema import SkillsConfig
        with tempfile.TemporaryDirectory() as directory:
            _, root, layout, source = package(directory)
            original_discover = adapter.discover_file_skills
            with mock.patch.object(adapter, "_collect_skill_paths", side_effect=AssertionError("user discovery forbidden")) as collector, \
                 mock.patch.object(adapter, "discover_file_skills", wraps=original_discover) as discover:
                with host.enterprise_skill_host(layout, source) as registration:
                    asyncio.run(registration.preflight())
                    with self.assertRaisesRegex(EvalError, "other Skill source"):
                        asyncio.run(adapter.create_skills_provider(SkillsConfig(paths=[str(root), "/outside"])))
                    with self.assertRaisesRegex(EvalError, "scope or API"):
                        adapter.discover_file_skills(["/outside"], script_extensions=(".py",))
                    with self.assertRaisesRegex(EvalError, "nested"):
                        with host.enterprise_skill_host(layout, source):
                            pass
                collector.assert_not_called()
                discover.assert_called_once_with([str(root)], script_extensions=(".py", ".sh", ".ps1"), search_depth=5)

    def test_physical_file_outside_manifest_still_denied_and_exception_restores_hooks(self):
        from chrys.service.skills import adapter
        from chrys.service.profiles.agents.schema import SkillsConfig
        original_collect, original_discover = adapter._collect_skill_paths, adapter.discover_file_skills
        with tempfile.TemporaryDirectory() as directory:
            _, root, layout, source = package(directory)
            unregistered = "references/a/b/c/d/unregistered.md"
            path = root / unregistered
            path.parent.mkdir(parents=True)
            path.write_text("physical but outside the declared depth")

            async def read_unknown():
                provider, warnings = await adapter.create_skills_provider(SkillsConfig(
                    paths=[str(root)], auto_load_user_agents_skills=False, auto_load_cwd_agents_skills=False))
                self.assertFalse(warnings)
                read = next(t for t in provider._chrys_tools if t.name == "read_skill_resource")
                result = await read.func(skill_name="ci-self-healing", resource_name=unregistered)
                self.assertIn("not found", result)

            with self.assertRaisesRegex(RuntimeError, "canary failure"):
                with host.enterprise_skill_host(layout, source):
                    asyncio.run(read_unknown())
                    raise RuntimeError("canary failure")
            self.assertIs(adapter._collect_skill_paths, original_collect)
            self.assertIs(adapter.discover_file_skills, original_discover)


if __name__ == "__main__":
    unittest.main()
