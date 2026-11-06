"""Harness revision 3: scoped enterprise-resource registration for native iCode.

This is an explicit evaluation-host adaptation, not a default iCode capability
or production compatibility proof. It preserves file paths, the native resource
manifest and native read gate. Only one trusted trial package is discovered;
user/project Skill discovery is never used. No model or script runs in preflight.
"""

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat

from common import EvalError, read_json, read_regular


CONTRACT = "ab-enterprise-resource-registration-v1"
HARNESS_REVISION = 3
SEARCH_DEPTH = 5
SKILL_NAME = "ci-self-healing"
PROFILE = "references/classification-profiles/synthetic/build/profile.md"
RULES = "references/classification-profiles/synthetic/build/rules.md"
POLICY = "references/enterprise/synthetic-ci-policy.md"
CASE_SOURCES = ("references/case-sources/edgescan.md", "references/case-sources/packaging.md")
REQUIRED = {
    "profile-policy-chain": (PROFILE, RULES, "references/enterprise-policy.md", POLICY),
    "profile-policy-missing": (PROFILE, RULES, "references/enterprise-policy.md"),
    "case-source-selection": (*CASE_SOURCES, "references/enterprise-policy.md"),
    "case-source-scope": (*CASE_SOURCES, "references/enterprise-policy.md"),
    "output-independent": ("references/enterprise-output-spec.md",),
    "output-collision": ("references/enterprise-output-spec.md",),
}


def _absolute(value):
    if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts:
        raise EvalError("enterprise registration requires explicit absolute paths")
    path = Path(os.path.abspath(value))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise EvalError("enterprise registration rejects symlink paths")
    return path


def _inventory(root):
    """Check every package path before the native scanner reads SKILL.md.

read_regular checks Markdown frontmatter before reading its body and rejects
ai_access:false/ambiguous declarations. Hashing also freezes the package for
later native catalog refreshes. The model's sandbox keeps this package readonly;
this check does not claim independent OS isolation or eliminate external races.
"""
    if not root.is_dir() or root.is_symlink():
        raise EvalError("enterprise Skill package is not a real directory")
    files = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise EvalError("enterprise registration rejects package symlinks")
            if stat.S_ISDIR(mode):
                pending.append(path)
            elif stat.S_ISREG(mode):
                name = path.relative_to(root).as_posix()
                files[name] = hashlib.sha256(read_regular(path)).hexdigest()
            else:
                raise EvalError("enterprise registration rejects nonregular package files")
    if "SKILL.md" not in files:
        raise EvalError("enterprise Skill entry is missing")
    return files


class EnterpriseRegistration:
    def __init__(self, layout, layout_path):
        if layout.get("enterprise_resource_registration") is not True or layout.get("role", "execute") != "execute":
            raise EvalError("enterprise registration is execute-only and requires explicit opt-in")
        self.scenario = layout.get("enterprise_resource_scenario")
        if self.scenario not in REQUIRED:
            raise EvalError("enterprise registration requires a known scenario")
        visible = _absolute(layout.get("visible"))
        self.trial = visible.parent
        self.root = _absolute(layout.get("skill"))
        config = _absolute(layout.get("config"))
        source = _absolute(str(layout_path))
        if (visible != self.trial / "visible"
                or self.root != self.trial / "runtime/skills" / SKILL_NAME
                or config != self.trial / "runtime/config"
                or source not in (self.trial / "prepared-layout.json", self.trial / "adapter/layout.json")):
            raise EvalError("enterprise registration paths do not belong to this trial")
        self.files = _inventory(self.root)
        self.required = REQUIRED[self.scenario]
        self.expected_missing = (POLICY,) if self.scenario == "profile-policy-missing" else ()
        if any(name not in self.files for name in self.required):
            raise EvalError("required enterprise resource is missing before registration")
        if any(name in self.files for name in self.expected_missing):
            raise EvalError("expected-missing enterprise policy unexpectedly exists")
        self.config = config

    def check_unchanged(self):
        if _inventory(self.root) != self.files:
            raise EvalError("enterprise Skill package changed after registration boundary")

    async def preflight(self):
        """Exercise real native provider/tool bodies, without model execution."""
        from chrys.foundation.tool_result_metadata import TOOL_ERROR_KIND_METADATA_KEY
        from chrys.service.profiles.agents.schema import SkillsConfig
        from chrys.service.skills.adapter import create_skills_provider
        from chrys.service.tools.result_metadata import tool_result_metadata

        self.check_unchanged()
        provider, warnings = await create_skills_provider(SkillsConfig(
            paths=[str(self.root)], auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False))
        if (provider is None or warnings or provider.skill_names() != [SKILL_NAME]
                or provider.skill_sources() != {str(self.root): [SKILL_NAME]}):
            raise EvalError("native enterprise Skill registration failed")
        tools = {t.name: t for t in getattr(provider, "_chrys_tools", ())}
        if any(not callable(getattr(tools.get(n), "func", None)) for n in ("load_skill", "read_skill_resource")):
            raise EvalError("native enterprise Skill callable capability missing")
        loaded = await tools["load_skill"].func(skill_name=SKILL_NAME)
        if not isinstance(loaded, str) or SKILL_NAME not in loaded:
            raise EvalError("native enterprise load_skill failed")
        checked = []
        for name in self.required:
            value = await tools["read_skill_resource"].func(
                skill_name=SKILL_NAME, resource_name=name, max_tokens=1_000_000)
            expected = read_regular(self.root / name).decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
            if value != expected:
                raise EvalError("native enterprise resource read is missing or incomplete: " + name)
            checked.append({"resource": name, "sha256": self.files[name], "status": "full_native_read"})

        # SKILL.md exists physically but is deliberately not a registered
        # resource; the other probes exercise unknown and out-of-package names.
        denied = ("SKILL.md", "references/__ab_unregistered_probe__.md", "../SKILL.md",
                  str(self.root / "SKILL.md"), str(self.trial / "visible/task.txt"), *self.expected_missing)
        denied_checks = []
        for name in denied:
            metadata = {}
            token = tool_result_metadata.set(metadata)
            try:
                await tools["read_skill_resource"].func(skill_name=SKILL_NAME, resource_name=name)
            finally:
                tool_result_metadata.reset(token)
            if metadata.get(TOOL_ERROR_KIND_METADATA_KEY) != "resource_not_found":
                raise EvalError("native resource registration gate did not reject probe")
            denied_checks.append({"resource": name, "status": "resource_not_found"})
        self.check_unchanged()
        return {"contract": CONTRACT, "harness_revision": HARNESS_REVISION, "status": "passed",
                "scenario": self.scenario, "skill": str(self.root), "search_depth": SEARCH_DEPTH,
                "required_reads": checked, "expected_missing": list(self.expected_missing),
                "denied_reads": denied_checks, "model_calls": 0, "script_calls": 0,
                "boundary": "Explicit isolated evaluation-host adaptation, not default iCode or production compatibility proof; host preflight reads are not Agent guide-reading evidence."}


@contextmanager
def enterprise_skill_host(layout, layout_path):
    """Temporarily adapt discovery in this worker process, retaining native gates."""
    registration = EnterpriseRegistration(layout, layout_path)
    from chrys.service.skills import adapter
    original_collect = adapter._collect_skill_paths
    original_discover = adapter.discover_file_skills
    if getattr(original_discover, "_ab_enterprise_registration", False):
        raise EvalError("nested enterprise registration adaptation is forbidden")

    def collect(config, runtime=None, *, project_skills_enabled=False):
        if (config.paths != [str(registration.root)] or config.inline
                or config.auto_load_user_agents_skills or config.auto_load_cwd_agents_skills):
            raise EvalError("enterprise host refuses any other Skill source")
        # Deliberately do not call the ordinary collector: even its always-on
        # user/config Skill directory must not be scanned for this experiment.
        return [str(registration.root)]

    def discover(paths, *, script_extensions, **kwargs):
        if list(paths) != [str(registration.root)] or kwargs:
            raise EvalError("enterprise host discovery scope or API changed")
        registration.check_unchanged()
        return original_discover(paths, script_extensions=script_extensions, search_depth=SEARCH_DEPTH)

    discover._ab_enterprise_registration = True
    adapter._collect_skill_paths = collect
    adapter.discover_file_skills = discover
    try:
        yield registration
    finally:
        adapter.discover_file_skills = original_discover
        adapter._collect_skill_paths = original_collect


def main():
    parser = argparse.ArgumentParser(description="Model-free revision-3 enterprise registration canary")
    parser.add_argument("layout", type=Path)
    args = parser.parse_args()
    with enterprise_skill_host(read_json(args.layout), args.layout) as registration:
        print(json.dumps(asyncio.run(registration.preflight()), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "error": str(exc), "model_calls": 0}))
        raise SystemExit(2)
