"""Q20 offline gate regression against the unchanged, sealed Q19 answers."""
import argparse
from pathlib import Path
import sys

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
from harness import calibration_gate as gate
from harness.core import read_allowed, read_json, save_json, sha


def replay(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    campaign = source.parent / 'campaign.json'
    campaign_before = sha(read_allowed(campaign))
    state = read_json(campaign)
    gate.require(state['status'] == 'stopped_after_phase1' and state['phase2_status'] == 'not_started_gate_failed',
                 'expected unchanged Q19 stopped campaign')
    original = gate.load_source(source)
    gate.require(not output.exists() and source not in output.parents, 'replay requires a new external directory')
    output.mkdir(parents=True)
    audit_path = ENGINE / 'fixtures/calibration/command-control-v1/q19-rationale-audit.json'
    # No audit can release even the four structurally valid original answers.
    pending = gate.assess(source, output / 'without-audit')
    gate.require(pending['counts'] == {'passed': 0, 'failed': 0, 'incomplete': 6}
                 and not pending['phase2_permitted'], 'missing audit unexpectedly released calibration')
    reviewed = gate.assess(source, output / 'with-audit', audit_path=audit_path)
    gate.require(gate.read_gate(output / 'with-audit') == reviewed
                 and gate.read_gate(output / 'without-audit') == pending,
                 'saved gate cannot be verified for later consumption')
    expected = {'c001': 'incomplete', 'c002': 'failed', 'c003': 'passed',
                'c004': 'failed', 'c005': 'passed', 'c006': 'incomplete'}
    gate.require({r['id']: r['status'] for r in reviewed['samples']} == expected,
                 'frozen three-part gate regression differs from explicit audit')
    gate.require(not reviewed['phase2_permitted'], 'historical calibration must remain blocked')
    gate.require(gate.load_source(source)['sha256'] == original['sha256']
                 and sha(read_allowed(campaign)) == campaign_before, 'original calibration/campaign changed')
    summary = {'status': 'passed', 'scope': 'offline gate regression, not a live calibration pass',
        'models_called': False, 'model_sessions_added': 0, 'historical_source_unchanged': True,
        'campaign_unchanged': True, 'gate_status': reviewed['status'], 'phase2_permitted': False,
        'counts': reviewed['counts'], 'audit_method': reviewed['review_method'],
        'original_valid_label_matches': sum(r['automatic_status'] == 'passed' for r in reviewed['samples']),
        'limits': 'The program verifies audit completeness and binding. Material semantic judgments remain Agent-checked, not human gold or inferred by string matching.'}
    save_json(output / 'summary.json', summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='sealed Q19 phase1 directory')
    parser.add_argument('--output', type=Path, required=True, help='new directory')
    args = parser.parse_args()
    print(replay(args.source, args.output))
