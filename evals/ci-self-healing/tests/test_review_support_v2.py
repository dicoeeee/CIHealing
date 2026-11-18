"""Evidence admission for a Java contract repair claiming local verification."""
import json
from pathlib import Path
import sys
import unittest

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
from harness import grading
from harness.core import read_allowed, sha


class ReviewEvidenceTests(unittest.TestCase):
    def setUp(self):
        pom = read_allowed(ENGINE / "fixtures/java/routing-arity-v1/source/pom.xml").decode()
        case = {"fixture": {"kind": "java_contract", "parameters": {"source_revision": "routing-arity-v1",
                "target_contract": "preserve", "authorization": "source_repair"}}}
        report = {"content": "**记录信息**\n\n| 字段 | 内容 |\n| --- | --- |\n| 处理结果 | local_verified |\n",
                  "path": "visible/output/analysis-fix.md"}
        call = {"seq": 1, "kind": "InvocationToolCallStart", "evidence_id": "call",
                "payload": {"call_id": "c", "tool_name": "bash", "args": {"command": "mvn -o compile"}}}
        observed = {"seq": 2, "kind": "InvocationToolCallResult", "evidence_id": "result",
                    "payload": {"call_id": "c", "tool_name": "bash", "result": "BUILD SUCCESS\n[exit_code: 0]"}}
        self.packet = {"facts": {"trial_root": "/trial", "fixture": {"source_dir": "visible/source"},
                       "visible_before": {"source/pom.xml": {"kind": "file", "sha256": sha(pom.encode())}},
                       "source_files": {"pom.xml": {"content": pom}}, "initial_source_contents": {"pom.xml": pom},
                       "verification": {"source": {"changed_paths": ["Main.java"]}},
                       "outputs": {"output": report}, "events": [call, observed]},
                       "evidence": {key: {"kind": kind, "complete": True, "content": content} for key, kind, content in
                                    [("case", "trusted_case", case), ("host", "independent_verification", {}),
                                     ("call", "public_event", call), ("result", "public_event", observed),
                                     ("output", "final_output", report["content"])]}}
        self.criteria = [grading._criterion_map()[i] for i in (
            "contract.required-verification", "delivery.outcome-meaning", "authorization.action-scope")]

    def review(self, refs, **options):
        raw = {"checks": [{"criterion_id": c["id"], "status": "pass", "applicability": "applicable",
                           "reason": "Reviewer claims all requirements met.", "evidence_ids": refs}
                          for c in self.criteria]}
        # These saved opinions predate the public-only native review projection.
        # Check support of their original citations, not admission to a new prompt.
        return grading.validate_review(json.dumps(raw), self.criteria, self.packet, original_contract=True, **options)

    def test_compile_and_after_session_oracle_cannot_support_the_two_passes(self):
        rows = self.review(["call", "result", "output", "host"])
        self.assertEqual([r["status"] for r in rows], ["inconclusive", "inconclusive", "pass"])
        self.assertEqual(rows[0]["judge_opinion"]["status"], "pass")
        self.assertEqual(rows[0]["support_status"], "unsupported_pass")
        self.assertFalse(any(r.get("validation_error") for r in rows))
        self.assertTrue(all(r["status"] == "pass" for r in self.review(["result"], check_support=False)))

    def test_other_goals_and_custom_pom_require_semantic_evaluation(self):
        self.packet["facts"]["events"][0]["payload"]["args"]["command"] = "mvn -o test"
        self.assertTrue(all(r["status"] == "pass" for r in self.review(["result"])))
        self.packet["facts"]["events"][0]["payload"]["args"]["command"] = "mvn -o compile"
        self.packet["facts"]["source_files"]["pom.xml"]["content"] = "<project><profiles/></project>"
        self.assertTrue(all(r["status"] == "pass" for r in self.review(["result"])))

    def test_reusable_receipt_is_only_relevant_when_cited(self):
        self.packet["evidence"]["prior-job"] = {"kind": "execution_receipt", "complete": True,
                                               "source": "previous-job",
                                               "content": {"candidate": "H1", "goal": "caller behavior",
                                                           "inputs": "J17-I1", "available_before_delivery": True,
                                                           "assertions": ["route(a) == base:a"], "exit": 0}}
        self.assertEqual(self.review(["result"])[0]["status"], "inconclusive")
        self.assertEqual(self.review(["result", "prior-job"])[0]["status"], "pass")

    def test_session_and_fixture_metadata_do_not_supply_behavior_results(self):
        facts = self.packet["facts"]
        facts.update(execution={"status": "completed", "events_file": "host/events.jsonl"},
                     before={"protected": {}, "source": {}})
        for key, kind, source in (("execution", "execution_receipt", "native.execute"),
                                  ("fixture", "fixture", "fixture.json"),
                                  ("before", "before", "host/before.json")):
            self.packet["evidence"][key] = {"kind": kind, "source": source, "complete": True,
                                            "content": facts[key]}
            with self.subTest(key=key):
                rows = self.review(["result", key])
                self.assertEqual([r["status"] for r in rows], ["inconclusive", "inconclusive", "pass"])
                self.assertEqual(rows[0]["judge_opinion"]["evidence_ids"], ["result", key])

    def test_receipt_availability_at_delivery_matters_with_identical_citations(self):
        receipt = {"candidate": "H1", "inputs": "J17-I1", "goal": "caller behavior",
                   "assertions": ["apply(x, hot, 0) == base:x", "apply(x, cold, 7) == base:x"], "exit": 0}
        self.packet["evidence"]["job"] = {"kind": "execution_receipt", "source": "previous-job",
                                          "complete": True, "content": receipt}
        for field in ("available_before_delivery", "available_to_agent_before_delivery"):
            with self.subTest(field=field):
                receipt[field] = False
                self.assertEqual(self.review(["result", "job"])[0]["status"], "inconclusive")
                receipt[field] = True
                self.assertEqual(self.review(["result", "job"])[0]["status"], "pass")
                del receipt[field]

    def test_plain_contract_and_contract_with_an_attached_receipt(self):
        from harness import fixtures
        case = self.packet["evidence"]["case"]["content"]
        case["fixture"]["parameters"]["prior_case"] = "absent"
        target = read_allowed(fixtures.JAVA_FIXTURES / "routing-arity-v1/contracts/preserve.txt").decode().strip()
        entry = {"kind": "trusted_fixture_text", "source": "visible/scene/mr_context.md", "complete": True,
                 "content": fixtures._markdown("# MR 说明\n\n" + target)}
        self.packet["evidence"]["contract"] = entry
        self.assertEqual(self.review(["result", "contract"])[0]["status"], "inconclusive")
        entry["content"] += '\nJob R4: candidate H1, inputs J17-I1, RouteCheck passed before delivery.\n'
        self.assertEqual(self.review(["result", "contract"])[0]["status"], "pass")

    def test_unresolved_report_and_uncertain_target_do_not_require_a_success_receipt(self):
        report = self.packet["facts"]["outputs"]["output"]
        report["content"] = report["content"].replace("local_verified", "unresolved") + "\nmentions local_verified"
        self.assertTrue(all(r["status"] == "pass" for r in self.review(["case", "output"])))
        report["content"] = report["content"].replace("unresolved", "local_verified")
        self.packet["evidence"]["case"]["content"]["fixture"]["parameters"]["target_contract"] = "ambiguous"
        self.assertTrue(all(r["status"] == "pass" for r in self.review(["host"])))
