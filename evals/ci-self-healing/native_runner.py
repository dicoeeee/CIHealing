"""Reusable, bounded macOS Chrys CLI reviewer with native exchange receipts.

No internal Chrys execution API or HTTP fallback. Only the existing trajectory
HMAC key and its exact lock are accessible outside the selected model/runtime;
no global profile/settings writes. The key must already exist.
"""
import json
import os
from pathlib import Path
import platform
import sys

from common import EvalError, command, digest, json_bytes, read_json, read_regular, run_logged, write_json, write_new
from runtime_diagnostics import write_diagnostics
from resource_cleanup import OwnedResources, validate_cleanup
from review import validate_verdict
from review_chrys import assess_native, prepare_native, strict_json
from review_limits import input_limits
from suite import load_suite, select_trials
from chrys_capabilities import require_cli_options


def load_native(config):
    required = {"chrys", "python", "profile", "endpoint_host"}
    optional = {"timeout_seconds", "max_packet_bytes", "version"}
    if not isinstance(config, dict) or required - config.keys() or config.keys() - required - optional:
        raise EvalError("native reviewer needs chrys/python/profile/endpoint_host and optional timeout_seconds/max_packet_bytes")
    result = dict(config)
    # Legacy input accepted only for config migration, never used as a pin.
    result.pop("version", None)
    for key in ("chrys", "python", "profile"):
        result[key] = str(Path(result[key]).absolute())
    result.setdefault("timeout_seconds", 900)
    result.setdefault("max_packet_bytes", 524288)
    if type(result["timeout_seconds"]) is not int or not 1 <= result["timeout_seconds"] <= 1800:
        raise EvalError("invalid native timeout")
    input_limits(result["max_packet_bytes"])
    return result


def native_identity(config):
    from urllib.parse import urlsplit
    import yaml
    profile_path = Path(config["profile"])
    expected_dir = Path.home() / ".chrys/models"
    if profile_path.parent != expected_dir:
        raise EvalError("native CLI requires the explicitly selected installed model profile")
    raw = read_regular(profile_path, 65536)
    try:
        p = yaml.safe_load(raw)
    except yaml.YAMLError:
        # Parser diagnostics may quote a credential-bearing source line.
        raise EvalError("selected native model profile is invalid YAML") from None
    if not isinstance(p, dict):
        raise EvalError("native profile must be a mapping")
    url = urlsplit(p.get("base_url", ""))
    if (p.get("name") != "huoshan-glm5.3" or p.get("model_id") != "glm-5.3"
            or p.get("provider") != "openai" or p.get("api_style") != "responses"
            or p.get("bypass_proxy") is not True or p.get("http_headers") or p.get("chat_options")):
        raise EvalError("native review requires the selected non-Flash GLM 5.3 profile without route/header/options overrides")
    if (url.scheme != "https" or url.hostname != config["endpoint_host"] or url.username or url.password
            or url.query or url.fragment or url.port not in (None, 443)):
        raise EvalError("native endpoint differs from the approved HTTPS gateway")
    key = Path.home() / ".chrys/trajectory-hmac.key"
    if not key.is_file() or key.is_symlink():
        raise EvalError("existing Chrys trajectory HMAC key required; this runner does not create installation secrets")
    return {"profile_sha256": digest(raw), "profile_id": p.get("id", profile_path.stem),
            "requested_model_id": p["model_id"], "profile_name": p["name"],
            "endpoint_host": url.hostname, "profile_output_cap": p.get("max_output_tokens"),
            "sdk_http_max_retries": p.get("http_max_retries", 2),
            "version": command([config["chrys"], "--version"], timeout=15).decode().strip(),
            "adapter_sha256": digest(read_regular(Path(__file__))),
            "projection_sha256": digest(read_regular(Path(__file__).with_name("review_packet.py"))),
            "contract_sha256": digest(read_regular(Path(__file__).with_name("review.py"))),
            "collector_sha256": digest(read_regular(Path(__file__).with_name("review_chrys.py")))}


def policy(config, directory, online):
    """Port filtering is not a destination-host firewall. Record that boundary."""
    if platform.system() != "Darwin":
        raise EvalError("native reviewer isolation currently supports macOS only; no unsandboxed fallback")
    directory = Path(directory).absolute()
    chrys, python = Path(config["chrys"]), Path(config["python"])
    home = Path.home()
    runtime = python.parent.parent
    if runtime == home or runtime in home.parents:
        raise EvalError("Python must be a dedicated runtime, not a broad home/root")
    config_dir = home / ".chrys"
    subpaths = ["/System", "/usr", "/bin", "/sbin", "/Library", "/private/etc", "/private/var/db/dyld", "/dev",
                str(runtime), str(python.resolve().parent.parent),
                *(str(directory / n) for n in ("input", "work", "sessions", "tmp"))]
    literal = ["/", "/Users", str(home), str(config_dir), str(config_dir / "models"), config["profile"],
               str(chrys), str(config_dir / "trajectory-hmac.key"), str(config_dir / "trajectory-hmac.key.lock"),
               str(directory), *(str(p) for p in directory.parents)]
    def filters(kind, paths):
        return " ".join("(" + kind + " " + json.dumps(str(p)) + ")" for p in paths)
    rows = ["(version 1)", "(allow default)", "(deny file-read*)", "(deny file-write*)", "(deny network*)",
            "(deny process-exec)", "(deny process-fork)", "(allow file-read-metadata)",
            "(allow file-read* " + filters("subpath", subpaths) + " " + filters("literal", literal) + ")",
            "(allow file-write* " + filters("subpath", [directory / "sessions", directory / "tmp"])
            + " " + filters("literal", ["/dev/null", "/dev/dtracehelper", config_dir / "trajectory-hmac.key.lock"]) + ")",
            "(allow process-exec " + filters("literal", [chrys, chrys.resolve(), python, python.resolve(), python.parent / "python3"]) + ")"]
    if online:
        rows += ['(allow network-outbound (remote tcp "*:443") (literal "/private/var/run/mDNSResponder"))']
    return "\n".join(rows) + "\n"


def native_env(directory):
    directory = Path(directory)
    env = {k: os.environ[k] for k in ("HOME", "USER", "LOGNAME") if k in os.environ}
    env.update(PATH="/usr/bin:/bin:/usr/sbin:/sbin", LANG="en_US.UTF-8", PYTHONDONTWRITEBYTECODE="1",
               CHRYS_SESSION_ROOT_DIR=str(directory / "sessions"), CHRYS_SESSION_TITLE_AUTO="0",
               CHRYS_MAX_TRANSIENT_RETRIES="0", CHRYS_HISTORY_DISABLE="1", CHRYS_OTEL="0",
               CHRYS_OTEL_SENSITIVE_DATA="0", CHRYS_DEBUG_LLM_RAW_HTTP_LOG="0", TMPDIR=str(directory / "tmp"))
    return env


def setup_dirs(directory, resources=None):
    directory = Path(directory)
    for name in ("input", "work", "sessions", "tmp"):
        if resources is not None and name == "tmp":
            resources.mkdir(name)
        else:
            (directory / name).mkdir(parents=True, exist_ok=True)


def native_preflight(config, directory):
    directory = Path(directory).absolute()
    directory.mkdir(parents=True, exist_ok=True)
    with OwnedResources(directory, "preflight", evidence=("input", "work", "sessions", "cli-probe", "probe", "trajectory-probe")) as resources:
        result = _native_preflight(config, directory, resources)
        if resources.finish()["status"] not in {"cleaned", "not_applicable"}:
            raise EvalError("native preflight resource cleanup incomplete")
        return result


def _native_preflight(config, directory, resources):
    identity = native_identity(config)
    setup_dirs(directory, resources)
    directory = Path(directory)
    write_new(directory / "offline.sb", policy(config, directory, False))
    resources.watch(directory / "cli-probe/process.json")
    help_process = run_logged(["/usr/bin/sandbox-exec", "-f", str(directory / "offline.sb"),
                               config["chrys"], "run", "--help"], directory / "cli-probe", 30,
                              env=native_env(directory), cwd=directory / "work")
    if help_process["status"] != "completed" or help_process["returncode"] != 0:
        raise EvalError("native headless CLI capability probe failed; no model called")
    require_cli_options(read_regular(directory / "cli-probe/stdout.log").decode(),
                        ("--agent", "--model", "--workdir", "--task", "--json", "--quiet"))
    resources.watch(directory / "probe/process.json")
    process = run_logged(["/usr/bin/sandbox-exec", "-f", str(directory / "offline.sb"), config["chrys"], "agents", "--json"],
                         directory / "probe", 45, env=native_env(directory), cwd=directory / "work")
    if process["status"] != "completed" or process["returncode"] != 0:
        raise EvalError("native sandbox preflight failed")
    agents = read_json(directory / "probe/stdout.log")["agents"]
    qa = next((a for a in agents if a.get("name") == "QA"), {})
    if qa.get("id") != "b0119a000005" or qa.get("source") != "builtin" or qa.get("model") != "active":
        raise EvalError("native QA is overridden or separately model-bound")
    # Chrys securely opens every parent component, so CLI startup alone does
    # not prove the trajectory writer can open its lease/events or existing key.
    # This is an offline filesystem probe, not an alternate model executor.
    probe = '''from pathlib import Path
from chrys.foundation.trajectory.keys import ensure_owner_only_directory, load_or_create_fingerprint_key
from chrys.foundation.trajectory.lease import WriterLease
from chrys.foundation.platform.files import secure_open_owner_only_append
import os
directory = Path.cwd().parent / "sessions/preflight/trajectory"
ensure_owner_only_directory(directory)
lease = WriterLease.try_acquire(directory / "writer.lock")
assert lease is not None
key = Path.home() / ".chrys/trajectory-hmac.key"
assert key.is_file() and not key.is_symlink()
load_or_create_fingerprint_key(key.parent)
handle = secure_open_owner_only_append(directory / "events.jsonl")
os.close(handle.fd)
lease.release()
print("trajectory-files-ready")
'''
    resources.watch(directory / "trajectory-probe/process.json")
    io_process = run_logged(["/usr/bin/sandbox-exec", "-f", str(directory / "offline.sb"), config["python"], "-B", "-c", probe],
                            directory / "trajectory-probe", 30, env=native_env(directory), cwd=directory / "work")
    if io_process["status"] != "completed" or io_process["returncode"] != 0:
        raise EvalError("native trajectory filesystem preflight failed; no model called")
    return identity


def responses_completion(rows, start, end):
    """When Responses success omits stop reason, require lifecycle evidence.

    This establishes the native client's accepted completion, not an observed
    wire-level response.completed event. Never synthesize a provider stop flag.
    """
    if start["payload"].get("api_style") != "responses" or end["payload"].get("api_style") != "responses":
        raise EvalError("missing finish reason without a supported Responses lifecycle")

    def one(kind):
        matched = [e for e in rows if e["event_type"] == kind]
        if len(matched) != 1:
            raise EvalError("native lifecycle marker missing or repeated: " + kind)
        return matched[0]

    ts, rs, cs, valid, ce, re, te = [one(k) for k in (
        "turn.started", "model.run.started", "model.cycle.started", "model.validation.finished",
        "model.cycle.finished", "model.run.finished", "turn.finished")]
    oid = start["operation_id"]
    sequence = [ts, rs, cs, start, valid, end, ce, re, te]
    if any(a["sequence"] >= b["sequence"] for a, b in zip(sequence, sequence[1:])):
        raise EvalError("native lifecycle ordering mismatch")
    turn_id = ts.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id or any(e.get("turn_id") != turn_id for e in sequence):
        raise EvalError("native lifecycle turn identity mismatch")
    for event in sequence:
        actor = event.get("actor")
        if not isinstance(actor, dict) or actor.get("kind") != "agent" or actor.get("role") != "main":
            raise EvalError("native lifecycle is not the main agent")
    for opened, closed in ((rs, re), (cs, ce)):
        if not opened.get("operation_id") or opened["operation_id"] != closed.get("operation_id"):
            raise EvalError("native lifecycle operation identity mismatch")
    if (any(e.get("parent_operation_id") != rs["operation_id"] for e in (cs, ce))
            or any(e.get("parent_operation_id") != cs["operation_id"] for e in (start, end))
            or valid.get("parent_operation_id") != oid
            or valid["payload"].get("exchange_operation_id") != oid
            or valid["payload"].get("outcome") != "accepted"
            or ce["payload"].get("outcome") != "success"
            or type(ce["payload"].get("exchange_count")) is not int or ce["payload"]["exchange_count"] != 1
            or type(ce["payload"].get("function_call_count")) is not int or ce["payload"]["function_call_count"] != 0
            or ce["payload"].get("final_exchange_operation_id") != oid
            or re["payload"].get("outcome") != "completed" or te["payload"].get("end_reason") != "completed"):
        raise EvalError("native Responses lifecycle did not establish accepted completion")


def exchange_receipt(raw, session_id, model):
    """Verify a fresh, complete native trajectory, not model self-description.

    This counts Chrys exchanges, not opaque retries inside the provider SDK.
    Initial support is deliberately one successful exchange and no tools.
    """
    rows = [strict_json(line) for line in raw.splitlines() if line.strip()]
    if not rows or len(rows) > 20000:
        raise EvalError("missing or oversized native trajectory")
    runtime_ids, coverage_ids, starts, ends = set(), set(), {}, {}
    coverage, closed = [], []
    forbidden = ("tool.", "hosted_call.", "sub_agent.", "compaction.started", "hook.", "profile.switched",
                 "trajectory.gap", "trajectory.runtime.recovered", "retry.", "session.forked", "session.rollback")
    for index, event in enumerate(rows, 1):
        if (not isinstance(event, dict) or type(event.get("schema_version")) is not int or event.get("schema_version") != 1
                or type(event.get("sequence")) is not int or event.get("sequence") != index
                or event.get("session_id") != session_id or not isinstance(event.get("payload"), dict)):
            raise EvalError("native trajectory identity/sequence/schema gap")
        if any(not isinstance(event.get(k), str) or not event[k] for k in ("runtime_id", "coverage_id")):
            raise EvalError("missing native runtime/coverage identity")
        runtime_ids.add(event.get("runtime_id"))
        coverage_ids.add(event["coverage_id"])
        kind = event.get("event_type", "")
        if not isinstance(kind, str) or kind.startswith(forbidden):
            raise EvalError("native trajectory contains tools, retries, compaction, hooks, or gaps")
        if kind == "trajectory.coverage.started":
            coverage.append(event["payload"].get("coverage_reason"))
        if kind == "trajectory.coverage.ended":
            closed.append(index)
            if (event["payload"].get("coverage_id") != event["coverage_id"]
                    or type(event["payload"].get("last_sequence")) is not int
                    or event["payload"]["last_sequence"] != index - 1):
                raise EvalError("native coverage closure mismatch")
        if kind in ("model.exchange.started", "model.exchange.finished"):
            actor = event.get("actor")
            if not isinstance(actor, dict) or actor.get("kind") != "agent" or actor.get("role") != "main":
                raise EvalError("unexpected native side-call")
            target = starts if kind.endswith("started") else ends
            oid = event.get("operation_id")
            if not isinstance(oid, str) or not oid or oid in target:
                raise EvalError("missing or duplicate native exchange identity")
            target[oid] = event
    if (len(runtime_ids) != 1 or len(coverage_ids) != 1 or coverage != ["session_started"]
            or rows[0].get("event_type") != "trajectory.coverage.started" or closed != [len(rows) - 1]
            or sum(e["event_type"] == "trajectory.runtime.started" for e in rows) != 1
            or sum(e["event_type"] == "trajectory.runtime.finished" for e in rows) != 1
            or rows[-1].get("event_type") != "trajectory.runtime.finished"
            or rows[-1]["payload"].get("reason") != "graceful_shutdown"):
        raise EvalError("native trajectory is not a complete fresh runtime")
    if len(starts) != 1 or starts.keys() != ends.keys():
        raise EvalError("expected exactly one fully paired native model exchange")
    oid = next(iter(starts))
    start, end = starts[oid], ends[oid]
    terminal = end["payload"]
    if (start["sequence"] >= end["sequence"] or start["payload"].get("request_model") != model
            or terminal.get("response_model") != model or terminal.get("outcome") != "success"
            or not isinstance(terminal.get("response_id"), str) or not terminal["response_id"]):
        raise EvalError("native model completion/identity is not confirmed")
    reason = terminal.get("finish_reason")
    if reason is None:
        responses_completion(rows, start, end)
    elif reason != "stop":
        raise EvalError("native model stopped abnormally")
    return {"status": "validated", "session_id": session_id, "exchange_count": 1,
            "response_model": terminal["response_model"], "response_id": terminal["response_id"],
            "finish_reason": reason, "completion_basis": "chrys-responses-lifecycle" if reason is None else "explicit-stop",
            "provider_terminal_status": "not_recorded", "trajectory_sha256": digest(raw),
            "boundary": "native exchange provenance; not backend attestation or SDK wire-retry audit"}


def review_process_failure(process):
    """Primary lifecycle cause precedes parsing an absent final envelope."""
    status = process.get("status")
    if status == "timeout":
        return "review_timeout"
    if status == "interrupted":
        return "review_interrupted"
    if status != "completed":
        return "review_process_incomplete"
    if process.get("returncode") != 0:
        return "review_process_nonzero"
    return None


def native_review(suite, trial_id, directory, config, expected_identity, *, publish=True,
                  frozen_packet=None, attempt_source=None):
    if native_identity(config) != expected_identity:
        raise EvalError("native model/configuration drift")
    suite, directory = Path(suite).absolute(), Path(directory).absolute()
    binding = {"transport": "chrys-run-native-v1", **expected_identity, "timeout_seconds": config["timeout_seconds"],
               "max_packet_bytes": config["max_packet_bytes"]}
    binding_path = suite / "reviewer-binding.json" if publish else directory / "reviewer-binding.json"
    if publish and binding_path.exists():
        if read_json(binding_path) != binding:
            raise EvalError("reviewer binding drift")
    elif publish:
        write_json(binding_path, binding)
    prepare_native(suite, trial_id, directory, expected_identity["profile_id"], config["endpoint_host"],
                   expected_identity["version"], config["max_packet_bytes"], compact=True, frozen_packet=frozen_packet,
                   cli=config["chrys"])
    if not publish:
        write_json(binding_path, binding)
    if attempt_source is not None:
        write_json(directory / "attempt-source.json", attempt_source)
    with OwnedResources(directory, "review", evidence=("input", "work", "sessions", "execution",
                        "runtime-diagnostics.json", "assessment.json", "binding.json", "invocation.json")) as resources:
        setup_dirs(directory, resources)
        write_new(directory / "sandbox.sb", policy(config, directory, True))
        argv = [config["chrys"], *read_json(directory / "invocation.json")["argv"][1:]]
        interrupted = False
        resources.watch(directory / "execution/process.json")
        try:
            process = run_logged(["/usr/bin/sandbox-exec", "-f", str(directory / "sandbox.sb"), *argv],
                                 directory / "execution", config["timeout_seconds"], env=native_env(directory), cwd=directory / "work", grace_seconds=5)
        except KeyboardInterrupt:
            process = read_json(directory / "execution/process.json")
            interrupted = True
        diagnostic = write_diagnostics(directory, directory / "sessions", process)
        raw = read_regular(directory / "execution/stdout.log", 1024 * 1024)
        task_binding = read_json(directory / "binding.json")
        packet = read_json(directory / "input/packet.json")
        receipt = {"requested_model": "glm-5.3", "transport": binding["transport"], "status": "incomplete",
                   "grade_sha256": task_binding["grade_sha256"], "packet_sha256": task_binding["packet_sha256"],
                   "reviewer_binding_sha256": digest(read_regular(binding_path)), "process": process,
                   "isolation_confirmed": process.get("cleanup", {}).get("status") == "confirmed",
                   "runtime_diagnostics": diagnostic, "diagnostics_sha256": digest(read_regular(directory / "runtime-diagnostics.json")),
                   "diagnostics_directory": str(directory)}
        receipt["suite_review_written"] = publish
        if attempt_source is not None:
            receipt["attempt_source"] = attempt_source
        failure = review_process_failure(process)
        if failure:
            receipt.update(gap=failure, failure_code=failure,
                           output_status="absent" if not raw else "retained_not_accepted_after_process_failure")
            receipt["resource_cleanup"] = resources.finish()
            write_json(directory / "receipt.json", receipt)
            if publish:
                write_json(suite / "trials" / trial_id / "review.json", receipt)
            if interrupted:
                raise KeyboardInterrupt()
            return receipt
        try:
            reply = strict_json(raw)
            sid = reply["session_id"]
            import uuid
            sid = str(uuid.UUID(sid))
            folder = directory / "sessions/sessions" / sid.replace("-", "")[:12]
            session_raw = read_regular(folder / "session.json")
            session = strict_json(session_raw)
            assessment = assess_native(raw, process, session, packet, read_regular(directory / "input/task.txt"), task_binding)
            write_json(directory / "assessment.json", assessment)
            if assessment["gaps"] or assessment["errors"]:
                raise EvalError("native public output/session validation failed; see assessment.json")
            exchange = exchange_receipt(read_regular(folder / "trajectory/events.jsonl"), sid, "glm-5.3")
            if native_identity(config) != expected_identity:
                raise EvalError("native profile changed during review")
            receipt.update(status="validated", verdict=validate_verdict(strict_json(reply["result"]), packet),
                           model_receipt=exchange, session_sha256=digest(session_raw), stdout_sha256=digest(raw))
            receipt["native_directory"] = str(directory)
            receipt["native_artifacts"] = {str(p.relative_to(directory)): digest(read_regular(p)) for p in
                                           (directory / "execution/stdout.log", directory / "execution/process.json",
                                            directory / "input/task.txt", directory / "input/packet.json",
                                            directory / "input/model-packet.json",
                                            folder / "session.json", folder / "trajectory/events.jsonl")}
        except (EvalError, OSError, ValueError, KeyError, TypeError) as exc:
            receipt["gap"] = str(exc)
        receipt["resource_cleanup"] = resources.finish()
        write_json(directory / "receipt.json", receipt)
        if publish:
            write_json(suite / "trials" / trial_id / "review.json", receipt)
        if interrupted:
            raise KeyboardInterrupt()
        return receipt


def validate_native_artifacts(receipt):
    if "resource_cleanup" in receipt:
        validate_cleanup(receipt["resource_cleanup"])
    if receipt.get("diagnostics_sha256") and digest(read_regular(Path(receipt["diagnostics_directory"]) / "runtime-diagnostics.json")) != receipt["diagnostics_sha256"]:
        raise EvalError("native review diagnostics changed")
    directory = Path(receipt["native_directory"])
    artifacts = receipt["native_artifacts"]
    if not isinstance(artifacts, dict) or len(artifacts) not in {6, 7} or not directory.is_absolute():
        raise EvalError("missing native review artifacts")
    for relative, expected in artifacts.items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or digest(read_regular(directory / path)) != expected:
            raise EvalError("native review evidence changed")
