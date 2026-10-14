"""Model-free installed-Chrys tool checks inside the selected runtime sandbox."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

from common import EvalError, read_regular
from repair_support import SKILL_MOUNT, SKILL_NAME
from chrys_capabilities import observer_capabilities, require_cli_options


def selected_skills(profile_dir, agent):
    import yaml
    matches = []
    for path in sorted((Path(profile_dir) / "agents").glob("*.yaml")):
        row = yaml.safe_load(read_regular(path))
        if isinstance(row, dict) and agent in (row.get("name"), row.get("id")):
            matches.append(row)
    if len(matches) != 1:
        raise EvalError("preflight requires one unambiguous Agent profile")
    skills = matches[0].get("skills", {})
    if (not isinstance(skills, dict) or skills.get("paths") != [SKILL_MOUNT]
            or skills.get("auto_load_user_agents_skills") is not False
            or skills.get("auto_load_cwd_agents_skills") is not False or skills.get("inline")):
        raise EvalError("isolated Agent must discover only " + SKILL_MOUNT + "; regenerate its profile")
    return skills


async def check_skills(profile_dir, agent):
    from chrys.service.profiles.agents.schema import SkillsConfig
    from chrys.service.skills.adapter import create_skills_provider
    provider, warnings = await create_skills_provider(SkillsConfig(**selected_skills(profile_dir, agent)))
    if (warnings or provider is None or provider.skill_names() != [SKILL_NAME]
            or provider.skill_sources() != {SKILL_MOUNT: [SKILL_NAME]}):
        raise EvalError("native Skill registration failed")
    # Use the same callable bodies as native load_skill/read_skill_resource.
    # Validate private API capabilities explicitly; a release string cannot
    # establish compatibility. Missing APIs fail before any model execution.
    tool_map = {tool.name: tool for tool in getattr(provider, "_chrys_tools", [])}
    if any(not callable(getattr(tool_map.get(name), "func", None)) for name in ("load_skill", "read_skill_resource", "run_skill_script")):
        raise EvalError("Chrys native Skill callable capability missing")
    loaded = await tool_map["load_skill"].func(skill_name=SKILL_NAME)
    resource = "references/invocation-inputs.md"
    content = await tool_map["read_skill_resource"].func(skill_name=SKILL_NAME, resource_name=resource, max_tokens=None)
    if (read_regular(Path(SKILL_MOUNT) / "SKILL.md").decode().split("---", 2)[-1].strip() not in loaded
            or read_regular(Path(SKILL_MOUNT) / resource).decode().strip() not in content):
        raise EvalError("native Skill tool returned incomplete or invalid content")
    # Exercise the native script runner, not an equivalent shell invocation.
    help_text = await tool_map["run_skill_script"].func(
        skill_name=SKILL_NAME, script_name="scripts/workspace_check.py",
        arguments=["capture", "--help"], cwd=SKILL_MOUNT, max_tokens=4000)
    # Current native runner omits zero, but renders nonzero exit codes. Probe
    # both paths instead of requiring the shell tool's different output format.
    bad_args = await tool_map["run_skill_script"].func(
        skill_name=SKILL_NAME, script_name="scripts/workspace_check.py",
        arguments=["capture", "--ci-eval-invalid-option"], cwd=SKILL_MOUNT, max_tokens=4000)
    if (not isinstance(help_text, str) or "--manifest" not in help_text
            or ("[exit_code:" in help_text and "[exit_code: 0]" not in help_text)
            or not isinstance(bad_args, str) or "[exit_code: 2]" not in bad_args):
        raise EvalError("native Skill script runner unavailable; no model called")
    return {"name": SKILL_NAME, "source": SKILL_MOUNT, "load_skill": True, "read_skill_resource": True,
            "run_skill_script": True, "script_nonzero_exit": True}


async def check_read_file():
    """Exercise the selected runtime's argument validator and full report read.

    No model, profiles or user files. Stringified line_range caused two real
    pre-dispatch failures; never coerce such arguments in the adapter.
    """
    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.service.tools.builtins.filesystem import FilesystemTools
    function = next((t for t in FilesystemTools(SessionEnvironment.capture()).tools()
                     if t.name == "read_file"), None)
    if not callable(getattr(function, "invoke", None)):
        raise EvalError("native read_file validator unavailable")
    with tempfile.TemporaryDirectory(prefix="ci-eval-readback-") as temp:
        report = Path(temp) / "analysis-fix.md"
        report.write_text("CI_EVAL_REPORT_START\nCI_EVAL_REPORT_END\n")
        for extra in ({}, {"line_range": [1, -1], "max_tokens": 1000}):
            value = await function.invoke(arguments={"path": str(report), **extra}, skip_parsing=True)
            if not isinstance(value, str) or any(marker not in value for marker in ("CI_EVAL_REPORT_START", "CI_EVAL_REPORT_END")):
                raise EvalError("native read_file incomplete synthetic report readback")
        try:
            await function.invoke(arguments={"path": str(report), "line_range": "[1, 20]"}, skip_parsing=True)
        except TypeError as exc:
            if "Invalid arguments" not in str(exc):
                raise EvalError("native read_file failed outside argument validation") from None
        else:
            raise EvalError("native read_file accepted stringified line_range")
    return {"full_readback": True, "array_line_range": True, "string_line_range_rejected": True, "model_calls": 0}


async def check_search():
    from chrys.service.tools.builtins.search import _find_rg, _grep_impl
    rg = _find_rg()
    subprocess.run([rg, "--version"], check=True, capture_output=True, timeout=10)
    with tempfile.TemporaryDirectory(prefix="ci-eval-search-") as temp:
        probe = Path(temp) / "probe.txt"
        probe.write_text("CI_EVAL_SEARCH_READY\n")
        result = await _grep_impl("CI_EVAL_SEARCH_READY", path=str(probe), context_lines=0, max_results=1)
    if "Found 1 match" not in result or "CI_EVAL_SEARCH_READY" not in result or "Error:" in result:
        raise EvalError("native Chrys search failed")
    return {"native_grep": True, "rg_path": rg}


async def check_shell():
    """Exercise the real tool body (including PTY), not a substitute subprocess.

    Check both successful and nonzero exits, stdout/stderr, working directory,
    and a create/read/delete cycle in a private temporary directory. No model.
    """
    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.service.tools.builtins.shell import ShellTools
    runtime = SessionEnvironment.capture()
    shell = ShellTools(runtime)
    tools = shell.tools()
    # Invoke the bound FunctionTool, whose __call__ supplies the ShellTools
    # instance. Calling .func directly would bypass binding and never test PTY.
    function = tools[0] if tools else None
    if not callable(function):
        raise EvalError("Chrys native shell callable capability missing")
    with tempfile.TemporaryDirectory(prefix="ci-eval-shell-") as temp:
        cwd = str(Path(temp).resolve())
        for code in (0, 7):
            script = ("import os,pathlib,sys; assert os.getcwd()==sys.argv[1]; "
                      "p=pathlib.Path('shell-probe.txt'); p.write_text('synthetic'); "
                      "assert p.read_text()=='synthetic'; p.unlink(); "
                      "print('CI_EVAL_SHELL_STDOUT'); print('CI_EVAL_SHELL_STDERR',file=sys.stderr); "
                      "sys.exit(int(sys.argv[2]))")
            result = await asyncio.wait_for(function(
                command=shlex.join([sys.executable, "-B", "-c", script, cwd, str(code)]),
                reason="Model-free evaluator shell capability probe", working_dir=cwd,
                timeout=5, max_tokens=1000), timeout=15)
            expected = {"CI_EVAL_SHELL_STDOUT", "CI_EVAL_SHELL_STDERR", f"[exit_code: {code}]"}
            if not isinstance(result, str) or not expected <= set(result.splitlines()):
                raise EvalError("native Chrys shell failed (PTY/command/output/exit-code probe); no model called")
    return {"native_shell": True, "stdout_stderr": True, "exit_codes": [0, 7],
            "working_directory": True, "temporary_file_roundtrip": True}


async def check_git():
    """Exercise Git through the actual Chrys shell in a private synthetic repo."""
    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.service.tools.builtins.shell import ShellTools
    from common import snapshot
    git = os.environ.get("CI_EVAL_GIT")
    if not git or not Path(git).is_absolute():
        raise EvalError("selected Git path missing from native preflight")
    with tempfile.TemporaryDirectory(prefix="ci-eval-git-") as temp:
        repo = Path(temp).resolve()
        def run(*args):
            return subprocess.run([git, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
                                  cwd=repo, env=os.environ, capture_output=True, check=True, timeout=10).stdout.decode()
        run("-c", "init.templateDir=", "init", "-q")
        (repo / "tracked.txt").write_text("before\n")
        run("add", "tracked.txt")
        run("-c", "user.name=CI Eval", "-c", "user.email=ci-eval@example.invalid", "commit", "-qm", "synthetic-baseline")
        head = run("rev-parse", "HEAD").strip()
        (repo / "tracked.txt").write_text("after\n")
        (repo / "untracked.txt").write_text("preexisting-input\n")
        before = snapshot(repo, skip_git=False)
        script = (
            "import os,pathlib,shutil,subprocess,sys; "
            "assert pathlib.Path(shutil.which('git')).resolve()==pathlib.Path(sys.argv[1]).resolve(); "
            "assert os.environ['GIT_CONFIG_NOSYSTEM']=='1' and os.environ['GIT_CONFIG_GLOBAL']==os.devnull; "
            "assert os.environ['GIT_OPTIONAL_LOCKS']=='0'; "
            "run=lambda *a: subprocess.run(['git',*a],check=True,capture_output=True,text=True,timeout=5).stdout; "
            "assert run('rev-parse','HEAD').strip()==sys.argv[2]; "
            "s=run('status','--porcelain'); assert ' M tracked.txt' in s and '?? untracked.txt' in s; "
            "d=run('diff','--no-ext-diff','--no-textconv','--','tracked.txt'); assert '-before' in d and '+after' in d; "
            "assert run('log','-1','--format=%s').strip()=='synthetic-baseline'; "
            "print('CI_EVAL_GIT_READY')")
        function = ShellTools(SessionEnvironment.capture()).tools()[0]
        result = await asyncio.wait_for(function(
            command=shlex.join([sys.executable, "-B", "-c", script, git, head]),
            reason="Model-free evaluator Git operations probe", working_dir=str(repo), timeout=10, max_tokens=1000), 15)
        if not {"CI_EVAL_GIT_READY", "[exit_code: 0]"} <= set(str(result).splitlines()) or snapshot(repo, skip_git=False) != before:
            raise EvalError("native Git status/diff/log/rev-parse or nonmutation check failed")
    return {"git_path": git, "status": True, "diff": True, "log": True, "rev_parse": True,
            "workspace_unchanged": True, "global_config_disabled": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles")
    parser.add_argument("--agent")
    args = parser.parse_args()
    capabilities = observer_capabilities()
    if Path.home() != Path("/home/eval") or not Path("/opt/maven-repository").is_dir():
        raise EvalError("invalid image home or Maven cache")
    for tool in ("git", "rg", "mvn", "java", "javac"):
        subprocess.run([tool, "--version"], check=True, capture_output=True, timeout=10)
    help_result = subprocess.run(["chrys", "run", "--help"], check=True, capture_output=True, text=True, timeout=10)
    require_cli_options(help_result.stdout, ("--agent", "--model", "--workdir", "--task", "--json"))
    result = {"status": "passed", **capabilities, "search": asyncio.run(check_search()),
              "shell": asyncio.run(check_shell()), "read_file": asyncio.run(check_read_file()), "headless_cli": True}
    if args.profiles:
        result["skill"] = asyncio.run(check_skills(args.profiles, args.agent))
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(2)
