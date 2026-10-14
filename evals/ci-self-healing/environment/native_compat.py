#!/usr/bin/env python3
"""Offline installed-Chrys or selected-wheel check; never starts an Agent/model.

Use an existing dependency-complete Chrys Python. --wheel optionally tests an
explicitly selected package instead. Does not install globally or read user
profiles. Tests host event/Skill capabilities, not Linux search or model behavior.
"""

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ci-eval-native-compat-") as temp:
        root = Path(temp).resolve()
        if args.wheel:
            with zipfile.ZipFile(args.wheel) as archive:
                for item in archive.infolist():
                    name = Path(item.filename)
                    if name.is_absolute() or ".." in name.parts or item.file_size > 32 * 1024 * 1024:
                        raise ValueError("unsupported wheel entry")
                if sum(i.file_size for i in archive.infolist()) > 256 * 1024 * 1024:
                    raise ValueError("oversized wheel")
                archive.extractall(root / "package")
        script = r'''
import asyncio, json, pathlib, sys, types
from unittest import mock
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2])
import chrys, yaml
from common import write_new
from suite import REPO, skill_from_dir
import chrys_preflight
from chrys.service.skills.adapter import create_skills_provider
from chrys.service.profiles.agents.schema import SkillsConfig
from chrys_capabilities import observer_capabilities
capabilities = observer_capabilities()
readback = asyncio.run(chrys_preflight.check_read_file())
root = pathlib.Path(sys.argv[3]); skill = root / "ci-self-healing"; profiles = root / "profiles"
for name, data in skill_from_dir(REPO / "skills/ci-self-healing").items():
    write_new(skill / name, data)
write_new(profiles / "agents/EvalCode.yaml", yaml.safe_dump({"name": "EvalCode", "skills": {
    "paths": [str(skill)], "auto_load_user_agents_skills": False, "auto_load_cwd_agents_skills": False}}))
chrys_preflight.SKILL_MOUNT = str(skill)
with mock.patch("chrys.foundation.platform.get_platform", return_value=types.SimpleNamespace(config_dir=root / "clean-config")):
    asyncio.run(chrys_preflight.check_skills(profiles, "EvalCode"))
    old = root / "skill"
    write_new(old / "SKILL.md", (skill / "SKILL.md").read_bytes())
    provider, warnings = asyncio.run(create_skills_provider(SkillsConfig(
        paths=[str(old)], auto_load_user_agents_skills=False, auto_load_cwd_agents_skills=False)))
    if provider is None or provider.skill_names() or not warnings:
        raise ValueError("old-path negative control did not fail as expected")
print(json.dumps({**capabilities, "native_skill_tools": "passed", "read_file": readback, "old_path_negative_control": "rejected", "model_calls": 0}))
'''
        return subprocess.run([sys.executable, "-B", "-c", script, str(root / "package"),
                               str(Path(__file__).resolve().parents[1]), str(root / "probe")], timeout=45).returncode


if __name__ == "__main__":
    raise SystemExit(main())
