"""New assessments of sealed evidence; never replay repair or rewrite its history."""
from pathlib import Path
from .review_schema import contract_for

from .catalog import Catalog
from .core import EvaluationError, Ledger, derive_status, read_allowed, read_json, save_json, sha, tree_digest, utc

SCHEMA = 'ci-eval-reassessment/v1'


def _destination(source_run, output):
    source_run, output = Path(source_run).resolve(), Path(output).resolve()
    if output == source_run or source_run in output.parents:
        raise EvaluationError('reassessment output must be outside the source run')
    if output.exists():
        raise EvaluationError('reassessment output exists; never overwrite or replay')
    return output


def _source(source_run):
    from . import runner
    source_run = Path(source_run).resolve()
    summary = runner.report(source_run)
    catalog = Catalog(source_run/'spec', verify_sources=False)
    plan = read_json(source_run/'plan.json')
    if catalog.digest != plan['catalog_sha256']:
        raise EvaluationError('frozen source catalog does not match the source plan')
    binding = {'run': str(source_run), 'run_seal_sha256': sha(read_allowed(source_run/'seal.json')),
               'catalog_sha256': catalog.digest, 'plan_sha256': sha(read_allowed(source_run/'plan.json')),
               'source_implementation_sha256': sha(read_allowed(source_run/'implementation.json'))}
    return source_run, summary, catalog, binding


def _validate_packet(packet, case, catalog):
    if packet.get('case_id') != case['id']:
        raise EvaluationError('source packet case identity mismatch')
    snapshot = packet.get('criteria_snapshot', [])
    if (not isinstance(snapshot, list) or any(not isinstance(c, dict) or 'id' not in c for c in snapshot)
            or len(snapshot) != len({c['id'] for c in snapshot})
            or {c['id']: c for c in snapshot} != {c['id']: c for c in catalog.for_case(case)}
            or packet.get('criteria_sha256') != sha(catalog.documents['criteria'])):
        raise EvaluationError('source packet criteria do not match the frozen catalog')
    evidence = packet.get('evidence', {})
    if not isinstance(evidence, dict) or any(not isinstance(item, dict) or item.get('sha256') != sha(item.get('content')) for item in evidence.values()):
        raise EvaluationError('source packet evidence digest mismatch')


def _trial(source_run, row, catalog):
    trial = source_run/'trials'/row['id']
    case = catalog.cases[row['case_id']]
    stored_case = read_json(trial/'host/case.json')
    if stored_case != case:
        raise EvaluationError('source trial case differs from its frozen catalog')
    packet = read_json(trial/'host/packet.json')
    _validate_packet(packet, case, catalog)
    result = read_json(trial/'result.json')
    files = {'packet_sha256': sha(read_allowed(trial/'host/packet.json')),
             'case_sha256': sha(read_allowed(trial/'host/case.json')),
             'result_sha256': sha(read_allowed(trial/'result.json')),
             'trial_seal_sha256': sha(read_allowed(trial/'seal.json'))}
    return case, packet, result, files


def _derived_packet(trial, case, original_packet, catalog):
    """Re-project sealed raw observations, without altering the original packet."""
    from . import grading
    execution = read_json(trial/'host/execution.json')
    verification = read_json(trial/'host/verification.json')
    if (original_packet.get('facts', {}).get('execution') != execution
            or original_packet.get('facts', {}).get('verification') != verification):
        raise EvaluationError('source packet does not match its sealed execution/verification receipts')
    expected_skill = execution.get('runtime_skill_sha256_after')
    if (not isinstance(expected_skill, str) or expected_skill != execution.get('runtime_skill_sha256_before')
            or tree_digest(trial/'runtime/skills/ci-self-healing') != expected_skill):
        raise EvaluationError('runtime Skill used for evidence reconstruction is missing, changed, or unbound')
    packet = grading.collect(trial, case, execution, verification)
    _validate_packet(packet, case, catalog)
    if packet.get('facts', {}).get('execution') != execution or packet.get('facts', {}).get('verification') != verification:
        raise EvaluationError('evidence reconstruction must preserve original execution and verification facts')
    return packet


def _execution_complete(packet):
    execution = packet.get('facts', {}).get('execution', {})
    return packet.get('trace_complete') is True and execution.get('status') == 'completed' and execution.get('trace_complete') is True


def _old_semantic(result, criteria, packet, *, original_packet_sha256=None, derived_packet=None):
    from . import grading
    wanted = {c['id'] for c in criteria}
    rows = [dict(row) for row in result.get('checks', []) if row.get('criterion_id') in wanted]
    # Revalidate the saved schema and references; do not infer missing opinions.
    raw = {'checks': [{key: row[key] for key in ('criterion_id','applicability','status','evidence_ids','reason') if key in row} for row in rows]}
    validated = grading.validate_review(raw, criteria, packet, check_support=False,
                                        original_contract=packet.get("review_view") != "public-actions-v1")
    saved = {r["criterion_id"]: r for r in rows}
    for row in validated:
        previous = saved.get(row['criterion_id'], {})
        if not row.get('validation_error') and previous.get('support_status') == 'unsupported_pass':
            row.update(support_status=previous['support_status'], judge_opinion=previous['judge_opinion'])
        if derived_packet is not None:
            for reference in row['evidence_ids']:
                old = packet['evidence'].get(reference)
                new = derived_packet['evidence'].get(reference)
                if old is None or new is None or any(old.get(key) != new.get(key) for key in ('kind','sha256','complete')):
                    raise EvaluationError('old semantic evidence changed or disappeared during projection: '+reference)
    return [dict(row, assessment_origin='original_opinion_unavailable_or_invalid' if row.get('validation_error') else 'reused_original_review',
                 assessment_packet_sha256=original_packet_sha256) for row in validated]


def _record_binding(kind, source, implementation):
    return {'schema_version': SCHEMA, 'kind': kind, 'created_at': utc(), 'source': source,
            'implementation': implementation, 'implementation_sha256': sha(implementation),
            'criteria_policy': 'frozen_source_catalog', 'new_execute_sessions': 0}


def _seal_output(output):
    files = {}
    for path in sorted(output.rglob('*')):
        if path.is_symlink():
            raise EvaluationError('symlink in reassessment evidence')
        if path.is_file() and path != output/'seal.json':
            files[str(path.relative_to(output))] = sha(read_allowed(path))
    save_json(output/'seal.json', {'schema_version':'ci-eval-reassessment-seal/v1','files':files})


def grade(source_run, output, *, check_review_evidence=False):
    """Recompute deterministic checks, explicitly reusing original semantic opinions."""
    from . import grading, runner
    source_run = Path(source_run).resolve()
    output = _destination(source_run, output)
    source_run, source_summary, catalog, binding = _source(source_run)
    implementation = runner.implementation_identity()
    policy = ('original_opinions_with_current_citation_check_no_new_judge' if check_review_evidence
              else 'reused_original_opinions_no_new_judge')
    rows, inputs = [], {}
    for source_row in source_summary['trials']:
        case, original_packet, original, hashes = _trial(source_run, source_row, catalog)
        packet = _derived_packet(source_run/'trials'/source_row['id'], case, original_packet, catalog)
        hashes.update(original_packet_sha256=hashes['packet_sha256'], derived_packet_canonical_sha256=sha(packet))
        mechanical = [dict(row, assessment_origin='recomputed_current_deterministic') for row in grading.deterministic(case, packet)]
        semantic = _old_semantic(original, catalog.for_case(case, 'semantic'), original_packet,
                                 original_packet_sha256=hashes['original_packet_sha256'], derived_packet=packet)
        if check_review_evidence:
            semantic = grading.check_review_support(semantic, packet)
            for check in semantic:
                if check.get('support_status') == 'unsupported_pass':
                    check['assessment_origin'] = 'original_opinion_held_for_evidence_review'
        checks = mechanical + semantic
        original_checks = {check['criterion_id']: check for check in original['checks']}
        differences = []
        for check in checks:
            previous = original_checks.get(check['criterion_id'], {})
            before = {key: previous.get(key) for key in ('status', 'applicability')}
            after = {key: check[key] for key in before}
            if before != after:
                differences.append({'criterion_id': check['criterion_id'], 'original': before,
                                    'replay': after, 'evidence_ids': check['evidence_ids']})
        coverage = grading.coverage(case, packet)
        rows.append({key: original[key] for key in ('id','case_id','version','repeat')} |
                    {'status':derive_status(checks, execution_complete=_execution_complete(packet), covered=coverage['status']=='covered'),
                     'original_trial_status':original['status'], 'checks':checks, 'coverage':coverage,
                     'check_differences':differences,
                     'source_hashes':hashes, 'semantic_policy':policy})
        inputs[source_row['id']] = {'case':case,'packet':packet,'original_packet':original_packet}
    # Detect mutation during regrading, before creating a new successful record.
    runner.report(source_run)
    if sha(read_allowed(source_run/'seal.json')) != binding['run_seal_sha256'] or runner.implementation_identity() != implementation:
        raise EvaluationError('source or assessment implementation changed during reassessment')
    summary = runner.summarize(output, rows, read_json(source_run/'plan.json'), write=False)
    result = {**_record_binding('grade', binding, implementation), **summary,
              'schema_version':SCHEMA, 'kind':'grade', 'models_called':False, 'new_judge_sessions':0,
              'semantic_policy':policy,
              'packet_policy':'reconstructed_from_unchanged_sealed_observations',
              'source_run_status':source_summary['status']}
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    runner.freeze_implementation(output/'implementation', implementation)
    save_json(output/'implementation.json', implementation)
    for trial_id, values in inputs.items():
        save_json(output/'trials'/trial_id/'case.json', values['case'])
        save_json(output/'trials'/trial_id/'packet.json', values['packet'])
        save_json(output/'trials'/trial_id/'original-packet.json', values['original_packet'])
        record = next(row for row in rows if row['id'] == trial_id)
        record['source_hashes']['derived_packet_sha256'] = sha(read_allowed(output/'trials'/trial_id/'packet.json'))
    save_json(output/'assessment.json', result)
    _seal_output(output)
    return result


def review(source_trial, output, runtime, execute_live=False, *, adapter=None):
    """Plan or perform one new judge on sealed evidence. Never calls execute()."""
    from . import grading, native, runner
    source_trial = Path(source_trial).resolve()
    if source_trial.parent.name != 'trials':
        raise EvaluationError('source trial must be a direct member of a sealed run/trials directory')
    source_run = source_trial.parent.parent
    output = _destination(source_run, output)
    source_run, summary, catalog, binding = _source(source_run)
    row = next((row for row in summary['trials'] if row['id'] == source_trial.name), None)
    if row is None:
        raise EvaluationError('source trial is not in the frozen run inventory')
    case, packet, original, hashes = _trial(source_run, row, catalog)
    criteria = catalog.for_case(case, 'semantic')
    if not criteria:
        raise EvaluationError('source case has no semantic criteria to reassess')
    prompt = grading.review_prompt(case, packet, criteria)
    source_binding = {**binding, 'trial':str(source_trial), 'trial_id':row['id'], **hashes}
    implementation = runner.implementation_identity()
    result = {**_record_binding('review', source_binding, implementation), 'status':'planned', 'exit_code':0,
              'output':str(output), 'original_trial_status':original['status'],
              'semantic_policy':'new_independent_judge_only_when_explicitly_requested',
              'maximum_sessions':1, 'wall_seconds':900, 'stage_seconds':900,
              'models_called':False, 'new_judge_sessions':0, 'prompt_sha256':sha(prompt)}
    if not execute_live:
        return result
    adapter = adapter or native
    # Preflight any Skill capability against this source's frozen bundle, not
    # whichever editable Skill happens to be in the workspace today.
    runtime = {**runtime, 'skill_root':str(source_run/'skills'/original['version'])}
    identity = adapter.preflight(runtime)
    if identity.get('status') != 'passed':
        raise EvaluationError('review runtime preflight did not pass; no judge started')
    runner.report(source_run)
    if sha(read_allowed(source_run/'seal.json')) != binding['run_seal_sha256'] or runner.implementation_identity() != implementation:
        raise EvaluationError('source or assessment implementation changed before review')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    runner.freeze_implementation(output/'implementation', implementation)
    save_json(output/'implementation.json', implementation)
    save_json(output/'input/case.json', case)
    save_json(output/'input/packet.json', packet)
    save_json(output/'runtime-identity.json', identity)
    save_json(output/'source.json', source_binding)
    ledger = Ledger(output/'ledger.json', 1, 900)
    selected_runtime = {**runtime, '_frozen_identity':identity}
    judged = None
    result.update(status='incomplete', exit_code=2, models_called=None,
                  semantic_policy='new_independent_judge_on_unchanged_source_evidence')
    try:
        judged = runner._call(ledger, 'review_existing_evidence', row['id'], 900,
                             lambda seconds: adapter.judge(output/'review', selected_runtime, prompt, seconds,
                                                           review_contract=contract_for(criteria, packet)))
        save_json(output/'review-receipt.json', judged)
        if judged.get('status') == 'completed' and judged.get('trace_complete') is True:
            semantic = grading.validate_review(judged.get('answer',''), criteria, packet)
        else:
            semantic = runner._missing_checks(criteria, 'new independent review did not complete')
        semantic = [dict(check, assessment_origin='new_independent_review') for check in semantic]
        mechanical = [dict(check, assessment_origin='recomputed_current_deterministic') for check in grading.deterministic(case,packet)]
        coverage = grading.coverage(case,packet)
        checks = mechanical + semantic
        status = derive_status(checks, execution_complete=_execution_complete(packet), covered=coverage['status']=='covered')
        # The original execution can remain incomplete even if a new judge finishes.
        semantic_status = derive_status(semantic, execution_complete=judged.get('status')=='completed' and judged.get('trace_complete') is True)
        runner.report(source_run)
        if sha(read_allowed(source_run/'seal.json')) != binding['run_seal_sha256'] or runner.implementation_identity() != implementation:
            raise EvaluationError('source or assessment implementation changed during review')
        result.update(status=status, exit_code={'passed':0,'failed':1,'incomplete':2}[status],
                      semantic_status=semantic_status, checks=checks, coverage=coverage,
                      model_receipt_status=judged.get('status'))
    except Exception as exc:
        result.update(status='incomplete',exit_code=2,error=runner._safe_error(exc))
    finally:
        result['new_judge_sessions'] = len(ledger.data['sessions'])
        if judged is not None:
            result['models_called'] = (True if adapter is native and judged.get('model_identity',{}).get('confirmed') is True else None)
        save_json(output/'assessment.json',result)
        _seal_output(output)
    return result
