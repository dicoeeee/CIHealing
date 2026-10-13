#!/usr/bin/env python3
"""Explicit local environment setup; no secret files or global context changes.

Run with the existing Chrys Python (PyYAML installed). configure copies only
approved model fields and the built-in Code instructions. gateway injects the
upstream key via stdin into a one-use FIFO in container tmpfs, not Docker env.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from repair_support import SKILL_MOUNT
CONTEXT = "colima-ci-self-healing-eval"
INTERNAL = "ci-eval-internal"
EGRESS = "ci-eval-egress"
GATEWAY = "ci-eval-gateway"
# Not an upstream credential. Only grants access to the local model-only gateway.
LOCAL_TOKEN = "ci-eval-local-only"


def docker(*args, data=None, check=True, timeout=60):
    result = subprocess.run(["docker", "--context", CONTEXT, *args], input=data,
                            capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("Docker operation failed: " + args[0])
    return result


def source_model(path):
    from chrys.foundation.util.env_templates import resolve_env_templates
    model = yaml.safe_load(Path(path).read_text())
    if model.get("name") != "huoshan-glm5.2" or model.get("provider") != "openai" or model.get("api_style") != "responses":
        raise ValueError("requires the explicitly selected GLM 5.2 Responses profile")
    model["api_key"] = resolve_env_templates(model["api_key"], location="evaluation gateway bootstrap")
    if not model["api_key"] or not model["model_id"]:
        raise ValueError("incomplete model profile")
    return model


def configure(args):
    import chrys
    target = Path(args.output).resolve()
    target.mkdir(parents=True, exist_ok=False)
    model = source_model(args.model_profile)
    profiles = target / "profiles"
    (profiles / "models").mkdir(parents=True)
    (profiles / "agents").mkdir()
    (target / "tmp").mkdir()
    selected = {key: model[key] for key in ("id", "name", "provider", "api_style", "model_id", "max_context_tokens", "max_output_tokens", "stream") if key in model}
    selected.update(base_url="http://ci-eval-gateway:8080/v1", api_key="{{CHRYS_EVAL_API_KEY}}",
                    bypass_proxy=True, http_max_retries=1)
    (profiles / "models" / (selected["id"] + ".yaml")).write_text(yaml.safe_dump(selected, sort_keys=False))
    builtin = Path(chrys.__file__).parent / "service/profiles/agents/builtins/Code.yaml"
    agent = yaml.safe_load(builtin.read_text())
    agent.update(name="EvalCode", id="c1eaa1000001", display_name="Isolated evaluation Code",
                 tools={"builtins": ["filesystem.read", "filesystem.write", "shell", "search", "sleep", "todo"]},
                 skills={"paths": [SKILL_MOUNT], "auto_load_user_agents_skills": False, "auto_load_cwd_agents_skills": False},
                 sub_agents={"agents": []}, memory={"files": []})
    (profiles / "agents/EvalCode.yaml").write_text(yaml.safe_dump(agent, sort_keys=False))
    image = json.loads(docker("image", "inspect", args.image).stdout)[0]["Id"]
    runtime = {"image": image, "network": INTERNAL, "chrys_config_dir": str(profiles),
               "agent": "EvalCode", "expected_model_id": model["model_id"], "timeout_seconds": 900, "max_tool_calls": 80}
    (target / "runtime.json").write_text(json.dumps(runtime, indent=2) + "\n")
    receipt = {"chrys_version": chrys.__version__, "code_instructions_sha256": hashlib.sha256(agent["instructions"].encode()).hexdigest(),
               "docker_context": CONTEXT, "upstream_secret_written": False}
    (target / "setup.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"configured": str(target), "image": image, "upstream_secret_written": False}))


def gateway(args):
    runtime = json.loads(Path(args.runtime).read_text())
    model = source_model(args.model_profile)
    if runtime["expected_model_id"] != model["model_id"]:
        raise ValueError("runtime model differs from gateway model")
    host_args = []
    if args.pin_upstream_dns:
        host = urlsplit(model["base_url"]).hostname
        addresses = sorted({row[4][0] for row in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)})
        if not host or not addresses:
            raise ValueError("host could not resolve the selected upstream")
        for address in addresses:
            host_args.extend(["--add-host", host + ":" + address])
    for name, internal in ((INTERNAL, True), (EGRESS, False)):
        inspection = docker("network", "inspect", name, check=False)
        if inspection.returncode:
            docker("network", "create", *(["--internal"] if internal else []),
                   "--label", "ci-eval-owned=true", name)
        else:
            network = json.loads(inspection.stdout)[0]
            if network["Internal"] != internal or network.get("Labels", {}).get("ci-eval-owned") != "true":
                raise ValueError("existing network is not this evaluator's expected network")
    if docker("container", "inspect", GATEWAY, check=False).returncode == 0:
        raise ValueError("gateway already exists; inspect it before an explicit stop/remove/restart")
    uid, gid = os.getuid(), os.getgid()
    docker("run", "-d", "--name", GATEWAY, "--label", "ci-eval-owned=true", "--pull=never",
           "--read-only", "--user", f"{uid}:{gid}", "--cap-drop=ALL", "--security-opt=no-new-privileges",
           "--pids-limit=64", "--memory=256m", "--cpus=1", "--network", INTERNAL,
           *host_args,
           "--tmpfs", f"/run/gateway:rw,noexec,uid={uid},gid={gid},mode=0700,size=1m",
           "--entrypoint", "python3", runtime["image"], "/opt/ci-eval/gateway.py", "--bootstrap-fifo")
    docker("network", "connect", EGRESS, GATEWAY)
    bootstrap = {"base_url": model["base_url"], "api_key": model["api_key"],
                 "model_id": model["model_id"], "local_token": LOCAL_TOKEN, "max_requests": 256}
    injector = "import os,stat,sys,time; p='/run/gateway/bootstrap'; deadline=time.monotonic()+10\nwhile not os.path.exists(p) and time.monotonic()<deadline: time.sleep(.1)\nassert stat.S_ISFIFO(os.stat(p).st_mode)\nwith open(p,'w') as f: f.write(sys.stdin.read())"
    docker("exec", "-i", GATEWAY, "python3", "-c", injector, data=json.dumps(bootstrap).encode(), timeout=20)
    print(json.dumps({"gateway": GATEWAY, "secret_transport": "stdin to one-use tmpfs FIFO", "max_requests": 256,
                      "upstream_dns_pinned_by_host": args.pin_upstream_dns}))


def execute(args):
    runtime = Path(args.runtime).resolve()
    env = {**os.environ, "DOCKER_CONTEXT": CONTEXT, "CHRYS_EVAL_API_KEY": LOCAL_TOKEN,
           "TMPDIR": str(runtime.parent / "tmp")}
    if not Path(env["TMPDIR"]).is_dir():
        raise ValueError("missing shared temporary directory")
    forwarded = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    if not forwarded:
        raise ValueError("missing evaluator subcommand")
    return subprocess.call([sys.executable, "-B", str(ROOT / "eval.py"), *forwarded], env=env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    config = commands.add_parser("configure")
    config.add_argument("--model-profile", required=True)
    config.add_argument("--image", required=True)
    config.add_argument("--output", required=True)
    start = commands.add_parser("gateway")
    start.add_argument("--model-profile", required=True)
    start.add_argument("--runtime", required=True)
    start.add_argument("--pin-upstream-dns", action="store_true",
                       help="resolve only the configured upstream via host DNS and pin those addresses for this gateway lifetime")
    run = commands.add_parser("exec")
    run.add_argument("--runtime", required=True)
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        return {"configure": configure, "gateway": gateway, "exec": execute}[args.action](args) or 0
    except Exception as exc:
        # Error messages from credential-bearing transports are not safe to echo.
        print(json.dumps({"status": "environment_error", "error_type": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
