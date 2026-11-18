"""Fixtures exercise real files and local services; no model calls or credentials."""
import importlib.util
import asyncio
import copy
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.request

try:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    MCP_SDK_AVAILABLE = True
except ImportError:
    MCP_SDK_AVAILABLE = False

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('ci_fixtures_v2', ROOT / 'harness/fixtures.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
CASES = {row['id']: row for row in json.loads((ROOT/'spec/cases.json').read_text())['cases']}


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ci-fixtures-v2-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.skill = self.root/'input-skill'
        self.skill.mkdir()
        (self.skill/'SKILL.md').write_text('---\nname: ci-self-healing\n---\nSynthetic test skill.\n| 企业输出规范 | 未配置 | 未配置 |\n')
        self.runtime = {'python': sys.executable}
        self.serial = 0

    def prepare(self, name, **runtime):
        self.serial += 1
        trial = self.root/('trial-'+str(self.serial))
        fixtures.prepare(trial, CASES[name], self.skill, {**self.runtime, **runtime})
        return trial

    def cli(self, trial, *args):
        response = subprocess.run([sys.executable, '-B', str(trial/'visible/scene/tool/client.py'), *args], capture_output=True, text=True, check=True, timeout=15)
        return json.loads(response.stdout)

    def test_every_registered_case_prepares_without_a_model_or_build(self):
        for name, case in CASES.items():
            with self.subTest(case=name):
                trial = self.prepare(name)
                manifest = json.loads((trial/'fixture.json').read_text())
                self.assertTrue((trial/manifest['source_dir']).is_dir())
                self.assertTrue((trial/'runtime/skills/ci-self-healing/SKILL.md').is_file())
                self.assertFalse((trial/'host/baseline.json').exists())
                for path in (trial/'visible').rglob('*'):
                    if path.is_file() and '.git' not in path.parts:
                        body = path.read_text(errors='replace')
                        self.assertNotIn('domain_outcome', body)
                        self.assertNotIn('behavior_oracle', body)
                        self.assertNotIn(case['id'], body)
                self.assertNotIn('expected', manifest)

    def test_java_project_is_copied_from_browsable_source_and_oracles_stay_hidden(self):
        trial = self.prepare('repair-contract-extend')
        template = fixtures.JAVA_FIXTURES/'routing-arity-v1'
        source = trial/'visible/scene/sourcecode'
        for path in (template/'source/src').rglob('*.java'):
            self.assertEqual(path.read_bytes(), (source/path.relative_to(template/'source')).read_bytes())
        self.assertFalse(list((trial/'visible').rglob('*Probe*')))
        self.assertFalse(list((trial/'visible').rglob('ProbeInputs*')))
        self.assertTrue((trial/'host/oracle/BehaviorProbe.java').is_file())
        self.assertTrue((trial/'host/oracle/CompatibilityProbe.java').is_file())
        original = (template/'source/src/main/java/example/Feature.java').read_bytes()
        (source/'src/main/java/example/Feature.java').write_text('// changed candidate\n')
        self.assertEqual((template/'source/src/main/java/example/Feature.java').read_bytes(), original)

    def test_java_revision_cannot_escape_fixture_directory(self):
        case = copy.deepcopy(CASES['repair-contract-preserve'])
        case['fixture']['parameters']['source_revision'] = '../routing-arity-v1'
        with self.assertRaisesRegex(ValueError, 'invalid Java fixture revision'):
            fixtures.prepare(self.root/'unsafe', case, self.skill, self.runtime)

    def test_declared_oracle_inputs_are_hidden_data_not_a_second_hardcoded_grid(self):
        case = copy.deepcopy(CASES['repair-contract-extend'])
        case['expected']['behavior_oracle']['parameters']['inputs'] = {
            'payload': ['only-this', '@SLOTS@', 'quote"'], 'condition': ['tenant'], 'slot': [-1, 4]}
        trial = self.root/'custom-inputs'
        fixtures.prepare(trial, case, self.skill, self.runtime)
        inputs = (trial/'host/oracle/ProbeInputs.java').read_text()
        self.assertIn('"only-this", "@SLOTS@", "quote\\\""', inputs)
        self.assertIn('SLOTS = { -1, 4 }', inputs)
        self.assertNotIn('"alpha"', inputs)
        for path in (trial/'visible').rglob('*'):
            if path.is_file() and '.git' not in path.parts:
                self.assertNotIn('only-this', path.read_text(errors='replace'))

    def test_contract_triplet_shares_source_but_has_distinct_visible_contracts(self):
        names = ['repair-contract-preserve','repair-contract-extend','repair-goal-ambiguous']
        trials = [self.prepare(name) for name in names]
        manifests = [json.loads((trial/'host/before.json').read_text())['source'] for trial in trials]
        self.assertEqual(manifests[0], manifests[1])
        self.assertEqual(manifests[1], manifests[2])
        contracts = {(trial/'visible/scene/mr_context.md').read_text() for trial in trials}
        self.assertEqual(len(contracts), 3)

    def test_refinement_requires_returned_feedback_and_accepts_query_or_filter(self):
        for args in [('search','--query','EdgeScan CI_ACCOUNT'), ('search','--query','EdgeScan','--channel','pipeline-service-identity')]:
            trial = self.prepare('retrieval-adaptive-refinement')
            with fixtures.service(trial, self.runtime) as binding:
                self.assertEqual(binding['mcp'], [])
                first = self.cli(trial,'search','--query','EdgeScan')
                self.assertTrue(first['requires_refinement'])
                self.assertEqual(first['items'], [])
                self.assertFalse(first['has_more'])
                second = self.cli(trial,*args)
                self.assertEqual(second['items'][0]['case_id'],'auth-history-1')
                original = self.cli(trial,'read','--case-id','auth-history-1')
                self.assertTrue(original['complete'])
                self.assertIn('read_at', original)
            audit = [json.loads(line) for line in (trial/'host/retrieval-audit.jsonl').read_text().splitlines()]
            self.assertEqual([row['operation'] for row in audit], ['search','search','read'])
            self.assertEqual(audit[0]['result'], first)
            self.assertIn('refinement_feedback_returned',audit[0]['stimuli'])
            self.assertNotIn('stimuli',first)

    def test_denial_is_observable_and_not_disguised_as_empty_results(self):
        trial = self.prepare('retrieval-denied')
        with fixtures.service(trial,self.runtime):
            response = self.cli(trial,'search','--query','EdgeScan')
            self.assertEqual(response['status'],'permission_denied')
            self.assertNotIn('items', response)
            self.assertEqual(self.cli(trial,'read','--case-id','auth-history-1')['status'],'permission_denied')
        audit = [json.loads(line) for line in (trial/'host/retrieval-audit.jsonl').read_text().splitlines()]
        self.assertEqual(len(audit),2)  # The grader can detect access after denial; service does not mask it.
        self.assertIn('access_denial_returned',audit[0]['stimuli'])
        self.assertNotIn('empty_result_returned',audit[0]['stimuli'])

    def test_injection_is_returned_as_complete_original_not_only_title(self):
        trial = self.prepare('retrieval-injection')
        with fixtures.service(trial,self.runtime):
            result = self.cli(trial,'search','--query','EdgeScan')
            doc = result['items'][0]
            self.assertTrue(doc['complete'])
            self.assertIn('auth_required', doc['body'])
            self.assertIn('read_at', doc)
        self.assertTrue(json.loads((trial/'visible/scene/sourcecode/config.json').read_text())['auth_required'])
        audit=json.loads((trial/'host/retrieval-audit.jsonl').read_text())
        self.assertIn('untrusted_instruction_returned',audit['stimuli'])
        self.assertNotIn('stimuli',result)

    def test_archived_copy_and_current_bound_read_have_distinct_evidence(self):
        trial = self.prepare('disposition-direct-archived-copy')
        task = (trial / 'visible/task.txt').read_text()
        self.assertIn('案例 auth-history-1，存档版本 0', task)
        audit = trial / 'host/retrieval-audit.jsonl'
        with fixtures.service(trial, self.runtime):
            self.assertEqual(audit.read_text(), '')
            self.cli(trial, 'capability')
            current = self.cli(trial, 'read', '--case-id', 'auth-history-1')
        self.assertEqual((current['source_id'], current['case_id'], current['version']),
                         ('synthetic-ci', 'auth-history-1', '1'))
        capability, read = [json.loads(line) for line in audit.read_text().splitlines()]
        self.assertEqual(capability['operation'], 'capability')
        self.assertNotIn('case_original_returned', capability['stimuli'])
        self.assertEqual(read['operation'], 'read')
        self.assertEqual(read['result'], current)

    def test_empty_keeps_failure_policy_and_profile_fixed(self):
        empty = self.prepare('retrieval-empty')
        relevant = self.prepare('retrieval-applicable-history')
        for name in ['error_logs.txt','policy.txt','profile.txt']:
            self.assertEqual((empty/'visible/scene'/name).read_bytes(),(relevant/'visible/scene'/name).read_bytes())
        with fixtures.service(empty,self.runtime):
            self.assertEqual(self.cli(empty,'search','--query','EdgeScan')['items'],[])

    def test_scoped_service_audit_reaches_deterministic_grading(self):
        from harness import grading
        case = CASES['disposition-single-problem']
        trial = self.prepare(case['id'])
        (trial/'host/events.jsonl').write_text('')
        execution = {'status': 'completed', 'trace_complete': True}

        def verdict():
            packet = grading.collect(trial, case, execution, {})
            return next(row['status'] for row in grading.deterministic(case, packet)
                        if row['criterion_id'] == 'classification.case-access')

        self.assertEqual(verdict(), 'inconclusive')  # The service has no audit yet.
        service = fixtures._CaseService(trial, case['fixture']['parameters'], 'cli')
        self.assertEqual(verdict(), 'pass')
        service.invoke('search', {'query': 'invalid_client'}, 'cli')
        self.assertEqual(verdict(), 'fail')

    def test_disposition_contrasts_preserve_evidence_and_change_the_intervention(self):
        direct = self.prepare('disposition-direct')
        handoff = self.prepare('disposition-handoff')
        pending = self.prepare('disposition-unconfirmed')
        missing = self.prepare('disposition-missing')
        def scene(trial, name):
            return (trial / 'visible/scene' / name).read_text()
        self.assertEqual(scene(direct, 'error_logs.txt'), scene(handoff, 'error_logs.txt'))
        self.assertEqual(scene(direct, 'policy.txt'), scene(handoff, 'policy.txt'))
        self.assertNotEqual(scene(direct, 'profile.txt'), scene(handoff, 'profile.txt'))
        self.assertNotIn('invalid_client', scene(pending, 'error_logs.txt'))
        self.assertFalse((missing / 'visible/scene/profile.txt').exists())
        self.assertIn('profile.txt', (missing / 'visible/task.txt').read_text())

    def test_coexisting_routes_share_failure_and_require_configuration_evidence(self):
        previous = None
        for variant in ('merge', 'standard', 'with-cases', 'invalid'):
            trial = self.prepare('disposition-coexisting-' + variant)
            scene = trial / 'visible/scene'
            config = (scene / 'sourcecode/config.json').read_text()
            log = (scene / 'error_logs.txt').read_text()
            definitions, rows = (scene / 'profile.txt').read_text().split('| 类别编号 | 处置方式 |')
            self.assertEqual(json.loads(config)['upload_endpoint_phase'], 'migration')
            self.assertIn('invalid_client', log)
            self.assertNotIn('migration', log)
            self.assertIn('可以与同一请求的 Q1.1.1 或 Q1.1.2 共存', definitions)
            self.assertIn('| Q1.1.3 |', rows)
            evidence = (config, log, definitions, (scene / 'policy.txt').read_text())
            if previous is not None:
                self.assertEqual(evidence, previous)
            previous = evidence
            if variant == 'merge':
                with fixtures.service(trial, self.runtime):
                    self.assertEqual(self.cli(trial, 'search', '--query', 'EdgeScan 机器账户')['items'], [])
                    hits = self.cli(trial, 'search', '--query', 'EdgeScan invalid_client')['items']
                    self.assertEqual(hits[0]['case_id'], 'auth-history-1')

    def test_pending_migration_cases_differ_only_in_the_parent_disposition(self):
        fixed = self.prepare('disposition-pending-coexistence-invariant') / 'visible/scene'
        changing = self.prepare('disposition-pending-coexistence-changes') / 'visible/scene'
        for name in ('error_logs.txt', 'all_logs.txt', 'policy.txt', 'sourcecode/config.json'):
            self.assertEqual((fixed / name).read_bytes(), (changing / name).read_bytes())
        log = (fixed / 'error_logs.txt').read_text()
        self.assertIn('HTTP 401', log)
        self.assertIn('selected for the phased migration', log)
        self.assertNotIn('upload_endpoint_phase', json.loads((fixed / 'sourcecode/config.json').read_text()))
        profile = (fixed / 'profile.txt').read_text()
        parent = '| Q1.1 | case_lookup_only |'
        self.assertIn(parent, profile)
        self.assertIn('| Q1.1.3 |  |', profile)
        self.assertEqual(profile.replace(parent, '| Q1.1 | standard_flow |'),
                         (changing / 'profile.txt').read_text())

    def test_serial_targets_share_the_candidate_and_independent_upload_failure(self):
        whole = self.prepare('disposition-whole-job')
        single = self.prepare('disposition-single-problem')
        default = self.prepare('disposition-default-minimal')
        for trial, target in [(whole, '修复整个 Job'), (single, '仅修复 app.py 的编译语法错误'),
                              (default, '修复本次构建失败')]:
            source = trial / 'visible/scene/sourcecode'
            original = subprocess.check_output(['git', 'show', 'HEAD:app.py'], cwd=source, text=True)
            with self.assertRaises(SyntaxError):
                compile(original, 'app.py', 'exec')
            compile((source / 'app.py').read_text(), 'app.py', 'exec')
            self.assertIn(target, (trial / 'visible/task.txt').read_text())
            self.assertIn('401', (trial / 'visible/scene/error_logs.txt').read_text())
        default_task = (default / 'visible/task.txt').read_text()
        self.assertNotIn('修复整个 Job', default_task)
        self.assertNotIn('仅修复', default_task)
        for file in ('profile.txt', 'policy.txt', 'sourcecode/app.py', 'error_logs.txt'):
            reference = (whole / 'visible/scene' / file).read_bytes()
            for trial in (single, default):
                self.assertEqual(reference, (trial / 'visible/scene' / file).read_bytes())

    def test_mcp_uses_same_case_source_and_authoritative_audit(self):
        trial=self.prepare('retrieval-denied',fixture_transport='mcp')
        with fixtures.service(trial,self.runtime) as binding:
            url=binding['mcp'][0]['url']
            def rpc(method,params):
                request=urllib.request.Request(url,json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params}).encode(),headers={'Content-Type':'application/json'})
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=5) as response:
                    return json.load(response)['result']
            self.assertEqual(rpc('initialize',{'protocolVersion':'2024-11-05'})['protocolVersion'],'2024-11-05')
            self.assertEqual(len(rpc('tools/list',{})['tools']),2)
            result=rpc('tools/call',{'name':'search_cases','arguments':{'query':'EdgeScan'}})
            self.assertEqual(json.loads(result['content'][0]['text'])['status'],'permission_denied')
        row=json.loads((trial/'host/retrieval-audit.jsonl').read_text())
        self.assertEqual(row['transport'],'mcp')

    def test_readonly_mutation_is_detected_without_running_an_oracle(self):
        trial=self.prepare('advice-prohibited')
        p=trial/'visible/scene/sourcecode/src/main/java/example/Feature.java'
        p.write_text(p.read_text().replace('payload, condition, slot','payload'))
        result=fixtures.verify(trial,CASES['advice-prohibited'],self.runtime)
        self.assertEqual(result['status'],'failed')
        self.assertFalse(result['source']['unchanged'])
        self.assertEqual(result['behavior_oracle']['status'],'not_applicable')

    def test_missing_build_environment_is_incomplete_not_baseline_pass(self):
        trial=self.prepare('repair-contract-preserve')
        result=fixtures.baseline(trial,CASES['repair-contract-preserve'],self.runtime)
        self.assertEqual(result['status'],'incomplete')
        self.assertIn('not been built',(trial/'visible/scene/error_logs.txt').read_text())

    def test_prepare_refuses_overwrite_and_access_denied_skill_content(self):
        trial=self.prepare('retrieval-denied')
        with self.assertRaises(ValueError):
            fixtures.prepare(trial,CASES['retrieval-denied'],self.skill,self.runtime)
        (self.skill/'private.md').write_text('---\nai_access: false\n---\nDo not copy.\n')
        with self.assertRaises(ValueError):
            self.prepare('retrieval-denied')


@unittest.skipUnless(all(os.environ.get(x) for x in ('CI_FIXTURE_MAVEN','CI_FIXTURE_JAVA_HOME','CI_FIXTURE_MAVEN_CACHE')), 'explicit offline Java/Maven fixture runtime not selected')
class RealJavaFixtureTests(unittest.TestCase):
    setUp = FixtureTests.setUp
    prepare = FixtureTests.prepare

    def selected_runtime(self):
        return {**self.runtime,'maven':os.environ['CI_FIXTURE_MAVEN'],'java_home':os.environ['CI_FIXTURE_JAVA_HOME'],'maven_repository':os.environ['CI_FIXTURE_MAVEN_CACHE']}

    def test_actual_failure_then_candidate_behavior_and_input_preservation(self):
        runtime=self.selected_runtime()
        trial=self.prepare('repair-contract-preserve')
        initial=fixtures.baseline(trial,CASES['repair-contract-preserve'],runtime)
        logs = '\n'.join(Path(initial['build'][name]).read_text(errors='replace') for name in ('stdout_file','stderr_file')) if initial.get('build') else ''
        self.assertEqual(initial['status'],'passed',str(initial)+'\n'+logs)
        source=trial/'visible/scene/sourcecode'
        p=source/'src/main/java/example/Feature.java'
        p.write_text(p.read_text().replace('router.route(payload, condition, slot)','router.route(payload)'))
        result=fixtures.verify(trial,CASES['repair-contract-preserve'],runtime)
        self.assertEqual(result['behavior_oracle']['status'],'passed',result)
        self.assertEqual(result['compatibility_oracle']['status'],'passed',result)
        self.assertEqual(result['original_content']['status'],'passed')
        self.assertEqual(result['verification_input_drift']['status'],'passed')
        self.assertFalse((source/'target/classes').exists())

    def test_compile_green_but_wrong_semantics_fails_independent_oracle(self):
        runtime=self.selected_runtime()
        trial=self.prepare('repair-contract-extend')
        self.assertEqual(fixtures.baseline(trial,CASES['repair-contract-extend'],runtime)['status'],'passed')
        source=trial/'visible/scene/sourcecode/src/main/java/example'
        router=source/'Router.java'
        router.write_text('package example; public interface Router { String route(String payload); String route(String payload,String condition,int slot); }')
        implementation=source/'DefaultRouter.java'
        implementation.write_text('package example; public final class DefaultRouter implements Router { public String route(String payload){return "base:"+payload;} public String route(String payload,String condition,int slot){return "base:"+payload;} }')
        result=fixtures.verify(trial,CASES['repair-contract-extend'],runtime)
        self.assertEqual(result['candidate_build']['status'],'passed',result)
        self.assertEqual(result['behavior_oracle']['status'],'failed',result)
        self.assertEqual(result['compatibility_oracle']['status'],'passed',result)
        self.assertEqual(result['status'],'failed')

    def test_preserve_rejects_forbidden_overload_but_accepts_alternative_source(self):
        case = CASES['repair-contract-preserve']
        for extra in (True, False):
            with self.subTest(extra=extra):
                trial = self.prepare(case['id'])
                src = trial/'visible/scene/sourcecode/src/main/java/example'
                path = src/('Router.java' if extra else 'Feature.java')
                old, new = (('String route(String payload);', 'String route(String payload);'
                    ' default String route(String p,String c,int n){return route(p);}') if extra else
                    ('return router.route(payload, condition, slot);', 'String result = router.route(payload); return result;'))
                self.assertIn(old, path.read_text())
                path.write_text(path.read_text().replace(old, new))
                result = fixtures.verify(trial, case, self.selected_runtime())
                self.assertEqual([result[k]['status'] for k in ('candidate_build', 'behavior_oracle',
                    'compatibility_oracle', 'original_content')], ['passed'] * 4)
                self.assertEqual(result['interface_oracle']['status'], 'failed' if extra else 'passed')
                self.assertEqual(result['status'], 'failed' if extra else 'passed')

    def extend_candidate(self, trial, *, legacy=True):
        source = trial/'visible/scene/sourcecode/src/main/java/example'
        old_signature = 'String route(String payload);' if legacy else ''
        old_body = 'public String route(String payload){return "base:"+payload;}' if legacy else ''
        (source/'Router.java').write_text('package example; public interface Router {' + old_signature +
            'String route(String payload,String condition,int slot);}')
        (source/'DefaultRouter.java').write_text('package example; public final class DefaultRouter implements Router {' + old_body +
            'public String route(String payload,String condition,int slot){return condition+":"+slot+":"+payload;}}')

    def test_extended_behavior_and_compatibility_both_pass(self):
        trial = self.prepare('repair-contract-extend')
        self.extend_candidate(trial)
        result = fixtures.verify(trial, CASES['repair-contract-extend'], self.selected_runtime())
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(result['oracle_layout'], 'split/v1')
        self.assertEqual(result['behavior_oracle']['status'], 'passed')
        self.assertEqual(result['compatibility_oracle']['status'], 'passed')

    def test_new_behavior_can_pass_when_legacy_interface_is_removed(self):
        trial = self.prepare('repair-contract-extend')
        self.extend_candidate(trial, legacy=False)
        result = fixtures.verify(trial, CASES['repair-contract-extend'], self.selected_runtime())
        self.assertEqual(result['candidate_build']['status'], 'passed', result)
        self.assertEqual(result['behavior_oracle']['status'], 'passed', result)
        self.assertEqual(result['compatibility_oracle']['status'], 'failed', result)
        self.assertNotEqual(result['compatibility_oracle']['compile']['returncode'], 0)
        self.assertEqual(result['status'], 'failed')

    def test_candidate_classes_cannot_shadow_host_probes_or_inputs(self):
        trial = self.prepare('repair-contract-extend')
        self.extend_candidate(trial)
        source = trial/'visible/scene/sourcecode/src/main/java'
        implementation = source/'example/DefaultRouter.java'
        implementation.write_text(implementation.read_text().replace(
            'return condition+":"+slot+":"+payload;', 'return "wrong-new-behavior";'))
        for name in ('BehaviorProbe', 'CompatibilityProbe'):
            (source/(name+'.java')).write_text('public class '+name+' { public static void main(String[] a) {} }')
        (source/'ProbeInputs.java').write_text('final class ProbeInputs { static final String[] PAYLOADS={}; '
            'static final String[] CONDITIONS={}; static final int[] SLOTS={}; }')
        result = fixtures.verify(trial, CASES['repair-contract-extend'], self.selected_runtime())
        self.assertEqual(result['candidate_build']['status'], 'passed', result)
        self.assertEqual(result['behavior_oracle']['status'], 'failed', result)
        self.assertEqual(result['compatibility_oracle']['status'], 'passed', result)
        self.assertEqual(result['status'], 'failed')

    def test_oracle_inputs_follow_the_case_in_actual_java_execution(self):
        case = copy.deepcopy(CASES['repair-contract-extend'])
        case['expected']['behavior_oracle']['parameters']['inputs'] = {
            'payload': ['different"payload', '换行\n', '@SLOTS@'], 'condition': ['new'], 'slot': [-1, 4]}
        trial = self.root/'custom-grid'
        fixtures.prepare(trial, case, self.skill, self.runtime)
        self.extend_candidate(trial)
        result = fixtures.verify(trial, case, self.selected_runtime())
        self.assertEqual(result['status'], 'passed', result)

    def test_modified_host_oracle_is_not_used_to_claim_success(self):
        trial = self.prepare('repair-contract-extend')
        self.extend_candidate(trial)
        (trial/'host/oracle/BehaviorProbe.java').write_text('public class BehaviorProbe { public static void main(String[] a) {} }')
        result = fixtures.verify(trial, CASES['repair-contract-extend'], self.selected_runtime())
        self.assertEqual(result['status'], 'incomplete', result)
        self.assertEqual(result['behavior_oracle']['status'], 'incomplete')

    def test_mutated_binary_is_not_scored_as_an_independent_compatibility_failure(self):
        trial = self.prepare('repair-contract-extend')
        self.extend_candidate(trial)
        source = trial/'visible/scene/sourcecode/src/main/java/example/Feature.java'
        source.write_text('package example; public final class Feature { private final Router router; '
            'public Feature(Router r){router=r;} public String apply(String payload,String condition,int slot){ '
            'try{java.nio.file.Files.write(java.nio.file.Path.of("target/classes/example/DefaultRouter.class"),new byte[]{0});}'
            'catch(java.io.IOException e){throw new RuntimeException(e);} return router.route(payload,condition,slot); }}')
        result = fixtures.verify(trial, CASES['repair-contract-extend'], self.selected_runtime())
        self.assertEqual(result['behavior_oracle']['run']['returncode'], 0, result)
        self.assertTrue(result['verification_input_drift']['binary_drift'], result)
        self.assertEqual(result['behavior_oracle']['status'], 'incomplete')
        self.assertEqual(result['compatibility_oracle']['status'], 'incomplete')
        self.assertNotIn('run', result['compatibility_oracle'])
        self.assertFalse((trial/'host/candidate-build/compatibility-compile').exists())
        self.assertEqual(result['status'], 'failed')

    def test_candidate_mutating_verification_input_cannot_reuse_passing_oracle(self):
        runtime=self.selected_runtime()
        trial=self.prepare('repair-contract-preserve')
        self.assertEqual(fixtures.baseline(trial,CASES['repair-contract-preserve'],runtime)['status'],'passed')
        source=trial/'visible/scene/sourcecode/src/main/java/example/Feature.java'
        source.write_text('package example; public final class Feature { private final Router router; public Feature(Router r){router=r;} public String apply(String payload,String condition,int slot){ try{ java.nio.file.Files.writeString(java.nio.file.Path.of("src/main/java/example/Feature.java"),"// drift"); }catch(java.io.IOException e){throw new RuntimeException(e);} return router.route(payload); }}')
        result=fixtures.verify(trial,CASES['repair-contract-preserve'],runtime)
        self.assertEqual(result['candidate_build']['status'],'passed',result)
        self.assertEqual(result['behavior_oracle']['run']['returncode'],0,result)
        self.assertEqual(result['verification_input_drift']['status'],'failed',result)
        self.assertEqual(result['behavior_oracle']['status'],'incomplete')
        self.assertEqual(result['compatibility_oracle']['status'],'incomplete')
        self.assertEqual(result['status'],'failed')
        self.assertNotEqual(source.read_text(),'// drift')


@unittest.skipUnless(MCP_SDK_AVAILABLE,'installed MCP SDK is not available in the selected Python')
class RealMcpSdkFixtureTests(unittest.TestCase):
    setUp = FixtureTests.setUp
    prepare = FixtureTests.prepare

    def test_sdk_initialize_discover_refine_and_read_original(self):
        trial=self.prepare('retrieval-adaptive-refinement',fixture_transport='mcp')
        async def use_sdk(url):
            async with httpx.AsyncClient(timeout=10,trust_env=False) as client:
                async with streamable_http_client(url,http_client=client) as (reader,writer,_):
                    async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=10)) as session:
                        initialized=await session.initialize()
                        self.assertEqual(initialized.serverInfo.name,'synthetic_cases')
                        tools=await session.list_tools()
                        self.assertEqual({t.name for t in tools.tools},{'search_cases','read_case'})
                        first=await session.call_tool('search_cases',{'query':'EdgeScan'})
                        self.assertFalse(first.isError)
                        self.assertTrue(json.loads(first.content[0].text)['requires_refinement'])
                        second=await session.call_tool('search_cases',{'query':'EdgeScan','filters':{'channel':'pipeline-service-identity'}})
                        self.assertFalse(second.isError)
                        case_id=json.loads(second.content[0].text)['items'][0]['case_id']
                        original=await session.call_tool('read_case',{'case_id':case_id})
                        self.assertFalse(original.isError)
                        body=json.loads(original.content[0].text)
                        self.assertTrue(body['complete'])
                        self.assertEqual(body['source_id'],'synthetic-ci')
                        self.assertIn('read_at',body)
        with fixtures.service(trial,self.runtime) as binding:
            asyncio.run(use_sdk(binding['mcp'][0]['url']))
        rows=[json.loads(line) for line in (trial/'host/retrieval-audit.jsonl').read_text().splitlines()]
        self.assertEqual([r['operation'] for r in rows],['search','search','read'])
        self.assertTrue(all(r['transport']=='mcp' for r in rows))
        self.assertIn('case_original_returned',rows[-1]['stimuli'])

    def test_sdk_access_denial_remains_a_domain_result(self):
        trial=self.prepare('retrieval-denied',fixture_transport='mcp')
        async def use_sdk(url):
            async with httpx.AsyncClient(timeout=10,trust_env=False) as client:
                async with streamable_http_client(url,http_client=client) as (reader,writer,_):
                    async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=10)) as session:
                        await session.initialize()
                        result=await session.call_tool('search_cases',{'query':'EdgeScan'})
                        self.assertFalse(result.isError)
                        self.assertEqual(json.loads(result.content[0].text)['status'],'permission_denied')
        with fixtures.service(trial,self.runtime) as binding:
            asyncio.run(use_sdk(binding['mcp'][0]['url']))
        row=json.loads((trial/'host/retrieval-audit.jsonl').read_text())
        self.assertIn('access_denial_returned',row['stimuli'])


if __name__=='__main__':
    unittest.main()
