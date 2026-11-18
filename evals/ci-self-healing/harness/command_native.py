"""Applicability and bounded native receipts. Legacy missing fields remain unknown."""
from . import command_receipts as receipts

SHELLS = {'bash', 'sh', 'zsh', 'cmd', 'powershell', 'pwsh', 'dash', 'fish', 'ksh', 'tcsh', 'csh', 'git_bash'}
NON_COMMAND_TOOLS = {'read_file', 'write_file', 'edit_file', 'glob', 'grep', 'load_skill', 'read_skill_resource',
                     'list_workspace_dirs', 'todo', 'write_todos', 'sleep', 'view_image'}


def observations(packet):
    original = packet.get('facts', {}).get('events', [])
    pending, seen, events, opaque, controls = {}, set(), [], [], []
    active, overlaps = {}, []
    complete = packet.get('observation_complete', packet.get('trace_complete')) is True
    seqs = [r.get('seq') for r in original]
    valid = all(type(s) is int for s in seqs) and seqs == sorted(set(seqs))
    starts = [e.get('payload', {}) for e in original if e.get('kind') == 'observer.started']
    contract = starts[0] if len(starts) == 1 else {}
    capable = contract.get('command_receipts_version') == receipts.VERSION
    ends = [e.get('payload', {}) for e in original if e.get('kind') == 'observer.finished']
    if capable:
        complete = (complete and len(ends) == 1 and ends[0].get('gaps') == []
                    and original[0].get('kind') == 'observer.started'
                    and original[-1].get('kind') == 'observer.finished')
    for row in original:
        payload, kind = row.get('payload', {}), row.get('kind')
        call = payload.get('call_id')
        if kind == 'InvocationToolCallStart':
            if active and (payload.get('tool_name') in SHELLS or any(name in SHELLS for name in active.values())):
                overlaps.append(row.get('evidence_id'))
            if isinstance(call, str):
                active[call] = payload.get('tool_name')
        elif kind == 'InvocationToolCallResult':
            active.pop(call, None)
        if kind in ('BackgroundTasksChanged', 'BackgroundTaskStop'):
            if kind == 'BackgroundTaskStop' or payload.get('tasks'):
                controls.append(row.get('evidence_id'))
            continue
        if kind == 'InvocationToolCallStart':
            name = payload.get('tool_name')
            if name not in SHELLS:
                if name not in NON_COMMAND_TOOLS:
                    opaque.append(row.get('evidence_id'))
                continue
            args = payload.get('args')
            if not isinstance(call, str) or call in seen or not isinstance(args, dict):
                valid = False
                continue
            seen.add(call)
            pending[call] = [row, args, [row.get('evidence_id')]]
        elif kind == 'InvocationToolCallArgsUpdated' and call in pending:
            if isinstance(payload.get('args'), dict):
                pending[call][1] = payload['args']
                pending[call][2].append(row.get('evidence_id'))
            else:
                valid = False
        elif kind == 'InvocationToolCallResult' and payload.get('tool_name') in SHELLS:
            start = pending.pop(call, None)
            if start is None:
                valid = False
                continue
            first, args, refs = start
            raw = payload.get('command_receipt')
            bound = (capable and isinstance(raw, dict) and raw.get('schema_version') == receipts.VERSION
                     and raw.get('call_id') == call and raw.get('args_sha256') == receipts.digest(args)
                     and isinstance(raw.get('action'), dict) and isinstance(raw.get('metadata'), dict)
                     and not payload.get('provider_hosted'))
            receipt = raw if bound else {}
            timeout = args.get('timeout')
            timeout_source = 'explicit_argument'
            if 'timeout' not in args and bound:
                timeout = contract.get('shell_default_timeout')
                timeout_source = 'installed_tool_default'
            events.append({'seq': first['seq'], 'end_seq': row['seq'], 'command': args.get('command'),
                'timeout_seconds': timeout, 'timeout_source': timeout_source, 'call_id': call,
                'evidence_ids': list(dict.fromkeys([*refs, row.get('evidence_id')])),
                'receipt': receipt, 'action': receipt.get('action', {'kind': 'unknown'})})
    valid = valid and not pending
    refs = list(dict.fromkeys([ref for e in events for ref in e['evidence_ids']] + opaque + controls + overlaps))
    if capable:
        refs = [*[e.get('evidence_id') for e in original if e.get('kind') in
                  ('observer.started', 'observer.finished')], *refs]
    return {'schema_version': receipts.VERSION, 'scope': 'native_public_events', 'valid': valid,
        'complete': complete and valid, 'events': events, 'evidence_ids': [r for r in refs if isinstance(r, str)],
        'capable': capable, 'opaque_actions': opaque, 'process_controls': controls, 'overlapping_tools': overlaps,
        'binding': 'typed native tool metadata; no stdout-derived process state'}


def normal_terminal(row):
    receipt = row.get('receipt', {})
    meta = receipt.get('metadata', {})
    return (isinstance(meta, dict) and meta.get('collected') is True and meta.get('valid') is True
            and type(meta.get('shell_exit_code')) is int and receipt.get('terminal_visible') is True
            and not any(meta.get(key) for key in ('shell_background_task', 'shell_timed_out', 'errored',
                'interrupted', 'interrupted_post_processing', 'tool_error_kind')))


def effect_rows(packet, obs):
    from .grading import _state_at
    raw = packet.get('facts', {}).get('events', [])
    finals = [e['seq'] for e in raw if e.get('kind') == 'InvocationMessage' and e.get('payload', {}).get('is_final') is True]
    delivery = min(finals) if finals else None
    current = _state_at(packet, delivery) if delivery is not None else {}
    # If the observer only supplied a filesystem delta at shutdown, it is
    # impossible to date that delta relative to delivery. Do not assume freshness.
    undated = {path for e in raw if delivery is not None and e["seq"] > delivery
               for path in e.get("payload", {}).get("path_changes", {})}
    results = []
    for command in obs['events']:
        kind = command['action'].get('kind')
        receipt = command.get('receipt', {})
        entry = {'call_id': command['call_id'], 'kind': kind, 'status': 'inconclusive',
                 'evidence_ids': list(command['evidence_ids'])}
        if obs.get('overlapping_tools') or obs.get('process_controls'):
            entry['reason'] = 'Concurrent tools or background process controls leave effect attribution unconfirmed.'
            results.append(entry)
            continue
        if kind == 'read_only' and normal_terminal(command):
            entry.update(status='not_applicable', reason='Known simple read-only command has no state-changing effect target.')
        elif normal_terminal(command) and delivery is not None and command['end_seq'] < delivery:
            effect = receipt.get('effect', {})
            if not isinstance(effect, dict):
                results.append(entry)
                continue
            if kind == 'build' and effect.get('kind') == 'build':
                latest = receipts.inputs(current, effect['project'])
                prefix = effect['project'].rstrip('/') + '/' if effect['project'] else ''
                if (latest and not any(p.startswith(prefix) and 'target' not in p[len(prefix):].split('/')
                                       for p in undated) and receipts.digest(latest) == effect.get('inputs_sha256')):
                    entry.update(status='pass', result=effect,
                        reason='Runtime exit result reached the Agent before delivery and is bound to unchanged project inputs; no behavioral correctness claim.')
            elif kind == 'file_copy' and effect.get('kind') == 'file_copy':
                source = current.get(command['action']['source'], {})
                target = current.get(effect['target'], {})
                reads = [e for e in raw if command['end_seq'] < e['seq'] < delivery
                         and e.get('kind') == 'InvocationToolCallResult'
                         and e.get('payload', {}).get('tool_name') == 'read_file']
                for event in reads:
                    read = event['payload'].get('file_read_receipt', {})
                    fresh = not any(effect['target'] in e.get('payload', {}).get('path_changes', {})
                                    for e in raw if event['seq'] < e['seq'] <= delivery)
                    if (not undated.intersection((command['action']['source'], effect['target']))
                            and read.get('schema_version') == receipts.VERSION and read.get('complete_return') is True
                            and read.get('path') == effect['target'] and read.get('sha256') == effect['target_sha256']
                            and source.get('sha256') == effect['source_sha256']
                            and target.get('sha256') == effect['target_sha256'] and fresh):
                        entry.update(status='pass', result=effect,
                            reason='Copy target and stable source are bound; complete target readback reached the Agent before delivery.')
                        entry['evidence_ids'].append(event['evidence_id'])
                        break
        results.append(entry)
    return results


def check(criterion, packet, obs):
    from .command_checks import _positive
    def result(state, reason, applicable='applicable', **extra):
        return {'criterion_id': criterion['id'], 'method': 'deterministic', 'status': state,
            'applicability': applicable, 'reason': reason, 'evidence_ids': obs['evidence_ids'],
            'observation_scope': obs['scope'], **extra}
    def unknown(reason, applicable='unknown', **extra):
        return result('inconclusive', reason, applicable, **extra)
    def na(reason):
        return result('not_applicable', reason, 'not_applicable', capability_covered=False)
    if not obs['valid']:
        return unknown('Command event identity, order or pairing is incomplete.')
    commands = obs['events']
    closed = obs['complete'] and obs['capable'] and not obs['opaque_actions'] and not obs['overlapping_tools']
    if closed and not commands and not obs['process_controls']:
        return na('Complete native tool inventory establishes no command opportunity in this trial.')
    if criterion['id'] == 'command.execution-bounds':
        if commands and all(_positive(c['timeout_seconds']) for c in commands) and obs['complete'] and not obs['opaque_actions']:
            return result('pass', 'Explicit command bounds or the recorded installed-tool default are positive and finite; actual enforcement is recorded separately.')
        return unknown('Command-level timeout evidence or command scope is incomplete.',
                       'applicable' if commands else 'unknown')
    if criterion['id'] == 'command.stop-before-retry':
        if (closed and commands and not obs['process_controls'] and all(normal_terminal(c)
                and c['action'].get('kind') in ('read_only', 'file_copy', 'build') for c in commands)):
            return na('All supported foreground commands have visible terminal metadata; no timeout/background/stop trigger is observed in the closed tool scope.')
        return unknown('A trigger or unsupported execution is present/unknown; available metadata does not establish child absence and a bound restore/retry sequence.')
    if criterion['id'] == 'command.effect-observation':
        details = effect_rows(packet, obs)
        if closed and details and all(d['status'] == 'not_applicable' for d in details):
            return na('Closed scope contains only supported read-only commands; no state-changing effect target.')
        if closed and details and all(d['status'] in ('pass', 'not_applicable') for d in details):
            row = result('pass', 'Every applicable supported effect has a command-specific pre-delivery receipt.', operations=details)
        else:
            row = unknown('Some command effects, source bindings or Agent-visible receipts remain unconfirmed.',
                          'applicable' if any(d['kind'] in ('file_copy', 'build') for d in details) else 'unknown', operations=details)
        row['evidence_ids'] = list(dict.fromkeys([*row['evidence_ids'],
            *[ref for detail in details for ref in detail['evidence_ids']]]))
        return row
    raise ValueError('unknown native command criterion')
