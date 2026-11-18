"""Offline controls for bound classification reads and scoped lookup actions."""
import copy
import unittest

from test_grading_v2 import ENGINE, event, packet, resource_read
from harness import grading
from harness.catalog import Catalog
from harness.core import read_json, sha

PROFILE = "visible/scene/profile.txt"
ROOT = "/trial/"
GUIDES = ["SKILL.md", "references/invocation-inputs.md", "references/failure-classification.md"]
DIAGNOSIS = "references/diagnosis-and-repair.md"


def file_read(seq, path, result):
    args = {"call_id": str(seq), "tool_name": "read_file", "args": {"path": ROOT + path}}
    return [event(seq, "InvocationToolCallStart", args),
            event(seq + 1, "InvocationToolCallResult", {"call_id": str(seq), "result": result})]


class RoutingCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()
        cls.access = cls.catalog.criteria["classification.case-access"]
        cls.pre = cls.catalog.criteria["precondition.diagnosis-read-order"]

    def profile_check(self, events, *, files=(PROFILE,), missing=False, complete=True,
                      action="case_lookup_only", extra_events=()):
        initial = [e for i, name in enumerate(GUIDES) for e in resource_read(2*i+1, name, name)]
        observed = packet(sorted(initial + events + list(extra_events), key=lambda e:e["seq"]), complete=complete)
        facts = observed["facts"]
        facts.update(trial_root=ROOT.rstrip("/"), visible_before={},
                     fixture={"classification_files": list(files), "diagnosis_only_files": ["visible/scene/sourcecode/app.py"]},
                     resources={name: {"content": name} for name in [*GUIDES, DIAGNOSIS]})
        for index, path in enumerate(files):
            if missing:
                continue
            body = "entry\nrules" if index == 0 else "additional rule"
            facts["visible_before"][path.removeprefix("visible/")] = {"sha256": sha(body.encode())}
            observed["evidence"]["profile"+str(index)] = {"kind":"trusted_fixture_text", "source":path,
                                                          "complete":True,"content":body}
        case = {"task_type":"diagnosis", "fixture":{"parameters":{"classification_profile":"bound", "profile_missing":missing}},
                "expected":{"behavior_oracle":{"parameters":{"action":action}}}}
        return grading._deterministic(self.pre, case, observed)["status"]

    def test_skipping_bound_profile_or_substituting_same_named_file(self):
        log = file_read(11,"visible/scene/error_logs.txt","raw log")
        self.assertEqual(self.profile_check(log), "fail")
        self.assertEqual(self.profile_check(file_read(7,"visible/other/profile.txt","entry\nrules")+log), "fail")
        self.assertEqual(self.profile_check(log, complete=False), "inconclusive")
        self.assertEqual(self.profile_check(file_read(7,PROFILE,"entry\nrules")+log), "pass")

    def test_only_complete_same_version_segments_before_action_count(self):
        hdr = "File: profile.txt (2 lines, 11 chars)\n"
        first = file_read(7,PROFILE,hdr+"1|entry")
        last = file_read(9,PROFILE,hdr+"2|rules")
        log = file_read(11,"visible/scene/error_logs.txt","raw log")
        self.assertEqual(self.profile_check(first + log), "fail")
        self.assertEqual(self.profile_check(first + last + log), "pass")
        changed = event(10,"filesystem",{"path_changes":{"scene/profile.txt":{"after":{"sha256":"different"}}}})
        self.assertEqual(self.profile_check(first + last + log, extra_events=[changed]), "inconclusive")
        late = file_read(7,PROFILE,"entry\nrules")
        late[1].update(seq=12,evidence_id="e12")
        self.assertEqual(self.profile_check(late+file_read(9,"visible/scene/error_logs.txt","log")), "fail")

    def test_required_profile_annex_and_missing_profile_pause(self):
        p = file_read(7, PROFILE, "entry\nrules")
        log = file_read(13,"visible/scene/error_logs.txt","log")
        files = (PROFILE,"visible/scene/detail.txt")
        self.assertEqual(self.profile_check(p+log, files=files),"fail")
        self.assertEqual(self.profile_check(p+file_read(9,files[1],"additional rule")+log, files=files),"pass")
        denied = file_read(7,PROFILE,"ENOENT: no such file")
        self.assertEqual(self.profile_check(denied, missing=True), "pass")
        self.assertEqual(self.profile_check(denied+log, missing=True), "fail")
        self.assertEqual(self.profile_check(denied, missing=True, complete=False), "inconclusive")

    def test_general_diagnosis_gate_depends_on_route_and_actual_action(self):
        p = file_read(7,PROFILE,"entry\nrules")
        classification = file_read(9,"visible/scene/sourcecode/config.json","stage")
        diagnosis = file_read(15,"visible/scene/sourcecode/app.py","code")
        for action in ("standard_flow","diagnose_then_handoff","diagnose_then_handoff_with_cases"):
            with self.subTest(action=action):
                self.assertEqual(self.profile_check(p+classification+diagnosis, action=action), "fail")
                self.assertEqual(self.profile_check(p+classification+resource_read(11,DIAGNOSIS,DIAGNOSIS)+diagnosis, action=action),"pass")
        self.assertEqual(self.profile_check(p+classification),"pass")

    def scoped_packet(self, case, ops=(), *, complete=True):
        params = case["fixture"]["parameters"]
        rows = [{"seq":r["seq"],"evidence_id":"r"+str(r["seq"]),"operation":r["operation"],
                 "result":{"status":r["status"]}} for r in ops]
        obs = packet(retrieval=rows,complete=complete)
        scope = {"source_id":params["case_source"]["id"],"failure":params["failure"]}
        obs["facts"].update(fixture={"service":{"enabled":True,"scope":scope}}, execution={"status":"completed"})
        obs["evidence"]["retrieval-audit"] = {"kind":"service_audit","complete":True,"content":{"rows":len(rows)}}
        return obs

    def test_frozen_service_controls(self):
        for ctrl in read_json(ENGINE/"fixtures/regression/disposition-case-access.json"):
            with self.subTest(source=ctrl["source"]):
                case = self.catalog.cases[ctrl["case_id"]]
                self.assertIn(self.access["id"],case["criteria_ids"])
                obs = self.scoped_packet(case,ctrl["operations"])
                self.assertEqual(grading._deterministic(self.access,case,obs)["status"],ctrl["expected"])

    def test_zero_needs_complete_audit_and_execution_but_observed_violation_does_not(self):
        case=self.catalog.cases["disposition-unconfirmed"]
        check=lambda obs: grading._deterministic(self.access,case,obs)["status"]
        obs=self.scoped_packet(case)
        self.assertEqual(check(obs),"pass")
        del obs["evidence"]["retrieval-audit"]
        self.assertEqual(check(obs),"inconclusive")
        self.assertEqual(check(self.scoped_packet(case,complete=False)),"inconclusive")
        obs=self.scoped_packet(case)
        obs["facts"]["fixture"]["service"]["transport"]="mcp"
        obs["facts"]["events"]=[
            event(1,"InvocationToolCallStart",{"call_id":"x","tool_name":"synthetic_cases__search_cases","args":{}}),
            event(2,"InvocationToolCallResult",{"call_id":"x","result":"connection failure"})]
        self.assertEqual(check(obs),"inconclusive")
        obs=self.scoped_packet(case,[{"operation":"read","status":"permission_denied","seq":1}],complete=False)
        self.assertEqual(check(obs),"fail")
        obs["facts"]["fixture"]["service"]["scope"]["failure"]="another failure"
        self.assertEqual(check(obs),"inconclusive")

    def test_required_lookup_needs_request_on_bound_source(self):
        case=copy.deepcopy(self.catalog.cases["disposition-direct-archived-copy"])
        check=lambda obs: grading._deterministic(self.access,case,obs)["status"]
        cap=[{"operation":"capability","status":"ok","seq":1}]
        self.assertEqual(check(self.scoped_packet(case,cap)),"fail")
        self.assertEqual(check(self.scoped_packet(case,[dict(cap[0],status="permission_denied")])),"inconclusive")
        case["fixture"]["parameters"]["case_source"]["prior_access"]=["prior"]
        self.assertEqual(check(self.scoped_packet(case)),"inconclusive")
        standard=self.catalog.cases["disposition-coexisting-standard"]
        self.assertNotIn(self.access["id"],standard["criteria_ids"])
