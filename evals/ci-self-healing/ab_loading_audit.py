"""Uniform v6/v7 post-audit of three already-frozen loading prerequisites.

This is a bounded observation repair, not a new Skill rule or a model rerun.
Only trusted fixture bindings and native public tool events participate.
"""
import argparse
import json
from pathlib import Path
import shlex

from common import EvalError, digest, json_bytes, read_json, read_regular, write_json
from skill_execution import is_build, script_tokens

CONTRACT = "ci-ab-common-loading-audit-v1"
RULES = {
    "classification_before_bound_profile": {
        "guide": "references/failure-classification.md",
        "clause": "先完整读取[分类执行](references/failure-classification.md)"},
    "case_guide_before_bound_source": {
        "guide": "references/case-retrieval.md",
        "clause": "案例访问顺序为：确认触发条件 → 完整读取案例指南 → 将已选定的案例接入说明列入当前读取范围"},
    "output_guide_before_output_action": {
        "guide": "references/output-delivery.md",
        "clause": "完成后再使用输出路径或执行写入式诊断/验证"},
}


def fixture_bindings(trial, row):
    """Explicit synthetic-fixture mapping; no directory discovery or prose policy parser."""
    trial = Path(trial)
    scene, skill = trial / "visible/scene", trial / "runtime/skills/ci-self-healing"
    profiles, sources = set(), set()
    group, scenario = row["group"], row["scenario"]
    if group == "handoff":
        if scenario != "empty":
            profiles.add(str(scene / "profile.txt"))
        sources.add(str(scene / "case-source.txt"))
    elif group == "keyword":
        sources.add(str(scene / "case-source.txt"))
    elif group == "enterprise":
        if scenario in {"profile-policy-chain", "profile-policy-missing"}:
            profiles.add(str(skill / "references/classification-profiles/synthetic/build/profile.md"))
        elif scenario in {"case-source-selection", "case-source-scope"}:
            # Both entries are declared by the frozen package. Permission to
            # query the second source is a separate scope check, not this gate.
            sources.update(str(skill / "references/case-sources" / name) for name in ("edgescan.md", "packaging.md"))
    elif group != "main":
        raise ValueError("unsupported fixture group")
    outputs = {str(trial / "visible/output")}
    if group == "enterprise" and scenario in {"output-independent", "output-collision"}:
        outputs.add(str(scene / "enterprise-output"))
    return {"profiles": sorted(profiles), "case_sources": sorted(sources), "output_roots": sorted(outputs),
            "source_root": str(scene / "sourcecode"), "skill_root": str(skill),
            "basis": "frozen cases.py/handoff_cases.py/keyword_cases.py/enterprise_cases.py fixture bindings, not filename resemblance"}


def beneath(path, root):
    if not isinstance(path, str) or not Path(path).is_absolute():
        return False
    # Resolve lexical .. without following a model-controlled filesystem link.
    import os
    value, base = os.path.normpath(path), os.path.normpath(root)
    return value == base or value.startswith(base + "/")


def literal_paths(payload, skill):
    args, name = payload.get("args", {}), payload.get("tool_name")
    paths = [args[key] for key in ("path", "file_path", "filename", "directory") if isinstance(args.get(key), str)]
    if name == "read_skill_resource" and args.get("skill_name") == "ci-self-healing":
        value = args.get("resource_name")
        if isinstance(value, str) and not Path(value).is_absolute() and ".." not in Path(value).parts:
            paths.append(str(Path(skill) / value))
    if name == "run_skill_script" and args.get("skill_name") == "ci-self-healing":
        try:
            paths.extend(t for t in script_tokens(args) if isinstance(t, str) and Path(t).is_absolute())
        except ValueError:
            pass
    if name == "bash":
        try:
            lex = shlex.shlex(args.get("command", ""), posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            # Literal argument tokens only: do not interpret code, substitutions,
            # relative shell cwd changes, variable expansions or quoted prose.
            paths.extend(t for t in lex if t.startswith("/") and not any(c in t for c in ("\n", "$", "`")))
        except ValueError:
            pass
    return paths


def reads_content(payload):
    if payload.get("tool_name") in {"read_file", "read_skill_resource"}:
        return True
    if payload.get("tool_name") == "bash":
        try:
            tokens = shlex.split(payload.get("args", {}).get("command", ""))
            return bool(tokens) and Path(tokens[0]).name in {"cat", "head", "tail", "sed"}
        except ValueError:
            return False
    return False


def rule_sources(skill):
    body = read_regular(Path(skill) / "SKILL.md")
    lines = body.decode().splitlines()
    result = {}
    for name, rule in RULES.items():
        matches = [i for i, text in enumerate(lines, 1) if rule["clause"] in text]
        if len(matches) != 1:
            raise ValueError("frozen common rule clause missing or ambiguous: " + name)
        result[name] = {"path": str(Path(skill) / "SKILL.md"), "line": matches[0],
                        "sha256": digest(body), "clause": rule["clause"], "guide": rule["guide"]}
    return result


def audit_events(events, skill, bindings, *, execution_contract, resource_reads=(), trace_gaps=()):
    if execution_contract not in {"skill-execution-v6", "skill-execution-v7", "skill-execution-v8"}:
        raise ValueError("post-audit only supports frozen v6/v7/v8 contracts")
    sources = rule_sources(skill)
    calls, complete, observed, actions, violations = {}, {}, [], [], []
    gaps = list(trace_gaps)
    if any(type(e.get("seq")) is not int or e["seq"] != n for n, e in enumerate(events, 1)):
        gaps.append("noncontinuous_public_trace")
    prior_rows = {(r.get("resource"), r.get("seq"), r.get("complete")) for r in resource_reads}
    guide_bodies = {rule["guide"]: read_regular(Path(skill) / rule["guide"]).decode().strip() for rule in RULES.values()}
    for event in events:
        seq, kind, p = event.get("seq"), event.get("kind"), event.get("payload", {})
        if type(seq) is not int:
            continue
        if kind == "InvocationToolCallResult":
            start = calls.get(p.get("call_id"))
            if start and start["seq"] < seq:
                args = start["payload"].get("args", {})
                name = args.get("resource_name")
                if (start["payload"].get("tool_name") == "read_skill_resource"
                        and args.get("skill_name") == "ci-self-healing" and name in guide_bodies):
                    full = isinstance(p.get("result"), str) and p["result"].strip() == guide_bodies[name]
                    observed.append({"resource": name, "start_seq": start["seq"], "result_seq": seq,
                                     "evidence_id": event.get("evidence_id"), "complete": full,
                                     "original_resource_reads_agree": (name, seq, full) in prior_rows})
                    if full:
                        complete.setdefault(name, seq)
        triggered = []
        if kind == "InvocationToolCallStart":
            calls[p.get("call_id")] = event
            paths = literal_paths(p, bindings["skill_root"])
            for target in paths:
                if reads_content(p) and target in bindings["profiles"]:
                    triggered.append(("classification_before_bound_profile", target))
                if reads_content(p) and target in bindings["case_sources"]:
                    triggered.append(("case_guide_before_bound_source", target))
                if any(beneath(target, root) for root in bindings["output_roots"]):
                    triggered.append(("output_guide_before_output_action", target))
                if p.get("tool_name") in {"write_file", "edit_file"} and beneath(target, bindings["source_root"]):
                    triggered.append(("output_guide_before_output_action", target))
            if is_build(p):
                triggered.append(("output_guide_before_output_action", "recognized_build_command"))
        changed = p.get("changed_paths_since_previous_event", [])
        for path in changed:
            if isinstance(path, str) and (path == "output" or path.startswith(("output/", "scene/enterprise-output/", "scene/sourcecode/"))):
                target = str(Path(bindings["source_root"]).parents[1] / path)
                if any(beneath(target, root) for root in [*bindings["output_roots"], bindings["source_root"]]):
                    triggered.append(("output_guide_before_output_action", "observed_changed_path:" + path))
        for rule, target in sorted(set(triggered)):
            guide = RULES[rule]["guide"]
            read_seq = complete.get(guide)
            status = "pass" if read_seq is not None and read_seq < seq else "incomplete" if gaps else "fail"
            record = {"rule": rule, "status": status, "action_seq": seq, "action_evidence_id": event.get("evidence_id"),
                      "target": target, "required_resource": guide, "earliest_complete_result_seq": read_seq}
            actions.append(record)
            if status == "fail":
                violations.append(record)
    for action in actions:
        later = [r for r in observed if r["resource"] == action["required_resource"] and r["complete"]]
        action["first_observed_complete_result_seq"] = min((r["result_seq"] for r in later), default=None)
        action["complete_read_evidence_ids"] = [r["evidence_id"] for r in later]
    statuses = {r["status"] for r in actions}
    status = "fail" if violations else "incomplete" if gaps or "incomplete" in statuses else "pass"
    return {"contract": CONTRACT, "execution_contract": execution_contract, "status": status,
            "violations": violations, "gaps": sorted(set(gaps)), "actions": actions, "resource_reads": observed,
            "rule_sources": sources, "rules": {name: {"trigger_count": sum(a["rule"] == name for a in actions),
                "status": "not_applicable" if not any(a["rule"] == name for a in actions) else
                "fail" if any(a["rule"] == name and a["status"] == "fail" for a in actions) else
                "incomplete" if any(a["rule"] == name and a["status"] == "incomplete" for a in actions) else "pass"} for name in RULES},
            "boundary": "same frozen rules for v6/v7; exact complete Agent tool returns before dependent starts; host preflight excluded; independent parallelism and earlier valid reads allowed; opaque shell/relative paths need separate semantic review"}


def audit_trial(trial, row, *, snapshot_root=None):
    trial = Path(trial)
    trace_path, run_path = trial / "public-trace.json", trial / "run.json"
    if not trace_path.exists() or not run_path.exists():
        return {"contract": CONTRACT, "status": "not_assessed", "gaps": ["completed_trial_evidence_missing"], "violations": []}
    try:
        trace_raw, run_raw = read_regular(trace_path), read_regular(run_path)
        trace, run = json.loads(trace_raw), json.loads(run_raw)
        objective_raw = read_regular(trial / "objective.json") if (trial / "objective.json").exists() else None
        objective = json.loads(objective_raw) if objective_raw else {}
        bindings = fixture_bindings(trial, row)
        result = audit_events(trace["events"], bindings["skill_root"], bindings,
                    execution_contract=run["skill_execution_contract"], trace_gaps=trace.get("gaps", []),
                    resource_reads=objective.get("execution_checks", {}).get("resource_reads", []))
        result.update(trial_path=str(trial), fixture={k: row[k] for k in ("group", "scenario")}, bindings=bindings,
            evidence_sha256={"public_trace": digest(trace_raw), "run": digest(run_raw),
                             "objective": digest(objective_raw) if objective_raw else None,
                             "auditor": digest(read_regular(Path(__file__)))})
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, EvalError) as exc:
        result = {"contract": CONTRACT, "status": "incomplete", "trial_path": str(trial), "violations": [],
                  "gaps": ["post_audit_evidence_unavailable:" + type(exc).__name__]}
    if snapshot_root is not None:
        folder = Path(snapshot_root)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (digest(json_bytes(result)) + ".json")
        if not path.exists():
            write_json(path, result)
        elif read_regular(path) != json_bytes(result):
            raise EvalError("immutable loading snapshot differs")
        result = {**result, "snapshot_path": str(path)}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trial", type=Path)
    parser.add_argument("--snapshot-root", required=True, type=Path)
    args = parser.parse_args()
    row = read_json(args.trial / "ab-case.json")
    result = audit_trial(args.trial, row, snapshot_root=args.snapshot_root)
    print(json.dumps({k: result.get(k) for k in ("status", "snapshot_path", "gaps")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
