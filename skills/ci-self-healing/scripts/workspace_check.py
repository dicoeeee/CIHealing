#!/usr/bin/env python3
"""Capture/compare workspace facts. Never build, clean/restore source, or execute Git.

Python 3.9+, POSIX with descriptor-relative no-follow file operations.
The manifest is a comparison baseline, NOT a backup or security boundary.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import sys


SCHEMA = "ci-workspace-check/v1"
CHUNK = 64 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
EXCLUSIONS = ["root/.git (metadata only; nested repositories are not skipped)"]


class CheckError(Exception):
    def __init__(self, code, message, path=None, phase=None, errno=None):
        super().__init__(message)
        self.code, self.path = code, path
        self.phase, self.errno = phase, errno


def fail(code, message, path=None):
    raise CheckError(code, message, path)


@contextmanager
def error_context(phase, path):
    """Keep the innermost known I/O location; never infer a missing file."""
    try:
        yield
    except CheckError as error:
        if error.phase is None:
            error.phase = phase
        if error.path is None and path is not None:
            error.path = str(path)
        raise
    except (OSError, ValueError, NotImplementedError) as error:
        raise CheckError("io_or_format_error", str(error),
                         str(path) if path is not None else None,
                         phase, getattr(error, "errno", None)) from error


def warn(phase, path, error):
    # Warnings must not change an already published JSON receipt. A broken
    # diagnostics channel must not mask the original failure either.
    warning = {"warning": "state_cleanup_failed", "phase": phase,
               "path": str(path), "errno": getattr(error, "errno", None), "message": str(error)}
    try:
        print(json.dumps(warning, ensure_ascii=True), file=sys.stderr)
    except (OSError, ValueError):
        pass


def close_fd(fd, phase, path, published=False):
    primary_error = sys.exc_info()[0] is not None
    try:
        os.close(fd)
    except OSError as error:
        if published or primary_error:
            warn(phase, path, error)
        else:
            raise


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def stamp():
    return datetime.now(timezone.utc).isoformat()


def require_platform():
    if (os.name != "posix" or not hasattr(os, "O_NOFOLLOW")
            or os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd
            or os.readlink not in os.supports_dir_fd or os.listdir not in os.supports_fd):
        fail("unsupported_platform", "Requires POSIX descriptor-relative no-follow operations.")


def signature(info):
    # Used only to detect drift DURING observation, not to reject a valid restore
    # merely because its inode or timestamps differ from the captured file.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def open_directory(path):
    """Open an already canonical absolute path without following swapped parents."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(path).parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        close_fd(fd, "path_open", path)
        raise


def roots_from(values):
    roots = []
    for raw in values:
        if not raw:
            fail("invalid_root", "Use explicit nonempty workspace paths.", raw)
        with error_context("preflight", raw):
            path = Path(raw).resolve(strict=True)
            if not path.is_dir() or path == Path(path.anchor):
                fail("invalid_root", "Root must be a specific existing directory, not a filesystem root.", str(path))
        roots.append(str(path))
    roots.sort()
    for index, path in enumerate(roots):
        for previous in roots[:index]:
            if Path(path) == Path(previous) or Path(previous) in Path(path).parents:
                fail("overlapping_roots", "Roots must be unique and disjoint.", path)
    return roots


def outside_path(raw, roots):
    # Parent must already exist; this tool never creates source/state directories.
    with error_context("preflight", raw):
        path = Path(raw).absolute()
        path = path.parent.resolve(strict=True) / path.name
        if any(path == Path(root) or Path(root) in path.parents for root in roots):
            fail("state_inside_workspace", "Manifest/output must be outside every checked root.", str(path))
    return path


def yaml_comment_free(line):
    """Strip only unquoted YAML comments; retain uncertainty for open quotes."""
    quote = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote == '"' and char == "\\":
            index += 2
            continue
        if quote == "'" and line[index:index + 2] == "''":
            index += 2
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in {"'", '"'} and (index == 0 or line[index - 1].isspace() or line[index - 1] in ":[{,"):
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip(), True
        index += 1
    return line.rstrip(), quote is None


def ordinary_scalar(value):
    if re.fullmatch(r"'(?:[^']|'')*'", value) or re.fullmatch(r'"(?:[^"\\]|\\.)*"', value):
        return True
    # Limit the relaxation to unambiguous, single-line scalar values. Flow
    # collections, tags, anchors, aliases and multiline syntax stay conservative.
    return (bool(value) and value[0] not in "[{&*!|>'\"%@`?-"
            and not re.search(r":(?:\s|$)", value))


def check_markdown_access(fd, path):
    """Honor explicit frontmatter restrictions before hashing the Markdown body.

    No YAML dependency: ambiguous ai_access syntax fails closed. This is not a
    general repository permission interpreter; callers must honor other rules.
    """
    if Path(path).suffix.lower() not in {".md", ".markdown"}:
        return
    with os.fdopen(os.dup(fd), "rb", buffering=0) as stream:
        first = stream.readline(16).lstrip(b"\xef\xbb\xbf")
        if first.strip() != b"---":
            os.lseek(fd, 0, os.SEEK_SET)
            return
        header = []
        size = 0
        while True:
            line = stream.readline(CHUNK + 1)
            size += len(line)
            if not line or size > CHUNK:
                fail("ambiguous_frontmatter", "Cannot safely complete Markdown frontmatter inspection.", path)
            if line.strip() in {b"---", b"..."}:
                break
            header.append(line.decode("utf-8", "strict"))
        for line in header:
            clean, closed = yaml_comment_free(line)
            if "ai_access" not in clean:
                continue
            field = re.fullmatch(r"\s*([A-Za-z_][\w-]*|'[A-Za-z_][\w-]*'|\"[A-Za-z_][\w-]*\")\s*:\s*(.*?)\s*", clean)
            if closed and field:
                key, value = field.group(1).strip("'\""), field.group(2)
                if key == "ai_access" and value == "true":
                    continue
                if key != "ai_access" and ordinary_scalar(value):
                    continue
            fail("markdown_access_restricted", "Markdown access is false or cannot be safely established.", path)
    os.lseek(fd, 0, os.SEEK_SET)


class Scanner:
    def __init__(self, max_entries, max_bytes):
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.count, self.bytes = 0, 0
        self.observations = {}

    def entry(self, info, path):
        self.count += 1
        if self.count > self.max_entries:
            fail("entry_limit", "Entry limit reached; scan is incomplete.", path)
        return {"mode": stat.S_IMODE(info.st_mode)}

    def file_hash(self, parent, name, before, path):
        with error_context("scan", path):
            return self._file_hash(parent, name, before, path)

    def _file_hash(self, parent, name, before, path):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or signature(before) != signature(os.fstat(fd)):
                fail("source_changed", "File changed before it could be read.", path)
            check_markdown_access(fd, path)
            result = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                block = os.read(fd, min(CHUNK, remaining))
                if not block:
                    fail("source_changed", "File shrank while being read.", path)
                remaining -= len(block)
                self.bytes += len(block)
                if self.bytes > self.max_bytes:
                    fail("byte_limit", "Byte limit reached; scan is incomplete.", path)
                result.update(block)
            if os.read(fd, 1) or signature(before) != signature(os.fstat(fd)):
                fail("source_changed", "File changed while being read.", path)
            return result.hexdigest()
        finally:
            close_fd(fd, "scan", path)

    def walk(self, fd, root, relative, device, entries, depth=0):
        with error_context("scan", Path(root) / relative):
            self._walk(fd, root, relative, device, entries, depth)

    def _walk(self, fd, root, relative, device, entries, depth):
        if depth > 128:
            fail("depth_limit", "Directory nesting exceeds supported depth.", relative)
        before = os.fstat(fd)
        key = relative or "."
        record = self.entry(before, str(Path(root) / relative))
        record["kind"] = "directory"
        entries[key] = record
        self.observations[(root, key)] = signature(before)
        for name in sorted(os.listdir(fd)):
            child_path = f"{relative}/{name}" if relative else name
            display = str(Path(root) / child_path)
            if name == ".git":
                if not relative:
                    continue
                fail("nested_repository", "Declare separate disjoint repository roots; nested .git is not silently skipped.", display)
            with error_context("scan", display):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if info.st_dev != device:
                    fail("mount_boundary", "Nested filesystem is outside supported scan scope.", display)
                self.observations[(root, child_path)] = signature(info)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        if signature(info) != signature(os.fstat(child)):
                            fail("source_changed", "Directory changed before traversal.", display)
                        self.walk(child, root, child_path, device, entries, depth + 1)
                    finally:
                        close_fd(child, "scan", display)
                else:
                    record = self.entry(info, display)
                    if stat.S_ISREG(info.st_mode):
                        record.update(kind="file", size=info.st_size,
                                      sha256=self.file_hash(fd, name, info, display))
                    elif stat.S_ISLNK(info.st_mode):
                        record.update(kind="symlink", target=os.readlink(name, dir_fd=fd))
                    else:
                        fail("unsupported_entry", "Special files cannot be checked as regular workspace content.", display)
                    entries[child_path] = record
                if signature(info) != signature(os.stat(name, dir_fd=fd, follow_symlinks=False)):
                    fail("source_changed", "Entry changed during traversal.", display)
        if signature(before) != signature(os.fstat(fd)):
            fail("source_changed", "Directory changed during traversal.", str(Path(root) / relative))

    def scan(self, roots):
        result = []
        for root in roots:
            with error_context("scan", root):
                fd = open_directory(root)
                try:
                    identity = os.fstat(fd)
                    entries = {}
                    self.walk(fd, root, "", identity.st_dev, entries)
                    if signature(identity) != signature(os.stat(root, follow_symlinks=False)):
                        fail("source_changed", "Root identity changed during scan.", root)
                    result.append({"path": root, "device": identity.st_dev,
                                   "inode": identity.st_ino, "entries": entries})
                finally:
                    close_fd(fd, "scan", root)
        return result


def stable_scan(roots, max_entries, max_bytes):
    first = Scanner(max_entries, max_bytes)
    contents = first.scan(roots)
    second = Scanner(max_entries, max_bytes)
    observed = second.scan(roots)
    if contents != observed or first.observations != second.observations:
        fail("source_changed", "Two observations differ; stop writers before retrying.")
    return observed


def write_new(path, value, phase="state_write"):
    """Publish a complete private file with an exclusive same-directory link.

    No directory fsync/power-loss guarantee. Once linked, ordinary housekeeping
    failures are stderr warnings, not a different result in the stdout receipt.
    """
    with error_context(phase, path):
        _write_new(path, value, phase)


def _write_new(path, value, phase):
    payload = canonical(value) + b"\n"
    if len(payload) > MAX_MANIFEST_BYTES:
        fail("output_limit", "Output exceeds the supported size limit.", str(path))
    parent = open_directory(str(path.parent))
    temporary = ".workspace-check-" + secrets.token_hex(16) + ".tmp"
    identity = None
    published = False
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            info = os.fstat(fd)
            identity = (info.st_dev, info.st_ino)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(fd, remaining)
                if written == 0:
                    raise OSError(errno.EIO, "State file write made no progress.")
                remaining = remaining[written:]
            # os.write is unbuffered: no userspace flush remains before fsync.
            os.fsync(fd)
        finally:
            close_fd(fd, phase, path)
        os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        published = True
    finally:
        if identity is not None:
            try:
                info = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                if (info.st_dev, info.st_ino) != identity:
                    raise OSError(errno.EBUSY, "Temporary state path no longer belongs to this write.")
                os.unlink(temporary, dir_fd=parent)
            except OSError as error:
                warn(phase + "_cleanup", path.parent / temporary, error)
        close_fd(parent, phase, path.parent, published=published)


def unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            fail("invalid_manifest", "Duplicate JSON keys are not allowed.")
        value[key] = item
    return value


def read_manifest(path):
    with error_context("manifest_read", path):
        parent = open_directory(str(path.parent))
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_MANIFEST_BYTES:
                    fail("invalid_manifest", "Manifest must be a bounded regular file.", str(path))
                blocks = []
                remaining = MAX_MANIFEST_BYTES + 1
                while remaining:
                    block = os.read(fd, min(CHUNK, remaining))
                    if not block:
                        break
                    blocks.append(block)
                    remaining -= len(block)
                if signature(before) != signature(os.fstat(fd)):
                    fail("source_changed", "Manifest changed while being read.", str(path))
            finally:
                close_fd(fd, "manifest_read", path)
        finally:
            close_fd(parent, "manifest_read", path)
        return json.loads(b"".join(blocks), object_pairs_hook=unique_pairs)


def validate_manifest(manifest, roots, invocation_id, candidate_id, expected_hash):
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        fail("invalid_manifest", "Unknown manifest schema.")
    if digest(manifest) != expected_hash:
        fail("manifest_hash_mismatch", "Use the exact digest returned by the original capture.")
    if (manifest.get("invocation_id"), manifest.get("candidate_id")) != (invocation_id, candidate_id):
        fail("binding_mismatch", "Invocation/candidate binding does not match capture.")
    if manifest.get("exclusions") != EXCLUSIONS or not isinstance(manifest.get("roots"), list):
        fail("invalid_manifest", "Manifest scan policy is invalid.")
    if [root.get("path") for root in manifest["roots"] if isinstance(root, dict)] != roots:
        fail("scope_mismatch", "Pass the same complete root set used for capture.")
    for root in manifest["roots"]:
        if not isinstance(root, dict) or not all(type(root.get(k)) is int for k in ("device", "inode")):
            fail("invalid_manifest", "Root identity is invalid.")
        entries = root.get("entries")
        if (not isinstance(entries, dict) or not all(isinstance(value, dict) for value in entries.values())
                or entries.get(".", {}).get("kind") != "directory"):
            fail("invalid_manifest", "Root directory entry is required.")
        for path, entry in entries.items():
            if (not isinstance(path, str) or not path or "\0" in path
                    or (path != "." and (path.startswith("/") or any(p in {"", ".", "..", ".git"} for p in path.split("/"))))):
                fail("invalid_manifest", "Invalid manifest-relative path.")
            if not isinstance(entry, dict) or type(entry.get("mode")) is not int or not 0 <= entry["mode"] <= 0o7777:
                fail("invalid_manifest", "Entry mode is invalid.", path)
            if path != ".":
                parent = str(PurePosixPath(path).parent)
                if entries.get(parent, {}).get("kind") != "directory":
                    fail("invalid_manifest", "Entry parent is absent or not a directory.", path)
            kind = entry.get("kind")
            fields = {"kind", "mode"}
            if kind == "file":
                fields |= {"size", "sha256"}
                if type(entry.get("size")) is not int or entry["size"] < 0 or not re.fullmatch("[0-9a-f]{64}", str(entry.get("sha256"))):
                    fail("invalid_manifest", "File size/hash is invalid.", path)
            elif kind == "symlink":
                fields.add("target")
                if not isinstance(entry.get("target"), str) or "\0" in entry["target"]:
                    fail("invalid_manifest", "Link target is invalid.", path)
            elif kind != "directory":
                fail("invalid_manifest", "Unsupported entry type.", path)
            if set(entry) != fields:
                fail("invalid_manifest", "Unexpected or missing entry fields.", path)


def compare(before, after, limit):
    counts = {"added": 0, "missing": 0, "changed": 0}
    differences = []
    for old, current in zip(before, after):
        if (old["device"], old["inode"]) != (current["device"], current["inode"]):
            fail("root_identity_changed", "Workspace root was replaced; confirm scope before proceeding.", old["path"])
        for path in sorted(set(old["entries"]) | set(current["entries"])):
            left, right = old["entries"].get(path), current["entries"].get(path)
            if left == right:
                continue
            change = "added" if left is None else "missing" if right is None else "changed"
            counts[change] += 1
            if len(differences) < limit:
                differences.append({"root": old["path"], "path": path, "change": change,
                                    "before": left, "after": right})
    return counts, differences


def positive(raw):
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("Must be positive.")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    for operation in ("capture", "check"):
        command = sub.add_parser(operation, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
        command.add_argument("--root", action="append", required=True, help="Explicit source root; repeat for disjoint repositories.")
        command.add_argument("--manifest", required=True, help="New manifest (capture) or original manifest (check), outside roots.")
        command.add_argument("--invocation-id", required=True)
        command.add_argument("--candidate-id", required=True, help="Reference to the actual pre-build candidate; not a Git HEAD substitute.")
        command.add_argument("--max-entries", type=positive, default=100000, help="Per-pass entry budget, including directories.")
        command.add_argument("--max-bytes", type=positive, default=10 * 1024 ** 3, help="Per-pass content byte budget; two passes are used.")
        if operation == "check":
            command.add_argument("--manifest-sha256", required=True, help="Canonical JSON digest returned by capture, not a file-byte checksum; never recompute to accept edits.")
            command.add_argument("--max-differences", type=positive, default=500, help="Maximum displayed differences; full counts and mismatch result are retained.")
            command.add_argument("--output", help="Optional new JSON report outside roots; stdout is always emitted.")
    args = parser.parse_args(argv)
    result = {"schema": SCHEMA, "operation": args.operation, "started_at": stamp(),
              "invocation_id": args.invocation_id, "candidate_id": args.candidate_id,
              "compared": ["paths", "entry_types", "regular_file_contents", "permission_bits", "symlink_target_text"],
              "not_compared": ["git_metadata", "symlink_target_contents", "owners_acl_xattrs", "timestamps", "hardlink_topology"],
              "result": "incomplete", "gaps": []}
    code = 2
    phase, context_path = "preflight", None
    try:
        require_platform()
        if any(not value.strip() or len(value) > 512 for value in (args.invocation_id, args.candidate_id)):
            fail("invalid_binding", "Invocation and candidate references must be nonempty and bounded.")
        roots = roots_from(args.root)
        result["roots"] = roots
        result["exclusions"] = EXCLUSIONS
        manifest_path = outside_path(args.manifest, roots)
        if args.operation == "capture":
            if os.path.lexists(manifest_path):
                fail("manifest_exists", "Capture never replaces an existing baseline.", str(manifest_path))
            phase = "scan"
            contents = stable_scan(roots, args.max_entries, args.max_bytes)
            manifest = {"schema": SCHEMA, "invocation_id": args.invocation_id,
                        "candidate_id": args.candidate_id, "captured_at": stamp(),
                        "exclusions": EXCLUSIONS, "roots": contents}
            phase, context_path = "manifest_write", str(manifest_path)
            write_new(manifest_path, manifest, phase=phase)
            phase = "manifest_read"
            if read_manifest(manifest_path) != manifest:
                fail("manifest_readback_failed", "Captured manifest failed readback.", str(manifest_path))
            result.update(result="captured", manifest=str(manifest_path), manifest_sha256=digest(manifest), observed_at=stamp())
            code = 0
        else:
            output = outside_path(args.output, roots) if args.output else None
            if output is not None and os.path.lexists(output):
                fail("output_exists", "Check never overwrites an existing output or source alias.", str(output))
            phase, context_path = "manifest_read", str(manifest_path)
            manifest = read_manifest(manifest_path)
            phase = "manifest_validate"
            validate_manifest(manifest, roots, args.invocation_id, args.candidate_id, args.manifest_sha256)
            phase, context_path = "scan", None
            contents = stable_scan(roots, args.max_entries, args.max_bytes)
            phase = "compare"
            counts, differences = compare(manifest["roots"], contents, args.max_differences)
            total = sum(counts.values())
            result.update(result="mismatch" if total else "match", manifest=str(manifest_path),
                          manifest_sha256=args.manifest_sha256, counts=counts, differences=differences,
                          differences_truncated=total > len(differences), observed_at=stamp())
            code = 1 if total else 0
            if output is not None:
                phase, context_path = "report_write", str(output)
                write_new(output, result, phase=phase)
    except (CheckError, OSError, ValueError, UnicodeError, RecursionError, KeyboardInterrupt) as error:
        result["result"] = "incomplete"
        result["observed_at"] = stamp()
        result["gaps"].append({"code": getattr(error, "code", "interrupted" if isinstance(error, KeyboardInterrupt) else "io_or_format_error"),
                               "message": str(error), "path": getattr(error, "path", None) or context_path,
                               "phase": getattr(error, "phase", None) or phase,
                               "errno": getattr(error, "errno", None)})
        code = 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
