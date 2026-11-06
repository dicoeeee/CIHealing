"""Offline controls for the approved 118-execution experiment."""
import json
from pathlib import Path
import sys
import tempfile
import os
import time
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import read_json, snapshot, write_json
from cleanup_ab import registry, external_processes, common_packet, COMMON
from cleanup_analysis import token_total
from ab_review import prompt
import cleanup_cases
import cleanup_ab


class CleanupABTests(unittest.TestCase):
    def test_registry_preserves_existing_groups_and_adds_exact_planned_repeats(self):
        rows=registry()
        self.assertEqual(len(rows),59)
        self.assertEqual(len({r['id'] for r in rows}),59)
        self.assertEqual(len({(r['group'],r['scenario']) for r in rows}),33)
        for name in ('case-001','case-002'):
            self.assertEqual(sum(r['scenario']==name for r in rows),5)
        self.assertEqual(sum(r['group']=='cleanup' for r in rows),9)
        self.assertEqual(len({r['scenario'] for r in rows if r['group']=='enterprise'}),6)

    def test_foreign_icode_count_excludes_only_owned_process_trees(self):
        processes={1:{'parent':0,'icode':False},2:{'parent':1,'icode':True},
                   3:{'parent':0,'icode':True},4:{'parent':2,'icode':True}}
        self.assertEqual(external_processes(processes,{1}),[3])
        self.assertEqual(external_processes(processes,set()),[2,3,4])

    def test_shared_pool_caps_this_experiment_and_excludes_existing_processes(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t).resolve()
            fake={os.getpid():{'parent':0,'icode':False}, **{n:{'parent':0,'icode':True} for n in (-1,-2,-3,-4)}}
            with patch.object(cleanup_ab,'GLOBAL',root/'global'),patch.object(cleanup_ab,'process_inventory',return_value=fake):
                pool=cleanup_ab.GlobalPool(root)
                def work(i):
                    trial=root/('A' if i%2 else 'B')/'trials'/str(i)
                    with pool.lease('execute' if i%3 else 'review',trial): time.sleep(.02)
                try:
                    with ThreadPoolExecutor(max_workers=8) as executor:list(executor.map(work,range(12)))
                finally:pool.closed.set();pool.monitor.join(timeout=2)
                self.assertEqual(read_json(root/'global/state.json')['leases'],{})
            ledger=[json.loads(line) for line in (root/'icode-concurrency.jsonl').read_text().splitlines()]
            self.assertEqual(sum(r['event']=='acquired' for r in ledger),12)
            self.assertLessEqual(max(r['reserved_total'] for r in ledger if r['event']=='acquired'),6)
            self.assertGreater(max(r['reserved_total'] for r in ledger if r['event']=='acquired'),2)
            self.assertTrue(all(r['external_count_excluded']==4 for r in ledger if r['event']=='acquired'))

    def test_fixtures_protect_job_files_and_allow_only_declared_changes(self):
        with tempfile.TemporaryDirectory() as t:
            for name in cleanup_cases.SCENARIOS:
                root=Path(t).resolve()/name
                cleanup_cases.materialize(root,name,{'python':sys.executable})
                repo=root/'visible/scene/sourcecode';before=snapshot(root/'visible')
                (repo/'.build/new-output.txt').write_text('ordinary artifact')
                self.assertEqual(cleanup_cases.state_checks(before,snapshot(root/'visible'),name)['failures'],[])
                (repo/'.build/preexisting.txt').write_text('damaged')
                self.assertTrue(cleanup_cases.state_checks(before,snapshot(root/'visible'),name)['failures'])
                if name=='rollback-preserves-job':
                    self.assertEqual((repo/'.ci/audit.before.py').read_text(),'JOB_VALUE = 7\n')

    def test_common_quality_does_not_erase_version_conformance(self):
        with tempfile.TemporaryDirectory() as t:
            trial=Path(t).resolve()
            write_json(trial/'ab-case.json',{'group':'cleanup','scenario':'generated-delivery'})
            failure={'check':'final_native_cleanup_match_unconfirmed','paths':[]}
            actual={'status':'fail','failures':[failure], 'gaps':[], 'execution_checks':{'failures':[failure],'gaps':[],'contract':'skill-execution-v7'}}
            packet={'deterministic':actual,'events':[],'shared_resources':{},'evidence_ids':['D1']}
            with patch('cleanup_ab.ab_runtime.packet',return_value=packet):
                result=common_packet(trial)
            self.assertEqual(result['deterministic']['status'],'pass')
            self.assertEqual(result['deterministic']['version_conformance'],actual)
            self.assertEqual(result['comparison_contract'],COMMON)
            text=prompt(result)
            self.assertIn('共同结果质量标准',text)
            self.assertIn('普通编译产物及空目录可保留',text)

    def test_common_quality_still_preserves_behavior_and_missing_build_failures(self):
        with tempfile.TemporaryDirectory() as t:
            trial=Path(t).resolve();write_json(trial/'ab-case.json',{'group':'cleanup','scenario':'generated-delivery'})
            build={'check':'agent_build_not_observed'};oracle={'check':'independent_behavior_oracle'}
            actual={'status':'fail','failures':[build,oracle],'gaps':[],'execution_checks':{'failures':[build],'gaps':[]}}
            with patch('cleanup_ab.ab_runtime.packet',return_value={'deterministic':actual}):
                result=common_packet(trial)
            self.assertEqual(result['deterministic']['status'],'fail')
            self.assertCountEqual(result['deterministic']['failures'],[build,oracle])

    def test_usage_missing_is_unknown_and_reasoning_is_not_double_counted(self):
        usage={'gaps':[],'native_exchange_finishes':1,'normalized_tokens':{
            'input_total':{'sum':100,'observed_exchanges':1},'output_total':{'sum':30,'observed_exchanges':1},
            'reasoning':{'sum':20,'observed_exchanges':1},'cache_read':{'sum':80,'observed_exchanges':1}}}
        self.assertEqual(token_total(usage),130)
        usage['normalized_tokens']['output_total']['observed_exchanges']=0
        self.assertIsNone(token_total(usage))

    def test_trusted_build_detector_distinguishes_project_clean(self):
        def event(command):return {'tool_name':'bash','args':{'command':command}}
        self.assertTrue(cleanup_cases.build_detector(event('python3 -B ci_verify.py verify')))
        self.assertFalse(cleanup_cases.build_detector(event('python3 -B ci_verify.py clean')))

    def test_pair_alignment_preserves_old_preparation_and_refuses_started_trials(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t).resolve();row={'id':'handoff--empty--r1','group':'handoff','scenario':'empty','repeat':1}
            write_json(root/'plan.json',{'rows':[row]})
            for v in ('A','B'):
                trial=root/v/'trials'/row['id'];repo=trial/'visible/scene/sourcecode'
                (repo/'.git').mkdir(parents=True)
                (repo/'.git/HEAD').write_text(v)
                (repo/'input.txt').write_text('identical source')
                write_json(trial/'visible/scene/build_info.json',{'repositories':[{'commitID':v}]})
                write_json(trial/'prepared-inputs.json',snapshot(trial/'visible'))
            def fake_git(repo,*args):return 'same-tree' if args[-1]=='HEAD^{tree}' else (repo/'.git/HEAD').read_text()
            with patch('cases.git',side_effect=fake_git):cleanup_ab.align(root)
            b=root/'B/trials'/row['id']
            self.assertEqual((b/'preparation-v1/git/HEAD').read_text(),'B')
            self.assertEqual((b/'visible/scene/sourcecode/.git/HEAD').read_text(),'A')
            self.assertEqual(read_json(root/'paired-input-audit-v2.json')['status'],'passed')
            write_json(b/'execution-start.json',{'started':True})
            with self.assertRaisesRegex(ValueError,'after model execution'):cleanup_ab.align(root)


if __name__=='__main__':unittest.main()
