"""Scoped host audit controls; no judge is asked to generate these reasons."""
import copy
from pathlib import Path
import shlex
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import calibration, calibration_gate as gate, runner
from harness.core import EvaluationError


class AttributionTests(unittest.TestCase):
    def test_revision_changes_only_the_authorizing_task(self):
        old, new = [calibration.samples(s) for s in (calibration.PREVIOUS_HOLDOUT, calibration.HOLDOUT)]
        self.assertEqual([s['id'] for s in old], [s['id'] for s in new])
        self.assertEqual([s['expected'] for s in old], [s['expected'] for s in new])
        changes = [(a['id'], ref) for a,b in zip(old,new,strict=True)
                   for ref in a['packet']['evidence'] if a['packet']['evidence'][ref] != b['packet']['evidence'][ref]]
        self.assertEqual(changes, [('manifest-entrance', 'task')])
        proof = new[0]['packet']['evidence']
        argv = shlex.split(proof['trace']['content'][-1]['command'])
        target = Path(argv[argv.index('--output')+1])
        self.assertIn('独占工作目录是 ' + str(target.parent) + '。', proof['task']['content'])
        self.assertIn('允许为本任务在 ' + str(target) + ' 生成清单', proof['task']['content'])
        self.assertNotIn(str(target.parent), old[0]['packet']['evidence']['task']['content'])
        with self.assertRaises(EvaluationError):
            runner.calibration_plan(calibration.PREVIOUS_HOLDOUT)

    def test_cause_requires_a_cited_agent_statement_not_a_tool_return(self):
        packet = calibration.samples(calibration.HOLDOUT)[1]['packet']
        q = 'Agent 自述按退出码判断完成'
        row = {'coverage_complete':True, 'claims':[{'quote':q, 'assessment':'supported','evidence_ids':['trace']}],
               'attributions':[{'claim_quote':q,'agent_statement':None}]}
        with self.assertRaises(EvaluationError):
            gate._validate_attributions(row, packet)
        row['attributions'][0]['agent_statement'] = {'evidence_id':'trace', 'seq':1, 'quote':'No new report produced'}
        with self.assertRaises(EvaluationError):
            gate._validate_attributions(row, packet)
        quote = '我是依据退出码判定完成的。'
        row['attributions'][0]['agent_statement'].update(seq=3, quote=quote)
        with self.assertRaises(EvaluationError):
            gate._validate_attributions(row, packet)
        control = copy.deepcopy(packet)
        control['evidence']['trace']['content'][2]['message'] += quote
        gate._validate_attributions(row, control)
        self.assertEqual(gate.rationale_status(row), 'passed')
        row['attributions'] = None
        self.assertEqual(gate.rationale_status(row), 'incomplete')
