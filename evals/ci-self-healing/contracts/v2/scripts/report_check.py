#!/usr/bin/env python3
"""Read-only structural checks for analysis-fix.md; no semantic verdicts."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


TEMPLATE = Path(__file__).resolve().parents[1] / "assets" / "analysis-fix.template.md"
MAX_BYTES = 2 * 1024 * 1024
OUTCOMES = ("local_verified", "repair_advised", "not_applicable", "unresolved")


@dataclass
class Table:
    line: int
    header: list[str]
    rows: list[list[str]]


class CheckUnavailable(Exception):
    pass


def visible_lines(text: str) -> tuple[list[str], list[dict]]:
    """Ignore fenced evidence and comments, keeping original line numbers."""
    lines, errors = [], []
    fence, comment = None, False
    for number, raw in enumerate(text.splitlines(), 1):
        if fence:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}\s*", raw):
                fence = None
            lines.append("")
            continue
        value = ""
        rest = raw
        while rest:
            if comment:
                end = rest.find("-->")
                if end < 0:
                    rest = ""
                else:
                    comment, rest = False, rest[end + 3:]
            else:
                start = rest.find("<!--")
                if start < 0:
                    value += rest
                    rest = ""
                else:
                    value += rest[:start]
                    comment, rest = True, rest[start + 4:]
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", value)
        if opening:
            marker = opening.group(1)
            fence = (marker[0], len(marker), number)
            value = ""
        lines.append(value)
    if fence:
        errors.append({"code": "unclosed_fence", "line": fence[2], "message": "代码围栏未闭合。"})
    if comment:
        errors.append({"code": "unclosed_comment", "line": len(lines), "message": "HTML 注释未闭合。"})
    return lines, errors


def headings(lines: list[str]) -> list[tuple[int, int, str]]:
    found = []
    for index, line in enumerate(lines):
        match = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*$", line)
        if match:
            title = re.sub(r"\s+#+\s*$", "", match.group(2))
            found.append((index, len(match.group(1)), title))
        elif index and lines[index - 1].strip() and re.fullmatch(r" {0,3}(=+|-+)\s*", line):
            found.append((index - 1, 1 if line.lstrip().startswith("=") else 2, lines[index - 1].strip()))
    return found


def cells(line: str) -> list[str]:
    # GFM requires even pipes in inline code to be escaped in table cells.
    text = line.strip()[1:-1]
    result, start, slashes = [], 0, 0
    for index, char in enumerate(text):
        if char == "|" and slashes % 2 == 0:
            result.append(text[start:index].strip())
            start = index + 1
        slashes = slashes + 1 if char == "\\" else 0
    result.append(text[start:].strip())
    return result


def tables(lines: list[str], offset: int = 0) -> tuple[list[Table], list[dict]]:
    found, errors = [], []
    index = 0
    while index < len(lines):
        if not re.fullmatch(r"\s*\|.*\|\s*", lines[index]):
            index += 1
            continue
        start, group = index, []
        while index < len(lines) and re.fullmatch(r"\s*\|.*\|\s*", lines[index]):
            group.append(cells(lines[index]))
            index += 1
        number = offset + start + 1
        if len(group) < 2 or not all(re.fullmatch(r":?-{3,}:?", c) for c in group[1]):
            errors.append({"code": "table_separator", "line": number, "message": "表格缺少规范分隔行。"})
            continue
        width = len(group[0])
        for row_index, row in enumerate(group):
            if len(row) != width:
                errors.append({"code": "table_width", "line": number + row_index, "message": "表格列数不一致；单元格内竖线须转义。"})
            if row_index != 1 and any(not c or c in {"-", "—"} for c in row):
                errors.append({"code": "empty_cell", "line": number + row_index, "message": "表格空项须说明未知、不适用或未执行等实际情况。"})
        found.append(Table(number, group[0], group[2:]))
    return found, errors


def section_blocks(lines: list[str]) -> list[tuple[int, list[str]]]:
    positions = [i for i, level, _ in headings(lines) if level == 2]
    bounds = [0] + positions + [len(lines)]
    return [(start, lines[start:end]) for start, end in zip(bounds, bounds[1:])]


def validate(text: str, template: str, classification: str, checks: str) -> list[dict]:
    source, template_errors = visible_lines(template)
    model = section_blocks(source)
    model_tables = [tables(block, offset)[0] for offset, block in model]
    if template_errors or len(model) != 7 or [len(t) for t in model_tables] != [2, 0, 1, 0, 1, 1, 0]:
        raise CheckUnavailable("包内模板结构不可用。")
    lines, errors = visible_lines(text)

    def issue(code: str, message: str, line: int = 1) -> None:
        errors.append({"code": code, "line": line, "message": message})

    for number, line in enumerate(text.splitlines(), 1):
        if "{{REPORT:" in line:
            issue("placeholder", "尚有报告模板占位符。", number)
    expected = [(level, title) for _, level, title in headings(source) if level <= 2]
    actual = [(level, title) for _, level, title in headings(lines) if level <= 2]
    if actual != expected or not lines or lines[0] != source[0]:
        issue("headings", "使用模板的一级标题和六个二级章节，保持名称、层级、数量与顺序。")
        return errors
    blocks = section_blocks(lines)
    parsed = []
    for offset, block in blocks:
        items, gaps = tables(block, offset)
        parsed.append(items)
        errors.extend(gaps)

    def require_table(items: list[Table], schema: Table, *, required: bool = True, multiple: bool = False, line: int = 1) -> list[Table]:
        matches = [t for t in items if t.header == schema.header]
        if required and (not matches or (not multiple and len(matches) != 1)):
            quantity = "至少一张" if multiple else "且仅有一张"
            issue("required_table", "应有" + quantity + "指定表格：" + " / ".join(schema.header), line)
        for table in matches:
            if not table.rows:
                issue("empty_table", "表格应填写实际记录；不适用时按条件裁剪。", table.line)
        return matches

    metadata = require_table(parsed[0], model_tables[0][0])
    require_table(parsed[0], model_tables[0][1])
    values = {}
    if metadata:
        required_fields = [r[0] for r in model_tables[0][0].rows]
        actual_fields = [r[0] for r in metadata[0].rows]
        if [key for key in actual_fields if key in required_fields] != required_fields:
            issue("metadata_fields", "记录信息须保留模板的必需字段及相对顺序，可补充其他信息。", metadata[0].line)
        if len(set(actual_fields)) != len(actual_fields):
            issue("metadata_duplicate", "记录信息字段不能重复。", metadata[0].line)
        values = {r[0]: r[1].strip("`") for r in metadata[0].rows if len(r) == 2}
        if values.get("请求类型") != "修复":
            issue("outcome_request_type", "本报告仅用于双参数流水线内修复，请求类型须为修复。", metadata[0].line)
        if values.get("执行模式") != "local_verify":
            issue("outcome_mode", "流水线内修复报告的执行模式须为 local_verify。", metadata[0].line)
        outcomes = [(metadata[0].line + 2 + i, row) for i, row in enumerate(metadata[0].rows)
                    if row[0] == "处理结果"]
        if len(outcomes) != 1:
            issue("outcome_count", "记录信息表必须且只能有一行处理结果；正文说明不能代替该字段。", metadata[0].line)
        else:
            outcome_line, row = outcomes[0]
            # The enum cell is plain text: do not normalize lists, prose or Markdown into a result.
            outcome = row[1] if len(row) == 2 else None
            if outcome not in OUTCOMES:
                issue("outcome_enum", "处理结果只填写一个原样枚举：" + "、".join(OUTCOMES) + "。", outcome_line)
            else:
                if outcome == "local_verified":
                    if checks != "present":
                        issue("outcome_checks", "成功结果要求本次有验证检查，不能同时声明 --checks none。", outcome_line)
    expected_metadata = [t.header for t in model_tables[0]]
    if [t.header for t in parsed[0]] != expected_metadata:
        issue("metadata_layout", "记录区按模板排列记录信息、仓库与候选；原始 CI 证据写入第 2 节。")

    raw_lines = text.splitlines()
    for offset, block in blocks[1:]:
        body = "\n".join(raw_lines[offset + 1:offset + len(block)])
        body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
        body = re.sub(r"^ {0,3}#{3,6}\s+.*$", "", body, flags=re.MULTILINE)
        if not body.strip():
            issue("empty_section", "章节须有正文；不适用或未执行时说明原因。", offset + 1)

    offset, failure = blocks[2]
    objects = [i for i, level, title in headings(failure) if level == 3 and title.startswith("失败对象：")]
    grouped = bool(objects)
    if not grouped:
        objects = [0]
    present, classification_tables = [], []
    for start, end in zip(objects, objects[1:] + [len(failure)]):
        block = failure[start:end]
        if grouped and not block[0].split("失败对象：", 1)[1].strip():
            issue("failure_reference", "失败对象标题须包含可定位引用或未知原因。", offset + start + 1)
        object_tables = [t for t in parsed[2] if offset + start < t.line <= offset + end]
        matches = require_table(object_tables, model_tables[2][0], required=classification == "required", line=offset + start + 1)
        present.append(bool(matches))
        classification_tables.extend(matches)
        if len(matches) > 1:
            issue("classification_group", "多个分类对象分别使用失败对象分组。", offset + start + 1)
        for table in matches:
            if [row[0] for row in table.rows] != ["初始分类", "最终分类"]:
                issue("classification_rows", "分类表保留初始分类、最终分类两行及顺序。", table.line)
        for table in object_tables:
            if table.header != model_tables[2][0].header and any(row[0] in {"初始分类", "最终分类"} for row in table.rows):
                issue("classification_table", "分类表须使用模板列名。", table.line)
    if classification == "required" and not all(present):
        issue("classification_required", "本次要求分类，每个失败对象均须保留分类记录。", offset + 1)
    elif classification == "none" and any(present):
        for table in classification_tables:
            issue("classification_none",
                  "本次无分类要求，但此处保留了分类表。删除该表，保留普通诊断文字；"
                  "不要仅为通过检查修改 --classification，只有新的可信任务要求才改变此条件。",
                  table.line)
    elif classification == "mixed" and (not any(present) or all(present)):
        issue("classification_mixed", "mixed 仅用于同时存在有分类要求和无分类要求的对象；标准无法应用不等于无要求。", offset + 1)

    require_table(parsed[4], model_tables[4][0], required=False, line=blocks[4][0] + 1)
    check_tables = require_table(parsed[5], model_tables[5][0], required=checks == "present", multiple=True, line=blocks[5][0] + 1)
    check_header = model_tables[5][0].header
    for table in parsed[5]:
        if table.header[0] == check_header[0] and table.header != check_header:
            issue("verification_table", "检查表须使用模板的全部列名及顺序。", table.line)
    if checks == "none" and check_tables:
        issue("checks_none",
              "--checks none 声明本轮无验证计划、必需检查或实际验证检查，但报告保留了检查表。"
              "若该声明符合事实，保持 --checks none，删除不适用的检查表并保留未验证说明。"
              "仅在本轮验证计划或执行记录证明参数误传时纠正 --checks；"
              "表格存在、原始 Job 结果或后续人工验证建议均不能作为改为 present 的依据。",
              check_tables[0].line)
    return errors


def read_bytes(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise CheckUnavailable("输入须为普通文件。")
        data = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise CheckUnavailable("读取期间文件发生变化。")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, help="Report file named analysis-fix.md, or - for UTF-8 stdin.")
    parser.add_argument("--classification", required=True, choices=("required", "none", "mixed"), help="Delivery requirement: required even if the Profile cannot be applied; none only if no classification is requested; mixed if only some objects require classification.")
    parser.add_argument("--checks", required=True, choices=("present", "none"),
                        help="Choose from this invocation's verification plan and execution records, not from table presence. "
                             "present includes required or planned but unexecuted checks; none only if no checks are required, planned or executed. "
                             "Original Job results, report-format checks and future human verification suggestions alone do not count.")
    args = parser.parse_args()
    try:
        data = sys.stdin.buffer.read(MAX_BYTES + 1) if args.report == "-" else read_bytes(Path(args.report))
        template = read_bytes(TEMPLATE)
        if len(data) > MAX_BYTES or len(template) > MAX_BYTES:
            raise CheckUnavailable("报告或模板超过 2 MiB 读取上限。")
        errors = validate(data.decode("utf-8-sig"), template.decode("utf-8-sig"), args.classification, args.checks)
        if args.report != "-" and Path(args.report).name != "analysis-fix.md":
            errors.append({"code": "filename", "line": None, "message": "文件交付使用 analysis-fix.md。"})
        result = {"format_status": "invalid" if errors else "valid", "sha256": hashlib.sha256(data).hexdigest(), "errors": errors}
        code = 1 if errors else 0
    except (OSError, UnicodeError, CheckUnavailable) as exc:
        result = {"format_status": "incomplete", "errors": [{"code": "input_unavailable", "message": str(exc)}]}
        code = 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
