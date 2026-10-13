"""Use the installed Chrys CLI entrypoint with per-process, private config paths.

No HOME override, global install/edit, alternate model executor or copied Chrys.
The scoped platform override is capability-checked and occurs before CLI imports.
"""

import argparse
import asyncio
import dataclasses
import json
import os
from pathlib import Path
import sys

from common import EvalError, read_json, read_regular, write_new


def bootstrap(root):
    import chrys.foundation.platform as platform_api
    current = platform_api.get_platform()
    try:
        scoped = dataclasses.replace(current, config_dir=Path(root), data_dir=Path(root))
    except (TypeError, ValueError):
        raise EvalError("installed Chrys lacks scoped platform configuration capability") from None
    platform_api.get_platform = lambda: scoped


def profiles(layout):
    import yaml
    import chrys
    root = Path(layout["config"])
    builtin = Path(chrys.__file__).parent / "service/profiles/agents/builtins/Code.yaml"
    agent = yaml.safe_load(read_regular(builtin))
    agent.update(name="EvalCode", id="c1eaa1000001", display_name="Native isolated evaluation Code",
                 tools={"builtins": ["filesystem.read", "filesystem.write", "shell", "search", "sleep", "todo"]},
                 skills={"paths": [layout["skill"]], "auto_load_user_agents_skills": False, "auto_load_cwd_agents_skills": False},
                 sub_agents={"agents": []}, memory={"files": []}, mcp_servers=[],
                 model={"profile_id": ""})
    write_new(root / "agents/EvalCode.yaml", yaml.safe_dump(agent, sort_keys=False))
    if layout.get("model"):
        write_new(root / "models/eval-model.yaml", yaml.safe_dump(layout["model"], sort_keys=False))


def preflight(layout):
    from chrys_capabilities import observer_capabilities
    import chrys_preflight
    chrys_preflight.SKILL_MOUNT = layout["skill"]
    checks = observer_capabilities()
    checks["skill"] = asyncio.run(chrys_preflight.check_skills(layout["config"], "EvalCode"))
    checks["search"] = asyncio.run(chrys_preflight.check_search())
    checks["shell"] = asyncio.run(chrys_preflight.check_shell())
    checks["git"] = asyncio.run(chrys_preflight.check_git())
    # Real CLI profile discovery, with no task/model execution.
    from chrys.app.cli.app import main as chrys_main
    import contextlib, io
    out = io.StringIO()
    previous = sys.argv
    try:
        sys.argv = [layout.get("cli", "chrys"), "agents", "--json"]
        with contextlib.redirect_stdout(out):
            code = chrys_main()
    finally:
        sys.argv = previous
    rows = json.loads(out.getvalue()).get("agents", [])
    if code != 0 or not any(r.get("name") == "EvalCode" for r in rows):
        raise EvalError("native CLI did not discover isolated EvalCode")
    print(json.dumps({"status": "passed", **checks, "isolated_cli_profile": True}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("layout", type=Path)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    layout = read_json(args.layout)
    bootstrap(layout["config"])
    os.environ["CHRYS_SESSION_ROOT_DIR"] = layout["sessions"]
    profiles(layout)
    if args.preflight:
        preflight(layout)
        return 0
    import chrys_observer
    sys.argv = ["chrys_observer", "--agent", "EvalCode", "--model", layout["model"]["name"],
                "--task", layout["task"], "--max-tools", str(layout["max_tools"]),
                "--native-layout", str(args.layout)]
    return chrys_observer.main()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        import traceback
        error = {"status": "incomplete", "error_type": type(exc).__name__}
        error["frames"] = [{"file": Path(f.filename).name, "line": f.lineno, "function": f.name} for f in traceback.extract_tb(exc.__traceback__)[-6:]]
        if isinstance(exc, OSError):
            error.update(errno=exc.errno, path=str(exc.filename) if exc.filename else None)
        print(json.dumps(error), file=sys.stderr)
        raise SystemExit(2)
