"""New mechanical checks over frozen Q19 and Java records; zero model calls."""
import argparse
from pathlib import Path
import sys

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
from harness import calibration, calibration_gate, command_checks, runner
from harness.catalog import Catalog
from harness.core import EvaluationError, read_allowed, read_json, save_json, sha
from replay_review_protocol import verify_source


def require(condition, reason):
    if not condition:
        raise EvaluationError(reason)


def replay(output):
    output = Path(output).resolve()
    q19 = ENGINE / 'runs/q19-two-stage-20261117'
    java = ENGINE / 'runs/q17-two-stage-20261106/phase2-live'
    require(not output.exists(), 'new output directory required')
    require(q19 not in output.parents and java not in output.parents, 'output must be outside historical sources')
    before = verify_source(q19)
    campaign_digest = sha(read_allowed(q19 / 'campaign.json'))
    source = calibration_gate.load_source(q19 / 'phase1')
    old_audit = read_json(ENGINE / 'fixtures/calibration/command-control-v1/q19-rationale-audit.json')
    prior_gate = calibration_gate.evaluate(source, old_audit)
    require(prior_gate['counts'] == {'passed': 2, 'failed': 2, 'incomplete': 2}, 'historical Q20 interpretation changed')
    catalog = Catalog()
    implementation = runner.implementation_identity()
    output.mkdir(parents=True)
    save_json(output / 'implementation.json', implementation)
    runner.freeze_implementation(output / 'implementation', implementation)
    runner.freeze_tree(catalog.directory, output / 'spec')
    save_json(output / 'source.json', {'q19': str(q19), 'q19_seal': before, 'campaign_sha256': campaign_digest})
    labels = calibration.samples('command-control-split-v1')
    expected = ['pass', 'fail', 'pass', 'fail', 'pass', 'pass']
    records = []
    for item, label, expected_state in zip(source['records'], labels, expected):
        semantic = item['criteria'][0]['id']
        mechanical = command_checks.check(catalog.criteria[command_checks.PAIRS[semantic]], item['packet'])
        facts = command_checks.facts(item['packet'])
        require(mechanical['status'] == expected_state, 'mechanical frozen result mismatch: ' + item['id'])
        require(item['packet'] == label['packet'], 'new calibration changed frozen observations')
        row = {'id': item['id'], 'mechanical': mechanical, 'facts': facts,
               'new_semantic_expected': label['expected'], 'new_semantic_review': 'not_run',
               'original_answer_sha256': sha(item['answer'].encode()),
               'original_assessment': item['checks'],
               'boundary': 'New semantic expectations are host labels, never replacement model judgments.'}
        save_json(output / item['id'] / 'assessment.json', row)
        records.append(row)
    require(all(row['facts']['effects'][0]['target_matches_expected'] is False for row in records[4:]),
            'effect mismatch must remain separate from observation sufficiency')
    java_before = runner.report(java)
    java_seal = sha(read_allowed(java / 'seal.json'))
    native_rows = []
    for trial in java_before['trials']:
        packet_path = java / 'trials' / trial['id'] / 'host/packet.json'
        packet = read_json(packet_path)
        checks = [command_checks.check(catalog.criteria[key], packet) for key in command_checks.PAIRS.values()]
        require(checks[1]['status'] == checks[2]['status'] == 'inconclusive', 'native missing receipts were promoted to facts')
        native_rows.append({'id': trial['id'], 'case_id': trial['case_id'],
            'packet_sha256': sha(read_allowed(packet_path)), 'checks': checks,
            'scope': 'Additional observation check only; old frozen criteria and 55 original assessments are not replaced.'})
    save_json(output / 'java-observations.json', native_rows)
    save_json(output / 'historical-gate.json', prior_gate)
    require(verify_source(q19) == before and sha(read_allowed(q19 / 'campaign.json')) == campaign_digest,
            'Q19 source changed')
    # report() recalculates its rendering timestamp; source integrity is sealed.
    java_after = runner.report(java)
    require({k: v for k, v in java_after.items() if k != 'created_at'} ==
            {k: v for k, v in java_before.items() if k != 'created_at'} and sha(read_allowed(java / 'seal.json')) == java_seal,
            'Java source changed')
    require(runner.implementation_identity() == implementation, 'implementation changed during replay')
    summary = {'status': 'passed', 'models_called': False, 'model_sessions_added': 0,
        'scope': 'mechanical command split regression; not live judge calibration or Skill acceptance',
        'criteria': len(catalog.criteria), 'cases': len(catalog.cases),
        'q19_mechanical_counts': {'pass': 4, 'fail': 2},
        'q19_original_gate_counts': prior_gate['counts'], 'phase2_permitted': prior_gate['phase2_permitted'],
        'java_trials': len(native_rows), 'java_original_counts': java_before['counts'],
        'java_missing_stop_and_effect_receipts': 6, 'sources_unchanged': True,
        'new_semantic_reviews': 0,
        'limitations': ['Native timeout facts are declared arguments, not an OS enforcement proof.',
                       'Native child-stop and effect receipts remain missing.',
                       'These two historical waiting scenarios both have positive semantic remainder labels; this replay does not assess authorization-negative material from other suites.']}
    save_json(output / 'summary.json', summary)
    save_json(output / 'seal.json', {'files': {str(p.relative_to(output)): sha(read_allowed(p))
        for p in sorted(output.rglob('*')) if p.is_file()}})
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    print(replay(parser.parse_args().output))
