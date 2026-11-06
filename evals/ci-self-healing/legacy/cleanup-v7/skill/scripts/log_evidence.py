#!/usr/bin/env python3
"""Bounded, local-only CI log inspection. No diagnosis or command execution."""

from __future__ import annotations

import argparse
import codecs
from collections import deque
from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import random
import re
import stat
import sys
from typing import BinaryIO, Iterator, Optional


CHUNK_BYTES = 64 * 1024
MAX_LINE_CHARS = 16 * 1024
DIAGNOSTIC = re.compile(
    r"\b(?:error|exception|traceback|fatal|failed|failure|undefined reference|"
    r"cannot find symbol|segmentation fault|assertion failed)\b|"
    r"\b[A-Za-z_$][A-Za-z0-9_.$]{0,100}(?:Error|Exception)\b|错误|异常",
    re.IGNORECASE,
)
SUMMARY = re.compile(
    r"\bBUILD (?:FAILED|FAILURE)\b|\b(?:exit code|exited with (?:code|status))\b",
    re.IGNORECASE,
)


class EvidenceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Options:
    operation: str
    log: str
    start_line: int = 1
    end_line: Optional[int] = None
    literal: Optional[str] = None
    anchors: list[str] = field(default_factory=list)
    before: int = 40
    after: int = 40
    max_blocks: int = 5
    max_block_lines: int = 120
    max_chars: int = 24000
    max_scan_bytes: Optional[int] = None
    binding_ref: Optional[str] = None
    source_completeness: str = "unknown"
    completeness_ref: Optional[str] = None

    def validate(self) -> None:
        if self.operation not in {"scan", "search", "read"}:
            raise EvidenceError("invalid_operation", "Use scan, search or read.")
        if self.start_line < 1 or (self.end_line is not None and self.end_line < self.start_line):
            raise EvidenceError("invalid_range", "Line numbers start at 1; end must be >= start.")
        if self.operation == "read" and self.end_line is None:
            raise EvidenceError("invalid_range", "read requires --end-line.")
        if self.operation == "search" and not self.literal:
            raise EvidenceError("invalid_literal", "search requires a nonempty --literal.")
        for value, lower, upper, name in (
            (self.before, 0, 1000, "before"), (self.after, 0, 1000, "after"),
            (self.max_blocks, 1, 20, "max-blocks"),
            (self.max_block_lines, 1, 1000, "max-block-lines"),
            (self.max_chars, 1, 200000, "max-chars"),
        ):
            if not lower <= value <= upper:
                raise EvidenceError("invalid_budget", f"{name} must be in [{lower}, {upper}].")
        if self.max_scan_bytes is not None and self.max_scan_bytes < 1:
            raise EvidenceError("invalid_budget", "max-scan-bytes must be positive.")
        if len(self.anchors) > 32:
            raise EvidenceError("invalid_anchor", "At most 32 extra anchors are supported.")
        for value in self.anchors + ([self.literal] if self.literal is not None else []):
            if not value or len(value) > 1024 or "\n" in value or "\r" in value:
                raise EvidenceError("invalid_literal", "Literals must contain 1..1024 characters on one line.")
        if len(self.log) > 4096 or any(len(v) > 1024 for v in (self.binding_ref, self.completeness_ref) if v):
            raise EvidenceError("invalid_reference", "Path or evidence reference is too long.")
        if self.source_completeness not in {"complete", "incomplete", "unknown"}:
            raise EvidenceError("invalid_completeness", "Invalid source completeness value.")
        if self.source_completeness != "unknown" and not self.completeness_ref:
            raise EvidenceError("missing_completeness_reference", "A completeness assertion requires --completeness-ref.")


@dataclass
class Line:
    number: int
    text: str
    chars: int
    terminated: bool
    kind: Optional[str]


class Matcher:
    """Literal search across chunks; bounded generic anchors for scan."""

    def __init__(self, options: Options):
        self.options = options
        self.overlap = max([256] + [len(a) for a in options.anchors] + [len(options.literal or "")])
        self.tail = ""
        self.kind: Optional[str] = None

    def feed(self, text: str) -> None:
        if self.options.operation == "read":
            return
        combined = self.tail + text
        if self.options.operation == "search":
            if self.options.literal in combined:
                self.kind = "literal_match"
        elif self.options.operation == "scan":
            if any(anchor in combined for anchor in self.options.anchors):
                self.kind = "custom_literal_anchor"
            elif self.kind != "custom_literal_anchor":
                if SUMMARY.search(combined):
                    self.kind = "failure_summary_anchor"
                elif self.kind is None and DIAGNOSTIC.search(combined):
                    self.kind = "diagnostic_anchor"
        self.tail = combined[-self.overlap:]


class LogReader:
    """Read at most the initial byte extent, with bounded per-line retention."""

    def __init__(self, stream: BinaryIO, size: int, options: Options, prefix_limit: int):
        self.stream = stream
        self.size = size
        self.limit = min(size, options.max_scan_bytes or size)
        self.options = options
        self.prefix_limit = prefix_limit
        self.lines_visited = 0
        self.bytes_read = 0
        self.invalid_bytes = 0
        self.file_scan_complete = False
        self.byte_budget_reached = False
        self.short_read = False

    def __iter__(self) -> Iterator[Line]:
        remaining = self.limit
        number = 0
        prefix = ""
        chars = 0
        line_has_bytes = False
        matcher = Matcher(self.options)
        decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")

        def decode(part: bytes, final: bool = False) -> str:
            text = decoder.decode(part, final=final)
            invalid = 0 if text.isascii() else sum(0xDC80 <= ord(c) <= 0xDCFF for c in text)
            if invalid:
                self.invalid_bytes += invalid
                text = "".join("\ufffd" if 0xDC80 <= ord(c) <= 0xDCFF else c for c in text)
            return text

        while remaining:
            chunk = self.stream.read(min(CHUNK_BYTES, remaining))
            if not chunk:
                self.short_read = True
                break
            if self.bytes_read == 0 and chunk.startswith((b"\x1f\x8b", b"PK\x03\x04", b"PK\x05\x06")):
                raise EvidenceError("non_text_input", "Compressed archives are not supported; obtain a text log first.")
            if b"\x00" in chunk:
                raise EvidenceError("non_text_input", "NUL bytes detected; use a UTF-8 text log, not a binary/UTF-16 file.")
            remaining -= len(chunk)
            self.bytes_read += len(chunk)
            parts = chunk.split(b"\n")  # The chunk, never the whole file or an unbounded line.
            for index, part in enumerate(parts):
                terminated = index < len(parts) - 1
                if part or terminated:
                    line_has_bytes = True
                text = decode(part + (b"\n" if terminated else b""), final=terminated)
                matcher.feed(text)
                prefix += text[:max(0, self.prefix_limit - len(prefix))]
                chars += len(text)
                if terminated:
                    number += 1
                    self.lines_visited = number
                    yield Line(number, prefix, chars, True, matcher.kind)
                    prefix, chars, line_has_bytes = "", 0, False
                    matcher = Matcher(self.options)
                    decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
                    if self.options.end_line is not None and number >= self.options.end_line:
                        self.file_scan_complete = remaining == 0 and index == len(parts) - 2 and not parts[-1] and self.limit == self.size
                        return
        if line_has_bytes:
            text = decode(b"", final=True)
            matcher.feed(text)
            prefix += text[:max(0, self.prefix_limit - len(prefix))]
            chars += len(text)
            number += 1
            self.lines_visited = number
            yield Line(number, prefix, chars, False, matcher.kind)
        self.byte_budget_reached = self.limit < self.size and self.bytes_read == self.limit
        self.file_scan_complete = self.bytes_read == self.size and not self.short_read


@dataclass
class Window:
    start: int
    end: int
    reasons: set[str]
    hits: list[int] = field(default_factory=list)


class WindowPool:
    """Keep first, last and deterministic sampled interior windows, in bounded space."""

    def __init__(self, limit: int, last_only: bool = False):
        self.limit = limit
        self.last_only = last_only
        self.count = 0
        self.first: Optional[Window] = None
        self.last: Optional[Window] = None
        self.middle: list[Window] = []
        self.interior_seen = 0
        self.random = random.Random(0)

    def add(self, window: Window) -> None:
        if self.first is None:
            self.first = window
        elif self.count > 1 and self.limit > 2:
            self.interior_seen += 1
            capacity = self.limit - 2
            if len(self.middle) < capacity:
                self.middle.append(self.last)
            else:
                position = self.random.randrange(self.interior_seen)
                if position < capacity:
                    self.middle[position] = self.last
        self.last = window
        self.count += 1

    def selected(self) -> list[Window]:
        if self.first is None:
            return []
        if self.limit == 1 or self.count == 1:
            return [self.last if self.last_only else self.first]
        return sorted([self.first, *self.middle, self.last], key=lambda w: w.start)


class WindowBuilder:
    def __init__(self, pool: WindowPool, options: Options):
        self.pool, self.options = pool, options
        self.pending: Optional[Window] = None

    def add(self, line: Line) -> None:
        before = min(self.options.before, self.options.max_block_lines - 1)
        start = max(self.options.start_line, line.number - before)
        end = min(line.number + self.options.after, start + self.options.max_block_lines - 1)
        if self.options.end_line is not None:
            end = min(end, self.options.end_line)
        window = Window(start, end, {line.kind}, [line.number])
        current = self.pending
        if current and start <= current.end and max(end, current.end) - current.start + 1 <= self.options.max_block_lines:
            current.end = max(end, current.end)
            current.reasons.update(window.reasons)
            current.hits.append(line.number)
        else:
            self.finish()
            self.pending = window

    def finish(self) -> None:
        if self.pending:
            self.pool.add(self.pending)
            self.pending = None


def merge_windows(windows: list[Window], max_lines: int, last_line: int) -> list[Window]:
    merged: list[Window] = []
    for window in sorted(windows, key=lambda w: w.start):
        window.end = min(window.end, last_line)
        if merged and window.start <= merged[-1].end and max(window.end, merged[-1].end) - merged[-1].start + 1 <= max_lines:
            previous = merged[-1]
            previous.end = max(previous.end, window.end)
            previous.reasons.update(window.reasons)
            previous.hits = sorted(set(previous.hits + window.hits))
        else:
            merged.append(window)
    return merged


def source_state(value: os.stat_result) -> dict:
    return {"device": value.st_dev, "inode": value.st_ino, "size_bytes": value.st_size,
            "mtime_ns": value.st_mtime_ns, "ctime_ns": value.st_ctime_ns}


def render(stream: BinaryIO, size: int, options: Options, windows: list[Window]) -> tuple[list[dict], LogReader, bool]:
    # A single forward pass serves all selected windows, even when they overlap.
    stop_line = max((w.end for w in windows), default=0)
    read_options = replace(options, operation="read", end_line=stop_line or 1)
    reader = LogReader(stream, size, read_options, min(MAX_LINE_CHARS, options.max_chars))
    # Share the budget so a verbose early window cannot hide every later target.
    records = []
    for index, window in enumerate(windows):
        quota = options.max_chars // len(windows) + (index < options.max_chars % len(windows))
        focus = window.start if options.operation == "read" else (window.hits[0] if window.hits else window.end)
        records.append({"window": window, "lines": deque(), "chars": 0,
                        "quota": quota, "focus": focus, "limited": False})
    truncated = False
    for line in reader:
        for record in records:
            window = record["window"]
            if window.start <= line.number <= window.end:
                # Retain bounded pre-context near the anchor, then its following lines.
                prior = line.number < record["focus"]
                capacity = record["quota"] // 2 if prior else record["quota"] - record["chars"]
                text = line.text[:max(0, capacity)]
                if prior:
                    while record["lines"] and record["chars"] + len(text) > capacity:
                        _, removed, _ = record["lines"].popleft()
                        record["chars"] -= len(removed)
                        record["limited"] = True
                if not text and line.chars:
                    record["limited"] = True
                    continue
                record["lines"].append((line.number, text, line.chars))
                record["chars"] += len(text)
                if len(text) < line.chars:
                    record["limited"] = True
    blocks = []
    for record in records:
        lines = list(record["lines"])
        window = record["window"]
        if not lines:
            # A nonexistent range is a source/range gap, not an output-budget cut.
            if reader.lines_visited >= window.start:
                truncated = True
            continue
        start, end = lines[0][0], lines[-1][0]
        shortened = [n for n, text, chars in lines if len(text) < chars]
        partial_reasons = []
        if start > options.start_line:
            partial_reasons.append("earlier_context_not_returned")
        if not reader.file_scan_complete or end < reader.lines_visited:
            partial_reasons.append("later_context_may_exist")
        if shortened:
            partial_reasons.append("line_prefix_truncated")
        if start > window.start or end < window.end:
            partial_reasons.append("window_not_fully_returned")
        if record["limited"]:
            truncated = True
            partial_reasons.append("output_budget_limited")
        if reader.byte_budget_reached or reader.short_read:
            partial_reasons.append("input_read_incomplete")
        spans = []
        offset = 0
        for number, text, _ in lines:
            spans.append({"line": number, "text_start": offset, "text_length": len(text)})
            offset += len(text)
        blocks.append({
            "start_line": start, "end_line": end,
            "text": "".join(text for _, text, _ in lines),
            "selection_reason": sorted(window.reasons),
            "match_lines": window.hits,
            "partial": bool(partial_reasons), "partial_reasons": partial_reasons,
            "truncated_line_numbers": shortened,
            "line_spans": spans,
        })
    return blocks, reader, truncated


def extract(options: Options) -> dict:
    options.validate()
    path = Path(options.log).absolute()
    if not stat.S_ISREG(path.stat().st_mode):
        raise EvidenceError("non_regular_input", "Only local regular text files are supported.")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        before = source_state(os.fstat(stream.fileno()))
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise EvidenceError("non_regular_input", "Input changed to a non-regular file.")
        gaps = []
        matched_lines = 0
        omitted = 0
        if options.operation == "read":
            end = min(options.end_line, options.start_line + options.max_block_lines - 1)
            windows = [Window(options.start_line, end, {"requested_line_range"})]
            blocks, inspected, clipped = render(stream, before["size_bytes"], options, windows)
            if end < options.end_line:
                clipped = True
                gaps.append({"code": "requested_range_budget_limited", "next_start_line": end + 1})
        else:
            limit = options.max_blocks if options.operation == "search" or options.max_blocks == 1 else options.max_blocks - 1
            candidates = WindowPool(limit)
            summaries = WindowPool(1, last_only=True)
            candidate_builder = WindowBuilder(candidates, options)
            summary_builder = WindowBuilder(summaries, options)
            inspected = LogReader(stream, before["size_bytes"], options, 0)
            for line in inspected:
                if line.number >= options.start_line and line.kind:
                    matched_lines += 1
                    builder = summary_builder if line.kind == "failure_summary_anchor" else candidate_builder
                    builder.add(line)
            candidate_builder.finish()
            summary_builder.finish()
            windows = candidates.selected()
            selected_summaries = summaries.selected() if len(windows) < options.max_blocks else []
            windows.extend(selected_summaries)
            omitted = candidates.count + summaries.count - len(windows)
            if options.operation == "scan" and len(windows) < options.max_blocks and inspected.lines_visited >= options.start_line and not selected_summaries:
                tail_size = min(options.max_block_lines, options.before + options.after + 1)
                windows.append(Window(max(options.start_line, inspected.lines_visited - tail_size + 1), inspected.lines_visited, {"scope_tail"}))
            windows = merge_windows(windows, options.max_block_lines, inspected.lines_visited)
            stream.seek(0)
            blocks, rendered, clipped = render(stream, before["size_bytes"], options, windows) if windows else ([], inspected, False)
            if rendered.invalid_bytes > inspected.invalid_bytes:
                gaps.append({"code": "decoding_changed_between_passes"})
        after_handle = source_state(os.fstat(stream.fileno()))
        try:
            after_path = source_state(path.stat())
        except OSError:
            after_path = None

    changed = before != after_handle or before != after_path
    complete_scope = inspected.file_scan_complete or (options.end_line is not None and inspected.lines_visited >= options.end_line and not inspected.byte_budget_reached)
    if options.end_line is not None and inspected.lines_visited < options.end_line:
        complete_scope = False
        gaps.append({"code": "requested_end_not_reached", "last_visited_line": inspected.lines_visited})
    if inspected.lines_visited < options.start_line:
        gaps.append({"code": "requested_start_not_reached"})
    if inspected.invalid_bytes:
        gaps.append({"code": "utf8_replacement", "invalid_bytes": inspected.invalid_bytes})
    if inspected.byte_budget_reached:
        gaps.append({"code": "scan_byte_budget_reached"})
    if inspected.short_read:
        gaps.append({"code": "source_short_read"})
    if changed:
        gaps.append({"code": "source_changed", "detail": "Line references may not describe one stable source version."})
    if omitted:
        gaps.append({"code": "candidate_windows_omitted", "count": omitted})
    if clipped:
        gaps.append({"code": "output_text_limited", "detail": "Check block ranges and per-line spans before citing or extending them."})
    if options.operation != "read" and not matched_lines:
        gaps.append({"code": "no_anchor_match" if options.operation == "scan" else "no_literal_match",
                     "detail": "No match in the inspected scope; this is not a healthy-build conclusion."})
    completeness = "unknown" if changed else options.source_completeness
    if completeness != "complete":
        gaps.append({"code": "source_completeness_" + completeness})
    return {
        "status": "ok", "operation": options.operation,
        "source_ref": {"path": str(path), "binding_ref": options.binding_ref, "binding_verified_by_script": False},
        "source_completeness": completeness,
        "completeness_ref": options.completeness_ref,
        "source_state": {"before": before, "after_handle": after_handle, "after_path": after_path, "changed": changed, "check": "metadata_only"},
        "coverage": {
            "requested_start_line": options.start_line, "requested_end_line": options.end_line,
            "last_visited_line": inspected.lines_visited,
            "bytes_read_in_inspection_pass": inspected.bytes_read,
            "scan_complete": complete_scope, "file_scan_complete": inspected.file_scan_complete,
            "matched_lines": matched_lines if options.operation != "read" else None,
            "candidate_windows_omitted": omitted,
        },
        "blocks": blocks, "output_truncated": clipped or omitted > 0,
        "display": {"encoding": "utf-8", "ansi": "preserved_json_escaped", "redaction": "not_performed"},
        "gaps": gaps,
    }


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise EvidenceError("invalid_arguments", message)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = Parser(description=__doc__, epilog="Use OPERATION --help for budgets and source-reference options.")
    sub = parser.add_subparsers(dest="operation", required=True, parser_class=Parser)
    operations = {
        "scan": "Select bounded windows around diagnostic and failure-summary anchors.",
        "search": "Find a case-sensitive literal; return bounded context windows.",
        "read": "Read an inclusive original line range within the output budget.",
    }
    for operation, description in operations.items():
        defaults = Options(operation, "")
        command = sub.add_parser(
            operation, help=description, description=description,
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            epilog=f"Only local regular UTF-8 files. Long lines display at most {MAX_LINE_CHARS} "
                   "prefix characters, also limited by the text budget; matching can inspect undisplayed text. "
                   "Success/no matches are not CI health or repair-admission decisions.",
        )
        command.add_argument("--log", required=True, help="Original local log file; never modified.")
        command.add_argument("--start-line", type=int, default=defaults.start_line, help="First original line, 1-based and inclusive.")
        command.add_argument("--end-line", type=int, required=operation == "read", help="Last original line, inclusive; required for read, otherwise file end.")
        if operation == "search":
            command.add_argument("--literal", required=True, help="Case-sensitive literal, 1..1024 characters on one line; not a regex.")
        if operation == "scan":
            command.add_argument("--anchor", action="append", default=defaults.anchors, dest="anchors", help="Extra case-sensitive literal anchor; repeat up to 32 times, each 1..1024 characters on one line.")
        command.add_argument("--before", type=int, default=defaults.before, help="Context lines before scan/search hits, 0..1000; unused by read.")
        command.add_argument("--after", type=int, default=defaults.after, help="Context lines after scan/search hits, 0..1000; unused by read.")
        command.add_argument("--max-blocks", type=int, default=defaults.max_blocks, help="Returned scan/search window limit, 1..20; read uses one window.")
        command.add_argument("--max-block-lines", type=int, default=defaults.max_block_lines, help="Line limit per window, 1..1000; also bounds read.")
        command.add_argument("--max-chars", type=int, default=defaults.max_chars, help="Total evidence-text characters, 1..200000; excludes JSON metadata, not a token limit.")
        command.add_argument("--max-scan-bytes", type=int, help="Positive byte limit per pass; unset reads at most the initial file size.")
        command.add_argument("--binding-ref", help="Existing failure-binding reference; passed through, NOT verified by this script.")
        command.add_argument("--source-completeness", choices=["complete", "incomplete", "unknown"], default=defaults.source_completeness, help="Caller assertion, not inferred from scanning; non-unknown requires --completeness-ref.")
        command.add_argument("--completeness-ref", help="Evidence supporting the source-completeness assertion.")
        command.add_argument("--output", help="Optional NEW file in an authorized existing output directory; never overwrite.")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    try:
        args = vars(parse_args(argv))
        output = args.pop("output")
        result = extract(Options(**args))
        encoded = json.dumps(result, ensure_ascii=False) + "\n"
        if output:
            # Exclusive creation rejects the input itself, existing files and symlink/hardlink aliases.
            with open(output, "x", encoding="utf-8") as destination:
                destination.write(encoded)
            print(json.dumps({"status": "ok", "output": str(Path(output).absolute())}, ensure_ascii=False))
        else:
            print(encoded, end="")
        return 0
    except EvidenceError as error:
        code, message = error.code, str(error)
    except OSError as error:
        code, message = type(error).__name__, str(error)
    print(json.dumps({"status": "error", "error": {"code": code, "message": message[:1000]},
                      "blocks": [], "gaps": [{"code": "extraction_failed"}]}, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    sys.exit(main())
