"""Small, dependency-free primitives for host-owned evaluation artifacts."""

import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone


class EvalError(Exception):
    pass


class BudgetExhausted(EvalError):
    pass


_deadline = None
# A domain tag shared explicitly with native child gateways. Never compare
# monotonic timestamps from an unrelated host/process invocation by accident.
HOST_CLOCK_ID = uuid.uuid4().hex


@contextmanager
def wall_budget(seconds=None):
    """Cooperative aggregate deadline; subprocess timeouts share this bound."""
    global _deadline
    previous = _deadline
    _deadline = None if seconds is None else time.monotonic() + seconds
    try:
        yield
    finally:
        _deadline = previous


def remaining_timeout(seconds):
    if _deadline is None:
        return seconds
    remaining = _deadline - time.monotonic()
    if remaining <= 0:
        raise BudgetExhausted("suite wall-clock budget exhausted")
    return min(seconds, remaining)


def replace_json(path, value):
    """Atomic replacement for derived summaries only, never immutable evidence."""
    import tempfile
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise EvalError("symlink is not a summary output")
    fd, temp = tempfile.mkstemp(prefix=".summary-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = value if isinstance(value, bytes) else value.encode()
    with path.open("xb") as stream:
        stream.write(payload)


def write_json(path, value):
    write_new(path, json_bytes(value))


def read_regular(path, limit=16 * 1024 * 1024):
    """Never follow a candidate-controlled symlink (including ancestors)."""
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise EvalError("symlink is not an evaluation input: " + str(part))
    if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > limit:
        raise EvalError("not a bounded regular file: " + str(path))
    with path.open("rb") as stream:
        data = read_checked_stream(stream, str(path), limit)
    if len(data) > limit:
        raise EvalError("input grew beyond limit: " + str(path))
    return data


def read_checked_stream(stream, name, limit=16 * 1024 * 1024):
    prefix = b""
    if Path(name).suffix.lower() == ".md":
        prefix = stream.readline(min(limit + 1, 65537))
        if prefix.decode("utf-8-sig").strip() == "---":
            while True:
                line = stream.readline(65537)
                prefix += line
                if not line or len(prefix) > 65536:
                    raise EvalError("unclosed or oversized frontmatter: " + name)
                if line.strip() == b"---":
                    break
            markdown_allowed(prefix, name)
    remaining = max(0, limit + 1 - len(prefix))
    data = prefix + stream.read(remaining)
    if len(data) > limit:
        raise EvalError("input budget exceeded: " + name)
    return data


def read_json(path):
    return json.loads(read_regular(path))


def markdown_allowed(data, name):
    """Inspect frontmatter before body use; fail closed on access declarations."""
    lines = data.decode("utf-8-sig").splitlines()
    if not lines or lines[0].strip() != "---":
        return
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise EvalError("unclosed frontmatter: " + name)
    declarations = [s.strip() for s in lines[1:end] if "ai_access" in s]
    if declarations and declarations != ["ai_access: true"]:
        raise EvalError("restricted or ambiguous Markdown access: " + name)


def snapshot(root, skip_git=True):
    """Inventory content, modes, empty directories and links without following links."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise EvalError("invalid snapshot root: " + str(root))
    rows = {}
    total = 0

    def visit(directory):
        nonlocal total
        for path in sorted(directory.iterdir()):
            rel = path.relative_to(root).as_posix()
            if skip_git and path.name == ".git":
                continue
            mode = path.lstat().st_mode
            row = {"mode": stat.S_IMODE(mode)}
            if stat.S_ISLNK(mode):
                row.update(kind="symlink", target=os.readlink(path))
            elif stat.S_ISDIR(mode):
                row["kind"] = "directory"
            elif stat.S_ISREG(mode):
                data = read_regular(path)
                total += len(data)
                if total > 32 * 1024 * 1024:
                    raise EvalError("snapshot byte budget exceeded")
                if path.suffix.lower() == ".md":
                    markdown_allowed(data, str(path))
                row.update(kind="file", sha256=digest(data), size=len(data))
            else:
                raise EvalError("special file in workspace: " + rel)
            rows[rel] = row
            if len(rows) > 10000:
                raise EvalError("snapshot entry budget exceeded")
            if row["kind"] == "directory":
                visit(path)
    visit(root)
    return rows


def changes(before, after):
    return [p for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p)]


def command(argv, cwd=None, env=None, timeout=30):
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=remaining_timeout(timeout))
    if result.returncode:
        raise EvalError("command failed: " + argv[0] + "\n" + result.stderr.decode(errors="replace")[:2000])
    return result.stdout


def stop_owned_group(process, *, kill_already_sent=False):
    """Kill/reap only our group; do not claim coverage of detached descendants."""
    errors = []
    # Only the caller's successful kernel delivery may set this flag. Avoid
    # signalling a group a second time while it is being torn down (macOS can
    # return EPERM then). Reap and absence checks below are never skipped.
    if not kill_already_sent:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            errors.append("group_signal_failed")
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        errors.append("leader_not_reaped")
    deadline = time.monotonic() + 1
    absent = False
    while True:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            absent = True
            break
        except OSError:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    return {"status": "confirmed" if absent and process.returncode is not None and not errors else "unconfirmed",
            "coverage": "owned_process_group_only", "leader_reaped": process.returncode is not None,
            "group_absent": absent, "errors": errors}


def run_logged(argv, directory, timeout, env=None, stdin=None, limit=16 * 1024 * 1024, cwd=None, grace_seconds=0, identity=None):
    """Bound the whole process group; never shell-expand prompts or CLI arguments."""
    timeout = remaining_timeout(timeout)
    if grace_seconds < 0:
        raise ValueError("negative shutdown grace")
    # Grace is reserved WITHIN the existing wall-clock bound, not added to it.
    grace = min(grace_seconds, timeout / 2)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    status = "completed"
    interrupted = False
    interrupt_sent = False
    pending_error = None
    signals = []

    def cancel(process, sig, reason):
        row = {"reason": reason, "signal": sig.name, "requested_ns": time.monotonic_ns()}
        try:
            os.killpg(process.pid, sig)
            row["delivery"] = "sent"
        except ProcessLookupError:
            row["delivery"] = "process_absent"
        except OSError:
            row["delivery"] = "failed"
        row["completed_ns"] = time.monotonic_ns()
        signals.append(row)
        return row["delivery"] == "sent"
    with (directory / "stdout.log").open("xb") as out, (directory / "stderr.log").open("xb") as err:
        if stdin is not None:
            write_new(directory / "stdin.txt", stdin)
        with (directory / "stdin.txt").open("rb") if stdin is not None else open(os.devnull, "rb") as source:
            process = subprocess.Popen(argv, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=env, cwd=cwd, start_new_session=True)
            selector = None
            captured = 0
            try:
                selector = selectors.DefaultSelector()
                selector.register(process.stdout, selectors.EVENT_READ, out)
                selector.register(process.stderr, selectors.EVENT_READ, err)
                while selector.get_map() or process.poll() is None:
                    elapsed = time.monotonic() - started
                    if elapsed >= timeout:
                        status = "timeout"
                        break
                    if grace and elapsed >= timeout - grace and not interrupt_sent and process.poll() is None:
                        status = "timeout"
                        interrupt_sent = cancel(process, signal.SIGINT, "timeout")
                    try:
                        for key, _ in selector.select(timeout=min(0.05, max(0, timeout - elapsed))):
                            data = os.read(key.fileobj.fileno(), 65536)
                            if not data:
                                selector.unregister(key.fileobj)
                                continue
                            retained = data[:max(0, limit - captured)]
                            key.data.write(retained)
                            key.data.flush()  # Also retain the bounded graceful-shutdown output.
                            captured += len(retained)
                            if len(retained) != len(data):
                                status = "output_limit"
                                break
                    except KeyboardInterrupt:
                        # Receipt is written before propagating operator cancellation.
                        interrupted = True
                        break
                    if status == "output_limit":
                        break
            except KeyboardInterrupt:
                interrupted = True
            except Exception as exc:
                status = "execution_error"
                pending_error = exc
            finally:
                reason = "operator" if interrupted else status
                kill_sent = False
                if reason in {"operator", "timeout", "output_limit", "execution_error"}:
                    kill_sent = cancel(process, signal.SIGKILL, reason)
                cleanup = stop_owned_group(process, kill_already_sent=kill_sent)
                if selector is not None:
                    selector.close()
                process.stdout.close()
                process.stderr.close()
    result = {"status": "interrupted" if interrupted else status, "returncode": process.returncode,
              "duration_seconds": round(time.monotonic() - started, 3), "started_at": started_at,
              "ended_at": datetime.now(timezone.utc).isoformat(), "cleanup": cleanup,
              "identity": dict(identity or {}), "clock_id": HOST_CLOCK_ID,
              "termination": {"reason": "operator" if interrupted else status,
                              "pid": process.pid, "pgid": process.pid, "signals": signals},
              "shutdown": {"grace_seconds": grace, "interrupt_sent": interrupt_sent,
                           "within_execution_budget": True}}
    write_json(directory / "process.json", result)
    if interrupted:
        raise KeyboardInterrupt()
    if pending_error:
        raise pending_error
    return result
