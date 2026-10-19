"""Deterministic review projection. Raw evidence remains immutable beside it.

Never summarize agent decisions or discard repository evidence. Only known,
fully received Skill guidance is projected to scoped verbatim clauses. Unknown
resources and incomplete reads remain verbatim. Hashes bind excluded material;
they do not pretend the reviewer read it.
"""
import copy
import json
from collections import Counter

from common import EvalError, digest, json_bytes
from repair_support import EXECUTION_CONTRACT, INPUT_EXECUTION_CONTRACT, POLICY_EXECUTION_CONTRACT, SPLIT_EXECUTION_CONTRACT


FORMAT = "ci-review-projection-v2"
# Five-criterion semantic review, not an exhaustive audit of every Skill rule.
# Heading changes fail preparation rather than silently drop a required clause.
SECTIONS = {
    "SKILL.md": ["3. 判断修复准入", "保持硬边界"],
    "references/diagnosis-and-repair.md": ["变更上下文与预期行为", "修复依据核查", "自主推进与停止"],
    "references/repair-and-verification.md": ["修复依据", "候选修改前的准入记录", "决定修复准入", "分层验证", "结果判定"],
    "references/workspace-cleanup.md": ["执行前条件", "执行闭环", "结束与协作"],
    "references/execution-modes.md": ["基线与候选恢复", "CI Job 工作空间交付", "交互与移交"],
    "references/core-contracts.md": ["调用身份与限额", "收据绑定与失效"],
    # Their runtime mechanics are checked by the deterministic layer; semantics
    # are assessed from the task, report and full repo evidence, not these manuals.
    "references/invocation-inputs.md": [],
    "references/ci-job-failure-directory.md": [],
    "references/result-report.md": [],
    "references/java-maven-compilation.md": [],
    "assets/analysis-fix.template.md": [],
}

# Historical packets retain the original section map and its failure behavior.
# Split resources must contribute their own verbatim semantic clauses.
SPLIT_SECTIONS = {
    **SECTIONS,
    "references/diagnosis-and-repair.md": ["变更上下文与预期行为", "自主推进与停止"],
    "references/repair-basis.md": ["目标行为与证据", "选择修复位置", "重要反证", "语义方向歧义", "完整候选与缺口"],
    "references/workspace-recovery.md": ["基准缺失或时序错误的恢复", "候选撤销与基准"],
    "references/output-delivery.md": [],
}

POLICY_SECTIONS = {
    **SPLIT_SECTIONS,
    "references/repair-and-verification.md": ["修复依据", "候选修改前的准入记录", "决定修复准入", "结果判定"],
    "references/candidate-verification.md": ["构建与更新候选", "分层验证"],
    "references/verification-failures.md": ["失败后的路径选择"],
    "references/execution-modes.md": ["基线与候选恢复", "CI Job 工作空间交付"],
    "references/collaboration.md": ["交互与移交"],
    "references/enterprise-policy.md": ["企业策略声明", "读取与信任", "适用范围与分类依赖", "判断与行动", "交付"],
}

INPUT_SECTIONS = {
    **POLICY_SECTIONS,
    "SKILL.md": ["1. 确认任务与现场", "3. 判断修复准入", "保持硬边界"],
}

# v6 moves the same semantic clauses into the admission guide. Historical maps
# remain unchanged and are selected from the packet's own execution contract.
MERGED_SECTIONS = {
    name: headings for name, headings in INPUT_SECTIONS.items() if name != "references/repair-basis.md"
}
MERGED_SECTIONS["references/repair-and-verification.md"] = [
    "修复依据", *SPLIT_SECTIONS["references/repair-basis.md"],
    "候选修改前的准入记录", "决定修复准入", "结果判定",
]


def sections(text, wanted):
    lines = text.splitlines(keepends=True)
    headings, fence = [], None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = None if fence == marker else marker if fence is None else fence
        if fence is None and line.startswith("## "):
            headings.append((i, line[3:].strip()))
    result = []
    for title in wanted:
        found = [j for j, (_, name) in enumerate(headings) if name == title]
        if len(found) != 1:
            raise EvalError("review clause missing or ambiguous: " + title)
        j = found[0]
        start = headings[j][0]
        end = headings[j + 1][0] if j + 1 < len(headings) else len(lines)
        result.append({"heading": title, "start_line": start + 1, "end_line": end,
                       "text": "".join(lines[start:end])})
    return result


def project_packet(packet, trial_prefix=None):
    value = copy.deepcopy(packet)
    calls = {e["payload"].get("call_id"): e for e in packet["events"] if e["kind"] == "InvocationToolCallStart"}
    execution = packet.get("deterministic", {}).get("execution_checks", {})
    section_map = {SPLIT_EXECUTION_CONTRACT: SPLIT_SECTIONS,
                   POLICY_EXECUTION_CONTRACT: POLICY_SECTIONS,
                   INPUT_EXECUTION_CONTRACT: INPUT_SECTIONS,
                   EXECUTION_CONTRACT: MERGED_SECTIONS}.get(execution.get("contract"), SECTIONS)
    confirmed = {(r["seq"], r["resource"]) for r in execution.get("resource_reads", []) if r.get("complete") is True}
    projection = []
    for event in value["events"]:
        if event["kind"] != "InvocationToolCallResult":
            continue
        p = event["payload"]
        call = calls.get(p.get("call_id"), {}).get("payload", {})
        args = call.get("args", {})
        name = call.get("tool_name")
        if name not in {"load_skill", "read_skill_resource"} or args.get("skill_name") != "ci-self-healing":
            continue
        resource = "SKILL.md" if name == "load_skill" else args.get("resource_name")
        if resource not in section_map or (event["seq"], resource) not in confirmed or not isinstance(p.get("result"), str):
            continue
        body = p["result"]
        excerpts = sections(body, section_map[resource])
        replacement = {"resource": resource, "full_read_confirmed_by_host": True,
            "original_result_sha256": digest(body.encode()), "verbatim_clauses": excerpts,
            "excluded_content": "other Skill guidance; not reviewed in this scoped five-criterion review"}
        # Keep exact public source for audit; never modify packet/grade in place.
        p["result"] = replacement
        projection.append({"evidence_id": event["evidence_id"], "resource": resource,
                           "original_bytes": len(body.encode()), "headings": section_map[resource]})
    # Reversible substitution inside arguments, logs, reports and paths. No
    # shortening of commands, decisions, outputs or counterevidence.
    aliases = {}
    if trial_prefix:
        alias = "<TRIAL_ROOT>"
        serialized = json.dumps(value, ensure_ascii=False)
        if alias in serialized:
            raise EvalError("review path alias collides with evidence")
        def replace(item):
            if isinstance(item, str):
                return item.replace(str(trial_prefix), alias)
            if isinstance(item, list):
                return [replace(v) for v in item]
            if isinstance(item, dict):
                return {replace(k): replace(v) for k, v in item.items()}
            return item
        value = replace(value)
        aliases[alias] = str(trial_prefix)
    counts = Counter()
    def count(item):
        if isinstance(item, str) and len(item) >= 256:
            counts[item] += 1
        elif isinstance(item, list):
            for child in item:
                count(child)
        elif isinstance(item, dict):
            for child in item.values():
                count(child)
    count(value)
    # Use an ORIGINAL evidence ID as the shared-text key, never a new T/P
    # citation namespace which the verdict contract cannot accept. Prefer a
    # file's content, then a public message/result with unambiguous provenance.
    pool = {}
    for item in value.get("files", []):
        text = item.get("text")
        if isinstance(text, str) and counts[text] > 1:
            pool.setdefault(text, item["evidence_id"])
    for event in value.get("events", []):
        key = {"InvocationMessage": "text", "InvocationToolCallResult": "result"}.get(event["kind"])
        text = event["payload"].get(key) if key else None
        if isinstance(text, str) and counts[text] > 1:
            pool.setdefault(text, event["evidence_id"])
    def intern(item):
        if isinstance(item, str) and item in pool:
            return {"shared_text_ref": pool[item]}
        if isinstance(item, list):
            return [intern(child) for child in item]
        if isinstance(item, dict):
            if set(item) == {"shared_text_ref"}:
                raise EvalError("review shared-text marker collides with evidence")
            return {key: intern(child) for key, child in item.items()}
        return item
    value = intern(value)
    value["shared_text"] = {key: text for text, key in pool.items()}
    value["review_projection"] = {"format": FORMAT, "source_packet_sha256": digest(json_bytes(packet)),
        "path_aliases": aliases, "skill_projections": projection,
        "scope": "five rubric criteria; every original event, seq, file, repository observation and agent decision retained; only listed Skill guide bodies excerpted",
        "boundary": "excluded guide text is NOT semantically reviewed; hashes prove binding only; shared_text_ref uses an ORIGINAL evidence ID and expands to its exact full text from shared_text; no new citation IDs"}
    return value
