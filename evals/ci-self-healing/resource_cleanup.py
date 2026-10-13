"""Host-owned disposable directories, with evidence retained outside their scope.

No discovery/deletion of historical trials or arbitrary caller-supplied trees.
Only directories exclusively created by this live scope can be reclaimed.
"""
import os
from pathlib import Path
import stat
import sys
import time

from common import EvalError, digest, read_json, read_regular, snapshot, write_json

CONTRACT = "owned-resource-cleanup-v1"


def open_directory(path):
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(path).parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def identity(info):
    return [info.st_dev, info.st_ino]


def make_parents(root, parts):
    fd = open_directory(root)
    try:
        device = os.fstat(fd).st_dev
        for part in parts:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            if os.fstat(child).st_dev != device:
                os.close(child)
                raise EvalError("cleanup parent crosses mount")
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def inventory(path):
    path = Path(path)
    if path.is_dir() and not path.is_symlink():
        return {"kind": "directory", "mode": stat.S_IMODE(path.stat().st_mode),
                "files": snapshot(path, skip_git=False)}
    return {"kind": "file", "sha256": digest(read_regular(path)), "mode": stat.S_IMODE(path.stat().st_mode)}


def erase_contents(fd, device, deadline, remaining):
    # Descriptor-relative traversal never follows a link. Recheck identities
    # before unlinking; a concurrent writer invalidates completion.
    for name in os.listdir(fd):
        remaining[0] -= 1
        if remaining[0] < 0 or time.monotonic() >= deadline:
            raise EvalError("cleanup budget exhausted")
        before = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if before.st_dev != device:
            raise EvalError("cleanup mount boundary")
        if stat.S_ISDIR(before.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                if identity(os.fstat(child)) != identity(before):
                    raise EvalError("cleanup directory replaced")
                erase_contents(child, device, deadline, remaining)
                if identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != identity(before):
                    raise EvalError("cleanup directory replaced")
                os.rmdir(name, dir_fd=fd)
            finally:
                os.close(child)
        elif stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            if identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != identity(before):
                raise EvalError("cleanup file replaced")
            os.unlink(name, dir_fd=fd)
        else:
            raise EvalError("unsupported cleanup entry")


class OwnedResources:
    def __init__(self, root, stage, *, evidence=()):
        self.root = Path(root).absolute()
        if self.root != self.root.resolve() or not self.root.is_dir():
            raise EvalError("cleanup requires an existing canonical owner root")
        if not stage or Path(stage).name != stage or stage in {".", ".."}:
            raise EvalError("invalid cleanup stage")
        self.stage = stage
        self.directory = self.root / "resource-cleanup" / stage
        parent = make_parents(self.root, ("resource-cleanup",))
        try:
            os.mkdir(stage, 0o700, dir_fd=parent)
        finally:
            os.close(parent)
        self.root_identity = identity(self.root.stat())
        self.owned, self.writers, self.notes = [], [], []
        self.handles = []  # Pin allocated inodes until scope exit, preventing reuse.
        self.evidence = list(evidence)
        if any(Path(p).is_absolute() or ".." in Path(p).parts for p in self.evidence):
            raise EvalError("invalid preserved evidence path")
        self.required = []
        self.hold_reason = None
        self.binding = None
        self.attempted = False
        write_json(self.directory / "scope.json", {"contract": CONTRACT, "root": str(self.root),
                                                   "root_identity": self.root_identity, "stage": stage})

    def mkdir(self, relative):
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] == "resource-cleanup":
            raise EvalError("invalid disposable path")
        path = self.root / rel
        if any(path == p or path in p.parents or p in path.parents for p, _ in self.owned):
            raise EvalError("overlapping disposable paths")
        # Parents are host-created scaffolding, never reclaimed implicitly.
        parent = make_parents(self.root, rel.parts[:-1])
        try:
            write_json(self.directory / f"intent-{len(self.owned):03}.json", {"path": str(path)})
            os.mkdir(path.name, 0o700, dir_fd=parent)
            handle = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            self.handles.append(handle)
            info = os.fstat(handle)
            self.owned.append((path, identity(info)))
            write_json(self.directory / f"owned-{len(self.owned):03}.json", {"path": str(path), "identity": identity(info)})
        finally:
            os.close(parent)
        return path

    def watch(self, process_file, *, group_receipt=False):
        self.writers.append((Path(process_file), group_receipt))

    def writers_stopped(self):
        for path, direct in self.writers:
            try:
                data = read_json(path)
            except (OSError, ValueError, EvalError):
                return False
            if (data if direct else data.get("cleanup", {})).get("status") != "confirmed":
                return False
        return True

    def finish(self):
        if self.binding is not None:
            return self.binding
        if self.attempted:
            raise EvalError("cleanup receipt publication previously failed")
        self.attempted = True
        result = {"contract": CONTRACT, "stage": self.stage, "root": str(self.root),
                  "status": "incomplete", "removed": [], "retained": [], "gaps": [],
                  "notes": self.notes, "evidence": {}, "remaining": []}
        try:
            if identity(self.root.stat()) != self.root_identity or self.root != self.root.resolve():
                raise EvalError("cleanup owner root changed")
            if not self.writers_stopped():
                raise EvalError("writers not confirmed stopped")
            if self.hold_reason:
                raise EvalError(self.hold_reason)
            for relative in self.required:
                if Path(relative).is_absolute() or ".." in Path(relative).parts or not (self.root / relative).exists():
                    raise EvalError("required evidence missing: " + relative)
            for relative in self.evidence:
                path = self.root / relative
                if any(path == p or p in path.parents or path in p.parents for p, _ in self.owned):
                    raise EvalError("evidence overlaps disposable scope")
                # Optional evidence paths can be absent on an early failure.
                if os.path.lexists(path):
                    result["evidence"][relative] = inventory(path)
            # Persist and read back the exact preservation inventory BEFORE deletion.
            preserved = self.directory / "preserved.json"
            write_json(preserved, result["evidence"])
            if read_json(preserved) != result["evidence"]:
                raise EvalError("preservation receipt readback failed")
            deadline, remaining = time.monotonic() + 20, [100000]
            for path, expected in self.owned:
                try:
                    parent = open_directory(path.parent)
                    try:
                        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                        if not stat.S_ISDIR(info.st_mode) or identity(info) != expected:
                            raise EvalError("disposable root replaced")
                        child = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                        try:
                            if identity(os.fstat(child)) != expected:
                                raise EvalError("disposable root replaced")
                            erase_contents(child, info.st_dev, deadline, remaining)
                            if identity(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) != expected:
                                raise EvalError("disposable root replaced")
                            os.rmdir(path.name, dir_fd=parent)
                        finally:
                            os.close(child)
                    finally:
                        os.close(parent)
                    if os.path.lexists(path):
                        raise EvalError("disposable path remains")
                    result["removed"].append(str(path.relative_to(self.root)))
                except (OSError, EvalError) as exc:
                    result["gaps"].append({"path": str(path.relative_to(self.root)), "error": str(exc)})
            for relative, before in result["evidence"].items():
                if inventory(self.root / relative) != before:
                    raise EvalError("preserved evidence changed: " + relative)
            if not result["gaps"]:
                result["status"] = "cleaned" if self.owned else "not_applicable"
        except (OSError, ValueError, EvalError) as exc:
            result["gaps"].append({"error": str(exc)})
        result["remaining"] = [str(p.relative_to(self.root)) for p, _ in self.owned if os.path.lexists(p)]
        result["retained"] = list(result["evidence"])
        path = self.directory / "receipt.json"
        write_json(path, result)
        if read_json(path) != result:
            raise EvalError("cleanup receipt readback failed")
        self.binding = {"contract": CONTRACT, "status": result["status"],
                        "receipt": str(path), "sha256": digest(read_regular(path))}
        return self.binding

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        try:
            if not self.attempted:
                try:
                    self.finish()
                except Exception as exc:
                    if kind is None:
                        raise
                    print("resource cleanup receipt unavailable: " + type(exc).__name__, file=sys.stderr)
        finally:
            for handle in self.handles:
                os.close(handle)
            self.handles.clear()


def validate_cleanup(binding):
    if not isinstance(binding, dict) or binding.get("contract") != CONTRACT:
        raise EvalError("unsupported resource cleanup receipt")
    raw = read_regular(binding["receipt"])
    if digest(raw) != binding.get("sha256"):
        raise EvalError("resource cleanup evidence changed")
    data = read_json(binding["receipt"])
    if data.get("status") != binding.get("status") or data.get("contract") != CONTRACT:
        raise EvalError("resource cleanup binding mismatch")
    root = Path(data["root"])
    for relative in [*data["removed"], *data["evidence"]]:
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise EvalError("invalid resource receipt path")
    if any(os.path.lexists(root / name) for name in data["removed"]):
        raise EvalError("cleaned resource reappeared")
    for name, before in data["evidence"].items():
        if inventory(root / name) != before:
            raise EvalError("retained cleanup evidence changed: " + name)
    return data["status"]


def cleanup_bindings(data):
    return {key: data[key] for key in ("resource_cleanup", "baseline_resource_cleanup") if key in data}


def cleanup_complete(data):
    return all(validate_cleanup(value) in {"cleaned", "not_applicable"}
               for value in cleanup_bindings(data).values())
