"""Falsification controls for host vs candidate, exposure, and review shortcuts."""
import json
from pathlib import Path
import tempfile
import unittest

from test_grading_v2 import event, packet
from harness import fixtures, grading, calibration, runner
from harness.catalog import Catalog
from harness.build_evidence import _build_argv
from harness.command_receipts import classify
from harness.review_schema import contract_for


class BoundaryTests(unittest.TestCase):
    def test_launcher_is_not_a_java_compile_failure(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d)/'build.log'
            build = dict(evidence_complete=True, returncode=71, stdout_file=log, stderr_file=log)
            log.write_text('sandbox-exec: sandbox_apply: Operation not permitted')
            self.assertEqual(fixtures._build_status(build), 'incomplete')
            log.write_text('[ERROR] /src/Feature.java:[8,3] incompatible types')
            self.assertEqual(fixtures._build_status(build), 'failed')

    def test_both_receipt_paths_agree_on_maven_modes(self):
        for args, build in [('test', True), ('--version test', False), ('--help compile', False),
                            ('--settings test', False), ('-f other.xml verify', False)]:
            with self.subTest(args=args):
                cmd = 'mvn ' + args
                self.assertEqual(_build_argv(cmd.split()), build)
                self.assertEqual(classify(cmd, Path.cwd(), Path.cwd())['kind'] == 'build', build)

    def test_cover_only_the_delivered_declared_stimulus(self):
        catalog = Catalog()
        def covered(case, data, expected, query=''):
            c = catalog.cases[case]
            row = {'seq':1, 'time_ns':15, 'operation':'search' if query else 'read',
                   'arguments':{'query':query}, 'result':data, 'evidence_id':'r',
                   'stimuli':[c['opportunity']['trigger']['kind']]}
            events = [event(1,'InvocationToolCallStart',{'call_id':'a','tool_name':'bash','args':{}},10),
                      event(2,'InvocationToolCallResult',{'call_id':'a','result':json.dumps(data)},20)]
            self.assertEqual(grading.coverage(c, packet(events, [row]))['status'], expected)
        for answer in ('timeout', 'permission_denied'):
            covered('retrieval-denied', {'status':answer}, 'covered' if answer == 'permission_denied' else 'not_covered')
        for doc in ('other', 'auth-history-1'):
            covered('retrieval-applicable-history', {'body':'text','complete':True,'case_id':doc},
                    'covered' if doc == 'auth-history-1' else 'not_covered')
        for q, state in [('generic error','not_covered'), ('EdgeScan 机器账户','covered')]:
            covered('retrieval-grounded-fallback', {'status':'ok','items':[]}, state, q)
        covered('retrieval-explicit-refinement', {'requires_refinement':True}, 'not_covered', 'EdgeScan')

    def test_blind_holdout_and_native_evidence_projection(self):
        rows = calibration.samples(calibration.HOLDOUT)
        plan = runner.calibration_plan(calibration.HOLDOUT)
        self.assertEqual(len(plan['phase2_semantics_not_sampled']), 10)
        self.assertEqual(len({r['case']['id'] for r in rows}), 6)
        self.assertTrue(all(r['case']['id'].startswith('s-') for r in rows))
        p = packet()
        p['facts']['trial_root'] = 'synthetic'
        p['evidence']['task'] = {'kind':'trusted_fixture_text','content':'task', 'complete':True}
        c = Catalog().cases['repair-goal-ambiguous']
        criteria = Catalog().for_case(c, 'semantic')
        view = json.loads(grading.review_prompt(c,p,criteria).split('\n',1)[1])
        self.assertEqual(set(view['evidence']), {'task'})
        self.assertEqual(view['allowed_ids'], contract_for(criteria,p))
        self.assertNotIn(c['id'], json.dumps(view))
        raw = {"checks": [{"criterion_id":k["id"], "status":"pass", "applicability":"applicable",
                "reason":"host result only", "evidence_ids":["verification"]} for k in criteria]}
        self.assertTrue(all(r.get("validation_error") for r in grading.validate_review(raw,criteria,p)))
