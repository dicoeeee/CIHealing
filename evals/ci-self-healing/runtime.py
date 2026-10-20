"""Explicit native or legacy Docker backends; never an automatic isolation fallback."""

import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from cases import oracle
from common import bind_json, EvalError, command, digest, json_bytes, read_json, read_regular, remaining_timeout, run_logged, snapshot, wall_budget, write_json, write_new
from repair_support import ADAPTER_FILES, RUNTIME_CONTRACT, execution_contract, delivery_contract, MANIFEST_DELIVERY_CONTRACT, SESSION_MOUNT, SKILL_MOUNT, budget_notice, session_receipt
from suite import ROOT, load_suite, select_trials


def load_runtime(path):
    config = read_json(path)
    if isinstance(config, dict) and config.get("backend") == "native":
        from native_repair import load_config
        return load_config(config)
    required = {"image", "network", "chrys_config_dir", "agent", "expected_model_id"}
    optional = {"timeout_seconds", "max_tool_calls"}
    if not isinstance(config, dict) or not required <= config.keys() or config.keys() - required - optional:
        raise EvalError("runtime requires image, network, chrys_config_dir, agent, expected_model_id; optional timeout_seconds/max_tool_calls")
    if not re.fullmatch(r"[A-Za-z0-9_./:-]+@sha256:[0-9a-f]{64}|sha256:[0-9a-f]{64}", config["image"]):
        raise EvalError("image must be immutable (digest or local sha256 image ID)")
    if config["network"] in {"host", "bridge", "default", "none"} or not re.fullmatch(r"[A-Za-z0-9_.-]+", config["network"]):
        raise EvalError("live runs require a dedicated internal Docker network with a controlled model gateway")
    directory = Path(config["chrys_config_dir"]).absolute()
    if directory.resolve() == (Path.home() / ".chrys").resolve():
        raise EvalError("do not mount the user's live Chrys configuration")
    for parent in (directory, *directory.parents):
        if parent.is_symlink():
            raise EvalError("configuration paths must not contain symlink aliases")
    rows = snapshot(directory, skip_git=False)
    if any(row["kind"] == "symlink" for row in rows.values()):
        raise EvalError("configuration must not contain symlinks")
    if any(Path(p).parts[0] in {"sessions", "memory", "memories", "logs"} for p in rows):
        raise EvalError("configuration contains session/history state")
    config["chrys_config_dir"] = str(directory)
    config["profile_snapshot_sha256"] = digest(json_bytes(rows))
    config.setdefault("timeout_seconds", 900)
    config.setdefault("max_tool_calls", 80)
    if not 1 <= config["timeout_seconds"] <= 1800 or not 1 <= config["max_tool_calls"] <= 200:
        raise EvalError("runtime budgets outside supported bounds")
    if not config["expected_model_id"] or not config["agent"]:
        raise EvalError("empty model identity or Agent profile")
    return config


def runtime_probe(config):
    if config.get("backend") == "native":
        from native_repair import doctor
        return doctor(config)
    return docker_probe(config)


def mount(source, target, writable=False):
    source = str(Path(source).absolute())
    if "," in source or "\n" in source or Path(source).is_symlink():
        raise EvalError("unsupported bind path")
    return ["--mount", "type=bind,src=" + source + ",dst=" + target + ("" if writable else ",readonly")]


def container_base(image, network, name):
    uid, gid = os.getuid(), os.getgid()
    if uid == 0:
        raise EvalError("v1 evaluator must run as a non-root user")
    return ["docker", "run", "--rm", "--pull=never", "--name", name, "--init", "--read-only",
            "--user", f"{uid}:{gid}", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--pids-limit=128", "--memory=2g", "--cpus=2", "--network", network,
            "--tmpfs", f"/tmp:rw,exec,uid={uid},gid={gid},mode=1777,size=768m",
            "--tmpfs", f"/home/eval/.chrys:rw,uid={uid},gid={gid},mode=0700,size=64m"]


def docker_probe(config):
    command(["docker", "version", "--format", "{{.Server.Version}}"], timeout=10)
    image = json.loads(command(["docker", "image", "inspect", config["image"]], timeout=10))[0]
    network = json.loads(command(["docker", "network", "inspect", config["network"]], timeout=10))[0]
    if network.get("Internal") is not True:
        raise EvalError("network is not internal; unrestricted model execution refused")
    name = "ci-eval-doctor-" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="ci-eval-doctor-") as temp:
        temp = Path(temp).resolve()
        for filename in ("common.py", "repair_support.py", "chrys_capabilities.py", "chrys_preflight.py"):
            write_new(temp / "adapter" / filename, read_regular(ROOT / filename))
        result = run_container([*container_base(config["image"], "none", name), *mount(temp / "adapter", "/harness"),
                                "--entrypoint", "python3", config["image"], "-B", "/harness/chrys_preflight.py"], temp / "probe", 45, name)
        if result["status"] != "completed" or result["returncode"] != 0:
            raise EvalError("container toolchain probe failed; check Chrys native grep/vendor binary, version, home, Maven cache and Java")
        capabilities = read_json(temp / "probe/stdout.log")
        if capabilities.get("status") != "passed" or not capabilities.get("observer_event_api") or not capabilities.get("headless_cli"):
            raise EvalError("container capability receipt incomplete")
    return {"image_id": image["Id"], "network_id": network["Id"], "internal_network": True,
            "model_connectivity": "not_tested", "chrys_version": capabilities.get("chrys_version"),
            "capabilities": capabilities}


def run_container(argv, directory, timeout, name):
    try:
        return run_logged(argv, directory, timeout)
    finally:
        # This removes only this evaluator's exact named disposable container.
        # Terminating docker CLI alone need not terminate the container.
        with wall_budget():  # Bounded cleanup still runs after the work deadline.
            try:
                command(["docker", "rm", "-f", name], timeout=15)
            except EvalError:
                listing = command(["docker", "ps", "-aq", "--filter", "name=^/" + name + "$"], timeout=10)
                if listing.strip():
                    raise EvalError("container cleanup not confirmed: " + name)


def probe_build(config, source, output, case_id=None):
    if config.get("backend") == "native":
        from native_repair import probe_build as native_build
        return native_build(config, source, output, case_id)
    name = "ci-eval-probe-" + Path(output).parent.name
    with tempfile.TemporaryDirectory(prefix="ci-eval-oracle-") as temp:
        temp = Path(temp)
        write_new(temp / "build_probe.py", read_regular(ROOT / "build_probe.py"))
        if case_id:
            write_new(temp / "ContractProbe.java", oracle(case_id))
        argv = [*container_base(config["image"], "none", name), *mount(source, "/source"),
                *mount(temp, "/oracle"), "--entrypoint", "python3", config["image"], "/oracle/build_probe.py"]
        if not case_id:
            argv.append("--baseline")
        process = run_container(argv, output, 180, name)
        try:
            result = json.loads(read_regular(Path(output) / "stdout.log"))
        except (ValueError, OSError, EvalError):
            result = {"status": "incomplete", "checks": {}}
        result["process"] = process
        if process["status"] != "completed" or process["returncode"] not in {0, 1}:
            result["status"] = "incomplete"
        return result


def run_trials(root, runtime_path, trial_id=None):
    root = Path(root).absolute()
    manifest = load_suite(root)
    config = load_runtime(runtime_path)
    if config.get("backend") == "native":
        from native_repair import run_trials as native_trials
        return native_trials(root, config, trial_id)
    environment = docker_probe(config)
    if not os.environ.get("CHRYS_EVAL_API_KEY"):
        raise EvalError("CHRYS_EVAL_API_KEY not set; credentials are never read from the user's default profile")
    binding = {"image_id": environment["image_id"], "network_id": environment["network_id"],
               "runtime_contract": RUNTIME_CONTRACT, "skill_mount": SKILL_MOUNT,
               "profile_snapshot_sha256": config["profile_snapshot_sha256"], "agent": config["agent"],
               "expected_model_id": config["expected_model_id"], "timeout_seconds": config["timeout_seconds"],
               "max_tool_calls": config["max_tool_calls"]}
    binding_path = root / "runtime-binding.json"
    bind_json(binding_path, binding, "suite runtime configuration changed; prepare a separate suite")
    summaries = []
    for row in select_trials(manifest, trial_id):
        if load_runtime(runtime_path) != config:
            raise EvalError("runtime configuration drift before trial")
        trial = root / "trials" / row["id"]
        if (trial / "run.json").exists() or (trial / "execution").exists() or (trial / "baseline-build").exists():
            raise EvalError("trial already started; prepare a new trial instead of overwriting evidence")
        if snapshot(trial / "visible") != read_json(trial / "initial.json"):
            raise EvalError("fixture changed before execution")
        baseline = probe_build(config, trial / "visible/scene/sourcecode", trial / "baseline-build")
        write_json(trial / "baseline-build.json", baseline)
        if baseline["status"] != "passed":
            receipt = {"status": "fixture_not_ready", "environment": environment}
            write_json(trial / "run.json", receipt)
            summaries.append({"id": row["id"], **receipt})
            continue
        log = baseline["checks"]["maven"]["stdout"] + baseline["checks"]["maven"]["stderr"]
        # Actual compile logs from an equivalent isolated copy, not invented diagnostics.
        for filename in ("all_logs.txt", "error_logs.txt"):
            write_new(trial / "visible/scene" / filename, log)
        write_new(trial / "visible/scene/log_origin.txt", "Baseline compile performed on an unchanged copy of this fixture; temporary paths in logs are verifier paths.\n")
        write_json(trial / "before-run.json", snapshot(trial / "visible"))
        adapter = trial / "adapter"
        for filename in ADAPTER_FILES:
            write_new(adapter / filename, read_regular(ROOT / filename))
        shutil.copytree(root / "skills" / row["version"], trial / "runtime-skill")
        audit_contract = execution_contract(trial / "runtime-skill")
        preflight_name = "ci-eval-registration-" + row["id"]
        preflight = run_container([
            *container_base(config["image"], "none", preflight_name),
            *mount(trial / "runtime-skill", SKILL_MOUNT), *mount(adapter, "/harness"),
            *mount(config["chrys_config_dir"], "/config"), "--entrypoint", "python3", config["image"],
            "-B", "/harness/chrys_preflight.py", "--profiles", "/config", "--agent", config["agent"]],
            trial / "preflight", 45, preflight_name)
        if preflight["status"] != "completed" or preflight["returncode"] != 0:
            receipt = {"status": "preflight_failed", "environment": environment, "preflight": preflight}
            write_json(trial / "run.json", receipt)
            summaries.append({"id": row["id"], "status": receipt["status"]})
            continue
        (trial / "native-session").mkdir(mode=0o700)
        effective_timeout = remaining_timeout(config["timeout_seconds"])
        task = read_regular(trial / "visible/task.txt").decode() + budget_notice(effective_timeout)
        write_new(adapter / "task.txt", task)
        name = "ci-eval-" + row["id"]
        argv = [*container_base(config["image"], config["network"], name),
                *mount(trial / "visible", "/trial", True), *mount(trial / "runtime-skill", SKILL_MOUNT),
                *mount(trial / "native-session", SESSION_MOUNT, True),
                *mount(adapter, "/harness"), *mount(config["chrys_config_dir"], "/config"),
                "--env", "CHRYS_EVAL_API_KEY", "--entrypoint", "python3", config["image"],
                "-B", "/harness/chrys_observer.py", "--agent", config["agent"],
                "--model", manifest["requested_model_profile"], "--max-tools", str(config["max_tool_calls"])]
        try:
            process = run_container(argv, trial / "execution", effective_timeout, name)
        finally:
            # Even interruption/cleanup failure must not discard the last checkpoint.
            write_json(trial / "session-receipt.json", session_receipt(trial / "native-session"))
        try:
            final = snapshot(trial / "visible")
            write_json(trial / "after-run.json", final)
            filesystem_status = "captured"
        except (EvalError, OSError):
            filesystem_status = "incomplete"
        receipt = {"status": process["status"] if load_runtime(runtime_path) == config else "runtime_drift", "process": process, "environment": environment,
                   "runtime_contract": RUNTIME_CONTRACT, "task_sha256": digest(task.encode()),
                   "skill_execution_contract": audit_contract,
                   "session_receipt_sha256": digest(read_regular(trial / "session-receipt.json")),
                   "runtime_binding_sha256": digest(json_bytes(binding)),
                   "expected_model_id": config["expected_model_id"], "agent": config["agent"],
                   "profile_snapshot_sha256": config["profile_snapshot_sha256"],
                   "budgets": {"timeout_seconds": config["timeout_seconds"], "max_tool_calls": config["max_tool_calls"]},
                   "skill_sha256": row["skill_sha256"], "filesystem_status": filesystem_status,
                   "stdout_sha256": digest(read_regular(trial / "execution/stdout.log"))}
        # Legacy Docker receipts did not carry a delivery contract. Add it only
        # for packages that require the new manifest; no timeout salvage here.
        if delivery_contract(trial / "runtime-skill") == MANIFEST_DELIVERY_CONTRACT:
            from delivery_evidence import assess_delivery
            receipt["delivery_contract"] = MANIFEST_DELIVERY_CONTRACT
            receipt["delivery_assessment"] = assess_delivery(trial, receipt)
        write_json(trial / "run.json", receipt)
        summaries.append({"id": row["id"], "status": receipt["status"]})
    return summaries
