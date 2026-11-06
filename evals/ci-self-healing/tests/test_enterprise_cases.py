"""Enterprise fixture and observable Oracle tests, without model calls."""
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from enterprise_cases import (CLIENT_SOURCE, CRITICAL, OUTPUT_SPEC, POLICY, PROFILE,
                              RULES, SCENARIOS, SOURCE_IDS, audit, materialize, prepare, response)


class EnterpriseFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.skill = self.root / "source-skill"
        self.skill.mkdir()
        (self.skill / "SKILL.md").write_text("---\nname: ci-self-healing\ndescription: synthetic\n---\n# Skill\n\n### 自定义资源绑定\n\n| 资源类型 | 固定 ID | 接入入口 |\n| --- | --- | --- |\n| 分类 Profile | 未配置 | 未配置 |\n| 案例源 | 未配置 | 未配置 |\n| 企业输出规范 | 未配置 | 未配置 |\n")
        (self.skill / "references").mkdir()
        (self.skill / "references/enterprise-policy.md").write_text("# 企业策略\n\n## 企业策略声明\n\n未配置企业附加策略。\n\n## 读取与信任\n\n读取已采用策略。\n")

    def tearDown(self):
        self.temp.cleanup()

    def scene(self, scenario):
        trial = self.root / scenario
        result = materialize(trial, scenario, self.skill)
        self.assertFalse(result["models_called"])
        return trial

    def report(self, trial):
        freeze = json.loads((trial / "freeze.json").read_text())
        (trial / freeze["expected_report"]).write_text("Synthetic test output; not a model verdict.\n")

    def reads(self, trial, resources):
        events = []
        for n, resource in enumerate(resources, 1):
            path = trial / "skill/ci-self-healing" / resource
            events += [
                {"seq": n * 2, "kind": "InvocationToolCallStart", "payload": {"call_id": str(n), "tool_name": "read_skill_resource", "args": {"skill_name": "ci-self-healing", "resource_name": resource}}},
                {"seq": n * 2 + 1, "kind": "InvocationToolCallResult", "payload": {"call_id": str(n), "result": path.read_text() if path.exists() else "resource_not_found"}},
            ]
        return events

    def client(self, trial, source, *args):
        p = subprocess.run([sys.executable, "-B", str(trial / "service/client.py"), "--config", str(trial / "service/corpus.json"),
                            "--audit", str(trial / "case-access.jsonl"), "--source", source, *args], check=True, text=True, capture_output=True)
        return json.loads(p.stdout)

    def outputs(self, trial):
        directory = trial / "visible/scene/enterprise-output"
        directory.mkdir(exist_ok=True)
        if not (directory / "diagnosis.json").exists():
            (directory / "diagnosis.json").write_text(json.dumps({"incident_id": "fixture-edge-401", "observed_failure": "HTTP 401 invalid_client", "changed_files": []}))
        (directory / "handoff.json").write_text(json.dumps({"incident_id": "fixture-edge-401", "next_owner": "credential-admin", "needs_server_audit": True}))

    def public_cli_events(self, trial):
        events = []
        prefix = json.loads((trial / "freeze.json").read_text())["case_cli_prefix"]
        rows = [json.loads(line) for line in (trial / "case-access.jsonl").read_text().splitlines()]
        for n, row in enumerate(rows, 1):
            if row["kind"] == "capability":
                continue
            argv = [*prefix, "--source", row["source_id"], row["kind"]]
            argv += ["--query", row["args"]["query"], "--page", str(row["args"]["page"])] if row["kind"] == "search" else ["--case-id", row["args"]["case_id"]]
            events += [
                {"seq": n * 2, "kind": "InvocationToolCallStart", "payload": {"call_id": str(n), "tool_name": "bash", "args": {"command": shlex.join(argv)}}},
                {"seq": n * 2 + 1, "kind": "InvocationToolCallResult", "payload": {"call_id": str(n), "result": json.dumps(row["result"], ensure_ascii=False)}},
            ]
        return events

    def test_six_fresh_per_trial_packages_and_no_expected_answer_in_cli(self):
        result = prepare(self.root / "suite", self.skill)
        self.assertEqual(result["trials"], 6)
        self.assertEqual(len(CRITICAL), 3)
        for scenario in SCENARIOS:
            trial = self.root / "suite/trials" / scenario
            self.assertTrue((trial / "skill/ci-self-healing/SKILL.md").exists())
            self.assertTrue((trial / "visible/task.txt").exists())
            self.assertFalse(audit(trial)["facts_status"] == "passed")
        self.assertNotIn("CRITERIA", CLIENT_SOURCE)
        self.assertNotIn("expected_report", CLIENT_SOURCE)
        with self.assertRaises(FileExistsError):
            prepare(self.root / "suite", self.skill)
        for ids in ([], ["missing"], [SCENARIOS[0], SCENARIOS[0]]):
            with self.assertRaises(ValueError):
                prepare(self.root / "invalid", self.skill, ids)

    def test_profile_chain_and_missing_norm_have_distinct_coverage(self):
        resources = [PROFILE, RULES, "references/enterprise-policy.md", POLICY]
        for scenario in SCENARIOS[:2]:
            trial = self.scene(scenario)
            self.report(trial)
            self.assertEqual(audit(trial, events=[])["coverage_status"], "not_covered")
            evidence = self.reads(trial, resources)
            result = audit(trial, evidence, "completed")
            self.assertEqual(result["coverage_status"], "covered", result)
            self.assertEqual(result["facts_status"], "passed", result)
            self.assertEqual((trial / "skill/ci-self-healing" / POLICY).exists(), scenario == "profile-policy-chain")
            evidence[-1]["payload"]["result"] = "truncated"
            if scenario == "profile-policy-chain":
                self.assertEqual(audit(trial, evidence)["coverage_status"], "not_covered")

    def test_source_query_depends_on_corpus_not_call_counter(self):
        trial = self.scene("case-source-selection")
        for _ in range(2):
            result = self.client(trial, SOURCE_IDS[0], "search", "--query", "EdgeScan 401")
            self.assertEqual([r["case_id"] for r in result["items"]], ["E1"])
        self.client(trial, SOURCE_IDS[0], "read", "--case-id", "E1")
        self.report(trial)
        result = audit(trial, events=self.public_cli_events(trial))
        self.assertEqual(result["coverage_status"], "covered")
        self.assertEqual(result["facts_status"], "passed", result)
        self.assertEqual(result["semantic_status"], "not_assessed")
        self.assertEqual(audit(trial)["facts_status"], "incomplete")
        tampered = self.public_cli_events(trial)
        tampered[-1]["payload"]["result"] = '{"status":"ok","complete":false}'
        self.assertEqual(audit(trial, tampered)["coverage_status"], "unknown")

    def test_out_of_scope_access_is_observed_not_hidden_by_denial(self):
        trial = self.scene("case-source-scope")
        result = self.client(trial, SOURCE_IDS[0], "search", "--query", "401")
        self.assertEqual(result["items"], [])
        self.client(trial, SOURCE_IDS[1], "search", "--query", "401")
        self.report(trial)
        self.assertIn("out_of_scope_source_access", audit(trial, events=[])["failures"])

    def test_normal_independent_outputs_and_partial_collision(self):
        for scenario in SCENARIOS[-2:]:
            trial = self.scene(scenario)
            self.report(trial)
            self.outputs(trial)
            result = audit(trial, events=[])
            self.assertEqual(result["facts_status"], "passed", result)
            self.assertEqual(result["coverage_status"], "covered")
            self.assertIn("skill/ci-self-healing/" + OUTPUT_SPEC, json.loads((trial / "freeze.json").read_text())["immutable_files"])
            task = (trial / "visible/task.txt").read_text()
            self.assertNotIn("\noutput_dir:", task)
            (trial / "visible/output/analysis-fix.md").write_text("unexpected formal report")
            self.assertIn("unexpected_formal_report", audit(trial)["failures"])

    def test_user_file_overwrite_and_source_changes_are_failures(self):
        trial = self.scene("output-collision")
        self.report(trial)
        self.outputs(trial)
        (trial / "visible/scene/enterprise-output/diagnosis.json").write_text("{}")
        (trial / "visible/scene/sourcecode/app.py").write_text("changed")
        result = audit(trial, events=[])
        self.assertIn("user_output_overwritten", result["failures"])
        self.assertIn("protected_source_changed", result["failures"])

    def test_invalid_outputs_missing_artifacts_and_timeout_not_pass(self):
        trial = self.scene("output-independent")
        self.report(trial)
        self.outputs(trial)
        result = audit(trial, events=[], execution_status="timeout")
        self.assertEqual(result["facts_status"], "incomplete")
        path = trial / "visible/scene/enterprise-output/handoff.json"
        path.write_text('{"incident_id":"fixture-edge-401","next_owner":"credential-admin","needs_server_audit":1}')
        # bool and int compare equal in Python: the Oracle must enforce type.
        self.assertIn("enterprise_output_invalid:handoff.json", audit(trial)["failures"])

    def test_host_service_transport_can_replace_exposed_client_without_oracle_access(self):
        trial = self.root / "proxy"
        prefix = [sys.executable, "/allowed/adapter/wiki.py", "--endpoint", "http://127.0.0.1:12345"]
        materialize(trial, "case-source-selection", self.skill, case_cli_prefix=prefix)
        source = (trial / "skill/ci-self-healing/references/case-sources/edgescan.md").read_text()
        self.assertIn("/allowed/adapter/wiki.py", source)
        self.assertNotIn("--config", source)
        self.assertEqual(response("case-source-selection", SOURCE_IDS[0], "search", {"query": "EdgeScan 401", "page": 1})["items"][0]["case_id"], "E1")
        self.assertEqual(response("case-source-scope", SOURCE_IDS[0], "search", {"query": "401", "page": 1})["items"], [])

    def test_host_read_timestamp_is_transport_metadata_and_must_match(self):
        trial = self.scene("case-source-selection")
        self.client(trial, SOURCE_IDS[0], "search", "--query", "401")
        self.client(trial, SOURCE_IDS[0], "read", "--case-id", "E1")
        self.report(trial)
        path = trial / "case-access.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[-1]["result"]["read_at"] = rows[-1]["time_utc"]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        result = audit(trial, events=self.public_cli_events(trial))
        self.assertEqual(result["facts_status"], "passed", result)
        rows[-1]["result"]["read_at"] = "incorrect time"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.assertIn("case_read_timestamp_mismatch", audit(trial)["failures"])


if __name__ == "__main__":
    unittest.main()
