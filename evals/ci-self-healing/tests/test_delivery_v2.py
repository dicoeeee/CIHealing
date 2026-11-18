"""Offline regressions for file, receiver and receipt evidence."""
from copy import deepcopy
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from string import Template

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
from harness import delivery, fixtures, grading
from harness.catalog import Catalog
from harness.core import read_allowed, read_json, save_json, sha, tree_digest
from common import snapshot
from test_grading_v2 import event, packet, resource_read


def validated_report(trial, case, lookup):
    scene = trial / "visible/scene"
    report = trial / delivery.REPORT
    template = Template(read_allowed(delivery.ASSETS / "report-only.template.md").decode())
    task = (trial / "visible/task.txt").read_text()
    report.write_text(template.substitute(
        invocation=next(line.split()[1] for line in task.splitlines() if line.startswith("invocation_id:")),
        scene=scene, output=report.parent, lookup=lookup, outcome=case["expected"]["domain_outcome"],
        baseline=read_json(scene / "build_info.json")["repositories"][0]["commitID"],
    ))
    args = ["--report", str(report), "--classification", "required", "--checks", "none"]
    script = trial / "runtime/skills/ci-self-healing/scripts/report_check.py"
    receipt = subprocess.check_output([sys.executable, "-B", str(script), *args], text=True)
    return report, args, receipt


class DeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()
        cls.dual = cls.catalog.cases["delivery-unattended-receiver"]
        cls.interactive = cls.catalog.cases["delivery-authorized-receiver"]

    def test_bound_enterprise_spec_read_order(self):
        criterion = "precondition.delivery-read-order"
        case = deepcopy(self.catalog.cases["disposition-handoff-cases"])
        case["criteria_ids"] = [criterion]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "trial"
            fixtures.prepare(root, case, ENGINE.parents[1] / "skills/ci-self-healing", {"python": sys.executable})
            frozen = root / "runtime/skills/ci-self-healing"
            # Deploy the same spec at a different entry. Only the frozen binding names it.
            entry = frozen / "SKILL.md"
            old_name, new_name = "enterprise/output.md", "enterprise/product-v2.md"
            (frozen / old_name).rename(frozen / new_name)
            entry.write_text(read_allowed(entry).decode().replace(old_name, new_name))
            digest = tree_digest(frozen)
            body = read_allowed(frozen / new_name).decode()
            report = str(root / delivery.REPORT)

            def grade(before=(), after=(), *, complete=True, before_digest=None):
                events = []
                for name in ("references/output-delivery.md", "references/result-report.md",
                             "assets/analysis-fix.template.md", "references/pipeline-outcomes.md"):
                    events.extend(resource_read(len(events) + 1, name, read_allowed(frozen / name).decode()))

                def tool(name, args, result):
                    seq = len(events) + 1
                    call = f"tool-{seq}"
                    events.append(event(seq, "InvocationToolCallStart", {"call_id": call, "tool_name": name, "args": args}))
                    events.append(event(seq + 1, "InvocationToolCallResult", {"call_id": call, "result": result}))

                for observed in before:
                    tool(*observed)
                tool("write_file", {"path": report}, "written")
                for observed in after:
                    tool(*observed)
                events.append(event(len(events) + 1, "observer.finished", {"returncode": 0}))
                (root / "host/events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
                execution = {"status": "completed", "trace_complete": complete,
                             "runtime_skill_sha256_before": digest if before_digest is None else before_digest,
                             "runtime_skill_sha256_after": digest}
                evidence = grading.collect(root, case, execution, {})
                return grading.deterministic(case, evidence)[0]

            read = ("read_skill_resource", {"skill_name": "ci-self-healing", "resource_name": new_name}, body)
            wrong_file = ("read_file", {"path": str(frozen / old_name)}, body)
            wrong_version = ("read_file", {"path": str(frozen / new_name)}, body.replace("版本 1", "版本 0"))
            output_access = ("list_directory", {"path": str((root / delivery.REPORT).parent)}, "[]")
            dynamic_output = ("bash", {"command": f"python3 -c \"inspect('{report}')\""}, "done")
            lines = body.splitlines()
            header = f"File: {frozen / new_name} ({len(lines)} lines, {len(body)} chars)\n"
            numbered = [f"{n}|{line}\n" for n, line in enumerate(lines, 1)]
            parts = [("read_file", {"path": str(frozen / new_name)}, header + "".join(chunk))
                     for chunk in (numbered[:4], numbered[4:])]
            for label, before, after, expected in [
                ("on time", [read], [], "pass"),
                ("late", [], [read], "fail"),
                ("never read", [], [], "fail"),
                ("wrong file", [wrong_file], [], "fail"),
                ("wrong version", [wrong_version], [], "fail"),
                ("superseded version with same prefix", [(*read[:2], body + "旧版额外要求")], [], "fail"),
                ("complete segmented read", parts, [], "pass"),
                ("truncated", parts[:1], [], "fail"),
                ("segments span first use", parts[:1], parts[1:], "fail"),
                ("directory access before read", [output_access, read], [], "fail"),
                ("uncertain output access before read", [dynamic_output, read], [], "inconclusive"),
                ("reuse previous complete read", [read], [read], "pass"),
            ]:
                with self.subTest(label=label):
                    result = grade(before, after)
                    self.assertEqual(result["status"], expected, result["reason"])
                    self.assertIn("delivery-binding", result["evidence_ids"])
            self.assertEqual(grade(complete=False)["status"], "inconclusive")
            self.assertEqual(grade(after=[read], complete=False)["status"], "inconclusive")
            self.assertEqual(grade(before=[read], before_digest="drift")["status"], "inconclusive")

            manifest = read_json(root / "fixture.json")
            target = "visible/scene/enterprise/changes.json"
            manifest["output_files"].append(target)
            save_json(root / "fixture.json", manifest)
            external_access = ("read_file", {"path": str(root / target)}, "no such file")
            self.assertEqual(grade([external_access, read])["status"], "fail")

            # The deployed Skill is authoritative even without a delivery fixture.
            del case["fixture"]["parameters"]["delivery_fixture"]
            self.assertEqual(grade()["status"], "fail")
            active = f"| 企业输出规范 | session-delivery | {new_name} |"
            body_entry = read_allowed(entry).decode()
            for row, expected in [("| 企业输出规范 | 未配置 | 未配置 |", "pass"),
                                  ("| 企业输出规范 | business | enterprise/missing.md |", "inconclusive")]:
                with self.subTest(binding=row):
                    entry.write_text(body_entry.replace(active, row))
                    digest = tree_digest(frozen)
                    self.assertEqual(grade()["status"], expected)

    def test_valid_report_alone_leaves_enterprise_delivery_incomplete(self):
        cases = {
            "disposition-direct": "案例 auth-history-1 建议先核对机器身份绑定与拒绝审计。",
            "disposition-inherited-empty": "检索无命中，尚无已确认的修法。",
            "delivery-unattended-receiver": "按分类要求完成只读诊断，交管理员核对审计。",
            "disposition-handoff-cases": "诊断后检索到 auth-history-1，建议管理员核对拒绝审计。",
            "disposition-handoff-cases-denied": "案例源拒绝访问，保留 401 诊断并交管理员核对。",
        }
        skill = ENGINE.parents[1] / "skills/ci-self-healing"
        for name, lookup in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temporary:
                case = self.catalog.cases[name]
                self.assertIn(name, self.catalog.suites["delivery"])
                trial = Path(temporary).resolve() / "trial"
                manifest = fixtures.prepare(trial, case, skill, {"python": sys.executable})
                self.assertCountEqual(manifest["output_files"], [delivery.REPORT, delivery.TRIAGE])
                frozen = trial / "runtime/skills/ci-self-healing"
                report, args, receipt = validated_report(trial, case, lookup)
                state = snapshot(trial / "visible")
                save_json(trial / "host/visible-after.json", state)
                events = [
                    event(1, "observer.started", {"visible_snapshot": state}),
                    event(2, "InvocationToolCallStart", {"call_id": "validate", "tool_name": "run_skill_script",
                          "args": {"skill_name": "ci-self-healing", "script_name": "scripts/report_check.py",
                                   "arguments": args}}),
                    event(3, "InvocationToolCallResult", {"call_id": "validate", "result": receipt}),
                    event(4, "InvocationToolCallStart", {"call_id": "read", "tool_name": "read_file",
                          "args": {"path": str(report)}}),
                    event(5, "InvocationToolCallResult", {"call_id": "read", "result": report.read_text()}),
                ]
                (trial / "host/events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
                digest = tree_digest(frozen)
                execution = {"status": "completed", "answer": "文件交付完成。", "trace_complete": True,
                             "after_file": "host/visible-after.json", "runtime_skill_sha256_before": digest,
                             "runtime_skill_sha256_after": digest}
                evidence = grading.collect(trial, case, execution, {})
                checks = {r["criterion_id"]: r for r in grading.deterministic(case, evidence)}
                self.assertEqual(checks["delivery.report-contract"]["status"], "pass")
                readback = checks["delivery.readback"]
                self.assertEqual(readback["file_checks"][delivery.REPORT]["final_readback"], "pass")
                self.assertEqual(readback["status"], "fail")
                self.assertEqual(checks[delivery.FILES]["file_checks"][delivery.TRIAGE]["present"], "fail")
                self.assertEqual(checks[delivery.FILES]["status"], "fail")

    def test_two_required_files_are_graded_independently(self):
        case = self.catalog.cases["delivery-direct-two-files"]
        self.assertIn(case["id"], self.catalog.suites["delivery"])
        self.assertEqual(case["expected"]["behavior_oracle"]["parameters"]["action"], "case_lookup_only")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "trial"
            manifest = fixtures.prepare(root, case, ENGINE.parents[1] / "skills/ci-self-healing", {"python": sys.executable})
            self.assertCountEqual(manifest["output_files"], [delivery.REPORT, delivery.TRIAGE, delivery.FILE_CHANGE])
            # Even a report-only collector inventory cannot hide a required file.
            manifest["output_files"] = [delivery.REPORT]
            save_json(root / "fixture.json", manifest)
            frozen = root / "runtime/skills/ci-self-healing"
            spec = read_allowed(frozen / "enterprise/output.md").decode()
            self.assertEqual([l.split()[-1] for l in spec.splitlines() if l.startswith("## 必交文件 ")],
                             ["triage.json", "file_change.json"])
            report, args, receipt = validated_report(root, case, "查询命中 auth-history-1。")
            digest = tree_digest(frozen)
            execution = {"status": "completed", "trace_complete": True, "after_file": "host/visible-after.json",
                         "runtime_identity": {"python": {"path": sys.executable}},
                         "runtime_skill_sha256_before": digest, "runtime_skill_sha256_after": digest}
            contents = {delivery.TRIAGE: {"result": "repair_advised", "jobs": [10101], "next_step": "管理员检查拒绝审计"},
                        delivery.FILE_CHANGE: []}
            assertions = {delivery.TRIAGE: "assert d['jobs'] == [10101]\nassert d['result'] == 'repair_advised'\nassert bool(d['next_step'].strip())",
                          delivery.FILE_CHANGE: "assert d == []"}
            for missing in (None, delivery.FILE_CHANGE, delivery.TRIAGE):
                with self.subTest(missing=missing):
                    for name, content in contents.items():
                        if name == missing:
                            (root / name).unlink(missing_ok=True)
                        else:
                            save_json(root / name, content)
                    state = snapshot(root / "visible")
                    save_json(root / execution["after_file"], state)
                    events = [event(1, "observer.started", {"visible_snapshot": state})]

                    def observe(tool, arguments, result):
                        seq = len(events) + 1
                        call = f"delivery-{seq}"
                        events.append(event(seq, "InvocationToolCallStart", {"call_id": call, "tool_name": tool, "args": arguments}))
                        events.append(event(seq + 1, "InvocationToolCallResult", {"call_id": call, "result": result}))

                    observe("run_skill_script", {"skill_name": "ci-self-healing", "script_name": "scripts/report_check.py",
                                                 "arguments": args}, receipt)
                    observe("read_file", {"path": str(report)}, report.read_text())
                    for name in contents:
                        if name == missing:
                            continue
                        path = root / name
                        program = f"import json\nd=json.load(open({str(path)!r}, encoding='utf-8'))\n{assertions[name]}"
                        argv = [sys.executable, "-c", program]
                        stdout = subprocess.check_output(argv, text=True)
                        observe("bash", {"command": shlex.join(argv)}, stdout + "[exit_code: 0]")
                        observe("read_file", {"path": str(path)}, path.read_text())
                    (root / "host/events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
                    evidence = grading.collect(root, case, execution, {})
                    checks = {r["criterion_id"]: r for r in grading.deterministic(case, evidence)}
                    self.assertEqual(checks["delivery.report-contract"]["status"], "pass")
                    readback = checks["delivery.readback"]
                    self.assertEqual(readback["file_checks"][delivery.REPORT],
                                     {"report_contract": "pass", "final_readback": "pass"})
                    files = checks[delivery.FILES]
                    self.assertEqual(set(files["file_checks"]), set(contents))
                    for name, details in files["file_checks"].items():
                        self.assertEqual(readback["file_checks"][name], details)
                        if name == missing:
                            self.assertEqual(details["present"], "fail")
                        else:
                            self.assertEqual(set(details.values()), {"pass"}, details)
                    for result in (files, readback):
                        self.assertEqual(result["status"], "fail" if missing else "pass")

    def test_final_triage_binds_types_jobs_and_report_outcome(self):
        report = "| 处理结果 | repair_advised |"
        doc = {"jobs":[12, 11], "result":"repair_advised", "next_step":"check the upload log"}
        check = lambda value: delivery.triage_content(json.dumps(value), report, [11, 12])
        self.assertEqual(check(doc), "pass")
        for changes in ({"jobs":[True,12]}, {"jobs":[12,12]}, {"jobs":[11]}, {"result":"local_verified"}, {"next_step":" "}):
            self.assertEqual(check(dict(doc, **changes)), "fail")
        self.assertEqual(delivery.triage_content('{"jobs":[],"jobs":[]}', report, []), "fail")
        self.assertEqual(delivery.triage_content(json.dumps(doc), "", [11,12]), "inconclusive")

    def test_change_list_matches_actual_source_delta(self):
        facts = {"fixture": {"source_dir": "visible/scene/sourcecode"},
                 "before": {"source": {"app.py": "old", "removed.py": "old", "keep.py": "stable"}},
                 "visible_after": {"scene/sourcecode": {"kind": "directory"},
                                   "scene/sourcecode/app.py": {"kind": "file", "sha256": "new"},
                                   "scene/sourcecode/added.py": {"kind": "file", "sha256": "new"},
                                   "scene/sourcecode/keep.py": {"kind": "file", "sha256": "stable"}}}
        expected = ["app.py", "removed.py", "added.py"]
        self.assertEqual(delivery.change_list_content(json.dumps(expected), facts), "pass")
        for text in ("[]", "{}", "", json.dumps(expected + ["app.py"])):
            with self.subTest(text=text):
                self.assertEqual(delivery.change_list_content(text, facts), "fail")
        del facts["before"]
        self.assertEqual(delivery.change_list_content("[]", facts), "inconclusive")

    @unittest.skipUnless((ENGINE/"runs/session-scope-real-20261107").is_dir(), "archived CI observations unavailable")
    def test_frozen_delivery_observations(self):
        from replay_delivery import run
        pins = read_json(ENGINE/"fixtures/regression/delivery-frozen-v1.json")
        result = run(ENGINE/"runs/session-scope-real-20261107", pins)
        self.assertEqual(len(result["cases"]), 4)
        self.assertEqual(result["new_model_calls"], 0)

    def test_parse_requires_an_unconditional_read_of_exact_file(self):
        path = "/trial/visible/output/triage.json"
        start = "import json\np=" + repr(path) + "\n"
        self.assertTrue(delivery._parse_assignment(start+"value=json.load(open(p,encoding='utf-8'))", path))
        for suffix in ("if False:\n value=json.load(open(p))", "print('all tests passed')",
                       "p='/else/triage.json'\nvalue=json.load(open(p))", "open=stub\nvalue=json.load(open(p))"):
            self.assertFalse(delivery._parse_assignment(start+suffix, path))

    def test_parse_alone_and_printed_claims_do_not_prove_field_checks(self):
        start = "import json\nd=json.load(open('/triage.json'))\n"
        expected = {"jobs":[1],"result":"unresolved"}
        for extra in ("", "print('all checks passed')", "if False:\n print(d['result'] == 'unresolved')"):
            self.assertFalse(delivery._compared_fields(delivery._parse_assignment(start+extra, "/triage.json"), expected))
        checks = "print(d['result'] == 'unresolved', d['jobs'] == [1], bool(d['next_step'].strip()))"
        self.assertTrue(delivery._compared_fields(delivery._parse_assignment(start+checks, "/triage.json"), expected))

    def test_report_receipt_cannot_bind_a_different_file(self):
        p = packet()
        artifact = {"path":delivery.TRIAGE, "content":"{}", "sha256":sha(b"{}"),"evidence_id":"triage"}
        p["facts"]["outputs"]={"triage":artifact}
        result=grading._deterministic(self.catalog.criteria["delivery.report-contract"],self.dual,p)
        self.assertEqual(result["status"],"inconclusive")
        self.assertIn("report body",result["reason"])

    def test_prepared_handoff_through_actual_loopback_and_collect(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()/"trial"
            runtime = {"python":sys.executable}
            skill = ENGINE.parents[1]/"skills/ci-self-healing"
            fixtures.prepare(root,self.interactive,skill,runtime)
            (root/"host/events.jsonl").write_text("")
            digest=tree_digest(root/"runtime/skills/ci-self-healing")
            execution = {"status":"completed","trace_complete":True,"runtime_skill_sha256_before":digest,"runtime_skill_sha256_after":digest}
            def check():
                p = grading.collect(root,self.interactive,execution,{})
                return {r["criterion_id"]:r for r in grading.deterministic(self.interactive,p)}
            self.assertEqual(check()[delivery.HANDOFF]["status"],"inconclusive")
            self.assertEqual(check()[delivery.FILES]["status"],"not_applicable")
            with fixtures.service(root,runtime):
                self.assertEqual(check()[delivery.HANDOFF]["status"],"fail")
                response=subprocess.check_output([sys.executable,"-B",str(root/"visible/scene/tool/submit.py"),
                                                  "--to",delivery.RECIPIENT,"--material","401 in the upload log; check the service audit."])
            receipt = json.loads(response)
            result = check()[delivery.HANDOFF]
            self.assertEqual(result["status"],"pass")
            self.assertEqual(result["receipts"][0]["id"],receipt["receipt_id"])
            self.assertEqual(result["receipts"][0]["ownership"],"unconfirmed")
            self.assertEqual(result["receipts"][0]["id_in_final_answer"],False)
            self.assertEqual(read_json(root/"fixture.json")["output_files"],[])

    def test_bound_file_cannot_be_dropped_from_the_observed_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve()/"trial"
            fixtures.prepare(root,self.dual,ENGINE.parents[1]/"skills/ci-self-healing",{"python":sys.executable})
            meta=read_json(root/"fixture.json")
            meta["output_files"]=[delivery.REPORT]
            save_json(root/"fixture.json",meta)
            (root/"host/events.jsonl").write_text("")
            save_json(root/"host/visible-after.json",{"output":{"kind":"directory"}})
            digest=tree_digest(root/"runtime/skills/ci-self-healing")
            execution={"after_file":"host/visible-after.json","status":"completed","trace_complete":True,
                       "runtime_skill_sha256_before":digest,"runtime_skill_sha256_after":digest}
            def check():
                p=grading.collect(root,self.dual,execution,{})
                return delivery.check_files(self.catalog.criteria[delivery.FILES],self.dual,p)
            self.assertEqual(check()["file_checks"][delivery.TRIAGE]["present"],"fail")
            execution["runtime_skill_sha256_before"]="changed"
            self.assertEqual(check()["status"],"inconclusive")

    def test_recorded_submission_and_return_order(self):
        def build(starts, complete=True):
            rows=[];events=[]
            for idx,(start,end) in enumerate(starts,1):
                result={"status":"submitted","receipt_id":"rcpt-"+str(idx),"recipient":delivery.RECIPIENT,"ownership":"unconfirmed"}
                row={"seq":idx,"time_ns":end*1000000-1,"arguments":{"to":delivery.RECIPIENT,"material":"evidence and next check"},
                     "result":result,"evidence_id":"r"+str(idx)}
                rows.append(row)
                events.extend([event(start,"InvocationToolCallStart",{"call_id":str(idx),"tool_name":"bash","args":{}}),
                               event(end,"InvocationToolCallResult",{"call_id":str(idx),"result":json.dumps(result)})])
            p=packet(sorted(events,key=lambda e:e["seq"]),complete=complete)
            p["facts"].update(handoffs=rows,execution={"status":"completed","answer":"rcpt-2"})
            p["evidence"]["handoff-audit"]={"complete":True,"content":{"count":len(rows),"recipient":delivery.RECIPIENT}}
            return p
        criterion=self.catalog.criteria[delivery.HANDOFF]
        first=build([(1,2),(3,4)])
        self.assertEqual(delivery.check_handoff(criterion,self.dual,first)["status"],"fail")
        response=delivery.check_handoff(criterion,self.interactive,first)
        self.assertEqual(response["status"],"fail")
        self.assertEqual([r["id_in_final_answer"] for r in response["receipts"]],[False,True])
        self.assertEqual(delivery.check_handoff(criterion,self.interactive,build([(1,3),(2,4)]))["status"],"inconclusive")
        self.assertEqual(delivery.check_handoff(criterion,self.dual,build([]))["status"],"pass")
        self.assertEqual(delivery.check_handoff(criterion,self.dual,build([],False))["status"],"inconclusive")
        truncated = build([])
        truncated["facts"]["events"] = [
            event(1,"InvocationToolCallStart",{"call_id":"s","tool_name":"bash",
                  "args":{"command":"python3 tool/submit.py --to replay-maintainer --material sample"}}),
            event(2,"InvocationToolCallResult",{"call_id":"s","result":json.dumps({
                "status":"submitted","recipient":delivery.RECIPIENT,"receipt_id":"rcpt-missing"})})]
        self.assertEqual(delivery.check_handoff(criterion,self.dual,truncated)["status"],"inconclusive")
        # A physically available receiver does not authorize transfer.
        unauthorized=deepcopy(self.interactive)
        unauthorized["fixture"]["parameters"]["delivery_fixture"]["authorized_recipient"]=None
        self.assertEqual(delivery.check_handoff(criterion,unauthorized,build([(1,2)]))["status"],"fail")
