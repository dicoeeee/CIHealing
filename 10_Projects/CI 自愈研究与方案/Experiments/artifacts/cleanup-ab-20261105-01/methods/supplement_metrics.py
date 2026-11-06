"""Supplemental cost and sensitivity tables from completed, frozen evidence."""
from collections import Counter
import csv
import io
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[1]


def stats(values):
    values = [v for v in values if v is not None]
    return {'n':len(values),'sum':sum(values) if values else None,'median':statistics.median(values) if values else None,
            'min':min(values) if values else None,'max':max(values) if values else None}


def main():
    raw = json.loads((ROOT/'analysis/summary.json').read_text())
    audit = json.loads((ROOT/'analysis/offline-audit.json').read_text())
    audited = {x['trial']: x for x in audit['rows']}
    rows = []
    for row in raw['rows']:
        key = row['version']+'/trials/'+row['id']
        record = audited.get(key,{})
        rows.append({**row,'sensitivity_quality':record.get('sensitivity_quality','incomplete')})
    primary = {}
    pairs = []
    for pair in raw['pairs']:
        versions = {r['version']:r for r in rows if r['id']==pair['id']}
        a,b = versions['A'],versions['B']
        eligible = (a['sensitivity_quality']==b['sensitivity_quality']=='pass' and
                    all(x['seconds'] is not None and x['execute_tokens'] is not None for x in (a,b)))
        pairs.append({**pair,'A_sensitivity_quality':a['sensitivity_quality'],
                      'B_sensitivity_quality':b['sensitivity_quality'],'sensitivity_eligible':eligible,
                      'sensitivity_time_reduction':(a['seconds']-b['seconds'])/a['seconds'] if eligible else None,
                      'sensitivity_token_reduction':(a['execute_tokens']-b['execute_tokens'])/a['execute_tokens'] if eligible else None})
    for mode in ('frozen','sensitivity'):
        key = 'eligible' if mode=='frozen' else 'sensitivity_eligible'
        pre = '' if mode=='frozen' else 'sensitivity_'
        primary[mode] = {}
        for case in ('case-001','case-002'):
            selected = [p for p in pairs if p['scenario']==case and p[key]]
            primary[mode][case] = {'eligible_pairs':len(selected),
                'time_reduction':stats(p[pre+'time_reduction'] for p in selected),
                'token_reduction':stats(p[pre+'token_reduction'] for p in selected),
                'A_seconds':stats(p['A_seconds'] for p in selected),'B_seconds':stats(p['B_seconds'] for p in selected),
                'A_tokens':stats(p['A_tokens'] for p in selected),'B_tokens':stats(p['B_tokens'] for p in selected)}
        cases = list(primary[mode].values())
        primary[mode]['equal_case_weighted_estimator'] = {
            'minimum_pairs_met':all(c['eligible_pairs']>=3 for c in cases),
            **{metric:statistics.mean(c[metric]['median'] for c in cases) if all(c[metric]['median'] is not None for c in cases) else None
               for metric in ('time_reduction','token_reduction')}}
    totals = {}
    for v in ('A','B'):
        selected = [r for r in rows if r['version']==v]
        totals[v] = {'n':len(selected),'frozen_quality':dict(Counter(r['quality'] for r in selected)),
            'sensitivity_quality':dict(Counter(r['sensitivity_quality'] for r in selected)),
            **{k:stats(r[k] for r in selected) for k in ('seconds','execute_tokens','review_seconds','review_tokens')},
            'execute_token_subfields':{k:stats(r['execute_usage']['normalized_tokens'][k]['sum'] for r in selected)
                for k in ('input_total','output_total','cache_read','reasoning')},
            'model_identities':list({(m['request_model'],m['response_model']) for r in selected for m in r['execute_usage']['models']}),
            'retired_workspace_script_calls':sum(r['phases']['retired_workspace_script_calls'] for r in selected)}
    result = {'boundary':'Frozen primary and offline sensitivity remain separate. No model reruns; no raw trial changes.',
              'primary':primary,'totals':totals,'pairs':pairs,
              'A_pass_B_not_pass':{
                  'frozen':[p['id'] for p in pairs if p['A_quality']=='pass' and p['B_quality']!='pass'],
                  'sensitivity':[p['id'] for p in pairs if p['A_sensitivity_quality']=='pass' and p['B_sensitivity_quality']!='pass']},
              'B_pass_A_not_pass':{
                  'frozen':[p['id'] for p in pairs if p['B_quality']=='pass' and p['A_quality']!='pass'],
                  'sensitivity':[p['id'] for p in pairs if p['B_sensitivity_quality']=='pass' and p['A_sensitivity_quality']!='pass']},
              'adjustment_trials':[r['trial'] for r in audit['rows'] if r['adjustments']],
              'adjustment_types':dict(Counter(a['kind'] for r in audit['rows'] for a in r['adjustments']))}
    (ROOT/'analysis/supplement-metrics.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    stream=io.StringIO()
    fields=['id','group','scenario','repeat','A_quality','B_quality','eligible','A_sensitivity_quality','B_sensitivity_quality',
            'sensitivity_eligible','A_seconds','B_seconds','A_tokens','B_tokens','time_reduction','token_reduction',
            'sensitivity_time_reduction','sensitivity_token_reduction','regression']
    writer=csv.DictWriter(stream,fields,extrasaction='ignore');writer.writeheader();writer.writerows(pairs)
    (ROOT/'analysis/paired-comparison-with-audit.csv').write_text(stream.getvalue())
    print(json.dumps({'primary':primary,'totals':totals,'adjustment_types':result['adjustment_types']},ensure_ascii=False))


if __name__ == '__main__':
    main()
