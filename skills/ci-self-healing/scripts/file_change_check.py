#!/usr/bin/env python3
"""Read-only validation of file_change.json; does not inspect repos or make patches."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys


MAX_BYTES = 2 * 1024 * 1024
OPERATIONS = {"modify", "create", "delete"}


class CheckUnavailable(Exception):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 对象包含重复字段。")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("JSON 不允许 NaN 或 Infinity。")


def valid_text(value):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def validate(data: bytes) -> list[dict]:
    errors = []

    def issue(code, location, message):
        errors.append({"code": code, "location": location, "message": message})

    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object,
                              parse_constant=reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        issue("invalid_json", "$", "须为有效 UTF-8 JSON，不含尾逗号、重复字段或非标准数值。")
        return errors
    if not isinstance(document, dict):
        issue("object_required", "$", "顶层须为对象。")
        return errors
    if set(document) != {"version", "changes"}:
        issue("fields", "$", "顶层仅包含必填的 version 和 changes。")
    if document.get("version") != "0.1":
        issue("version", "$.version", 'version 固定为字符串 "0.1"。')
    changes = document.get("changes")
    if not isinstance(changes, list):
        issue("changes_array", "$.changes", "changes 须为数组；无代码修改时使用 []。")
        return errors
    seen = set()
    for index, item in enumerate(changes):
        location = f"$.changes[{index}]"
        if not isinstance(item, dict):
            issue("entry_object", location, "变更条目须为对象。")
            continue
        if set(item) != {"file", "op", "repo", "desc"}:
            issue("entry_fields", location, "条目仅包含必填的 file、op、repo、desc。")
        file, op, repo, desc = (item.get(key) for key in ("file", "op", "repo", "desc"))
        if not valid_text(file) or (
            "\\" in file
            or any(part in {"", ".", ".."} for part in file.split("/"))
        ):
            issue("file_path", location + ".file",
                  "file 须为 Git root 相对 POSIX 文件路径，无绝对前缀、反斜杠或空/点/父路径段。")
        if not isinstance(op, str) or op not in OPERATIONS:
            issue("operation", location + ".op", "op 仅允许 modify、create、delete。")
        if not valid_text(repo) or (
            repo != repo.strip() or repo in {".", ".."} or "/" in repo or "\\" in repo
        ):
            issue("repository", location + ".repo", "repo 须为非空仓库名，不是路径或 owner/repo。")
        if not valid_text(desc):
            issue("description", location + ".desc", "desc 须为非空 UTF-8 修改说明。")
        if isinstance(repo, str) and isinstance(file, str):
            key = (repo, file)
            if key in seen:
                issue("duplicate_file", location, "同一 (repo, file) 只能有一条最终记录。")
            seen.add(key)
    return errors


def read_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise CheckUnavailable("输入须为普通文件。")
        data = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
        current = path.lstat()
        identity = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                                 info.st_mtime_ns, info.st_ctime_ns)
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise CheckUnavailable("读取期间文件发生变化或路径被替换。")
    if len(data) > MAX_BYTES:
        raise CheckUnavailable("清单超过 2 MiB 读取上限。")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="Path to file_change.json.")
    args = parser.parse_args()
    try:
        path = Path(args.manifest)
        data = read_bytes(path)
        errors = validate(data)
        if path.name != "file_change.json":
            errors.append({"code": "filename", "location": "$",
                           "message": "文件交付使用 file_change.json。"})
        result = {"format_status": "invalid" if errors else "valid",
                  "sha256": hashlib.sha256(data).hexdigest(), "errors": errors}
        code = 1 if errors else 0
    except (OSError, CheckUnavailable) as exc:
        result = {"format_status": "incomplete",
                  "errors": [{"code": "input_unavailable", "message": str(exc)}]}
        code = 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
