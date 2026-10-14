"""Allowlisted timing metadata only. Never export prompts or reasoning text."""

from datetime import datetime
import json
import math
from pathlib import Path

from common import EvalError, digest, read_regular, write_json
from gateway_evidence import read_gateway_evidence

# Never emit arbitrary tool names, IDs, argument values or raw error messages.
TOOL_NAMES = {"load_skill", "read_skill_resource", "run_skill_script", "read_file", "write_file", "edit_file",
              "bash", "grep", "glob", "sleep", "todo", "view_image"}
TOOL_OUTCOMES = {"success", "failed", "error", "invalid_arguments", "cancelled", "interrupted", "incomplete", "denied", "blocked"}


def tool_operation_summary(records, gaps):
    """Native dispatch attempts include failures absent from public tool results.

    Pair by per-source operation ID, including interleaved calls. This is timing
    metadata only; success does not establish semantic correctness or readback.
    """
    if not records:
        return None
    starts, finishes = {}, {}
    for source, event in records:
        operation = event.get("operation_id")
        if not isinstance(operation, str) or not operation:
            gaps.append("tool_operation_id_missing")
            continue
        target = starts if event["event_type"] == "tool.operation.started" else finishes
        key = (source, operation)
        if key in target:
            gaps.append("duplicate_tool_operation_metadata")
            continue
        target[key] = event["payload"]
    result = {"started": len(starts), "finished": len(finishes), "outcomes": {}, "by_tool": {},
              "unmatched_starts": len(starts.keys() - finishes.keys()),
              "unmatched_finishes": len(finishes.keys() - starts.keys())}
    if result["unmatched_starts"] or result["unmatched_finishes"]:
        gaps.append("unpaired_tool_operation_metadata")
    for key, data in finishes.items():
        name = starts.get(key, {}).get("tool_name")
        name = name if isinstance(name, str) and name in TOOL_NAMES else "other_or_unknown"
        outcome = data.get("outcome")
        outcome = outcome if isinstance(outcome, str) and outcome in TOOL_OUTCOMES else "unknown"
        if outcome == "unknown":
            gaps.append("tool_outcome_unconfirmed")
        result["outcomes"][outcome] = result["outcomes"].get(outcome, 0) + 1
        row = result["by_tool"].setdefault(name, {})
        row[outcome] = row.get(outcome, 0) + 1
    return result


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def timing_summary(sessions, process, public_log=None):
    exchanges, gaps = [], []
    tool_records = []
    sources = {}
    for path in sorted(Path(sessions).glob("sessions/*/trajectory/events.jsonl")):
        raw = read_regular(path)
        sources[str(path.relative_to(sessions))] = digest(raw)
        pending = None
        for line in raw.splitlines():
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
                    raise ValueError()
            except ValueError:
                gaps.append("partial_or_invalid_trajectory_line")
                continue
            kind, data = event.get("event_type"), event["payload"]
            if kind in {"tool.operation.started", "tool.operation.finished"}:
                # Retain only pairing/classification metadata even internally.
                tool_records.append((str(path), {"event_type": kind, "operation_id": event.get("operation_id"),
                    "payload": {k: data.get(k) for k in ("tool_name", "outcome")}}))
            if kind == "model.exchange.started":
                if pending is not None:
                    gaps.append("overlapping_exchange_metadata")
                pending = {"completed": False, "exchange_ended": False, "index": len(exchanges) + 1}
                # Keep valid timestamps, never arbitrary provider text.
                try:
                    pending["started_at"] = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00")).isoformat()
                except (KeyError, TypeError, ValueError, AttributeError):
                    gaps.append("exchange_timestamp_missing")
                exchanges.append(pending)
            elif kind == "model.exchange.finished":
                if pending is None:
                    gaps.append("exchange_start_missing")
                    continue
                pending["exchange_ended"] = True
                pending["completed"] = data.get("outcome") == "success"
                for key in ("duration_ms", "chunk_count", "stall_count", "ttfc_ms", "ttfv_ms", "max_chunk_gap_ms"):
                    if number(data.get(key)):
                        pending[key] = data[key]
                usage = data.get("usage", {})
                normalized = usage.get("normalized", {}) if isinstance(usage, dict) else {}
                if isinstance(normalized, dict):
                    pending["tokens"] = {k: normalized[k] for k in ("input_total", "cache_read", "output_total", "reasoning", "output_visible")
                                         if number(normalized.get(k))}
                if isinstance(data.get("outcome"), str) and data["outcome"] in {"success", "error", "cancelled", "incomplete", "interrupted"}:
                    pending["outcome"] = data["outcome"]
                else:
                    gaps.append("exchange_outcome_unconfirmed")
                pending = None
        if pending is not None and pending.get("started_at") and process.get("ended_at"):
            try:
                elapsed = (datetime.fromisoformat(process["ended_at"]) - datetime.fromisoformat(pending["started_at"])).total_seconds()
                if elapsed >= 0:
                    pending["observed_until_process_end_ms"] = round(elapsed * 1000)
            except (TypeError, ValueError):
                pass
    if not sources:
        gaps.append("native_timing_unavailable")
    tools, tool_ms = None, None
    if public_log and Path(public_log).exists():
        tools, tool_ms = 0, 0
        raw = read_regular(public_log)
        sources["public_events"] = digest(raw)
        for line in raw.splitlines():
            if not line.startswith(b"CI_EVAL_EVENT "):
                continue
            try:
                event = json.loads(line[14:])
                if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
                    raise ValueError()
            except ValueError:
                gaps.append("partial_public_event")
                continue
            if event.get("kind") == "InvocationToolCallResult":
                tools += 1
                value = event.get("payload", {}).get("duration_ms")
                if number(value):
                    tool_ms += value
    native_tools = tool_operation_summary(tool_records, gaps)
    return {"source_sha256": sources, "exchanges": exchanges, "native_tool_operations": native_tools,
            "tool_results": tools, "sum_tool_duration_ms": tool_ms,
            "gaps": sorted(set(gaps)), "coverage": "metadata_only_not_semantic_evidence"}


def write_diagnostics(directory, sessions, process, *, public_log=None, gateway_log=None, gateway_binding=None, changed_paths=None):
    directory = Path(directory)
    result = {"schema": "ci-runtime-diagnostics-v1", "author": "harness", "agent_report": False,
              "process": process, "changed_paths": changed_paths, "gateway_statuses": []}
    try:
        result["timing"] = timing_summary(sessions, process, public_log)
    except (EvalError, OSError, ValueError, TypeError, AttributeError):
        result["timing"] = {"gaps": ["timing_metadata_unavailable"]}
    if gateway_log is not None:
        result["gateway_evidence"] = read_gateway_evidence(gateway_log, gateway_binding)
        result["gateway_statuses"] = result["gateway_evidence"]["http_statuses"]
    write_json(directory / "runtime-diagnostics.json", result)
    return result
