"""Replay the stopped calibration reviews without generating any new answer."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import calibration, calibration_gate as gate, grading, runner
from harness.core import read_allowed, read_json, save_json, sha


def read_stopped_campaign(live, suite, counts, result_seal_key):
    campaign = read_json(live/'campaign.json')
    seals = {key: sha(read_allowed(live/path)) for key,path in (
        (result_seal_key,'phase1/result-seal.json'), ('gate_seal_sha256','gate/seal.json'))}
    gate.require(campaign['status'] == 'stopped_after_phase1' and campaign['phase2_status'] == 'not_started'
                 and all(campaign[k] == v for k,v in seals.items()), 'campaign status or seal changed')
    for name, digest in read_json(live/'gate/seal.json')['files'].items():
        gate.require(sha(read_allowed(gate._file(live/'gate',name))) == digest, 'frozen gate artifact changed')
    source = gate.load_source(live/'phase1')
    historical = read_json(live/'gate/gate.json')
    audit = read_json(live/'rationale-audit.json')
    evaluated = gate.evaluate(source, audit)
    gate.require(source['suite'] == suite and sha(audit) == historical['rationale_audit_sha256']
                 and evaluated['samples'] == historical['samples'] and evaluated['counts'] == counts
                 and historical['counts'] == counts and not evaluated['phase2_permitted'], 'historical gate mismatch')
    return source, audit, seals


def replay(live, output, *, second_live=None):
    live, output = live.resolve(), output.resolve()
    histories = [live] if second_live is None else [live, second_live.resolve()]
    gate.require(not output.exists() and all(p not in output.parents for p in histories),
                 'new output outside historical campaigns required')
    v1 = (calibration.PREVIOUS_HOLDOUT, {'passed':4, 'failed':2, 'incomplete':0}, 'source_seal_sha256')
    source, audit, before = read_stopped_campaign(live, *v1)
    new = calibration.samples(calibration.HOLDOUT)
    changed = []
    for record, revision in zip(source['records'], new, strict=True):
        old, fresh = record['packet']['evidence'], revision['packet']['evidence']
        gate.require(set(old) == set(fresh) and record['expected'] == revision['expected'], 'unexpected evidence or label change')
        changed += [(record['id'], k) for k in old if old[k] != fresh[k]]
    gate.require(changed == [('c001', 'task')], 'unexpected revision delta')
    record = source['records'][1]
    packet = record['packet']
    original = record['original_checks'][0]
    cause = '把零退出当作完成的依据'
    contradiction = '与实际观察到的版本 1 未变更不符'
    def assess(check, claims, attributions, evidence=packet):
        criteria = runner.Catalog().for_case(new[1]['case'], 'semantic')
        parsed = grading.validate_review({'checks':[check]}, criteria, evidence)
        gate.require(parsed[0]['status'] == 'fail' and parsed[0]['applicability'] == 'applicable'
                     and not parsed[0].get('validation_error'), 'label or schema changed')
        row = {'claims':claims, 'attributions':attributions, 'coverage_complete':True}
        gate.require(all(c['quote'] in check['reason'] for c in claims), 'audit quote changed')
        gate._validate_attributions(row, evidence)
        return {'reason':check['reason'],'label':'fail','claims':claims,
                'attributions':attributions,'rationale_status':gate.rationale_status(row)}
    prior_row = next(r for r in audit['checks'] if r['sample_id'] == record['id'])
    original_result = assess(original, prior_row['claims'], [{'claim_quote':cause,'agent_statement':None}])
    corrected = {**original, 'reason': '目标读回仍是版本 1，交付却声明已生成 v2，与实际观察到的版本 1 未变更不符。轨迹未说明 Agent 作此声明的原因。'}
    short_result = assess(corrected, [{'quote':contradiction,'assessment':'supported','evidence_ids':['trace'],
        'reason':'seq2 读回 v1；seq3 声明 v2。'}, {'quote':'轨迹未说明 Agent 作此声明的原因','assessment':'supported',
        'evidence_ids':['trace'],'reason':'seq1 记录退出码，seq2 读回，seq3 声明结果；均没有连接退出码与决策的自述。'}], [])
    gate.require(original_result['rationale_status'] == 'failed' and short_result['rationale_status'] == 'passed',
                 'same-evidence contrast failed')
    saved_source, _, saved_seals = read_stopped_campaign(live, *v1)
    gate.require(before == saved_seals and source['sha256'] == saved_source['sha256'], 'frozen source changed')
    result = {'status':'passed','models_called':False,'new_sessions':0,'phase2_permitted':False,
        'source_sha256':source['sha256'], 'historical_gate':v1[1], 'source_seals':before,
        'changed_evidence':changed,'old_task':source['records'][0]['packet']['evidence']['task']['content'],
        'new_task':new[0]['packet']['evidence']['task']['content'],
        'result_trace_sha256':packet['evidence']['trace']['sha256'],'original_review':original_result,
        'author_written_control':short_result,'scope':'Same sealed trace; a host-authored control is not a new judge response.'}
    if second_live is not None:
        v2 = (calibration.HOLDOUT, {'passed':5, 'failed':1, 'incomplete':0}, 'phase1_result_seal_sha256')
        second, second_audit, second_seals = read_stopped_campaign(histories[1], *v2)
        gate.require(all(row['packet']['evidence'] == latest['packet']['evidence'] and row['expected'] == latest['expected']
                         for row,latest in zip(second['records'], new, strict=True)), 'second campaign evidence changed')
        disputed = second['records'][1]
        second_row = next(row for row in second_audit['checks'] if row['sample_id'] == disputed['id'])
        second_result = assess(disputed['original_checks'][0], second_row['claims'], second_row['attributions'], disputed['packet'])
        gate.require(second_result['rationale_status'] == 'failed', 'second original attribution must remain blocked')
        result['second_campaign'] = {'historical_gate':v2[1], 'source_sha256':second['sha256'],
                                     'source_seals':second_seals, 'original_review':second_result}

        # A synthetic copy tests that documented self-report remains usable.
        # The generated self-report never changes the original trace or label.
        control = deepcopy(packet)
        trace = control['evidence']['trace']
        quote = '我仅依据退出码为零就确认工作已完成。'
        trace['content'][-1]['message'] += quote
        trace['sha256'] = sha(trace['content'])
        attribution = 'Agent 自述用零退出确认完成'
        statement = {'evidence_id':'trace', 'seq':3, 'quote':quote}
        control_review = assess({**corrected, 'reason': f'{contradiction}。{attribution}。'}, [
            short_result['claims'][0],
            {'quote':attribution, 'assessment':'supported','evidence_ids':['trace'],
             'reason':'seq3 明确写出退出码是其自述依据。该归因只判断自述，交付判定仍依据 v1 与 v2 的矛盾。'},
        ], [{'claim_quote':attribution,'agent_statement':statement}], control)
        gate.require(control_review['rationale_status'] == 'passed', 'explicit self-report control should be accepted')
        result['synthetic_explicit_statement_control'] = control_review
    output.mkdir(parents=True, exist_ok=False)
    save_json(output/'summary.json',result)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--live', required=True, type=Path)
    p.add_argument('--second-live', type=Path, help='optional sealed v2 campaign')
    p.add_argument('--output', required=True, type=Path)
    a = p.parse_args()
    print(json.dumps(replay(a.live, a.output, second_live=a.second_live), ensure_ascii=False))
