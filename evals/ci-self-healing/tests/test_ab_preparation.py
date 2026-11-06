"""A/B physical-path preparation and evidence-packet boundary regressions."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ab_runtime
from common import read_json, write_json, write_new
import enterprise_cases
import handoff_cases
import keyword_cases


class ABPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.skill = self.root / "original-skill"
        self.skill.mkdir()
        (self.skill / "SKILL.md").write_text("---\nname: ci-self-healing\ndescription: synthetic\n---\n# Skill\n\n### 自定义资源绑定\n\n| 资源类型 | 固定 ID | 接入入口 |\n| --- | --- | --- |\n| 分类 Profile | 未配置 | 未配置 |\n| 案例源 | 未配置 | 未配置 |\n| 企业输出规范 | 未配置 | 未配置 |\n")
        (self.skill / "references").mkdir()
        (self.skill / "references/enterprise-policy.md").write_text("# 企业策略\n\n## 企业策略声明\n\n未配置企业附加策略。\n\n## 读取与信任\n\n读取已采用策略。\n")
        self.config = {"python": sys.executable, "git": shutil.which("git")}
        self.setup_patch = patch.object(ab_runtime, "setup", side_effect=self.fake_setup)
        self.setup_patch.start()

    def tearDown(self):
        self.setup_patch.stop()
        self.temp.cleanup()

    def fake_setup(self, config, trial, source):
        skill = trial / "runtime/skills/ci-self-healing"
        shutil.copytree(source, skill)
        (trial / "adapter").mkdir()
        return {"skill": str(skill), "visible": str(trial / "visible")}

    def test_all_enterprise_native_preparations_preserve_frozen_inputs(self):
        for scenario in enterprise_cases.SCENARIOS:
            trial = self.root / scenario
            record = ab_runtime.prepare_trial(trial, "enterprise", scenario, self.skill, self.config)
            self.assertFalse((trial / "before-run.json").exists())
            self.assertTrue((trial / "fixture-before-run.json").exists())
            self.assertEqual(enterprise_cases.audit(trial, events=[])["failures"], [])
            self.assertEqual(record["pipeline_report"], scenario.startswith("profile-policy"))
            task = (trial / "visible/task.txt").read_text()
            self.assertIn(str(trial / "runtime/skills/ci-self-healing"), task)
            self.assertNotIn(str(trial / "skill/ci-self-healing"), task)
            self.assertNotEqual(record["source_skill_sha256"], record["configured_source_skill_sha256"])

    def test_keyword_and_handoff_template_paths_are_local_to_new_trial(self):
        keyword = self.root / "keyword-template"
        keyword_cases.prepare(keyword, self.skill, case_ids=["component-first"])
        handoff = self.root / "handoff-template"
        handoff_cases.prepare_cases(handoff, self.skill, git=self.config["git"])
        for group, scenario, template in (("keyword", "component-first", keyword / "trials/trial-01"),
                                           ("handoff", "adopt", handoff / "trials/adopt")):
            trial = self.root / (group + "-trial")
            ab_runtime.prepare_trial(trial, group, scenario, self.skill, self.config, template)
            for path in (trial / "visible").rglob("*"):
                if path.is_file() and ".git" not in path.parts:
                    self.assertNotIn(str(template), path.read_text(), str(path))
            if group == "keyword":
                task = (trial / "visible/task.txt").read_text()
                source = (trial / "visible/scene/case-source.txt").read_text()
                self.assertIn(str(trial / "runtime/skills/ci-self-healing"), task)
                self.assertIn(str(trial / "adapter/case_client.py"), source)
                self.assertNotIn(str(keyword / "harness"), source)
            self.assertFalse((trial / "before-run.json").exists())

    def prepare_packet(self):
        trial = self.root / "packet-trial"
        ab_runtime.prepare_trial(trial, "enterprise", "output-independent", self.skill, self.config)
        write_json(trial / "run.json", {"status": "completed"})
        write_json(trial / "objective.json", {"status": "incomplete", "failures": [], "gaps": ["test_no_real_execution"]})
        return trial

    def test_packet_deduplicates_only_full_resources_and_preserves_suffix(self):
        trial = self.prepare_packet()
        skill = trial / "runtime/skills/ci-self-healing"
        body = (skill / "SKILL.md").read_text()
        policy = (skill / "references/enterprise-policy.md").read_text()
        events = [
            {"kind": "InvocationToolCallStart", "seq": 1, "evidence_id": "E1", "payload": {"call_id": "1", "tool_name": "load_skill", "args": {"skill_name": "ci-self-healing"}}},
            {"kind": "InvocationToolCallResult", "seq": 2, "evidence_id": "E2", "payload": {"call_id": "1", "result": body + "\nHost metadata stays visible."}},
            {"kind": "InvocationToolCallStart", "seq": 3, "evidence_id": "E3", "payload": {"call_id": "2", "tool_name": "read_skill_resource", "args": {"skill_name": "ci-self-healing", "resource_name": "references/enterprise-policy.md"}}},
            {"kind": "InvocationToolCallResult", "seq": 4, "evidence_id": "E4", "payload": {"call_id": "2", "result": policy[:20]}},
        ]
        write_json(trial / "public-trace.json", {"events": events, "gaps": []})
        packet = ab_runtime.packet(trial)
        self.assertEqual(packet["shared_resources"]["SKILL.md"], body)
        self.assertIn("Host metadata stays visible.", packet["events"][1]["payload"]["result"]["trailing_text"])
        self.assertEqual(packet["events"][3]["payload"]["result"], policy[:20])
        self.assertNotIn("references/enterprise-policy.md", packet["shared_resources"])

    def test_packet_does_not_read_symlinks_or_access_restricted_markdown(self):
        trial = self.prepare_packet()
        write_json(trial / "public-trace.json", {"events": [], "gaps": []})
        write_new(trial / "host-only.txt", "HOST_ONLY_ORACLE_MARKER")
        (trial / "visible/secret-link.txt").symlink_to(trial / "host-only.txt")
        write_new(trial / "visible/restricted.md", "---\nai_access: false\n---\nRESTRICTED_BODY_MARKER\n")
        packet = ab_runtime.packet(trial)
        encoded = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn("HOST_ONLY_ORACLE_MARKER", encoded)
        self.assertNotIn("RESTRICTED_BODY_MARKER", encoded)
        self.assertEqual({r["reason"] for r in packet["omitted_files"]}, {"symlink_not_followed", "restricted_or_unreadable_file"})


if __name__ == "__main__":
    unittest.main()
