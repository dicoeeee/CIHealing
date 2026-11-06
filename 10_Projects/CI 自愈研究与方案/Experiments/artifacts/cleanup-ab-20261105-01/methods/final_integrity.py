"""Read-only completion/preservation audit; run only after the queue finishes."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT.parents[1]
sys.path.insert(0, str(ENGINE))
from cleanup_ab import verify_freeze
from native_repair import tool_identity
from common import digest, json_bytes, read_json, read_regular
from suite import bundle_hash, skill_from_dir


def main():
    errors = []
    config = verify_freeze(ROOT)
    expected_tools = read_json(ROOT/'freeze.json')['tools']
    observed_tools = tool_identity(config)
    tool_differences = {k:{'expected':expected_tools.get(k),'observed':observed_tools.get(k)}
                        for k in set(expected_tools)|set(observed_tools) if expected_tools.get(k)!=observed_tools.get(k)}
    if tool_differences:
        errors.append('toolchain or Maven cache drift')
    plan = read_json(ROOT / 'plan.json')
    progress = [json.loads(line) for line in (ROOT/'progress.jsonl').read_text().splitlines()]
    phase_counts = Counter((p['phase'], p['trial']) for p in progress)
    expected_phases = {(phase, v+'/'+row['id']) for phase in ('execute','review') for v in ('A','B') for row in plan['rows']}
    phase_problems = {'missing':sorted(expected_phases-set(phase_counts)),
                      'extra':sorted(set(phase_counts)-expected_phases),
                      'duplicate':[(list(k),count) for k,count in phase_counts.items() if count!=1]}
    if any(phase_problems.values()):
        errors.append('missing or duplicate phase completion records')
    history = read_json(ROOT / 'historical-preservation.json')
    old = Path(history['root'])
    current = {}
    for parent, dirs, files in os.walk(old):
        for name in files:
            p = Path(parent) / name
            s = p.lstat()
            current[str(p.relative_to(old))] = [s.st_size, s.st_mtime_ns, s.st_mode]
    changed = sorted(k for k in set(current) | set(history['files']) if current.get(k) != history['files'].get(k))
    critical_changed = [k for k, sha in history['critical_hashes'].items() if digest(read_regular(old/k)) != sha]
    if changed or critical_changed:
        errors.append('historical preservation mismatch')
    skill_hashes = {}
    for version, source in plan['versions'].items():
        source_hash = bundle_hash(skill_from_dir(Path(source['source'])))
        frozen_hash = bundle_hash(skill_from_dir(ROOT/'skills'/version))
        skill_hashes[version] = {'source': source_hash, 'frozen': frozen_hash, 'expected': source['sha256']}
        if source_hash != source['sha256'] or frozen_hash != source['sha256']:
            errors.append('Skill changed: '+version)
    active, peaks = {}, {'total': 0, 'execute': 0, 'review': 0, 'execute_A': 0, 'execute_B': 0}
    events = [json.loads(line) for line in (ROOT/'icode-concurrency.jsonl').read_text().splitlines()]
    violations = []
    for event in events:
        token = event['token']
        if event['event'] == 'acquired':
            if token in active:
                violations.append('duplicate lease: '+token)
            active[token] = event
        else:
            if token not in active:
                violations.append('release without acquire: '+token)
            active.pop(token, None)
        counts = {'total': len(active),
                  'execute': sum(x['phase']=='execute' for x in active.values()),
                  'review': sum(x['phase']=='review' for x in active.values()),
                  **{'execute_'+v: sum(x['phase']=='execute' and x['version']==v for x in active.values()) for v in ('A','B')}}
        for name, value in counts.items():
            peaks[name] = max(peaks[name], value)
            if value > {'total':6,'execute':4,'review':2,'execute_A':2,'execute_B':2}[name]:
                violations.append(name+' exceeded')
    if active or violations:
        errors.append('lease accounting problem')
    shared = read_json(ENGINE/'runs/.icode-global/state.json')
    remaining = [v for v in shared['leases'].values() if v['root']==str(ROOT)]
    if remaining:
        errors.append('active experiment leases remain')
    rows = []
    for version in ('A','B'):
        for row in plan['rows']:
            trial = ROOT/version/'trials'/row['id']
            item = {'version':version,'id':row['id']}
            for phase, receipt, start in [('execute','run.json','execution-start.json'),('review','review/result.json','review-start.json')]:
                if not (trial/receipt).is_file() or not (trial/start).is_file():
                    errors.append('missing '+phase+' receipt/start: '+version+'/'+row['id'])
                    continue
                data = read_json(trial/receipt)
                process = data.get('process',{})
                item[phase] = {'status':data.get('status'),'verdict':data.get('verdict'),
                    'process_status':process.get('status'),'returncode':process.get('returncode'),
                    'seconds':process.get('duration_seconds'),'started_at':process.get('started_at'),
                    'ended_at':process.get('ended_at'),'termination':process.get('termination',{}).get('reason'),
                    'cleanup':process.get('cleanup'), 'shutdown':process.get('shutdown')}
                if process.get('cleanup',{}).get('status') != 'confirmed':
                    errors.append('process cleanup unconfirmed: '+version+'/'+row['id']+'/'+phase)
                if phase == 'execute':
                    item['execute']['profile_sha256'] = data.get('profile_sha256')
                    item['execute']['expected_model_id'] = data.get('expected_model_id')
                    if data.get('expected_model_id') != plan['executor']:
                        errors.append('executor identity mismatch')
                else:
                    exchange = read_json(trial/'review/exchange-receipt.json')
                    item['review']['model_receipt'] = exchange
            rows.append(item)
    # The raw evidence manifest contains hashes only, never credential content.
    manifest = {}
    for version in ('A','B'):
        for parent, dirs, files in os.walk(ROOT/version):
            for name in files:
                path = Path(parent)/name
                key = str(path.relative_to(ROOT))
                if path.is_symlink():
                    manifest[key] = {'symlink': os.readlink(path)}
                else:
                    body = read_regular(path, limit=128*1024*1024)
                    manifest[key] = {'bytes':len(body),'sha256':digest(body)}
    (ROOT/'analysis/raw-evidence-manifest.json').write_bytes(json_bytes(manifest))
    report = {'checked_at':datetime.now(timezone.utc).isoformat(),'errors':errors,
              'historical':{'files':len(current),'metadata_changes':changed,'critical_hash_changes':critical_changed,
                            'boundary':'all historical files compared by size/mtime/mode; six critical files also SHA256 checked'},
              'skill_hashes':skill_hashes,'toolchain_differences':tool_differences,
              'concurrency':{'peaks':peaks,'events':len(events),'violations':violations,
                  'unreleased':list(active),'remaining_experiment_leases':remaining,'external_processes_excluded':True},
              'phase_records':{'count':len(progress),'problems':phase_problems},
              'trials':rows,'raw_evidence_files':len(manifest),'source_sha256':digest(read_regular(Path(__file__)))}
    (ROOT/'analysis/final-integrity.json').write_bytes(json_bytes(report))
    print(json.dumps({'errors':errors,'trials':len(rows),'historical_files':len(current),'peaks':peaks,
                      'raw_evidence_files':len(manifest)},ensure_ascii=False))


if __name__ == '__main__':
    main()
