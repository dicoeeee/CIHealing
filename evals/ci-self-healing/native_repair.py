"""Native installed iCode/Chrys CLI runner; no Docker, global writes or model fallback.

The legacy config key 'chrys' holds the explicitly selected CLI executable.
Current iCode keeps the chrys Python package and runtime environment variables.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

from common import bind_json, HOST_CLOCK_ID, EvalError, changes, command, digest, json_bytes, read_json, read_regular, remaining_timeout, run_logged, snapshot, stop_owned_group, write_json, write_new
from host_sandbox import environment as child_env, policy
from repair_support import ADAPTER_FILES, RUNTIME_CONTRACT, execution_contract, delivery_contract, budget_notice, session_receipt
from suite import ROOT, load_suite, select_trials, skill_from_dir
from runtime_diagnostics import write_diagnostics
from resource_cleanup import OwnedResources, cleanup_complete
from native_clean import clean_build, maven_clean_plan

SETTINGS = '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"/>\n'
WORKER_FILES = (*ADAPTER_FILES, "host_worker.py")


def load_config(value):
    required = {"backend", "python", "chrys", "maven", "java_home", "maven_repository", "model_profile", "endpoint_host"}
    optional = {"timeout_seconds", "max_tool_calls", "rg", "git"}
    if "repair_max_output_tokens" in value:
        raise EvalError("repair_max_output_tokens is no longer supported; use the selected model profile unchanged in a new run")
    if required - value.keys() or value.keys() - required - optional or value["backend"] != "native":
        raise EvalError("native runtime requires backend/python/chrys/maven/java_home/maven_repository/model_profile/endpoint_host")
    config = dict(value)
    if not config.get("git"):
        found = shutil.which("git")
        clt = Path("/Library/Developer/CommandLineTools/usr/bin/git")
        config["git"] = str(clt) if found == "/usr/bin/git" and clt.is_file() else found
    if not config["git"]:
        raise EvalError("native repair requires a selected Git executable")
    config.setdefault("rg", shutil.which("rg"))
    if not config["rg"]:
        raise EvalError("native repair requires an explicitly available rg executable")
    for name in ("python", "chrys", "maven", "java_home", "maven_repository", "model_profile", "rg", "git"):
        path = Path(config[name])
        if not path.is_absolute():
            raise EvalError("native runtime paths must be absolute: " + name)
        config[name] = str(path.resolve()) if name in {"maven", "java_home", "rg", "git"} else str(path.absolute())
    for name in ("python", "chrys", "maven", "rg", "git"):
        if not Path(config[name]).is_file() or not os.access(config[name], os.X_OK):
            raise EvalError("missing executable: " + name)
    broad = {Path("/"), Path.home(), ROOT.parents[1]}
    if Path(config["python"]).parent.parent in broad or Path(config["java_home"]) in broad or Path(config["git"]).parent.parent in broad:
        raise EvalError("runtime read roots must not expose a workspace or home")
    cache = Path(config["maven_repository"])
    if ROOT / "runs" not in cache.parents or not cache.is_dir():
        raise EvalError("use an explicit dedicated Maven cache under evals/ci-self-healing/runs; never expose ~/.m2")
    config.setdefault("timeout_seconds", 900)
    config.setdefault("max_tool_calls", 80)
    for key, maximum in (("timeout_seconds", 1800), ("max_tool_calls", 200)):
        if type(config[key]) is not int or not 1 <= config[key] <= maximum:
            raise EvalError("invalid native runtime budget: " + key)
    return config


def generation_budget(model):
    """Record the selected profile's existing limit; never impose a repair cap."""
    profile_cap = model.get("max_output_tokens")
    if type(profile_cap) is not int or profile_cap <= 0:
        raise EvalError("invalid selected profile output limit")
    return {"contract": "profile-generation-v1", "profile_max_output_tokens": profile_cap,
            "effective_max_output_tokens": profile_cap}


def isolated_model(model, port, budget):
    selected = {k: model[k] for k in ("id", "name", "provider", "api_style", "model_id", "max_context_tokens", "stream") if k in model}
    selected.update(max_output_tokens=budget["effective_max_output_tokens"],
                    base_url=f"http://127.0.0.1:{port}/v1", api_key="{{CHRYS_EVAL_API_KEY}}", bypass_proxy=True, http_max_retries=1)
    return selected


def model_profile(config):
    import yaml
    raw = read_regular(config["model_profile"], 65536)
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        raise EvalError("invalid selected model profile YAML") from None
    if (not isinstance(data, dict) or data.get("name") != "huoshan-glm5.2" or data.get("model_id") != "glm-5.2"
            or data.get("provider") != "openai" or data.get("api_style") != "responses"
            or data.get("http_headers") or data.get("chat_options")):
        raise EvalError("native repair requires the explicitly selected GLM 5.2 Responses profile")
    url = urlsplit(data.get("base_url", ""))
    if (url.scheme != "https" or url.hostname != config["endpoint_host"] or url.username or url.password
            or url.query or url.fragment or url.port not in (None, 443)):
        raise EvalError("selected model gateway differs from approved HTTPS endpoint")
    return data, digest(raw)


def layout(trial):
    trial = Path(trial).resolve()
    return {"visible": str(trial / "visible"), "skill": str(trial / "runtime/skills/ci-self-healing"),
            "config": str(trial / "runtime/config"), "sessions": str(trial / "native-session"),
            "task": str(trial / "adapter/task.txt")}


def setup(config, trial, skill_source, resources=None):
    trial = Path(trial)
    info = layout(trial)
    info["cli"] = config["chrys"]
    for folder in ("runtime/config", "runtime/tmp", "runtime/java-home", "native-session", "adapter"):
        if resources is not None and folder in {"runtime/tmp", "runtime/java-home"}:
            resources.mkdir(folder)
        else:
            (trial / folder).mkdir(parents=True, exist_ok=False, mode=0o700)
    if resources is not None:
        resources.mkdir("runtime/m2")
    shutil.copytree(config["maven_repository"], trial / "runtime/m2", dirs_exist_ok=resources is not None)
    for name, data in skill_from_dir(skill_source).items():
        write_new(Path(info["skill"]) / name, data)
    write_new(trial / "runtime/settings.xml", SETTINGS)
    for name in WORKER_FILES:
        write_new(trial / "adapter" / name, read_regular(ROOT / name))
    return info


def tool_identity(config):
    # Only benign version queries, with an explicit JDK and sanitized environment.
    with tempfile.TemporaryDirectory(prefix="ci-native-version-") as temp:
        runtime = Path(temp).resolve()
        for name in ("tmp", "java-home"):
            (runtime / name).mkdir()
        env = child_env(config, runtime)
        cache = snapshot(config["maven_repository"], skip_git=False)
        if any(row["kind"] == "symlink" for row in cache.values()):
            raise EvalError("dedicated Maven cache must not contain symlinks")
        return {"chrys_version": command([config["chrys"], "--version"], env=env).decode().strip(),
                "java_version": command([str(Path(config["java_home"]) / "bin/java"), "--version"], env=env).decode().strip(),
                "maven_version": command([config["maven"], "--version"], env=env).decode().strip(),
                "git_path": config["git"], "git_version": command([config["git"], "--version"], env=env).decode().strip(),
                "git_sha256": digest(Path(config["git"]).read_bytes()),
                "cache_sha256": digest(json_bytes(cache))}


def doctor(config, output=None):
    if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise EvalError("native repair needs macOS sandbox-exec")
    versions = tool_identity(config)
    context = tempfile.TemporaryDirectory(prefix="ci-native-doctor-", dir=ROOT / "runs") if output is None else None
    directory = Path(context.name).resolve() if context else Path(output).resolve()
    try:
        info = setup(config, directory / "probe", ROOT.parents[1] / "skills/ci-self-healing")
        trial = directory / "probe"
        (trial / "visible/scene/sourcecode").mkdir(parents=True)
        write_json(trial / "adapter/layout.json", info)
        probe_read = [trial / "adapter", info["skill"], trial / "visible"]
        probe_write = [trial / "runtime/config", trial / "runtime/tmp", trial / "runtime/java-home", trial / "native-session"]
        write_new(trial / "adapter/offline.sb", policy(config, read=probe_read, write=probe_write, pty=True))
        env = child_env(config, trial / "runtime")
        argv = ["/usr/bin/sandbox-exec", "-f", str(trial / "adapter/offline.sb"), config["python"], "-B",
                str(trial / "adapter/host_worker.py"), str(trial / "adapter/layout.json"), "--preflight"]
        result = run_logged(argv, trial / "execution", 45, env=env, cwd=trial / "visible/scene/sourcecode")
        if result["status"] != "completed" or result["returncode"] != 0:
            raise EvalError("native capability/sandbox probe failed; no model called; " + str(trial / "execution"))
        capabilities = read_json(trial / "execution/stdout.log")
        if capabilities.get("status") != "passed" or capabilities.get("chrys_version") != versions["chrys_version"]:
            raise EvalError("CLI and Python Chrys runtime differ")
        # Guard against a false-positive preflight: the same real shell tool
        # must fail without PTY permission, before a gateway/model is started.
        write_new(trial / "adapter/no-pty.sb", policy(config, read=probe_read, write=probe_write))
        negative_script = (
            "import asyncio,sys; sys.path.insert(0,sys.argv[1]); "
            "from host_worker import bootstrap; bootstrap(sys.argv[2]); "
            "from chrys_preflight import check_shell; from common import EvalError\n"
            "try: asyncio.run(check_shell())\n"
            "except EvalError as e:\n"
            " assert str(e).startswith('native Chrys shell failed'); print('shell-negative-ready')\n"
            "else: raise AssertionError('shell probe passed without PTY permission')")
        negative = run_logged(["/usr/bin/sandbox-exec", "-f", str(trial / "adapter/no-pty.sb"), config["python"], "-B", "-c", negative_script,
                               str(trial / "adapter"), info["config"]],
                              trial / "shell-negative", 20, env=env, cwd=trial / "visible/scene/sourcecode")
        if negative["status"] != "completed" or negative["returncode"] != 0:
            raise EvalError("native shell negative probe failed")
        # Child processes must inherit denial of sibling evidence and global files.
        forbidden = directory / "forbidden.txt"
        write_new(forbidden, "synthetic-boundary-canary")
        canary = "import pathlib,subprocess,sys; p=pathlib.Path(sys.argv[1]); failures=0\nfor action in (lambda:p.read_text(),lambda:p.write_text('changed')):\n try: action()\n except PermissionError: failures+=1\nassert failures==2\nr=subprocess.run(['/bin/cat',str(p)],capture_output=True); assert r.returncode!=0; print('boundary-ready')"
        check = run_logged(["/usr/bin/sandbox-exec", "-f", str(trial / "adapter/offline.sb"), config["python"], "-B", "-c", canary, str(forbidden)],
                           trial / "boundary", 15, env=env, cwd=trial / "visible/scene/sourcecode")
        if check["returncode"] != 0 or read_regular(forbidden) != b"synthetic-boundary-canary":
            raise EvalError("native sandbox boundary probe failed")
        # An unrelated synthetic host PTY remains open so its device cannot be
        # recycled. The child must use its own PTY but cannot open this one.
        master, slave = os.openpty()
        try:
            script = ("import os,sys; m,s=os.openpty()\n"
                      "try:\n"
                      " assert os.isatty(m) and os.isatty(s)\n"
                      " os.write(s,b'CI_EVAL_PTY_READY\\n'); assert b'CI_EVAL_PTY_READY' in os.read(m,128)\n"
                      "finally: os.close(s); os.close(m)\n"
                      "for flag in (os.O_RDONLY,os.O_WRONLY,os.O_RDWR):\n"
                      " try: fd=os.open(sys.argv[1],flag|os.O_NOCTTY|os.O_NONBLOCK)\n"
                      " except PermissionError: pass\n"
                      " else: os.close(fd); raise AssertionError('unrelated PTY allowed')\n"
                      "print('pty-boundary-ready')")
            pty_check = run_logged(["/usr/bin/sandbox-exec", "-f", str(trial / "adapter/offline.sb"), config["python"], "-B", "-c", script, os.ttyname(slave)],
                                   trial / "pty-boundary", 15, env=env, cwd=trial / "visible/scene/sourcecode")
            if pty_check["status"] != "completed" or pty_check["returncode"] != 0:
                raise EvalError("native own/unrelated PTY boundary probe failed")
        finally:
            os.close(slave)
            os.close(master)
        with socket.socket() as allowed, socket.socket() as denied:
            allowed.bind(("127.0.0.1", 0)); allowed.listen(1)
            denied.bind(("127.0.0.1", 0)); denied.listen(1)
            port, other = allowed.getsockname()[1], denied.getsockname()[1]
            write_new(trial / "adapter/network.sb", policy(config, read=[trial / "adapter", info["skill"], trial / "visible"],
                       write=[trial / "runtime/tmp", trial / "runtime/java-home"], port=port))
            script = ("import pathlib,socket,sys; "
                      "s=socket.create_connection(('127.0.0.1',int(sys.argv[1])),timeout=2); s.close()\n"
                      "try: socket.create_connection(('127.0.0.1',int(sys.argv[2])),timeout=2)\n"
                      "except PermissionError: pass\nelse: raise AssertionError('unrelated network allowed')\n"
                      "try: pathlib.Path(sys.argv[3]).write_text('changed')\n"
                      "except PermissionError: pass\nelse: raise AssertionError('skill write allowed')\n"
                      "print('network-and-skill-boundary-ready')")
            network = run_logged(["/usr/bin/sandbox-exec", "-f", str(trial / "adapter/network.sb"), config["python"], "-B", "-c", script,
                                  str(port), str(other), str(Path(info["skill"]) / "SKILL.md")],
                                 trial / "network-boundary", 15, env=env, cwd=trial / "visible/scene/sourcecode")
            if network["status"] != "completed" or network["returncode"] != 0:
                raise EvalError("native loopback/readonly Skill boundary probe failed")
        # Do not bind a random temporary probe directory into suite identity.
        capabilities["skill"].pop("source", None)
        return {"backend": "native", **versions, "capabilities": capabilities, "boundary_canary": "passed",
                "network_canary": "passed", "pty_canary": "passed", "shell_negative_canary": "passed",
                "model_connectivity": "not_tested"}
    finally:
        if context:
            context.cleanup()


@contextmanager
def gateway(config, directory, expected_profile_sha256):
    directory = Path(directory)
    directory.mkdir(mode=0o700)
    token = secrets.token_urlsafe(32)
    binding = {"trial_id": directory.parent.name, "gateway_id": secrets.token_hex(16), "clock_id": HOST_CLOCK_ID}
    write_json(directory / "binding.json", binding)
    with (directory / "process.log").open("xb") as log:
        process = subprocess.Popen([config["python"], "-B", str(ROOT / "host_gateway.py")],
                                   stdin=subprocess.PIPE, stdout=log, stderr=log, start_new_session=True)
        try:
            request = {"config": config, "profile_sha256": expected_profile_sha256,
                       "local_token": token, "ready": str(directory / "ready.json"), "event_binding": binding}
            process.stdin.write((json.dumps(request) + "\n").encode())
            process.stdin.close()
            deadline = time.monotonic() + remaining_timeout(15)
            while not (directory / "ready.json").exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise EvalError("native gateway startup failed; inspect safe process log")
                time.sleep(0.05)
            yield read_json(directory / "ready.json")["port"], token
        finally:
            write_json(directory / "cleanup.json", stop_owned_group(process))


def probe_build(config, source, output, case_id=None):
    from cases import oracle
    output, source = Path(output).absolute(), Path(source).absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or (output.parent / (output.name + "-workspace")).exists():
        raise EvalError("build already started; preserve historical artifacts")
    evidence = output.parent / (output.name + "-evidence")
    clean_output = output.parent / (output.name + "-clean")
    with OwnedResources(output.parent, output.name,
                        evidence=(output.name, evidence.name, clean_output.name)) as resources:
        workspace = resources.mkdir(output.name + "-workspace")
        evidence.mkdir(mode=0o700)
        for folder in ("tmp", "java-home"):
            (workspace / folder).mkdir()
        # Preserve exact inputs outside the disposable copy, before executing them.
        source_files = snapshot(source)  # Access/type checks precede copying candidate content.
        if any(row["kind"] == "symlink" for row in source_files.values()):
            raise EvalError("verification inputs cannot contain symlinks")
        shutil.copytree(source, evidence / "source", ignore=shutil.ignore_patterns(".git", "target"))
        shutil.copytree(evidence / "source", workspace / "source")
        cache_before = snapshot(config["maven_repository"], skip_git=False)
        shutil.copytree(config["maven_repository"], workspace / "m2")
        for name, data in {"settings.xml": SETTINGS, "build_probe.py": read_regular(ROOT / "build_probe.py"),
                           **({"ContractProbe.java": oracle(case_id)} if case_id else {})}.items():
            write_new(evidence / name, data)
            write_new(workspace / name, data)
        write_json(evidence / "inputs.json", {"source": snapshot(evidence / "source"),
                   "cache_source": config["maven_repository"], "cache": cache_before,
                   "case_id": case_id, "tools": {k: config[k] for k in ("python", "maven", "java_home")}})
        # Readback is required before any later cleanup may discard the copies.
        if read_json(evidence / "inputs.json")["cache"] != cache_before:
            raise EvalError("build input preservation failed")
        write_new(evidence / "sandbox.sb", policy(config, write=[workspace]))
        argv = ["/usr/bin/sandbox-exec", "-f", str(evidence / "sandbox.sb"), config["python"], "-B",
                str(workspace / "build_probe.py"), "--native-root", str(workspace)]
        if not case_id:
            argv.append("--baseline")
        write_json(evidence / "invocation.json", {"argv": argv, "cwd": str(workspace / "source")})
        resources.required = [str(Path(output.name) / "stdout.log"), str(Path(output.name) / "process.json"),
                              str(Path(evidence.name) / "inputs.json"), str(Path(evidence.name) / "post-build-source.json")]
        resources.hold_reason = "build evidence not sealed"
        resources.watch(output / "process.json")
        interrupted = False
        try:
            process = run_logged(argv, output, 180, env=child_env(config, workspace), cwd=workspace / "source")
        except KeyboardInterrupt:
            process = read_json(output / "process.json")
            interrupted = True
        try:
            result = read_json(output / "stdout.log")
        except (ValueError, OSError, EvalError):
            result = {"status": "incomplete", "checks": {}}
        result["process"] = process
        if process["status"] != "completed" or process["returncode"] not in {0, 1}:
            result["status"] = "incomplete"
        # Capture actual outputs and retain changed original inputs before clean.
        # A read/type/access failure preserves the disposable tree for diagnosis.
        observed = snapshot(workspace / "source", skip_git=False)
        write_json(evidence / "post-build-source.json", observed)
        input_drift = []
        for name, before in read_json(evidence / "inputs.json")["source"].items():
            after = observed.get(name)
            if before != after:
                input_drift.append(name)
            if before != after and after and after["kind"] == "file":
                content = read_regular(workspace / "source" / name)
                retained = evidence / "modified-inputs" / name
                write_new(retained, content)
                if read_regular(retained) != content:
                    raise EvalError("modified build input preservation failed")
        if input_drift:
            result.update(status="incomplete", verification_input_drift=input_drift)
        if read_json(evidence / "post-build-source.json") != observed:
            raise EvalError("post-build evidence readback failed")
        resources.hold_reason = None
        # Native clean is preferred only in this isolated, wholly disposable copy.
        # Missing offline dependencies never authorize a network fallback.
        native_clean = clean_build(config, workspace, clean_output, resources,
                                   plan=maven_clean_plan(config, workspace))
        result["native_clean"] = native_clean
        resources.notes.append(native_clean)
        if snapshot(config["maven_repository"], skip_git=False) != cache_before:
            raise EvalError("shared Maven cache changed during isolated verification")
        result["resource_cleanup"] = resources.finish()
        if interrupted:
            raise KeyboardInterrupt()
        return result


def run_trials(root, config, trial_id=None):
    root = Path(root).resolve()
    manifest = load_suite(root)
    if manifest.get("backend") != "native":
        raise EvalError("native backend requires a freshly prepared native suite")
    environment = doctor(config)
    model, profile_hash = model_profile(config)
    budget = generation_budget(model)
    binding = {"backend": "native", "environment": environment, "config": config, "profile_sha256": profile_hash}
    path = root / "runtime-binding.json"
    bind_json(path, binding, "native runtime drift; create a new suite")
    summaries = []
    for row in select_trials(manifest, trial_id):
        trial = root / "trials" / row["id"]
        if (trial / "baseline-build").exists() or (trial / "run.json").exists():
            raise EvalError("trial already started; do not replay")
        if snapshot(trial / "visible") != read_json(trial / "initial.json"):
            raise EvalError("native fixture drift")
        with OwnedResources(trial, "run", evidence=("visible", "adapter", "execution", "preflight",
                            "gateway", "native-session", "session-receipt.json", "runtime-diagnostics.json",
                            "runtime/skills", "runtime/config", "runtime/settings.xml")) as resources:
            baseline = probe_build(config, trial / "visible/scene/sourcecode", trial / "baseline-build")
            write_json(trial / "baseline-build.json", baseline)
            if baseline["status"] != "passed" or not cleanup_complete(baseline):
                write_json(trial / "run.json", {"status": "fixture_not_ready", "environment": environment, "resource_cleanup": resources.finish(),
                                               "baseline_resource_cleanup": baseline["resource_cleanup"]})
                summaries.append({"id": row["id"], "status": "fixture_not_ready"})
                continue
            log = baseline["checks"]["maven"]["stdout"] + baseline["checks"]["maven"]["stderr"]
            for name in ("all_logs.txt", "error_logs.txt"):
                write_new(trial / "visible/scene" / name, log)
            write_new(trial / "visible/scene/log_origin.txt", "Baseline logs are from an unchanged isolated copy.\n")
            write_json(trial / "before-run.json", snapshot(trial / "visible"))
            info = setup(config, trial, root / "skills" / row["version"], resources)
            audit_contract = execution_contract(info["skill"])
            # A model-free worker preflight gets its own config/session directories.
            pre_info = {**info, "config": str(trial / "preflight-state/config"), "sessions": str(trial / "preflight-state/sessions")}
            write_json(trial / "adapter/preflight-layout.json", pre_info)
            write_new(trial / "adapter/preflight.sb", policy(config, read=[trial / "adapter", info["skill"], trial / "visible"],
                       write=[trial / "preflight-state", trial / "runtime/tmp", trial / "runtime/java-home"], pty=True))
            args = ["/usr/bin/sandbox-exec", "-f", str(trial / "adapter/preflight.sb"), config["python"], "-B",
                    str(trial / "adapter/host_worker.py"), str(trial / "adapter/preflight-layout.json"), "--preflight"]
            resources.watch(trial / "preflight/process.json")
            pre = run_logged(args, trial / "preflight", 45, env=child_env(config, trial / "runtime"), cwd=trial / "visible/scene/sourcecode")
            if pre["returncode"] != 0:
                write_json(trial / "run.json", {"status": "preflight_failed", "environment": environment, "resource_cleanup": resources.finish(),
                                               "baseline_resource_cleanup": baseline["resource_cleanup"]})
                summaries.append({"id": row["id"], "status": "preflight_failed"})
                continue
            interrupted = False
            resources.watch(trial / "gateway/cleanup.json", group_receipt=True)
            with gateway(config, trial / "gateway", profile_hash) as (port, token):
                gateway_binding = read_json(trial / "gateway/binding.json")
                selected = isolated_model(model, port, budget)
                info.update(model=selected, max_tools=config["max_tool_calls"], clock_id=gateway_binding["clock_id"])
                write_json(trial / "adapter/layout.json", info)
                timeout = remaining_timeout(config["timeout_seconds"])
                task = read_regular(trial / "visible/task.txt").decode() + budget_notice(timeout)
                write_new(trial / "adapter/task.txt", task)
                write_new(trial / "adapter/run.sb", policy(config, read=[trial / "adapter", info["skill"], trial / "runtime/settings.xml"],
                           write=[trial / "visible", trial / "runtime/config", trial / "runtime/m2", trial / "runtime/tmp",
                                  trial / "runtime/java-home", trial / "native-session"], port=port, pty=True))
                env = {**child_env(config, trial / "runtime"), "CHRYS_EVAL_API_KEY": token}
                args = ["/usr/bin/sandbox-exec", "-f", str(trial / "adapter/run.sb"), config["python"], "-B",
                        str(trial / "adapter/host_worker.py"), str(trial / "adapter/layout.json")]
                resources.watch(trial / "execution/process.json")
                try:
                    process = run_logged(args, trial / "execution", timeout, env=env, cwd=trial / "visible/scene/sourcecode", grace_seconds=5,
                                         identity={k: gateway_binding[k] for k in ("trial_id", "gateway_id")})
                except KeyboardInterrupt:
                    process = read_json(trial / "execution/process.json")
                    interrupted = True
                finally:
                    write_json(trial / "session-receipt.json", session_receipt(trial / "native-session"))
            after = snapshot(trial / "visible")
            write_json(trial / "after-run.json", after)
            diagnostic = write_diagnostics(trial, trial / "native-session", process, public_log=trial / "execution/stdout.log",
                                           gateway_log=trial / "gateway/process.log",
                                           gateway_binding=gateway_binding,
                                           changed_paths=changes(read_json(trial / "before-run.json"), after))
            gateway_clean = read_json(trial / "gateway/cleanup.json")["status"] == "confirmed"
            receipt = {"status": process["status"], "process": process, "environment": environment,
                       "backend": "native", "runtime_contract": RUNTIME_CONTRACT, "expected_model_id": model["model_id"],
                       "agent": "EvalCode", "skill_sha256": row["skill_sha256"], "filesystem_status": "captured",
                       "skill_execution_contract": audit_contract,
                       "delivery_contract": delivery_contract(info["skill"]),
                       "task_sha256": digest(task.encode()), "stdout_sha256": digest(read_regular(trial / "execution/stdout.log")),
                       "runtime_binding_sha256": digest(json_bytes(binding)), "profile_snapshot_sha256": profile_hash,
                       "session_receipt_sha256": digest(read_regular(trial / "session-receipt.json")),
                       "gateway_cleanup_confirmed": gateway_clean, "isolation_confirmed": gateway_clean and process.get("cleanup", {}).get("status") == "confirmed",
                       "runtime_diagnostics": diagnostic,
                       "diagnostics_sha256": digest(read_regular(trial / "runtime-diagnostics.json")),
                       "generation_budget": budget, "baseline_resource_cleanup": baseline["resource_cleanup"],
                       "budgets": {k: config[k] for k in ("timeout_seconds", "max_tool_calls")}}
            from generation_budget import generation_failure
            if process["status"] == "completed":
                gap = generation_failure(receipt)
                if gap:
                    receipt.update(status="incomplete", generation_gap=gap)
            from delivery_evidence import assess_delivery
            receipt["delivery_assessment"] = assess_delivery(trial, receipt)
            receipt["resource_cleanup"] = resources.finish()
            write_json(trial / "run.json", receipt)
            summaries.append({"id": row["id"], "status": receipt["status"]})
            if interrupted:
                raise KeyboardInterrupt()
    return summaries
