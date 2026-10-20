"""Container-only Chrys adapter gated by capabilities, not release numbers.

Observes public events at tool boundaries, not hidden reasoning. JSON frames go
to the parent process; the agent cannot overwrite the host-owned transcript.
This is an observer, not a syscall audit: transient writes within one tool call
and adversarial tampering with the in-process observer are outside v1 coverage.
"""

import argparse
import dataclasses
import io
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time

from common import EvalError, changes, digest, json_bytes, read_json, snapshot
from repair_support import SESSION_MOUNT, SKILL_MOUNT, SKILL_NAME, RUNTIME_CONTRACT, execution_contract, delivery_contract, source_state
from repair_telemetry import transport_timing
from chrys_capabilities import observer_capabilities


PREFIX = "CI_EVAL_EVENT "
FIELDS = {"text", "is_final", "is_intermediate", "presentation", "attempt_id", "segment_ids",
          "tool_name", "tool_kind", "args", "call_id", "provider_call_id", "result",
          "duration_ms", "provider_hosted", "status", "reason", "opening_prompt"}
EVENTS = {"InvocationMessage", "InvocationToolCallStart", "InvocationToolCallResult",
          "InvocationToolCallArgsUpdated", "InvocationPresentationAttemptAccepted",
          "InvocationPresentationAttemptRejected", "InvocationStarted", "ApprovalRequest"}


def headless_result(raw, returncode):
    # Headless failures may write only to stderr. Do not mask them with a JSON error.
    result = json.loads(raw) if raw.strip() else {}
    if not isinstance(result, dict) or (returncode == 0 and not result):
        raise EvalError("successful headless run did not return a JSON object")
    return result


class Recorder:
    def __init__(self, stream, root, max_tools=80, secret="", skill_path=SKILL_MOUNT):
        self.stream, self.root, self.max_tools, self.secret = stream, Path(root), max_tools, secret
        self.sequence = 0
        self.tools = 0
        self.bytes = 0
        self.previous = snapshot(self.root)
        self.skill_registration_confirmed = False
        self._emit_lock = threading.Lock()
        self.skill_path = str(skill_path)
        self.session_ids = set()

    def emit(self, kind, payload):
        with self._emit_lock:
            self.sequence += 1
            row = {"seq": self.sequence, "kind": kind, "observed_ns": time.monotonic_ns(), "payload": payload}
            line = json.dumps(row, ensure_ascii=False, default=str)
            if self.secret:
                line = line.replace(self.secret, "[REDACTED]")
            self.bytes += len(line.encode())
            if self.bytes > 12 * 1024 * 1024:
                raise EvalError("observer output budget exceeded")
            self.stream.write(PREFIX + line + "\n")
            self.stream.flush()

    def require_registration(self):
        if not self.skill_registration_confirmed:
            raise EvalError("model transport refused before native Skill registration")

    def observe(self, event):
        kind = type(event).__name__
        if kind in {"SessionReady", "AgentRuntimeUpdated"}:
            details = getattr(event, "runtime_details", None)
            sources = getattr(details, "skill_sources", {})
            names = getattr(event, "skill_names", [])
            confirmed = names == [SKILL_NAME] and sources == {self.skill_path: [SKILL_NAME]}
            self.skill_registration_confirmed = confirmed
            self.emit("observer.skill_registration", {"confirmed": confirmed, "skill_names": names,
                                                      "skill_sources": sources})
            if not confirmed:
                raise EvalError("live session Skill registration failed")
            return
        if kind not in EVENTS:
            return
        if kind == "InvocationToolCallStart":
            self.tools += 1
            if self.tools > self.max_tools:
                raise EvalError("tool-call budget exhausted")
        payload = {field.name: getattr(event, field.name) for field in dataclasses.fields(event) if field.name in FIELDS}
        payload = {name: dataclasses.asdict(value) if dataclasses.is_dataclass(value) else value for name, value in payload.items()}
        # Include serialized public invocation identity, without arbitrary config fields.
        origin = getattr(event, "origin", None)
        if origin is not None:
            payload["origin"] = dataclasses.asdict(origin) if dataclasses.is_dataclass(origin) else str(origin)
            if isinstance(payload["origin"], dict) and payload["origin"].get("session_id"):
                self.session_ids.add(payload["origin"]["session_id"])
        current = snapshot(self.root)
        payload["changed_paths_since_previous_event"] = changes(self.previous, current)
        payload["source_state_sha256"] = digest(json_bytes(source_state(current)))
        self.previous = current
        self.emit(kind, payload)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-tools", type=int, default=80)
    parser.add_argument("--task", default="/harness/task.txt")
    parser.add_argument("--native-layout")
    args = parser.parse_args()
    capabilities = observer_capabilities()
    # Image supplies an ordinary non-root user and clean home; no host home mount.
    if args.native_layout:
        layout = read_json(args.native_layout)
        scene, skill = layout["visible"], layout["skill"]
        session_root = Path(layout["sessions"])
    else:
        config = Path.home() / ".chrys"
        if config.exists() and any(config.iterdir()):
            raise EvalError("container configuration was not empty")
        shutil.copytree("/config", config, dirs_exist_ok=True)
        shutil.copytree("/opt/maven-repository", "/tmp/m2")
        scene, skill, session_root = "/trial", SKILL_MOUNT, Path(SESSION_MOUNT)
    audit_contract = execution_contract(skill)
    if not session_root.is_dir() or session_root.is_symlink() or any(session_root.iterdir()):
        raise EvalError("a new, persistent native session mount is required")
    os.environ["CHRYS_SESSION_ROOT_DIR"] = str(session_root)
    os.environ["CHRYS_MAX_TRANSIENT_RETRIES"] = "1"
    from chrys.foundation.events.bus import EventBus
    from chrys.app.cli.app import main as chrys_main
    recorder = Recorder(sys.stdout, scene, args.max_tools, os.environ.get("CHRYS_EVAL_API_KEY", ""), skill)
    original_publish = EventBus.publish

    async def publish(bus, event, *, raise_handler_errors=False):
        recorder.observe(event)
        return await original_publish(bus, event, raise_handler_errors=raise_handler_errors)

    EventBus.publish = publish
    cli = layout.get("cli", "chrys") if args.native_layout else "chrys"
    recorder.emit("observer.started", {**capabilities, "cli": cli, "runtime_contract": RUNTIME_CONTRACT,
                                      "skill_execution_contract": audit_contract,
                                      "delivery_contract": delivery_contract(skill),
                                      "clock_id": layout.get("clock_id") if args.native_layout else None,
                                      "coverage": "public-events-and-tool-boundary-snapshots"})
    buffer = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buffer
    sys.argv = [cli, "run", "--agent", args.agent, "--model", args.model,
                "--workdir", str(Path(scene) / "scene/sourcecode"), "--task", args.task, "--json"]
    try:
        with transport_timing(recorder.emit, before_send=recorder.require_registration):
            code = chrys_main()
    finally:
        sys.stdout = real_stdout
        EventBus.publish = original_publish
    result = headless_result(buffer.getvalue(), code)
    # Public event identity survives a cancelled final response. Never fill it
    # from --model, a configured default, or an arbitrary checkpoint.
    observed_ids = sorted(recorder.session_ids)
    session_id = observed_ids[0] if len(observed_ids) == 1 else None
    metas = []
    for path in session_root.rglob("session.json"):
        data = read_json(path)
        meta = data.get("meta", {})
        if meta.get("session_id") == session_id:
            metas.append({k: meta.get(k) for k in ("session_id", "model_id", "model_profile_id", "agent_profile_fingerprint", "app_version")})
    recorder.emit("observer.finished", {"returncode": code, "result": result, "model_receipts": metas,
                                        "observed_session_ids": observed_ids,
                                        "source_state_sha256": digest(json_bytes(source_state(snapshot(scene)))),
                                        "skill_registration_confirmed": recorder.skill_registration_confirmed,
                                        "changed_paths_since_previous_event": changes(recorder.previous, snapshot(scene))})
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # No settings/credentials in error payloads.
        print(PREFIX + json.dumps({"kind": "observer.error", "payload": {"error_type": type(exc).__name__}}), flush=True)
        sys.exit(2)
