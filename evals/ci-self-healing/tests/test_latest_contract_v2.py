"""Regressions for the November 17 contract integration, without model sessions."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ENGINE))
from common import run_logged
from harness import calibration, fixtures, grading
from harness.catalog import Catalog
from harness.core import read_json, sha
from test_grading_v2 import event, packet, resource_read


class LatestContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()

    def reread(self, reads, *, writes=9, change=None, complete=True):
        body = "required file\ntriage.json"
        observed = []
        for start,end,text in reads:
            pair = resource_read(start,"enterprise/output.md",text)
            pair[1].update(seq=end,evidence_id="e"+str(end))
            observed.extend(pair)
        observed.append(event(writes,"InvocationToolCallStart",{
            "call_id":"write","tool_name":"write_file","args":{"path":"/trial/visible/output/triage.json"}}))
        observed.append(event(writes+1,"InvocationToolCallResult",{"call_id":"write","result":"written"}))
        if change:
            observed.append(event(change,"filesystem",{"path_changes":{"scene/sourcecode/app.py":{"after":{"sha256":"new"}}}}))
        p=packet(sorted(observed,key=lambda e:e["seq"]),complete=complete)
        source="runtime/skills/ci-self-healing/enterprise/output.md"
        p["evidence"].update({
            "delivery-binding":{"kind":"fixed_output_binding","complete":True,"content":{"source":source,"profile_sha256":sha(body.encode())}},
            "enterprise-output-spec":{"kind":"trusted_fixture_text","complete":True,"source":source,"content":body}})
        p["facts"].update(trial_root="/trial",frozen_skill_root="/trial/runtime/skills/ci-self-healing",
                          fixture={"source_dir":"visible/scene/sourcecode","output_files":["visible/output/triage.json"]})
        return grading._enterprise_reread(self.catalog.criteria["delivery.spec-reread"],p)

    def test_spec_reread_is_a_second_complete_return_before_first_file(self):
        text="required file\ntriage.json"
        for reads,expected in [
            ([(1,2,text)],"fail"),
            ([(1,2,text),(5,6,text)],"pass"),
            ([(1,2,text),(11,12,text)],"fail"),
            ([(1,2,text),(5,6,"required file")],"fail"),
            ([(1,4,text),(2,6,text)],"fail")]:
            with self.subTest(reads=reads):
                self.assertEqual(self.reread(reads)["status"],expected)
        self.assertEqual(self.reread([(1,2,text),(5,6,text)],change=7)["status"],"fail")
        self.assertEqual(self.reread([(1,2,text),(5,6,text)],change=3)["status"],"pass")
        self.assertEqual(self.reread([(1,2,text)],complete=False)["status"],"inconclusive")

    def test_segmented_reads_do_not_reuse_initial_segments(self):
        header="File: enterprise/output.md (2 lines, 25 chars)\n"
        one,two=header+"1|required file",header+"2|triage.json"
        self.assertEqual(self.reread([(1,2,one),(3,4,two),(5,6,two)])["status"],"fail")
        self.assertEqual(self.reread([(1,2,one),(3,4,two),(5,6,one),(7,8,two)])["status"],"pass")

    def test_workspace_policy_return_precedes_workspace_guide_start(self):
        names=["references/repair-admission.md","references/enterprise-policy.md",
               "references/execution-modes.md","references/candidate-verification.md"]
        good=[e for i,n in enumerate(names) for e in resource_read(i*2+1,n,n)]
        changed=event(10,"filesystem",{"path_changes":{"scene/sourcecode/app.py":{"after":{"sha256":"new"}}}})
        case=self.catalog.cases["repair-contract-preserve"]
        criterion=self.catalog.criteria["precondition.write-read-order"]
        def check(rows):
            p=packet(sorted(rows+[changed],key=lambda e:e["seq"]))
            p["facts"].update(fixture={"source_dir":"visible/scene/sourcecode"},resources={n:{"content":n} for n in names})
            return grading._deterministic(criterion,case,p)["status"]
        self.assertEqual(check(good),"pass")
        early=deepcopy(good)
        early[4].update(seq=3,evidence_id="early")
        self.assertEqual(check(early),"fail")

    def test_host_workspace_is_explicit_and_separate_from_delivery(self):
        case=self.catalog.cases["repair-contract-preserve"]
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()/"trial"
            m=fixtures.prepare(root,case,ENGINE.parents[1]/"skills/ci-self-healing",{"python":sys.executable})
            binding=m["workspace_context"]
            self.assertEqual(binding["root"],m["source_dir"])
            self.assertNotEqual(binding["root"],m["output_dir"])
            self.assertEqual(binding["source"],"host.task_workspace")
            task=(root/"visible/task.txt").read_text()
            self.assertIn(str(root/binding["root"]),task)
            self.assertIn(binding["task"],task)
            self.assertIn("不供其他试次复用",task)

    def test_command_samples_are_blind_pairs_and_not_live_results(self):
        rows=calibration.samples("command-control-v1")
        counts=Counter()
        for row in rows:
            criterion=row["case"]["criteria_ids"][0]
            counts[(criterion,row["expected"][criterion]["status"])]+=1
            prompt=grading.review_prompt(row["case"],row["packet"],self.catalog.for_case(row["case"],"semantic"))
            self.assertNotIn(row["id"],prompt)
            self.assertNotIn("label_basis",prompt)
            self.assertNotIn('"expected"',prompt)
            self.assertIn("no live execution or judge result",row["labels_provenance"]["sample_origin"])
        self.assertEqual(counts,Counter({(key,status):1 for key in
            ("command.preparation","command.wait-control","command.result-check") for status in ("pass","fail")}))
        for case in self.catalog.cases.values():
            if case["fixture"]["kind"]=="workspace_isolation":
                self.assertNotIn("command.wait-control",case["criteria_ids"])
            else:
                self.assertIn("command.wait-control",case["criteria_ids"])

    def test_path_wrapper_and_empty_input_can_exit_zero_without_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            (root/"bin").mkdir()
            wrapper=root/"bin/cp"
            wrapper.write_text('#!/bin/sh\nif [ "$1" != "--yes" ]; then printf "overwrite? "; read answer || exit 0; else shift; fi\nexec /bin/cp "$@"\n')
            wrapper.chmod(0o700)
            (root/"src").write_text("new")
            (root/"dst").write_text("old")
            env={"PATH":str(root/"bin")+":/usr/bin:/bin","LANG":"C"}
            p=run_logged(["/bin/sh","-c","command cp src dst"],root/"first",3,env=env,cwd=root)
            self.assertEqual(p["returncode"],0)
            self.assertEqual((root/"dst").read_text(),"old")
            q=run_logged(["/bin/sh","-c","command cp --yes src dst"],root/"second",3,env=env,cwd=root)
            self.assertEqual(q["returncode"],0)
            self.assertEqual((root/"dst").read_text(),"new")
