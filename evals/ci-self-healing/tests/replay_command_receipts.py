"""Q21 frozen replay and installed native tools with local fixed responses only."""
import argparse
import os
from pathlib import Path
import sys

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
from harness.core import EvaluationError, read_allowed, save_json, sha
from harness import runner
from replay_command_split import replay
from test_native_v2 import InstalledWorkerProtocolTests


def verify(output, runtime):
    output = Path(output).resolve()
    if output.exists():
        raise EvaluationError('new output directory required')
    # Only paths to installed tools are consumed by the protocol fixture. It
    # supplies its own loopback endpoint, synthetic token and fixed responses.
    os.environ['CI_EVAL_V2_NATIVE_RUNTIME'] = str(Path(runtime).resolve(strict=True))
    output.mkdir(parents=True)
    implementation = runner.implementation_identity()
    frozen = replay(output / 'frozen-replay')
    probe = InstalledWorkerProtocolTests()
    for kind in ('copy', 'timeout'):
        probe.run_role('execute', command_probe=kind, artifact_root=output / 'native-probes')
    if runner.implementation_identity() != implementation:
        raise EvaluationError('implementation changed during verification')
    summary = {'status': 'passed', 'models_called': False, 'model_sessions_added': 0,
        'synthetic_provider': True, 'installed_native_tool_probes': 2,
        'source_frozen_replay': frozen,
        'boundary': 'Actual installed native tool events with fixed local Responses messages; not Agent or judge behavior acceptance.',
        'verification_sources': {p.name: sha(read_allowed(p)) for p in
            (Path(__file__), Path(__file__).with_name('test_native_v2.py'),
             Path(__file__).with_name('test_command_receipts_v2.py'),
             Path(__file__).with_name('replay_command_split.py'))}}
    save_json(output / 'summary.json', summary)
    save_json(output / 'seal.json', {'files': {str(p.relative_to(output)): sha(read_allowed(p))
        for p in sorted(output.rglob('*')) if p.is_file()}})
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--runtime', required=True, type=Path)
    args = parser.parse_args()
    print(verify(args.output, args.runtime))
