"""Reassessment provenance/budgets using sealed synthetic runs and a fake judge."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from harness import grading, reassessment, runner
from harness.catalog import Catalog
from harness.core import EvaluationError, freeze_tree, read_json, save_json, sha, tree_digest


class FakeJudge:
    def __init__(self,criteria,*,fail=False):
        self.criteria,self.fail=criteria,fail
        self.preflights=self.judges=self.executes=0
        self.runtime=None
    def preflight(self,runtime):
        self.preflights+=1
        return {'status':'passed','execution_model':{},'review_model':{'id':'fake'},'runtime_identity':{'test':'synthetic'}}
    def judge(self,directory,runtime,prompt,seconds, *, review_contract=None):
        self.judges+=1;self.runtime=runtime
        self.last_seconds=seconds
        if self.fail:raise RuntimeError('synthetic failure')
        checks=[{'criterion_id':c['id'],'applicability':'applicable','status':'pass','evidence_ids':['case'],'reason':'Synthetic protocol test, not a semantic accuracy claim.'} for c in self.criteria]
        Path(directory).mkdir(parents=True)
        save_json(Path(directory)/'synthetic-receipt.json',{'fake_backend':True})
        return {'status':'completed','trace_complete':True,'answer':json.dumps({'checks':checks})}
    def execute(self,*args,**kwargs):
        self.executes+=1
        raise AssertionError('repair must never be replayed')


class ReassessmentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ci-reassessment-v2-')
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve()
        self.run=self.base/'source-run';self.run.mkdir()
        freeze_tree(ROOT/'spec',self.run/'spec')
        self.catalog=Catalog(self.run/'spec',verify_sources=False)
        self.case=self.catalog.cases['advice-prohibited']
        self.row={'id':'t0001','case_id':self.case['id'],'version':'candidate','repeat':1}
        self.trial=self.run/'trials/t0001';(self.trial/'host').mkdir(parents=True)
        freeze_tree(ROOT.parents[1]/'skills/ci-self-healing',self.run/'skills/candidate')
        freeze_tree(self.run/'skills/candidate',self.trial/'runtime/skills/ci-self-healing')
        skill_digest=tree_digest(self.trial/'runtime/skills/ci-self-healing')
        self.execution={'status':'completed','trace_complete':True,'events_file':'host/events.jsonl',
                        'runtime_skill_sha256_before':skill_digest,'runtime_skill_sha256_after':skill_digest}
        self.verification={'status':'passed','source':{'before':{},'after':{},'changed_paths':[]}}
        save_json(self.trial/'host/execution.json',self.execution)
        save_json(self.trial/'host/verification.json',self.verification)
        (self.trial/'host/events.jsonl').write_text('')
        content={key:self.case[key] for key in ('id','title','task_type','fixture','opportunity')}
        self.packet={'schema_version':'synthetic-test-packet','case_id':self.case['id'],'trace_complete':True,
                     'criteria_snapshot':self.catalog.for_case(self.case),'criteria_sha256':sha(self.catalog.documents['criteria']),
                     'evidence':{'case':{'kind':'trusted_case','content':content,'sha256':sha(content),'complete':True}},
                     'facts':{'execution':self.execution,'verification':self.verification,'events':[],'retrieval':[],'resources':{}}}
        self.original={**self.row,'status':'passed','coverage':{'status':'covered'},'checks':self.checks()}
        save_json(self.trial/'host/case.json',self.case)
        save_json(self.trial/'host/packet.json',self.packet)
        save_json(self.trial/'result.json',self.original)
        self.plan={'catalog_sha256':self.catalog.digest,'versions':{'candidate':'synthetic'},'cases':[self.case['id']],'repeats':1}
        for name,value in [('plan.json',self.plan),('runtime.json',{}),('runtime-identity.json',{}),('implementation.json',{'source':'synthetic'}),('trials.json',[self.row]),('ledger.json',{'sessions':[]})]:save_json(self.run/name,value)
        self.reseal()

    def checks(self,method=None,state='pass'):
        return [{'criterion_id':c['id'],'method':c['method'],'status':state,'applicability':'applicable','evidence_ids':['case'],'reason':'Synthetic original opinion.'} for c in self.catalog.for_case(self.case,method)]

    def reseal(self):
        runner._seal(self.trial)
        runner.summarize(self.run,[read_json(self.trial/'result.json')],self.plan)
        save_json(self.run/'seal.json',{'files':runner._run_inventory(self.run)})

    def fake_mechanical(self,case,packet):
        return self.checks('deterministic')

    def test_grade_recomputes_mechanical_and_marks_semantic_as_reused(self):
        original_seal=(self.run/'seal.json').read_bytes()
        checks=self.checks('deterministic');checks[0]['status']='fail'
        with patch.object(grading,'deterministic',return_value=checks) as grade,patch.object(grading,'coverage',return_value={'status':'covered'}):
            result=reassessment.grade(self.run,self.base/'graded')
        self.assertEqual(grade.call_count,1)
        self.assertEqual(result['status'],'failed')
        self.assertFalse(result['models_called'])
        self.assertEqual(result['new_judge_sessions'],0)
        self.assertEqual(result['trials'][0]['original_trial_status'],'passed')
        semantic=[c for c in result['trials'][0]['checks'] if c['method']=='semantic']
        self.assertTrue(all(c['assessment_origin']=='reused_original_review' for c in semantic))
        self.assertTrue(all(c['assessment_packet_sha256']==result['trials'][0]['source_hashes']['original_packet_sha256'] for c in semantic))
        self.assertEqual(result['trials'][0]['source_hashes']['derived_packet_sha256'],sha((self.base/'graded/trials/t0001/packet.json').read_bytes()))
        self.assertEqual((self.run/'seal.json').read_bytes(),original_seal)
        self.assertEqual(runner.report(self.run)['status'],'passed')
        self.assertTrue((self.base/'graded/seal.json').is_file())
        frozen_implementation=read_json(self.base/'graded/implementation.json')
        self.assertEqual(frozen_implementation,result['implementation'])
        self.assertEqual({str(path.relative_to(self.base/'graded/implementation')):sha(path.read_bytes())
                          for path in (self.base/'graded/implementation').rglob('*') if path.is_file()},frozen_implementation)

    def test_plan_never_preflights_or_starts_judge_or_creates_output(self):
        adapter=FakeJudge(self.catalog.for_case(self.case,'semantic'))
        result=reassessment.review(self.trial,self.base/'planned',{},adapter=adapter)
        self.assertEqual(result['status'],'planned')
        self.assertEqual(result['maximum_sessions'],1)
        self.assertEqual(adapter.preflights,0)
        self.assertEqual(adapter.judges,0)
        self.assertFalse((self.base/'planned').exists())

    def test_live_hook_reserves_one_fake_judge_and_never_executes_repair(self):
        adapter=FakeJudge(self.catalog.for_case(self.case,'semantic'))
        with patch.object(grading,'deterministic',side_effect=self.fake_mechanical),patch.object(grading,'coverage',return_value={'status':'covered'}):
            result=reassessment.review(self.trial,self.base/'reviewed',{},True,adapter=adapter)
        self.assertEqual(result['new_execute_sessions'],0)
        self.assertEqual(result['new_judge_sessions'],1)
        self.assertEqual(adapter.executes,0)
        self.assertEqual(adapter.judges,1)
        self.assertLessEqual(adapter.last_seconds,900)
        self.assertIn('_frozen_identity',adapter.runtime)
        self.assertEqual(result['status'],'passed')
        self.assertEqual(len(read_json(self.base/'reviewed/ledger.json')['sessions']),1)
        self.assertEqual(runner.report(self.run)['status'],'passed')
        self.assertEqual(read_json(self.base/'reviewed/implementation.json'),result['implementation'])
        self.assertTrue((self.base/'reviewed/implementation/harness/runner.py').is_file())

    def test_failed_fake_judge_spends_reservation_without_retry(self):
        adapter=FakeJudge(self.catalog.for_case(self.case,'semantic'),fail=True)
        result=reassessment.review(self.trial,self.base/'review-failed',{},True,adapter=adapter)
        self.assertEqual(result['status'],'incomplete')
        self.assertEqual(result['new_judge_sessions'],1)
        self.assertEqual(adapter.judges,1)
        self.assertTrue((self.base/'review-failed/assessment.json').is_file())

    def test_missing_or_changed_evidence_is_rejected_before_new_output(self):
        (self.trial/'host/packet.json').unlink()
        with self.assertRaises((EvaluationError,OSError)):
            reassessment.grade(self.run,self.base/'missing')
        self.assertFalse((self.base/'missing').exists())
        save_json(self.trial/'host/packet.json',self.packet)
        self.reseal()
        (self.trial/'host/packet.json').write_text('{}')
        with self.assertRaises(EvaluationError):
            reassessment.review(self.trial,self.base/'changed',{})
        self.assertFalse((self.base/'changed').exists())

    def test_packet_criteria_mismatch_is_rejected_even_with_consistent_file_seals(self):
        self.packet['criteria_snapshot'][0]=dict(self.packet['criteria_snapshot'][0],title='Changed outside frozen rules')
        save_json(self.trial/'host/packet.json',self.packet)
        self.reseal()
        with self.assertRaises(EvaluationError):reassessment.grade(self.run,self.base/'wrong-criteria')

    def test_missing_original_judgments_are_not_presented_as_reused_reviews(self):
        self.original['checks']=self.checks('deterministic')
        save_json(self.trial/'result.json',self.original);self.reseal()
        with patch.object(grading,'deterministic',side_effect=self.fake_mechanical),patch.object(grading,'coverage',return_value={'status':'covered'}):
            result=reassessment.grade(self.run,self.base/'no-original-judge')
        self.assertEqual(result['status'],'incomplete')
        semantic=[c for c in result['trials'][0]['checks'] if c['method']=='semantic']
        self.assertTrue(all(c['assessment_origin']=='original_opinion_unavailable_or_invalid' for c in semantic))

    def test_old_incomplete_execution_is_not_upgraded_by_new_judge(self):
        self.packet['trace_complete']=False
        self.packet['facts']['execution']={'status':'timeout','trace_complete':False}
        save_json(self.trial/'host/execution.json',self.packet['facts']['execution'])
        self.original['status']='incomplete'
        save_json(self.trial/'host/packet.json',self.packet);save_json(self.trial/'result.json',self.original);self.reseal()
        adapter=FakeJudge(self.catalog.for_case(self.case,'semantic'))
        with patch.object(grading,'deterministic',side_effect=self.fake_mechanical),patch.object(grading,'coverage',return_value={'status':'covered'}):
            result=reassessment.review(self.trial,self.base/'incomplete-source',{},True,adapter=adapter)
        self.assertEqual(result['semantic_status'],'passed')
        self.assertEqual(result['status'],'incomplete')
        self.assertEqual(result['original_trial_status'],'incomplete')

    def test_source_and_existing_output_are_never_overwritten(self):
        with self.assertRaises(EvaluationError):reassessment.grade(self.run,self.run/'regrade')
        existing=self.base/'existing';existing.mkdir();(existing/'keep.txt').write_text('keep')
        with self.assertRaises(EvaluationError):reassessment.review(self.trial,existing,{})
        self.assertEqual((existing/'keep.txt').read_text(),'keep')

    def test_source_drift_during_grading_prevents_successful_new_record(self):
        def drift(case,packet):
            (self.trial/'host/packet.json').write_text('{}')
            return self.checks('deterministic')
        with patch.object(grading,'deterministic',side_effect=drift),patch.object(grading,'coverage',return_value={'status':'covered'}):
            with self.assertRaises(EvaluationError):reassessment.grade(self.run,self.base/'raced')
        self.assertFalse((self.base/'raced').exists())

    def test_grade_reconstructs_missing_resource_without_rewriting_original_packet(self):
        original_bytes=(self.trial/'host/packet.json').read_bytes()
        with patch.object(grading,'deterministic',side_effect=self.fake_mechanical),patch.object(grading,'coverage',return_value={'status':'covered'}):
            result=reassessment.grade(self.run,self.base/'rehydrated')
        rebuilt=read_json(self.base/'rehydrated/trials/t0001/packet.json')
        self.assertIn('references/ci-job-failure-directory.md',rebuilt['facts']['resources'])
        self.assertEqual((self.trial/'host/packet.json').read_bytes(),original_bytes)
        self.assertEqual(result['trials'][0]['source_hashes']['original_packet_sha256'],sha(original_bytes))
        self.assertFalse(result['models_called'])

    def test_unsealed_runtime_resource_must_match_sealed_runtime_identity(self):
        resource=self.trial/'runtime/skills/ci-self-healing/references/ci-job-failure-directory.md'
        resource.write_text('Changed after execution')
        runner.report(self.run)
        with self.assertRaises(EvaluationError):reassessment.grade(self.run,self.base/'changed-runtime')
        self.assertFalse((self.base/'changed-runtime').exists())

    def test_reconstruction_preserves_original_model_identity_gap(self):
        self.execution.update(trace_complete=False,gaps=['response_model_identity_unconfirmed'])
        self.packet['trace_complete']=False
        self.original['status']='incomplete'
        save_json(self.trial/'host/execution.json',self.execution)
        save_json(self.trial/'host/packet.json',self.packet)
        save_json(self.trial/'result.json',self.original)
        self.reseal()
        with patch.object(grading,'deterministic',side_effect=self.fake_mechanical),patch.object(grading,'coverage',return_value={'status':'covered'}):
            result=reassessment.grade(self.run,self.base/'identity-gap')
        derived=read_json(self.base/'identity-gap/trials/t0001/packet.json')
        self.assertEqual(derived['facts']['execution']['gaps'],['response_model_identity_unconfirmed'])
        self.assertFalse(derived['trace_complete'])
        self.assertEqual(result['status'],'incomplete')

    def test_reconstruction_cannot_replace_evidence_under_old_semantic_references(self):
        real_collect=grading.collect
        def changed_reference(*args):
            packet=real_collect(*args)
            packet['evidence']['case']['content']={'changed':'meaning'}
            packet['evidence']['case']['sha256']=sha({'changed':'meaning'})
            return packet
        with patch.object(grading,'collect',side_effect=changed_reference),patch.object(grading,'deterministic',side_effect=self.fake_mechanical):
            with self.assertRaises(EvaluationError):reassessment.grade(self.run,self.base/'changed-reference')
        self.assertFalse((self.base/'changed-reference').exists())


if __name__=='__main__':unittest.main()
