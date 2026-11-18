"""Bounded iCode execution and observation, independent of grading contracts.

Run live/preflight commands with runtime['python']. Only this trusted parent
reads model profiles. Isolated workers receive a loopback capability token.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from common import EvalError, command, digest, json_bytes, read_regular, run_logged, snapshot, stop_owned_group, write_json, write_new
from host_sandbox import environment, policy

from . import worker


SETTINGS = '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"/>\n'
PATH_KEYS = ("python", "chrys", "maven", "java_home", "maven_repository", "rg", "git")


def _config(runtime):
    if not isinstance(runtime, dict):
        raise ValueError("runtime must be an object")
    result = dict(runtime)
    for key in PATH_KEYS:
        path = Path(result.get(key, ""))
        if not path.is_absolute() or not path.exists():
            raise ValueError("missing absolute runtime path: " + key)
        if key not in {"java_home", "maven_repository"} and not os.access(path, os.X_OK):
            raise ValueError("runtime executable unavailable: " + key)
        result[key] = str(path)
    broad = {Path("/"), Path.home()}
    if Path(result["python"]).parent.parent in broad or Path(result["maven_repository"]) in broad:
        raise ValueError("runtime/cache root is too broad")
    if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise ValueError("native isolation requires macOS sandbox-exec")
    return result


def _profile(runtime, role):
    import yaml
    prefix = "execution" if role == "execute" else "review"
    path = Path(runtime[prefix + "_profile"])
    if not path.is_absolute():
        raise ValueError("model profile path must be absolute")
    raw = read_regular(path, 65536)
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError:
        raise ValueError("invalid model profile YAML") from None
    expected = runtime.get(prefix + "_model_id")
    if (not isinstance(value, dict) or not expected or value.get("model_id") != expected
            or value.get("provider") not in {"openai", "deepseek-openai"}
            or value.get("api_style") != "responses" or value.get("http_headers") or value.get("chat_options")):
        raise ValueError("configured model identity/options mismatch")
    url = urlsplit(value.get("base_url", ""))
    allowed_host = runtime.get(prefix + "_endpoint_host", runtime.get("endpoint_host"))
    if (url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment
            or url.port not in {None, 443} or (allowed_host and url.hostname != allowed_host)):
        raise ValueError("invalid or unapproved model endpoint")
    cap = value.get("max_output_tokens")
    if type(cap) is not int or cap <= 0:
        raise ValueError("model profile output limit must be positive")
    public = {"profile_sha256": hashlib.sha256(raw).hexdigest(), "profile_name": value.get("name"),
              "requested_model_id": expected, "provider": value["provider"], "api_style": value["api_style"],
              "max_output_tokens": cap}
    return value, public


def _resolve_secret(value):
    from chrys.foundation.util.env_templates import resolve_env_templates
    result = resolve_env_templates(value, location="evaluation model profile")
    if not isinstance(result, str) or not result:
        raise ValueError("selected model credential unavailable")
    return result


@contextmanager
def _gateway(profile, directory, timeout_seconds=10):
    directory.mkdir(parents=True, exist_ok=False)
    token = secrets.token_urlsafe(32)
    bootstrap = {"base_url": profile["base_url"], "api_key": _resolve_secret(profile.get("api_key", "")),
                 "model_id": profile["model_id"], "local_token": token,
                 "max_requests": 256, "max_output_tokens": profile["max_output_tokens"]}
    if "_review_wire_format" in profile:
        bootstrap["review_wire_format"] = profile["_review_wire_format"]
    # This trusted process owns all HTTP handler threads. Killing its process
    # group closes active upstream sockets too, unlike stopping an accept thread.
    script = (
        "import json,pathlib,sys\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "from environment.gateway import make_server\n"
        "try:\n"
        " config=json.loads(sys.stdin.buffer.readline(65537))\n"
        " server=make_server(config,host='127.0.0.1',port=0)\n"
        " with pathlib.Path(sys.argv[2]).open('x') as f: json.dump({'port':server.server_address[1]},f)\n"
        " server.serve_forever(poll_interval=0.05)\n"
        "except Exception as exc:\n"
        " print(json.dumps({'gateway_error_type':type(exc).__name__}),flush=True);sys.exit(2)\n"
    )
    helper, ready = directory / "entry.py", directory / "ready.json"
    write_new(helper, script)
    log = (directory / "events.jsonl").open("xb")
    env = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
    process = subprocess.Popen([sys.executable, "-B", str(helper), str(Path(__file__).resolve().parents[1]), str(ready)],
                               stdin=subprocess.PIPE, stdout=log, stderr=log, env=env, start_new_session=True)
    try:
        process.stdin.write(json.dumps(bootstrap).encode() + b"\n")
        process.stdin.close()
        deadline = time.monotonic() + min(10, max(0.01, timeout_seconds))
        while not ready.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("trusted model gateway did not start")
            time.sleep(0.02)
        port = json.loads(read_regular(ready))["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid gateway ready port")
        yield port, token
    finally:
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
        cleanup = stop_owned_group(process)
        log.close()
        write_json(directory / "cleanup.json", cleanup)


def _isolated_model(profile, port, *, role="execute", review_contract=None):
    allowed = {"id", "name", "provider", "api_style", "model_id", "max_context_tokens", "max_output_tokens", "stream"}
    model = {key: value for key, value in profile.items() if key in allowed}
    model.update(base_url=f"http://127.0.0.1:{port}/v1", api_key="{{CI_EVAL_MODEL_TOKEN}}",
                 bypass_proxy=True, http_max_retries=0)
    if role == "judge":
        from .review_schema import response_format
        # Use only the harness-owned contract; never forward profile.chat_options.
        model["chat_options"] = json.dumps({"response_format": response_format(review_contract)}, ensure_ascii=False, sort_keys=True)
    return model


def _within(trial, relative):
    path = trial / relative
    if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("fixture path must be relative to its trial")
    if path.resolve() != path or trial not in path.parents:
        raise ValueError("fixture path escapes its trial")
    return path


def _prepare_runtime(directory, runtime, *, copy_cache):
    for name in ("config", "sessions", "tmp", "java-home", "adapter"):
        (directory / name).mkdir(parents=True, exist_ok=False, mode=0o700)
    write_new(directory / "adapter/worker.py", Path(worker.__file__).read_bytes())
    write_new(directory / "adapter/command_receipts.py", Path(worker.command_receipts.__file__).read_bytes())
    write_new(directory / "settings.xml", SETTINGS)
    if copy_cache:
        # The source cache is never writable to the worker. Reject links before copying.
        cache = Path(runtime["maven_repository"])
        if any(path.is_symlink() for path in cache.rglob("*")):
            raise ValueError("dedicated Maven cache contains symlinks")
        shutil.copytree(cache, directory / "m2")


def _tool_identity(runtime, directory, *, deadline=None):
    """Only explicit executable versions and digests; no profile or environment dump."""
    commands = {"python": [runtime["python"], "--version"], "icode": [runtime["chrys"], "--version"],
                "java": [str(Path(runtime["java_home"]) / "bin/java"), "--version"],
                "maven": [runtime["maven"], "--version"], "git": [runtime["git"], "--version"],
                "rg": [runtime["rg"], "--version"]}
    result = {}
    for name, argv in commands.items():
        path = Path(argv[0]).resolve()
        remaining = 15 if deadline is None else min(15, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("identity inspection budget exhausted")
        output = command(argv, env=environment(runtime, directory), cwd=directory / "tmp", timeout=remaining).decode(errors="replace").strip()
        result[name] = {"path": argv[0], "resolved_path": str(path), "executable_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "version": output[:2048]}
    result["maven_cache_sha256"] = digest(json_bytes(snapshot(runtime["maven_repository"], skip_git=False)))
    if deadline is not None and time.monotonic() > deadline:
        raise TimeoutError("identity inspection budget exhausted")
    return result


def _assert_binding(runtime, role, profile_identity, tool_identity):
    """Reject a changed profile, binary, version, or cache before model startup."""
    if "_frozen_identity" not in runtime:
        return
    frozen = runtime["_frozen_identity"]
    key = "execution_model" if role == "execute" else "review_model"
    if (not isinstance(frozen, dict) or not isinstance(frozen.get(key), dict)
            or not isinstance(frozen.get("runtime_identity"), dict)):
        raise ValueError("missing or malformed frozen runtime identity")
    if profile_identity != frozen[key] or tool_identity != frozen["runtime_identity"]:
        raise ValueError("identity_drift: runtime differs from batch preflight")


def _finish_binding(result, runtime, role, profile_before, tools_before, directory, deadline):
    """Keep completed execution distinct from evidence that its identity stayed fixed."""
    binding = {"scope": "batch-preflight" if "_frozen_identity" in runtime else "session-local",
               "profile_before": profile_before, "tools_before_sha256": digest(json_bytes(tools_before)),
               "status": "unconfirmed"}
    try:
        _, profile_after = _profile(runtime, role)
        tools_after = _tool_identity(runtime, directory, deadline=deadline)
        binding.update(profile_after=profile_after, tools_after_sha256=digest(json_bytes(tools_after)))
        changed = profile_after != profile_before or tools_after != tools_before
        try:
            _assert_binding(runtime, role, profile_after, tools_after)
        except ValueError:
            changed = True
        binding["status"] = "drift" if changed else "stable"
        if changed:
            result["gaps"].append("identity_drift")
            result["trace_complete"] = False
    except Exception as exc:
        binding["error_type"] = type(exc).__name__
        result["gaps"].append("identity_recheck_incomplete")
        result["trace_complete"] = False
    result["identity_binding"] = binding
    result["gaps"] = sorted(set(result["gaps"]))


def _session_identity(sessions, expected, response_expected=None, *, allow_tools=False):
    identities, exchanges, gaps = [], [], []
    for path in sessions.rglob("session.json"):
        data = json.loads(read_regular(path))
        meta = data.get("meta", {})
        identities.append({key: meta.get(key) for key in ("session_id", "model_id", "model_profile_id", "agent_profile_fingerprint", "app_version")})
        trace = path.parent / "trajectory/events.jsonl"
        if trace.exists():
            for line in read_regular(trace, 32 * 1024 * 1024).splitlines():
                event = json.loads(line)
                if event.get("event_type") == "model.exchange.finished":
                    payload = event.get("payload", {})
                    exchanges.append({key: payload.get(key) for key in ("response_model", "outcome", "finish_reason", "response_id")})
    confirmed = len(identities) == 1 and identities[0].get("model_id") == expected and bool(identities[0].get("session_id"))
    if not confirmed:
        gaps.append("runtime_model_identity_unconfirmed")
    response_models = sorted({row["response_model"] for row in exchanges if isinstance(row.get("response_model"), str)})
    response_confirmed = bool(exchanges) and all(row.get("response_model") == (response_expected or expected) for row in exchanges)
    if not response_confirmed:
        gaps.append("response_model_identity_unconfirmed")
    allowed_reasons = {None, "stop", "tool_calls", "function_call"} if allow_tools else {None, "stop"}
    if not exchanges or any(row.get("outcome") != "success" or row.get("finish_reason") not in allowed_reasons for row in exchanges):
        gaps.append("native_model_exchange_incomplete")
    return {"confirmed": confirmed, "expected_model_id": expected, "sessions": identities,
            "response_models": response_models, "expected_response_model_id": response_expected or expected,
            "response_model_confirmed": response_confirmed, "gaps": gaps,
            "boundary": "native session/exchange metadata, not backend attestation"}


def _collect(process, log_directory, sessions, profile_identity, role, runtime, output):
    rows, gaps = [], []
    for line in read_regular(log_directory / "stdout.log", 32 * 1024 * 1024).decode().splitlines():
        if not line.startswith(worker.PREFIX):
            if line.strip():
                gaps.append("unstructured_worker_stdout")
            continue
        try:
            row = json.loads(line[len(worker.PREFIX):])
            if row.get("seq") != len(rows) + 1:
                gaps.append("public_event_sequence_gap")
            rows.append(row)
        except (ValueError, AttributeError):
            gaps.append("invalid_public_event")
    events_path = output / "events.jsonl"
    write_new(events_path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    finished = [row["payload"] for row in rows if row.get("kind") == "observer.finished"]
    if len(finished) != 1 or not rows or rows[0].get("kind") != "observer.started" or rows[-1].get("kind") != "observer.finished":
        gaps.append("public_trace_incomplete")
    terminal = finished[0] if len(finished) == 1 else {}
    gaps.extend(terminal.get("gaps", []))
    identity = _session_identity(sessions, profile_identity["requested_model_id"],
                                 runtime.get("review_response_model_id") if role == "judge" else runtime.get("execution_response_model_id"),
                                 allow_tools=role == "execute")
    gaps.extend(identity["gaps"])
    observed = terminal.get("observed_session_ids", [])
    if identity["confirmed"] and (terminal.get("session_id") != identity["sessions"][0]["session_id"]
                                   or observed != [identity["sessions"][0]["session_id"]]):
        gaps.append("public_session_identity_unconfirmed")
    completed = process.get("status") == "completed" and process.get("returncode") == 0
    if not completed:
        gaps.append("worker_execution_incomplete")
    if process.get("cleanup", {}).get("status") != "confirmed":
        gaps.append("process_cleanup_unconfirmed")
    gateway_cleanup = output / "gateway/cleanup.json"
    if gateway_cleanup.exists() and json.loads(read_regular(gateway_cleanup)).get("status") != "confirmed":
        gaps.append("gateway_cleanup_unconfirmed")
    result = {"status": "timeout" if process.get("status") == "timeout" else "completed" if completed else "error",
              "answer": terminal.get("answer", ""), "trace_complete": completed and not gaps,
              "model_identity": {**profile_identity, **identity}, "duration_seconds": process.get("duration_seconds"),
              "events_file": str(events_path), "gaps": sorted(set(gaps)), "process": process,
              "evidence_files": [str(events_path), str(log_directory / "process.json")],
              "observation_boundary": "public events and tool-boundary snapshots; not a syscall audit"}
    result["evidence_files"].extend(str(path) for path in (output / "gateway/events.jsonl", gateway_cleanup) if path.exists())
    return result


def _seconds(value):
    if type(value) not in {int, float} or not 0 < value <= 900:
        raise ValueError("stage timeout must be within 900 seconds")
    return float(value)


def execute(trial, runtime, timeout_seconds):
    """Execute one already-prepared fixture. No retry, grading, or suite selection."""
    from .fixtures import service
    started = time.monotonic()
    timeout = _seconds(timeout_seconds)
    runtime = _config(runtime)
    trial = Path(trial).resolve()
    fixture = json.loads(read_regular(trial / "fixture.json"))
    directory = trial / "runtime"
    output = trial / "host"
    output.mkdir(parents=True, exist_ok=True)
    if (output / "execution.json").exists() or (directory / "config").exists():
        raise ValueError("trial already started; automatic replay forbidden")
    profile, identity = _profile(runtime, "execute")
    _prepare_runtime(directory, runtime, copy_cache=True)
    runtime_identity = _tool_identity(runtime, directory, deadline=started + timeout)
    _assert_binding(runtime, "execute", identity, runtime_identity)
    cwd = _within(trial, fixture.get("cwd", fixture["source_dir"]))
    writable = [_within(trial, value) for value in fixture["writable_roots"]]
    visible, skill = trial / "visible", directory / "skills/ci-self-healing"
    if not skill.is_dir():
        raise ValueError("frozen Skill is missing")
    from .core import tree_digest
    skill_before = tree_digest(skill)
    with service(trial, runtime) as services:
        ports = services.get("ports", [])
        if any(type(port) is not int or not 1 <= port <= 65535 for port in ports):
            raise ValueError("invalid fixture service port")
        before = snapshot(visible)
        write_json(output / "visible-before.json", before)
        with _gateway(profile, output / "gateway", timeout - (time.monotonic() - started)) as (port, token):
            layout = {"role": "execute", "cli": runtime["chrys"], "visible": str(visible), "skill": str(skill),
                      "config": str(directory / "config"), "sessions": str(directory / "sessions"),
                      "task": str(_within(trial, fixture.get("task_file", "visible/task.txt"))), "cwd": str(cwd),
                      "model": _isolated_model(profile, port), "model_port": port,
                      "max_tool_calls": runtime.get("max_tool_calls", 80), "mcp": services.get("mcp", [])}
            write_json(directory / "adapter/layout.json", layout)
            allowed_write = [*writable, *(directory / name for name in ("config", "sessions", "m2", "tmp", "java-home"))]
            sb = policy(runtime, read=[visible, skill, directory / "adapter", directory / "settings.xml"],
                        write=allowed_write, port=port, pty=True)
            for extra in ports:
                sb += f'(allow network-outbound (remote tcp "localhost:{extra}"))\n'
            write_new(output / "run.sb", sb)
            env = {**environment(runtime, directory), "CI_EVAL_MODEL_TOKEN": token, "CHRYS_MAX_TRANSIENT_RETRIES": "0"}
            remaining = timeout - (time.monotonic() - started) - min(10, timeout / 4)
            if remaining <= 0:
                raise TimeoutError("stage budget exhausted before worker startup")
            process = run_logged(["/usr/bin/sandbox-exec", "-f", str(output / "run.sb"), runtime["python"], "-B",
                                  str(directory / "adapter/worker.py"), str(directory / "adapter/layout.json")],
                                 output / "process", remaining, env=env, cwd=cwd, grace_seconds=min(5, remaining / 2))
        after = snapshot(visible)
        write_json(output / "visible-after.json", after)
    result = _collect(process, output / "process", directory / "sessions", identity, "execute", runtime, output)
    result.update(before_file=str(output / "visible-before.json"), after_file=str(output / "visible-after.json"),
                  skill_root=str(skill), runtime_identity=runtime_identity,
                  runtime_skill_sha256_before=skill_before, runtime_skill_sha256_after=tree_digest(skill),
                  runtime_skill_digest_scope="entire runtime bundle, excluding only .git/__pycache__/bytecode",
                  runtime_skill_preparation_exclusions=["tests", "__pycache__", ".git"],
                  duration_seconds=round(time.monotonic() - started, 3))
    if result["runtime_skill_sha256_before"] != result["runtime_skill_sha256_after"]:
        result["gaps"].append("runtime_skill_changed")
        result["trace_complete"] = False
    _finish_binding(result, runtime, "execute", identity, runtime_identity, directory, started + timeout)
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    result["evidence_files"].extend([result["before_file"], result["after_file"]])
    write_json(output / "execution.json", result)
    return result


def _review_protocol(events, answer, expected_format):
    """Bind the final answer to one audited request and terminal public text."""
    from environment.gateway import json_digest
    formats = [row for row in events if row.get("kind") == "review_format_request"]
    responses = [row for row in events if row.get("kind") == "generation_response"]
    answer_hash = hashlib.sha256(answer.encode()).hexdigest()
    expected_hash = json_digest(expected_format)
    request_ok = (len(formats) == 1 and formats[0].get("matched") is True
                  and formats[0].get("expected_format_sha256") == expected_hash
                  and formats[0].get("actual_format_sha256") == expected_hash)
    response_ok = (request_ok and len(responses) == 1
                   and all(responses[0].get(key) is not None and responses[0][key] == formats[0].get(key)
                           for key in ("gateway_id", "request_id"))
                   and responses[0].get("status") == "completed" and responses[0].get("reason") == "none"
                   and responses[0].get("output_text_sha256") == answer_hash)
    return {"status": "confirmed" if response_ok else "unconfirmed",
            "request_format_confirmed": request_ok, "returned_text_confirmed": bool(response_ok),
            "expected_format_sha256": expected_hash, "answer_sha256": answer_hash}


def judge(directory, runtime, prompt, timeout_seconds, *, review_contract=None):
    """One fresh tool-free iCode judge session; returns raw answer, never a verdict."""
    started = time.monotonic()
    timeout = _seconds(timeout_seconds)
    runtime = _config(runtime)
    directory = Path(directory).resolve()
    if directory.exists():
        raise ValueError("judge directory already exists; automatic replay forbidden")
    directory.mkdir(parents=True, mode=0o700)
    profile, identity = _profile(runtime, "judge")
    from .review_schema import wire_format
    expected_format = wire_format(review_contract)
    profile = {**profile, "_review_wire_format": expected_format}
    write_json(directory / "review-contract.json", {"allowed_ids": review_contract, "wire_format": expected_format})
    _prepare_runtime(directory, runtime, copy_cache=False)
    runtime_identity = _tool_identity(runtime, directory, deadline=started + timeout)
    _assert_binding(runtime, "judge", identity, runtime_identity)
    (directory / "input").mkdir()
    (directory / "work").mkdir()
    write_new(directory / "input/task.txt", prompt)
    with _gateway(profile, directory / "gateway", timeout - (time.monotonic() - started)) as (port, token):
        layout = {"role": "judge", "cli": runtime["chrys"], "config": str(directory / "config"),
                  "sessions": str(directory / "sessions"), "task": str(directory / "input/task.txt"),
                  "cwd": str(directory / "work"),
                  "model": _isolated_model(profile, port, role="judge", review_contract=review_contract), "model_port": port}
        write_json(directory / "adapter/layout.json", layout)
        sb = policy(runtime, read=[directory / "adapter", directory / "input", directory / "work"],
                    write=[directory / name for name in ("config", "sessions", "tmp", "java-home")], port=port)
        sb += "(deny process-fork)\n"
        write_new(directory / "run.sb", sb)
        remaining = timeout - (time.monotonic() - started) - min(10, timeout / 4)
        if remaining <= 0:
            raise TimeoutError("judge budget exhausted before worker startup")
        process = run_logged(["/usr/bin/sandbox-exec", "-f", str(directory / "run.sb"), runtime["python"], "-B",
                              str(directory / "adapter/worker.py"), str(directory / "adapter/layout.json")],
                             directory / "process", remaining,
                             env={**environment(runtime, directory), "CI_EVAL_MODEL_TOKEN": token, "CHRYS_MAX_TRANSIENT_RETRIES": "0"},
                             cwd=directory / "work", grace_seconds=min(5, remaining / 2))
    result = _collect(process, directory / "process", directory / "sessions", identity, "judge", runtime, directory)
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    result["runtime_identity"] = runtime_identity
    _finish_binding(result, runtime, "judge", identity, runtime_identity, directory, started + timeout)
    try:
        events = [json.loads(line) for line in read_regular(directory / "gateway/events.jsonl").splitlines()]
        result["review_protocol"] = _review_protocol(events, result["answer"], expected_format)
    except (ValueError, TypeError, AttributeError, OSError):
        result["review_protocol"] = {"status": "unconfirmed"}
    if result["review_protocol"]["status"] != "confirmed":
        result["gaps"] = sorted(set(result["gaps"] + ["review_wire_protocol_unconfirmed"]))
        result["trace_complete"] = False
    result["evidence_files"].append(str(directory / "review-contract.json"))
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    write_json(directory / "execution.json", result)
    return result


def preflight(runtime):
    """No-model worker/profile discovery inside the actual OS sandbox."""
    runtime = _config(runtime)
    execution_profile, execution = _profile(runtime, "execute")
    review_profile, review = _profile(runtime, "judge")
    _resolve_secret(execution_profile.get("api_key", ""))
    _resolve_secret(review_profile.get("api_key", ""))
    directory = Path(tempfile.mkdtemp(prefix="ci-eval-v2-native-", dir="/private/tmp"))
    write_new(directory / "hidden-oracle.txt", "synthetic hidden evaluation evidence\n")
    probes = {}
    for role, profile in (("execute", execution_profile), ("judge", review_profile)):
        stage = directory / role
        _prepare_runtime(stage, runtime, copy_cache=False)
        if role == "execute":
            tools = _tool_identity(runtime, stage)
        (stage / "work").mkdir()
        layout = {"role": role, "cli": runtime["chrys"], "config": str(stage / "config"),
                  "sessions": str(stage / "sessions"), "model": _isolated_model(profile, 1, role=role),
                  "cwd": str(stage / "work"), "task": str(stage / "task.txt"),
                  "canary": str(directory / "hidden-oracle.txt")}
        readable = [stage / "adapter", stage / "work"]
        if role == "execute":
            source = Path(runtime.get("skill_root", Path(__file__).resolve().parents[3] / "skills/ci-self-healing"))
            target = stage / "skills/ci-self-healing"
            for path in source.rglob("*"):
                if path.is_file() and not any(part in {"__pycache__", ".git", "tests"} for part in path.relative_to(source).parts):
                    write_new(target / path.relative_to(source), read_regular(path))
            layout.update(skill=str(target), visible=str(stage / "work"))
            readable.append(target)
        write_json(stage / "adapter/layout.json", layout)
        sb = policy(runtime, read=readable,
                    write=[stage / name for name in ("config", "sessions", "tmp", "java-home")])
        write_new(stage / "offline.sb", sb)
        process = run_logged(["/usr/bin/sandbox-exec", "-f", str(stage / "offline.sb"), runtime["python"], "-B",
                              str(stage / "adapter/worker.py"), str(stage / "adapter/layout.json"), "--preflight"],
                             stage / "process", 45, env=environment(runtime, stage), cwd=stage / "work")
        passed = process["status"] == "completed" and process["returncode"] == 0
        probes[role] = {"status": "passed" if passed else "error", "process": process}
        if passed:
            probes[role]["capabilities"] = json.loads(read_regular(stage / "process/stdout.log"))
    result = {"status": "passed" if all(row["status"] == "passed" for row in probes.values()) else "error",
              "models_called": False, "directory": str(directory), "execution_model": execution, "review_model": review,
              "model_connectivity": "not_tested", "response_model_identity": "not_tested", "probes": probes}
    result["runtime_identity"] = tools
    write_json(directory / "result.json", result)
    return result
