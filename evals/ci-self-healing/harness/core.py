"""Small, standard-library contracts shared by catalog, runner and CLI."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time

ENGINE = Path(__file__).resolve().parents[1]
REPO = ENGINE.parents[1]
SPEC = ENGINE / "spec"


class EvaluationError(ValueError):
    pass


def read_allowed(path: Path) -> bytes:
    """Check Markdown access before consuming its body; reject symlinks."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise EvaluationError(f"not a regular input: {path}")
    if path.stat().st_size > 16 * 1024 * 1024:
        raise EvaluationError(f"input exceeds evidence limit: {path}")
    if path.suffix.lower() == ".md":
        with path.open("rb") as stream:
            first = stream.readline().decode("utf-8-sig").strip()
            if first == "---":
                header = []
                for line in stream:
                    text = line.decode("utf-8").strip()
                    if text == "---":
                        break
                    header.append(text)
                    if sum(map(len, header)) > 65536:
                        raise EvaluationError("oversized Markdown frontmatter")
                else:
                    raise EvaluationError(f"unclosed frontmatter: {path}")
                entries = [line for line in header if re.match(r"['\"]?ai_access['\"]?\s*:", line)]
                if entries and entries != ["ai_access: true"]:
                    raise EvaluationError(f"restricted or ambiguous Markdown access: {path}")
    return path.read_bytes()


def read_json(path):
    return json.loads(read_allowed(Path(path)))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def save_json(path, value):
    """Atomic replacement within the owning run; no partial receipts."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def tree_files(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise EvaluationError("invalid source directory")
    output = {}
    for directory, folders, files in os.walk(root, followlinks=False):
        folders[:] = sorted(f for f in folders if f not in {".git", "__pycache__"})
        for name in [*folders, *files]:
            if (Path(directory) / name).is_symlink():
                raise EvaluationError("symlink in frozen inputs")
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            path = Path(directory) / name
            output[path.relative_to(root).as_posix()] = read_allowed(path)
    if not output or sum(map(len, output.values())) > 32 * 1024 * 1024:
        raise EvaluationError("empty or oversized input bundle")
    return output


def tree_digest(root):
    return sha({name: sha(data) for name, data in tree_files(root).items()})


def freeze_tree(source, destination):
    rows = tree_files(source)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for name, body in rows.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return sha({name: sha(data) for name, data in rows.items()})


def utc():
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    """Reserve sessions durably before starting; interrupted reservations are spent."""
    def __init__(self, path, maximum, wall_seconds):
        if type(maximum) is not int or maximum < 1 or type(wall_seconds) is not int or wall_seconds < 1:
            raise EvaluationError("positive integer session and wall budgets required")
        self.path = Path(path)
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.deadline = time.monotonic() + wall_seconds
        self.data = {"schema_version": "ci-eval-ledger/v1", "created_at": utc(),
                     "maximum_sessions": maximum, "wall_seconds": wall_seconds, "sessions": []}
        # Exclusive run ownership is enforced by the caller. Never replay a ledger.
        if self.path.exists():
            raise EvaluationError("ledger exists; unknown starts cannot be replayed")
        save_json(self.path, self.data)

    def remaining(self):
        return 0 if self.cancelled.is_set() else max(0, self.deadline - time.monotonic())

    def reserve(self, stage, trial_id):
        with self.lock:
            if self.remaining() < 1 or len(self.data["sessions"]) >= self.data["maximum_sessions"]:
                raise EvaluationError("session or wall budget exhausted")
            entry = {"id": len(self.data["sessions"]) + 1, "stage": stage, "trial_id": trial_id,
                     "status": "reserved", "reserved_at": utc()}
            self.data["sessions"].append(entry)
            save_json(self.path, self.data)
            return entry["id"]

    def finish(self, entry_id, status):
        with self.lock:
            entry = self.data["sessions"][entry_id - 1]
            entry.update(status=status, finished_at=utc())
            save_json(self.path, self.data)


def derive_status(checks, *, execution_complete=True, covered=True):
    statuses = [row.get("status", row.get("verdict")) for row in checks]
    if "fail" in statuses:
        return "failed"
    if (not execution_complete or not covered or not statuses or "pass" not in statuses
            or any(status not in {"pass", "not_applicable"} for status in statuses)):
        return "incomplete"
    return "passed"
