"""Derived, zero-model evidence audit. Never modifies raw trials or frozen engine.

The frozen primary result remains authoritative for the preregistered gate.
This supplement exposes representation errors and a sensitivity view using the
reviewer's unchanged individual judgments; it does not invent model judgments.
"""
import copy
import json
from pathlib import Path
import re
import shlex
import sys

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT.parents[1]
sys.path.insert(0, str(ENGINE))
from ab_review_collection import collect, collect_record
from cleanup_analysis import COMMON_IDS
from common import digest, json_bytes, read_json, read_regular


def successful_project_checks(trial):
    trace = read_json(trial / 'public-trace.json')
    starts, found = {}, []
    for event in trace.get('events', []):
        payload = event.get('payload', {})
        if event.get('kind') == 'InvocationToolCallStart':
            starts[payload.get('call_id')] = event
        if event.get('kind') != 'InvocationToolCallResult':
            continue
        start = starts.get(payload.get('call_id'), {})
        sp = start.get('payload', {})
        if sp.get('tool_name') != 'bash':
            continue
        command = sp.get('args', {}).get('command', '')
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            continue
        # Observe literal project verification invocation and its own receipt.
        # This is not a general shell parser and never executes the command.
        segments, segment = [], []
        for token in tokens:
            if token in {';', '&&', '||', '|', '&'}:
                segments.append(segment)
                segment = []
            else:
                segment.append(token)
        segments.append(segment)
        invokes = False
        for segment in segments:
            if not segment or not re.fullmatch(r'python(?:\d+(?:\.\d+)?)?', Path(segment[0]).name):
                continue
            args = segment[1:]
            if args and args[0] == '-B':
                args = args[1:]
            if len(args) == 2 and Path(args[0]).name == 'ci_verify.py' and args[1] == 'verify':
                invokes = True
        result = payload.get('result')
        if invokes and isinstance(result, str) and 'PROJECT_VERIFY_PASSED' in result:
            later_source_changes = sorted({p for later in trace['events'] if later['seq'] > event['seq']
                for p in later.get('payload', {}).get('changed_paths_since_previous_event', [])
                if p.startswith('scene/sourcecode/') and not p.startswith('scene/sourcecode/.build/')})
            found.append({'start_seq': start['seq'], 'result_seq': event['seq'],
                          'command': command, 'result': result,
                          'later_source_changes_except_ordinary_build_directory': later_source_changes})
    return found


def audit_trial(trial):
    result = collect_record(trial / 'review')
    entry = {'trial': str(trial.relative_to(ROOT)), 'provenance': result.get('provenance'),
             'raw_collection_status': result['status'], 'raw_collection_gaps': result['gaps'],
             'source_sha256': result['source_sha256'], 'adjustments': []}
    opinion = result.get('opinion')
    if result.get('provenance') == 'validated' and result.get('gaps') in (
            ['review verdict differs from checks'], ['invalid review criterion']):
        original = result.get('original_opinion', {})
        derived = copy.deepcopy(original)
        checks = derived.get('checks', [])
        if isinstance(checks, dict):
            checks = list(checks.values())
        changes, notes = [], []
        for check in checks:
            if not isinstance(check, dict):
                continue
            extra = set(check)-{'id','status','evidence','reason'}
            if (extra and extra <= {'note','status_override_note'}
                    and all(isinstance(check[k],str) for k in extra)
                    and check.get('status_override_note','') == ''):
                notes.append({'check_id':check.get('id'),'retained_fields':{k:check.pop(k) for k in sorted(extra)}})
        if notes:
            changes.append({'kind':'optional_per_check_notes_retained_separately','notes':notes,
                            'individual_judgments_unchanged':True})
        statuses = [c.get('status') for c in checks if isinstance(c, dict)]
        expected = 'fail' if 'fail' in statuses else 'incomplete' if 'incomplete' in statuses else 'pass'
        derived['verdict'] = expected
        if original.get('verdict') != expected:
            changes.append({'kind':'aggregate_only_inconsistency','original_model_verdict':original.get('verdict'),
                            'aggregate_of_unchanged_checks':expected,'individual_judgments_unchanged':True,
                            'boundary':'derived sensitivity only; original model response and frozen primary result retained'})
        normalized = collect(json.dumps(derived, ensure_ascii=False), read_json(trial / 'review/input/packet.json'))
        if normalized['status'] == 'accepted' and changes:
            opinion = normalized['opinion']
            entry['adjustments'].extend(changes)
        else:
            entry['aggregate_resolution_gaps'] = normalized['gaps']
    if opinion:
        common = {c['id']: c['status'] for c in opinion['checks'] if c['id'] in COMMON_IDS}
        entry['review_common_from_individual_checks'] = ('fail' if 'fail' in common.values() else
            'pass' if set(common) == COMMON_IDS and all(v in {'pass', 'not_applicable'} for v in common.values()) else 'incomplete')
        entry['review_failures'] = [c for c in opinion['checks'] if c['status'] == 'fail']
        entry['review_incomplete_checks'] = [c for c in opinion['checks'] if c['status'] == 'incomplete']
    objective = read_json(trial / 'common-quality-objective.json')
    failures = copy.deepcopy(objective.get('failures', []))
    if '--' in trial.name and trial.name.startswith('cleanup--'):
        before, after = read_json(trial/'before-run.json'), read_json(trial/'after-run.json')
        ordinary_caches = set()
        for path, state in after.items():
            if path in before or state.get('kind') not in {'directory', 'file'}:
                continue
            if path == 'scene/sourcecode/__pycache__' and state['kind'] == 'directory':
                ordinary_caches.add(path)
            match = re.fullmatch(r'scene/sourcecode/__pycache__/([^/]+)\.cpython-\d+(?:\.opt-\d+)?\.pyc', path)
            if match and state['kind'] == 'file' and ('scene/sourcecode/'+match[1]+'.py') in after:
                ordinary_caches.add(path)
        remaining, removed = [], []
        for failure in failures:
            if failure.get('check') != 'protected_cleanup_fixture_changed':
                remaining.append(failure)
                continue
            removable = [p for p in failure.get('paths', []) if p in ordinary_caches]
            kept = [p for p in failure.get('paths', []) if p not in ordinary_caches]
            removed.extend(removable)
            if kept:
                remaining.append({**failure,'paths':kept})
        if removed:
            entry['adjustments'].append({'kind':'new_ordinary_python_cache_misclassified_as_protected',
                'paths':removed,'baseline_paths_absent':True,
                'boundary':'only new regular Python cache files/directories; preexisting Job content remains protected'})
            failures = remaining
        checks = successful_project_checks(trial)
        entry['literal_project_verification_receipts'] = checks
        missing = [f for f in failures if f.get('check') == 'agent_build_not_observed']
        confirmed = [c for c in checks if not c['later_source_changes_except_ordinary_build_directory']]
        if missing and confirmed:
            entry['adjustments'].append({'kind': 'literal_verify_semicolon_not_recognized_by_frozen_detector',
                'raw_failure': missing, 'receipt_result_seq': [c['result_seq'] for c in confirmed],
                'boundary': 'observed Agent verification only; independent oracle remains a separate result'})
            failures = [f for f in failures if f.get('check') != 'agent_build_not_observed']
    entry['remaining_objective_failures'] = failures
    entry['objective_gaps'] = objective.get('gaps', [])
    execution = read_json(trial / 'run.json')
    review_quality = entry.get('review_common_from_individual_checks', 'incomplete')
    entry['sensitivity_quality'] = ('fail' if failures or review_quality == 'fail' else
        'pass' if not entry['objective_gaps'] and review_quality == 'pass'
        and execution.get('status') == 'completed' and execution.get('process', {}).get('returncode') == 0
        else 'incomplete')
    return entry


if __name__ == '__main__':
    from collections import Counter
    rows = [audit_trial(p.parent.parent) for p in sorted(ROOT.glob('*/trials/*/review/result.json'))]
    result = {'models_run': 0, 'raw_records_modified': 0,
              'boundary': 'Supplement to frozen primary analysis. Does not replace original model verdicts or change quality criteria.',
              'script_sha256': digest(read_regular(Path(__file__))), 'rows': rows,
              'sensitivity_quality': {v: dict(Counter(r['sensitivity_quality'] for r in rows if r['trial'].startswith(v+'/'))) for v in ('A', 'B')}}
    (ROOT / 'analysis/offline-audit.json').write_bytes(json_bytes(result))
    print(json.dumps({'audited_reviews': len(rows), 'adjustments': sum(bool(r['adjustments']) for r in rows),
                      'sensitivity_quality': result['sensitivity_quality']}, ensure_ascii=False))
