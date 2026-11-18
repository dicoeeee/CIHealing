"""Offline calibration release gate. Rationale judgments are supplied, not inferred.

The program verifies provenance, coverage declarations and claim references.
An agent/user must assess semantic support; keywords never establish support.
"""
from pathlib import Path

from . import grading, review_schema
from .catalog import Catalog
from .calibration import HOLDOUT
from .core import EvaluationError, read_allowed, read_json, save_json, sha

POLICY = "calibration-three-part-v1"
AUDIT_SCHEMA = "ci-calibration-rationale-audit/v1"


def require(condition, message):
    if not condition:
        raise EvaluationError(message)


def _file(root, name):
    path = Path(name)
    require(not path.is_absolute() and '..' not in path.parts, "unsafe calibration evidence path")
    require(not any((root / Path(*path.parts[:i])).is_symlink() for i in range(1, len(path.parts) + 1)),
            "symlink in calibration evidence path")
    return root / path


def seal_results(root):
    """Seal host receipts even when a session failed; never manufacture a receipt."""
    root = Path(root)
    destination = root / 'result-seal.json'
    require(not destination.exists(), "calibration result seal already exists")
    paths = [root / 'input-seal.json', root / 'summary.json']
    paths.extend(sorted((root / 'reviews').glob('*.json')))
    save_json(destination, {'schema_version': 'ci-calibration-results/v1',
        'files': {str(path.relative_to(root)): sha(read_allowed(path)) for path in paths}})


def load_source(root):
    """Accept native seals or the preserved Q19 external seal, without rewriting either."""
    from .runner import _assert_calibration_inputs
    root = Path(root).resolve()
    _assert_calibration_inputs(root)
    inputs = read_json(root / 'input-seal.json')['files']
    if (root / 'result-seal.json').exists():
        seal_path, base, mode = root / 'result-seal.json', root, 'host_receipts'
        files = read_json(seal_path)['files']
        require(files.get('input-seal.json') == sha(read_allowed(root / 'input-seal.json')),
                'result seal does not bind calibration inputs')
        require('summary.json' in files, 'result seal lacks summary')
    else:
        require(root.name == 'phase1', 'sealed calibration results required')
        seal_path, base, mode = root.parent / 'phase1-result-seal.json', root.parent, 'q19_external'
        seal = read_json(seal_path)
        files = {**seal['files'], 'phase1-gate.json': seal['gate_sha256']}
    for name, digest in files.items():
        require(sha(read_allowed(_file(base, name))) == digest, 'calibration result changed: ' + name)
    source_digest = sha({'input_files': inputs, 'result_files': files,
                         'input_seal': sha(read_allowed(root / 'input-seal.json')),
                         'result_seal': sha(read_allowed(seal_path))})
    samples = read_json(root / 'samples.json')['samples']
    labels = read_json(root / 'labels.json')['labels']
    ids = [row['id'] for row in samples]
    require(ids == [f'c{i:03d}' for i in range(1, 7)], 'release requires the complete six-sample calibration')
    require(len(labels) == 6 and {row['id'] for row in labels} == set(ids), 'calibration labels are incomplete or duplicated')
    labels = {row['id']: row['expected'] for row in labels}
    catalog = Catalog(root / 'spec', verify_sources=False)
    records = []
    for index, sample in enumerate(samples, 1):
        identifier, packet = sample['id'], sample['packet']
        criteria = catalog.for_case(sample['case'], 'semantic')
        require(criteria and set(labels[identifier]) == {c['id'] for c in criteria}, 'selected criteria and labels differ')
        relative = f'reviews/{identifier}.json' if mode == 'host_receipts' else f'phase1/c{index:04d}/execution.json'
        receipt = read_json(_file(base, relative)) if relative in files else {}
        raw = receipt.get('answer', '')
        require(isinstance(raw, str), 'calibration answer must be text')
        checks = grading.validate_review(raw, criteria, packet)
        protocol_ok = (receipt.get('status') == 'completed' and receipt.get('trace_complete') is True
                       and not any(row.get('validation_error') for row in checks))
        actual = {row['criterion_id']: {k: row[k] for k in ('status', 'applicability')} for row in checks}
        auto = 'incomplete' if not protocol_ok else 'passed' if actual == labels[identifier] else 'failed'
        # Keep original reason strings available even when local support checks
        # mark a well-formed opinion inconclusive. Never audit rewritten reasons.
        try:
            decoded = review_schema.strict_loads(raw)
            original = decoded['checks'] if not review_schema.validate_structure(decoded) else []
        except (ValueError, TypeError, KeyError):
            original = []
        identity = receipt.get("model_identity") or {}
        records.append({'id': identifier, 'criteria': criteria, 'packet': packet,
            'confirmed_live_review': receipt.get("models_called") is not False and identity.get("confirmed") is True
                                     and identity.get("response_model_confirmed") is True,
            'answer': raw, 'original_checks': original, 'checks': checks, 'expected': labels[identifier],
            'automatic_status': auto, 'protocol_ok': protocol_ok,
            'binding_sha256': sha({'source': source_digest, 'sample': sample, 'criteria': criteria,
                                   'expected': labels[identifier], 'answer': raw})})
    from .runner import implementation_identity
    plan = read_json(root / 'plan.json')
    return {'root': str(root), 'sha256': source_digest, 'records': records, 'suite': plan.get('suite'),
            'current_implementation': (read_json(root / 'implementation.json') == implementation_identity()
                                       and plan.get('catalog_sha256') == Catalog().digest),
            'phase2_not_sampled': plan.get('phase2_semantics_not_sampled', [])}


def audit_template(source):
    revised = source.get('suite') == HOLDOUT
    return {'schema_version': 'ci-calibration-rationale-audit/v2' if revised else AUDIT_SCHEMA,
        'policy': POLICY, 'source_sha256': source['sha256'],
        'review_method': 'agent_checked', 'reviewed': False,
        'checks': [{'sample_id': row['id'], 'criterion_id': criterion['id'],
                    'binding_sha256': row['binding_sha256'], 'coverage_complete': False, 'claims': [],
                    **({'attributions': None} if revised and criterion['id'] == 'command.result-check' else {})}
                   for row in source['records'] for criterion in row['criteria']]}


def _validate_audit(source, audit):
    expected = audit_template(source)
    require(isinstance(audit, dict) and set(audit) == set(expected), 'invalid rationale audit fields')
    require(all(audit[k] == expected[k] for k in ('schema_version', 'policy', 'source_sha256')),
            'rationale audit does not bind this source and policy')
    require(audit['review_method'] in ('agent_checked', 'user_adjudicated') and type(audit['reviewed']) is bool,
            'invalid rationale review provenance')
    rows = audit['checks']
    require(isinstance(rows, list) and len(rows) == len(expected['checks']), 'rationale audit must cover every selected criterion')
    allowed = {(r['sample_id'], r['criterion_id']): r for r in expected['checks']}
    records = {r['id']: r for r in source['records']}
    seen = set()
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get('sample_id'), str)
                and isinstance(row.get('criterion_id'), str), 'invalid rationale identifiers')
        key = row['sample_id'], row['criterion_id']
        require(key in allowed and key not in seen, 'missing, duplicate or unknown rationale criterion')
        require(set(row) == set(allowed[key]), 'invalid rationale row fields')
        seen.add(key)
        require(row['binding_sha256'] == allowed[key]['binding_sha256'], 'rationale row source changed')
        require(type(row['coverage_complete']) is bool and isinstance(row['claims'], list), 'invalid rationale coverage declaration')
        record = records[row['sample_id']]
        reasons = [c['reason'] for c in record['original_checks'] if c['criterion_id'] == row['criterion_id']]
        for claim in row['claims']:
            require(isinstance(claim, dict) and set(claim) == {'quote', 'assessment', 'evidence_ids', 'reason'}, 'invalid claim audit fields')
            require(isinstance(claim['quote'], str) and claim['quote'].strip() and len(reasons) == 1
                    and claim['quote'] in reasons[0], 'audited claim is not in the original reason')
            require(claim['assessment'] in ('supported', 'unsupported', 'unknown'), 'invalid claim support judgment')
            refs = claim['evidence_ids']
            require(isinstance(refs, list) and refs and all(isinstance(ref, str) and ref in record['packet']['evidence'] for ref in refs),
                    'claim audit must reference real evidence IDs')
            require(isinstance(claim['reason'], str) and claim['reason'].strip(), 'claim audit needs a concrete explanation')
        if 'attributions' in row:
            _validate_attributions(row, record['packet'])
    return {(row['sample_id'], row['criterion_id']): row for row in rows}


def _validate_attributions(row, packet):
    """The auditor identifies causal assertions. The program verifies quoted Agent speech,
    not whether the quote semantically supports the asserted reasoning."""
    items = row['attributions']
    if items is None:
        return  # not reviewed; cannot pass
    require(isinstance(items, list), 'attributions must be a list or null')
    seen = set()
    for item in items:
        require(isinstance(item, dict) and set(item) == {'claim_quote', 'agent_statement'},
                'invalid attribution record')
        q, statement = item['claim_quote'], item['agent_statement']
        require(isinstance(q, str) and q not in seen, 'duplicate or invalid attribution')
        seen.add(q)
        claims = [claim for claim in row['claims'] if claim['quote'] == q]
        require(len(claims) == 1, 'attribution must identify one audited claim')
        if statement is None:
            require(claims[0]['assessment'] != 'supported', 'supported attribution needs a quoted Agent statement')
            continue
        require(isinstance(statement, dict) and set(statement) == {'evidence_id', 'seq', 'quote'}
                and isinstance(statement['evidence_id'], str) and type(statement['seq']) is int
                and isinstance(statement['quote'], str) and statement['quote'].strip(),
                'invalid Agent statement locator')
        entry = packet['evidence'].get(statement['evidence_id'], {})
        require(statement['evidence_id'] in claims[0]['evidence_ids'] and entry.get('kind') == 'public_execution_trace'
                and isinstance(entry.get('content'), list), 'attribution must cite the public trace')
        messages = [e.get('message') for e in entry['content'] if e.get('seq') == statement['seq']]
        require(len(messages) == 1 and isinstance(messages[0], str) and statement['quote'] in messages[0],
                'attribution quote is not in the identified Agent message')


def rationale_status(item):
    """Combine a validated claim audit; a missing attribution review is not a pass."""
    claims = item['claims'] if item else []
    if any(c['assessment'] == 'unsupported' for c in claims):
        return 'failed'
    if (item and item['coverage_complete'] and claims and item.get('attributions', []) is not None
            and all(c['assessment'] == 'supported' for c in claims)):
        return 'passed'
    return 'incomplete'


def evaluate(source, audit=None):
    """Only consume explicit semantic judgments. Missing/uncertain work cannot pass."""
    require([r['id'] for r in source['records']] == [f'c{i:03d}' for i in range(1, 7)],
            'release requires all six samples in source order')
    audits = _validate_audit(source, audit) if audit is not None else {}
    rows = []
    for record in source['records']:
        rationale = []
        for criterion in record['criteria']:
            item = audits.get((record['id'], criterion['id']))
            claims = item['claims'] if item else []
            support = rationale_status(item)
            rationale.append({'criterion_id': criterion['id'], 'status': support,
                              'requires_user_adjudication': any(c['assessment'] == 'unknown' for c in claims)})
        rational_status = 'failed' if any(r['status'] == 'failed' for r in rationale) else 'passed' if all(r['status'] == 'passed' for r in rationale) else 'incomplete'
        automatic = record['automatic_status']
        status = ('failed' if automatic == 'failed' or rational_status == 'failed' else
                  'passed' if automatic == rational_status == 'passed' else 'incomplete')
        rows.append({'id': record['id'], 'status': status, 'automatic_status': automatic,
            'structure_and_references': 'passed' if record['protocol_ok'] else 'incomplete',
            'labels': 'passed' if automatic == 'passed' else 'failed' if automatic == 'failed' else 'not_assessed',
            'rationale_status': rational_status, 'rationale_checks': rationale})
    passed = all(row['status'] == 'passed' for row in rows) and len(rows) == 6
    sampled = sorted({criterion["id"] for record in source["records"] for criterion in record["criteria"]})
    status = 'passed' if passed else 'failed' if any(row['status'] == 'failed' for row in rows) else 'incomplete'
    return {'schema_version': 'ci-calibration-gate/v1', 'policy': POLICY, 'status': status,
        'exit_code': {'passed': 0, 'failed': 1, 'incomplete': 2}[status],
        'phase2_permitted': passed and source.get('suite') == HOLDOUT and source.get('current_implementation') is True
                            and all(r['confirmed_live_review'] for r in source['records']),
        'phase2_semantics_not_sampled': source.get('phase2_not_sampled', []),
        'source_sha256': source['sha256'], 'rationale_audit_sha256': sha(audit) if audit is not None else None,
        'review_method': audit['review_method'] if audit is not None else None,
        'counts': {key: sum(row['status'] == key for row in rows) for key in ('passed', 'failed', 'incomplete')},
        'samples': rows, 'sampled_criteria': sampled, 'models_called': False, 'model_sessions_added': 0,
        'boundary': 'Program validates source binding and explicit audit records, not the truth of free-text judgments. No phase is launched.'}


def assess(root, output, *, audit_path=None):
    from .runner import freeze_implementation, implementation_identity
    root, output = Path(root).resolve(), Path(output).resolve()
    require(not output.exists() and root != output and root not in output.parents,
            'gate output must be a new directory outside the source run')
    source = load_source(root)
    audit = review_schema.strict_loads(read_allowed(Path(audit_path))) if audit_path is not None else None
    result = evaluate(source, audit)
    implementation = implementation_identity()
    output.mkdir(parents=True, exist_ok=False)
    save_json(output / 'source.json', {'run': str(root), 'sha256': source['sha256']})
    save_json(output / 'audit-template.json', audit_template(source))
    if audit is not None:
        save_json(output / 'rationale-audit.json', audit)
    save_json(output / 'automatic-checks.json', [{'id': r['id'], 'checks': r['checks'], 'expected': r['expected']} for r in source['records']])
    save_json(output / 'implementation.json', implementation)
    freeze_implementation(output / 'implementation', implementation)
    require(load_source(root)['sha256'] == source['sha256'], 'source changed during gate assessment')
    require(implementation_identity() == implementation, 'gate implementation changed during assessment')
    save_json(output / 'gate.json', result)
    save_json(output / 'seal.json', {'files': {str(p.relative_to(output)): sha(read_allowed(p))
                                             for p in sorted(output.rglob('*')) if p.is_file()}})
    return result


def read_gate(output):
    """Recheck a saved decision before consuming phase2_permitted; no model/runtime."""
    from .runner import implementation_identity
    output = Path(output).resolve()
    files = read_json(output / 'seal.json')['files']
    actual = {str(p.relative_to(output)) for p in output.rglob('*') if p.is_file() and p != output / 'seal.json'}
    require(set(files) == actual, 'gate file inventory changed')
    for name, digest in files.items():
        require(sha(read_allowed(_file(output, name))) == digest, 'gate artifact changed: ' + name)
    require(implementation_identity() == read_json(output / 'implementation.json'),
            'gate implementation changed; create a new offline assessment')
    binding = read_json(output / 'source.json')
    source = load_source(Path(binding['run']))
    require(source['sha256'] == binding['sha256'], 'gate source changed')
    audit = read_json(output / 'rationale-audit.json') if 'rationale-audit.json' in files else None
    result = evaluate(source, audit)
    require(result == read_json(output / 'gate.json'), 'gate decision differs from its source and audit')
    return result
