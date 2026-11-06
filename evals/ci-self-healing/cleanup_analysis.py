"""Read-only reduction of cleanup A/B raw evidence; derived reports are separate."""
from collections import Counter
import csv
import io
import json
from pathlib import Path
import statistics

from common import digest, read_json, read_regular, replace_json
from ab_compare import usage_metadata, resource_metrics, distribution
from ab_review import CRITERIA, MODEL_ID, RESPONSE_MODEL_ID
from skill_execution import is_build
import cleanup_cases

COMMON_IDS = set(CRITERIA) - {"loading_order"}


def optional(path):
    return read_json(path) if path.is_file() else {}


def token_total(usage):
    fields = usage['normalized_tokens']
    complete = (not usage['gaps'] and usage['native_exchange_finishes'] > 0 and all(
        fields[k]['observed_exchanges'] == usage['native_exchange_finishes'] for k in ('input_total', 'output_total')))
    return sum(fields[k]['sum'] for k in ('input_total', 'output_total')) if complete else None


def phase_metrics(trace, group):
    calls, passed = {}, []
    tools = Counter(); retired = 0; model_times = []
    for e in trace.get('events', []):
        p = e.get('payload', {}); kind = e.get('kind')
        if kind == 'InvocationToolCallStart':
            calls[p.get('call_id')] = e; tools[p.get('tool_name', 'unknown')] += 1
            args = p.get('args', {})
            if 'workspace_check.py' in str(args.get('script_name', '')): retired += 1
        if kind == 'transport.body_finished' and isinstance(p.get('elapsed_seconds'), (float, int)):
            model_times.append(p['elapsed_seconds'])
        if kind == 'InvocationToolCallResult':
            start = calls.get(p.get('call_id'), {})
            detect = cleanup_cases.build_detector if group == 'cleanup' else is_build
            if detect(start.get('payload', {})) and isinstance(p.get('result'), str):
                text = p['result']
                if 'BUILD SUCCESS' in text or 'PROJECT_VERIFY_PASSED' in text:
                    passed.append({'seq': e['seq'], 'observed_ns': e.get('observed_ns')})
    events = trace.get('events', [])
    end = events[-1].get('observed_ns') if events else None
    last = passed[-1] if passed else {}
    tail = ((end-last['observed_ns'])/1e9 if end and last.get('observed_ns') else None)
    return {**resource_metrics(trace), 'tool_calls': dict(tools), 'retired_workspace_script_calls': retired,
            'successful_build_observations': passed, 'last_successful_build_to_observer_end_seconds': tail,
            'model_response_durations': distribution(model_times),
            'boundary': 'last observed successful build is a timing landmark, not proof that all required validation was complete; tool durations may overlap'}


def record(root, version, row):
    trial = root / version / 'trials' / row['id']
    run = optional(trial/'run.json'); raw = optional(trial/'objective.json')
    common = optional(trial/'common-quality-objective.json'); review = optional(trial/'review/result.json')
    verdict = optional(trial/'review/verdict.json'); exchange = optional(trial/'review/exchange-receipt.json')
    collection = optional(trial/'review/collection.json')
    trace = optional(trial/'public-trace.json'); process = run.get('process', {})
    review_valid = False
    # Collection digests use canonical JSON, not raw pretty-printed file bytes.
    if collection:
        from common import json_bytes
        review_valid = (review.get('status') == 'completed' and exchange.get('status') == 'validated'
                        and exchange.get('request_model') == MODEL_ID and exchange.get('response_model') == RESPONSE_MODEL_ID
                        and review.get('collection_sha256') == digest(json_bytes(collection)))
    checks = verdict.get('checks', [])
    schema_ok = len(checks) == len(CRITERIA) and {c.get('id') for c in checks} == set(CRITERIA)
    semantic = {c['id']: c['status'] for c in checks if c.get('id') in COMMON_IDS} if schema_ok and review_valid else {}
    failed = [k for k, value in semantic.items() if value == 'fail']
    complete = run.get('status') == 'completed' and process.get('returncode') == 0
    quality = 'fail' if common.get('failures') or failed else 'pass' if (
        complete and common.get('status') == 'pass' and len(semantic) == len(COMMON_IDS)
        and all(v in {'pass', 'not_applicable'} for v in semantic.values())) else 'incomplete'
    execution_usage = usage_metadata(trial, 'execute')
    review_usage = usage_metadata(trial, 'review')
    started = (trial/'execution-start.json').exists()
    return {**row, 'version': version, 'trial': str(trial), 'started': started,
            'execution_status': run.get('status', 'started_without_receipt' if started else 'not_started'),
            'quality': quality, 'common_failures': common.get('failures', []), 'common_gaps': common.get('gaps', []),
            'semantic_failures': failed, 'review_valid': bool(review_valid), 'review_checks': checks,
            'conformance': raw.get('status', 'not_assessed'), 'conformance_failures': raw.get('failures', []),
            'seconds': process.get('duration_seconds'), 'review_seconds': review.get('process', {}).get('duration_seconds'),
            'execute_tokens': token_total(execution_usage), 'review_tokens': token_total(review_usage),
            'execute_usage': execution_usage, 'review_usage': review_usage, 'phases': phase_metrics(trace, row['group'])}


def compare(root):
    root = Path(root).resolve(); plan = read_json(root/'plan.json')
    rows = [record(root, v, r) for r in plan['rows'] for v in ('A', 'B')]
    pairs = []
    for r in plan['rows']:
        a, b = [x for x in rows if x['id'] == r['id']]
        eligible = a['quality'] == b['quality'] == 'pass' and all(x['seconds'] is not None and x['execute_tokens'] is not None for x in (a,b))
        pairs.append({**r, 'A_quality': a['quality'], 'B_quality': b['quality'], 'eligible': eligible,
            'A_seconds': a['seconds'], 'B_seconds': b['seconds'], 'A_tokens': a['execute_tokens'], 'B_tokens': b['execute_tokens'],
            'time_reduction': (a['seconds']-b['seconds'])/a['seconds'] if eligible and a['seconds'] else None,
            'token_reduction': (a['execute_tokens']-b['execute_tokens'])/a['execute_tokens'] if eligible and a['execute_tokens'] else None,
            'regression': a['quality'] == 'pass' and b['quality'] == 'fail'})
    groups = {}
    for version in ('A','B'):
        groups[version] = {}
        for group in ('all', 'repair-primary', 'main', 'handoff', 'keyword', 'enterprise', 'cleanup'):
            subset = [r for r in rows if r['version']==version and (group=='all' or r['group']==group or
                group=='repair-primary' and r['group']=='main' and r['scenario'] in {'case-001','case-002'})]
            passing = sum(r['quality']=='pass' for r in subset)
            observed = [r['execute_tokens'] for r in subset if r['execute_tokens'] is not None]
            groups[version][group] = {'trials':len(subset), 'started':sum(r['started'] for r in subset),
                'quality':dict(Counter(r['quality'] for r in subset)), 'execution':dict(Counter(r['execution_status'] for r in subset)),
                'seconds':distribution(r['seconds'] for r in subset), 'tokens':distribution(observed),
                'review_seconds':distribution(r['review_seconds'] for r in subset),
                'review_tokens':distribution(r['review_tokens'] for r in subset),
                'tokens_per_quality_pass':sum(observed)/passing if passing and len(observed)==len(subset) else None}
    primary = {}
    for case in ('case-001', 'case-002'):
        valid = [p for p in pairs if p['scenario']==case and p['eligible']]
        primary[case] = {'eligible_pairs':len(valid), 'time_reduction':distribution(p['time_reduction'] for p in valid),
                         'token_reduction':distribution(p['token_reduction'] for p in valid)}
    # Gains can be negative; distribution from the historical reducer ignores negatives.
    for case, stats in primary.items():
        valid=[p for p in pairs if p['scenario']==case and p['eligible']]
        for metric in ('time_reduction','token_reduction'):
            values=[p[metric] for p in valid]
            stats[metric]={'observed':len(values),'median':statistics.median(values) if values else None,
                           'min':min(values) if values else None,'max':max(values) if values else None}
    samples = []
    path=root/'process-concurrency.jsonl'
    if path.exists(): samples=[json.loads(s) for s in path.read_text().splitlines()]
    return {'contract':plan['contract'], 'boundary':plan['boundary'], 'rows':rows,'pairs':pairs,'groups':groups,'primary':primary,
            'regressions':[p for p in pairs if p['regression']],
            'concurrency':{'samples':len(samples),'max_total_reserved':max((x['total_reserved'] for x in samples),default=None),
                           'violations':[x for x in samples if x['total_reserved']>6]}}


def write_outputs(root, value):
    root=Path(root); out=root/'analysis';out.mkdir(exist_ok=True)
    replace_json(out/'summary.json',value)
    replace_json(out/'quality-regressions.json',{'pairs':value['regressions'],
        'all_B_failures':[r for r in value['rows'] if r['version']=='B' and r['quality']=='fail'],
        'boundary':'review findings require evidence adjudication; historical raw results are not overwritten'})
    stream=io.StringIO();fields=['id','group','scenario','repeat','A_quality','B_quality','eligible','A_seconds','B_seconds','A_tokens','B_tokens','time_reduction','token_reduction','regression']
    writer=csv.DictWriter(stream,fields,extrasaction='ignore');writer.writeheader();writer.writerows(value['pairs'])
    (out/'paired-comparison.csv').write_text(stream.getvalue())
    lines=['---','ai_generated: true','reviewed: false','---','','# 清理简化前后 A/B 对比','',value['boundary'],'',
           '## 主性能组','','| 场景 | 合格完整配对 | 时长配对降幅中位数 | Token 配对降幅中位数 |','| --- | ---: | ---: | ---: |']
    def pct(x):return f'{x:.1%}' if x is not None else '未取得'
    for case,s in value['primary'].items():lines.append(f"| {case} | {s['eligible_pairs']} | {pct(s['time_reduction']['median'])} | {pct(s['token_reduction']['median'])} |")
    lines+=['','## 分层质量与成本','','| 版本/组 | 样本 | 共同质量 | 执行秒中位数 | 执行 token 中位数 |','| --- | ---: | --- | ---: | ---: |']
    for v,groups in value['groups'].items():
        for group,s in groups.items():lines.append(f"| {v}/{group} | {s['trials']} | {json.dumps(s['quality'])} | {s['seconds']['median']} | {s['tokens']['median']} |")
    lines+=['','质量回归配对数：'+str(len(value['regressions']))+'；需要逐项核对原始证据。',
            '总并发观测：'+json.dumps(value['concurrency'],ensure_ascii=False),'',
            '失败、超时、未完成和 usage 缺口均保留。性能配对只选两版共同质量合格且指标完整的运行，另列全部试次成本，不能据此隐去质量退化。',
            '旧版流程合规与共同质量分别保存；原生清理门槛不用于共同质量。独立宿主 Oracle 不能冒充 Agent 已完成验证。',
            '阶段时间中的最后一次成功构建仅为观测标记，不单独证明全部必要验证完成。字符数、缓存和 reasoning 不重复折算为总 token。','']
    (out/'report.md').write_text('\n'.join(lines))


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args()
    value=compare(a.root);write_outputs(a.root,value)
    print(json.dumps({'quality':{v:x['all']['quality'] for v,x in value['groups'].items()},'primary':value['primary'],'concurrency':value['concurrency']},ensure_ascii=False))
