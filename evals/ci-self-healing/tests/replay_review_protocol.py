"""Replay sealed Q19 outputs locally; no runtime, network, retry, or model import."""
import argparse
import copy
from pathlib import Path
import sys

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
from harness import grading, review_schema, runner
from harness.catalog import Catalog
from harness.core import EvaluationError, read_allowed, read_json, save_json, sha


def require(condition, reason):
    if not condition:
        raise EvaluationError(reason)


def verify_source(source):
    seal = read_json(source / 'phase1-result-seal.json')
    files = dict(seal['files'])
    files['phase1-gate.json'] = seal['gate_sha256']
    # Bind the blind packets, criteria and labels as well as original outputs.
    for name, digest in read_json(source / 'phase1/input-seal.json')['files'].items():
        files['phase1/' + name] = digest
    for name, expected in files.items():
        path = Path(name)
        require(not path.is_absolute() and '..' not in path.parts, 'unsafe frozen source path')
        require(sha(read_allowed(source / path)) == expected, 'frozen source changed: ' + name)
    return {'seal_sha256': sha(read_allowed(source / 'phase1-result-seal.json')),
            'input_seal_sha256': sha(read_allowed(source / 'phase1/input-seal.json')), 'files': files}


def replay(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    require(not output.exists(), 'output already exists; never overwrite an assessment')
    require(source != output and source not in output.parents, 'assessment must be outside historical source')
    before = verify_source(source)
    catalog = Catalog(source / 'phase1/spec', verify_sources=False)
    samples = read_json(source / 'phase1/samples.json')['samples']
    labels = {row['id']: row['expected'] for row in read_json(source / 'phase1/labels.json')['labels']}
    require([row['id'] for row in samples] == [f'c{i:03d}' for i in range(1, 7)], 'expected Q19 six samples')
    require(read_json(source / 'phase1/samples.json')['suite'] == 'command-control-v1', 'expected command-control suite')
    implementation = runner.implementation_identity()
    output.mkdir(parents=True)
    save_json(output / 'source.json', {'path': str(source), **before})
    save_json(output / 'implementation.json', implementation)
    runner.freeze_implementation(output / 'implementation', implementation)
    rows = []
    for index, sample in enumerate(samples, 1):
        identifier, packet, case = sample['id'], sample['packet'], sample['case']
        criteria = catalog.for_case(case, 'semantic')
        contract = review_schema.contract_for(criteria, packet)
        raw_file = source / f'phase1/c{index:04d}/execution.json'
        answer = read_json(raw_file)['answer']
        checks = grading.validate_review(answer, criteria, packet)
        categories = sorted({row['validation_error_category'] for row in checks if row.get('validation_error')})
        expected_category = {'c001': ['reference'], 'c006': ['schema']}.get(identifier, [])
        require(categories == expected_category, 'unexpected frozen-output validation: ' + identifier)
        if not categories:
            actual = {row['criterion_id']: {key: row[key] for key in ('status', 'applicability')} for row in checks}
            require(actual == labels[identifier], 'valid-output label changed: ' + identifier)
        stage = output / identifier
        stage.mkdir()
        save_json(stage / 'review-contract.json', {'allowed_ids': contract, 'wire_format': review_schema.wire_format(contract)})
        (stage / 'prompt.txt').write_text(grading.review_prompt(case, packet, criteria))
        row = {'id': identifier, 'source_execution_sha256': sha(read_allowed(raw_file)), 'source_answer_sha256': sha(answer.encode()),
               'validation_categories': categories, 'original_accepted': not categories, 'checks': checks}
        if categories:
            control = copy.deepcopy(review_schema.strict_loads(answer))
            if identifier == 'c001':
                require(control['checks'][0]['evidence_ids'] == ['trusted_task', 'public_execution_trace'], 'unexpected Q19 ID counterexample')
                control['checks'][0]['evidence_ids'] = ['task', 'trace']
            else:
                require('reason_note' in control['checks'][0] and control['checks'][0]['reason_note'] is None, 'unexpected Q19 extra-field counterexample')
                del control['checks'][0]['reason_note']
            control_checks = grading.validate_review(control, criteria, packet)
            require(not any(check.get('validation_error') for check in control_checks), 'synthetic positive control rejected')
            require({check['criterion_id']: {key: check[key] for key in ('status', 'applicability')} for check in control_checks} == labels[identifier],
                    'synthetic control changed labels')
            save_json(stage / 'synthetic-positive-control.json', {'synthetic': True, 'model_generated': False,
                'purpose': 'Only remove the known protocol defect in a separate copy; never replace or reclassify the original.',
                'response': control, 'checks': control_checks})
            row['synthetic_positive_control_accepted'] = True
        save_json(stage / 'assessment.json', row)
        rows.append(row)
    require(verify_source(source) == before, 'historical source changed during replay')
    require(runner.implementation_identity() == implementation, 'implementation changed during replay')
    result = {'status': 'passed', 'scope': 'offline review protocol regression, not live judge acceptance',
              'models_called': False, 'model_sessions_added': 0, 'historical_source_unchanged': True,
              'original_accepted': 4, 'original_rejected': 2, 'synthetic_positive_controls': 2,
              'unsupported_rationale_followup': ['c002', 'c004'],
              'limitations': 'Schema and identifier validity do not prove that each reason is supported. Q19 remains stopped after phase 1.',
              'samples': rows}
    save_json(output / 'summary.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = replay(args.source, args.output)
    print({key: result[key] for key in ('status', 'models_called', 'original_accepted', 'original_rejected', 'historical_source_unchanged')})
