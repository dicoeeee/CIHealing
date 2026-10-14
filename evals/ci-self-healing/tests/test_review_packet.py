"""Projection preserves decisions, counterevidence and event ordering offline."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import EvalError, read_regular
from review_packet import project_packet, sections
from repair_support import EXECUTION_CONTRACT, SPLIT_EXECUTION_CONTRACT, POLICY_EXECUTION_CONTRACT


def expand(value, pool):
    if isinstance(value, dict):
        if set(value) == {"shared_text_ref"}:
            return pool[value["shared_text_ref"]]
        return {k: expand(v, pool) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v, pool) for v in value]
    return value


class PacketTests(unittest.TestCase):
    def packet(self, resource="references/diagnosis-and-repair.md"):
        body = ("# Guide\n## 变更上下文与预期行为\n目标材料\n## 修复依据核查\n保留反证\n"
                "## 自主推进与停止\n不能区分则停止\n## 其他\n" + "unrelated mechanics\n" * 100)
        events = [
            {"seq": 1, "evidence_id": "E0001", "kind": "InvocationToolCallStart", "payload": {
                "tool_name": "read_skill_resource", "call_id": "r", "args": {"skill_name": "ci-self-healing", "resource_name": resource}}},
            {"seq": 2, "evidence_id": "E0002", "kind": "InvocationToolCallResult", "payload": {"call_id": "r", "result": body}},
            {"seq": 3, "evidence_id": "E0003", "kind": "InvocationMessage", "payload": {"text": "allow despite opposing MR; 未解反证"}},
            {"seq": 4, "evidence_id": "E0004", "kind": "InvocationToolCallResult", "payload": {
                "changed_paths_since_previous_event": ["scene/sourcecode/src/main/java/X.java"], "result": "mismatch"}}]
        return {"events": events, "files": [{"evidence_id": "F0001", "path": "/frozen/scene/mr.txt", "text": "MR明确反对所选方向"}],
                "deterministic": {"execution_checks": {"resource_reads": [{"seq": 2, "resource": resource, "complete": True}]}}}

    def test_projection_preserves_every_event_and_counterevidence_without_mutation(self):
        p = self.packet(); old = copy.deepcopy(p); q = project_packet(p)
        self.assertEqual(p, old)
        self.assertEqual(q["events"][2:], p["events"][2:])
        self.assertEqual(q["files"], p["files"])
        self.assertEqual([e["seq"] for e in q["events"]], [1, 2, 3, 4])
        clauses = q["events"][1]["payload"]["result"]["verbatim_clauses"]
        self.assertIn("保留反证", clauses[1]["text"])
        self.assertNotIn("unrelated mechanics", json.dumps(q, ensure_ascii=False))

    def test_unconfirmed_or_unknown_resource_is_not_filtered(self):
        for p in [self.packet("references/custom-policy.md"), self.packet()]:
            if p["events"][0]["payload"]["args"]["resource_name"] != "references/custom-policy.md":
                p["deterministic"]["execution_checks"]["resource_reads"][0]["complete"] = False
            q = project_packet(p)
            self.assertEqual(q["events"], p["events"])

    def test_missing_required_clause_stops_instead_of_silent_omission(self):
        p = self.packet(); p["events"][1]["payload"]["result"] = "# shortened guide"
        with self.assertRaisesRegex(EvalError, "clause missing"):
            project_packet(p)

    def test_split_guides_preserve_their_own_verbatim_semantic_clauses(self):
        skill = Path(__file__).resolve().parents[3] / "skills/ci-self-healing"
        for resource in ("references/diagnosis-and-repair.md", "references/repair-basis.md", "references/workspace-recovery.md",
                         "references/candidate-verification.md", "references/verification-failures.md", "references/collaboration.md",
                         "references/enterprise-policy.md", "references/repair-and-verification.md", "references/execution-modes.md"):
            with self.subTest(resource=resource):
                p = self.packet(resource)
                body = read_regular(skill / resource).decode()
                p["events"][1]["payload"]["result"] = body
                p["deterministic"]["execution_checks"]["contract"] = EXECUTION_CONTRACT
                q = project_packet(p)
                clauses = q["events"][1]["payload"]["result"]["verbatim_clauses"]
                self.assertTrue(clauses)
                for clause in clauses:
                    lines = body.splitlines(keepends=True)
                    self.assertEqual(clause["text"], "".join(lines[clause["start_line"] - 1:clause["end_line"]]))
                self.assertEqual(q["events"][2:], p["events"][2:])
                if resource.endswith("repair-basis.md"):
                    self.assertIn("语义方向歧义", [c["heading"] for c in clauses])
                if resource.endswith("workspace-recovery.md"):
                    self.assertIn("基准缺失或时序错误的恢复", [c["heading"] for c in clauses])

    def test_missing_split_semantic_clause_is_not_silently_projected_away(self):
        p = self.packet("references/repair-basis.md")
        p["deterministic"]["execution_checks"]["contract"] = EXECUTION_CONTRACT
        p["events"][1]["payload"]["result"] = "# Missing basis\n"
        with self.assertRaisesRegex(EvalError, "clause missing"):
            project_packet(p)

    def test_v4_policy_statement_and_prohibitions_are_verbatim_and_not_fabricated(self):
        resource = "references/enterprise-policy.md"
        p = self.packet(resource)
        body = read_regular(Path(__file__).resolve().parents[3] / "skills/ci-self-healing" / resource).decode()
        # A trusted frozen policy may impose restrictions; projection must retain them.
        body = body.replace("未配置企业附加策略。", "合成测试策略：禁止修改 src/main/java。")
        p["events"][1]["payload"]["result"] = body
        p["deterministic"]["execution_checks"]["contract"] = EXECUTION_CONTRACT
        q = project_packet(p)
        clauses = q["events"][1]["payload"]["result"]["verbatim_clauses"]
        self.assertIn("禁止修改 src/main/java", clauses[0]["text"])
        self.assertTrue(any("repair_admission: stop" in c["text"] for c in clauses))
        p["deterministic"]["execution_checks"]["resource_reads"][0]["complete"] = False
        self.assertEqual(project_packet(p)["events"], p["events"])
        p["events"] = p["events"][2:]
        self.assertEqual(project_packet(p)["events"], p["events"])

    def test_v3_keeps_old_section_map_and_unknown_new_resource_verbatim(self):
        p = self.packet("references/candidate-verification.md")
        p["deterministic"]["execution_checks"]["contract"] = SPLIT_EXECUTION_CONTRACT
        self.assertEqual(project_packet(p)["events"], p["events"])
        p = self.packet("references/repair-and-verification.md")
        p["deterministic"]["execution_checks"]["contract"] = SPLIT_EXECUTION_CONTRACT
        headings = ("修复依据", "候选修改前的准入记录", "决定修复准入", "分层验证", "结果判定")
        p["events"][1]["payload"]["result"] = "\n".join("## " + h + "\nfrozen v3 clause" for h in headings)
        actual = project_packet(p)["events"][1]["payload"]["result"]["verbatim_clauses"]
        self.assertEqual([c["heading"] for c in actual], list(headings))

    def test_policy_review_notice_is_versioned_without_changing_five_criteria(self):
        from review import build_prompt, schema
        p = self.packet()
        original_schema = schema()
        for contract in (SPLIT_EXECUTION_CONTRACT, POLICY_EXECUTION_CONTRACT, EXECUTION_CONTRACT):
            p["deterministic"]["execution_checks"]["contract"] = contract
            prompt = build_prompt(p)
            self.assertEqual("当前 v4 准入评审" in prompt, contract == POLICY_EXECUTION_CONTRACT)
            self.assertEqual("当前 v5 准入评审" in prompt, contract == EXECUTION_CONTRACT)
        self.assertEqual(schema(), original_schema)

    def test_v5_projects_input_and_stop_routes_without_changing_v4_projection(self):
        resource = "SKILL.md"
        p = self.packet(resource)
        body = read_regular(Path(__file__).resolve().parents[3] / "skills/ci-self-healing/SKILL.md").decode()
        p["events"][1]["payload"]["result"] = body
        for contract, headings in ((POLICY_EXECUTION_CONTRACT, ["3. 判断修复准入", "保持硬边界"]),
                                   (EXECUTION_CONTRACT, ["1. 确认任务与现场", "3. 判断修复准入", "保持硬边界"])):
            p["deterministic"]["execution_checks"]["contract"] = contract
            clauses = project_packet(p)["events"][1]["payload"]["result"]["verbatim_clauses"]
            self.assertEqual([c["heading"] for c in clauses], headings)
            for c in clauses:
                self.assertEqual(c["text"], "".join(body.splitlines(keepends=True)[c["start_line"] - 1:c["end_line"]]))

    def test_fenced_fake_heading_does_not_end_real_clause(self):
        text = "## Real\n```text\n## Fake\n```\nimportant\n## Next\n"
        self.assertIn("important", sections(text, ["Real"])[0]["text"])

    def test_exact_repeated_text_is_shared_not_summarized(self):
        p = self.packet(); evidence = "相反方向仍成立\n" * 100
        p["files"][0]["text"] = evidence
        p["events"][2]["payload"]["text"] = evidence
        q = project_packet(p); restored = expand(q, q["shared_text"])
        self.assertEqual(restored["files"], p["files"])
        self.assertEqual(restored["events"][2:], p["events"][2:])
        self.assertEqual(len(q["shared_text"]), 1)
        self.assertEqual(set(q["shared_text"]), {"F0001"})
        allowed = {f["evidence_id"] for f in p["files"]} | {e["evidence_id"] for e in p["events"]}
        self.assertLessEqual(set(q["shared_text"]), allowed)

    def test_shared_public_message_retains_original_event_citation(self):
        p = self.packet(); text = "准入与反证\n" * 100
        p["events"][2]["payload"]["text"] = text
        p["events"][3]["payload"]["result"] = text
        q = project_packet(p)
        self.assertEqual(set(q["shared_text"]), {"E0003"})
        self.assertEqual(expand(q, q["shared_text"])["events"][2:], p["events"][2:])

    def test_unsupported_old_pool_ids_are_not_coerced_into_a_pass(self):
        from test_eval import packet, verdict
        from review import validate_verdict
        p = packet(); v = verdict()
        v["criteria"][2]["evidence"].append("T1")
        with self.assertRaisesRegex(EvalError, "evidence reference"):
            validate_verdict(v, p)
        self.assertEqual(v["criteria"][2]["evidence"][-1], "T1")

    def test_path_alias_is_reversible_and_collisions_fail(self):
        p = self.packet(); q = project_packet(p, "/frozen")
        path = q["files"][0]["path"]
        self.assertEqual(path.replace("<TRIAL_ROOT>", q["review_projection"]["path_aliases"]["<TRIAL_ROOT>"]), p["files"][0]["path"])
        p["files"][0]["text"] = "<TRIAL_ROOT>"
        with self.assertRaises(EvalError):
            project_packet(p, "/frozen")


if __name__ == "__main__":
    unittest.main()
