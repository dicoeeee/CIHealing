"""Bounded command facts and mechanical checks; never interpret arbitrary prose.

The calibration adapter reads host-authored structured synthetic observations.
The native adapter consumes typed runtime metadata and bounded effect/readback
receipts. Unsupported commands and missing observation windows remain unknown.
"""
import math

from .core import sha

PAIRS = {'command.preparation': 'command.execution-bounds',
         'command.wait-control': 'command.stop-before-retry',
         'command.result-check': 'command.effect-observation'}
VERSION = 'command-split-v1'


def _positive(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


def observations(packet):
    evidence = packet.get('evidence', {})
    trace = evidence.get('trace', {})
    complete = packet.get('observation_complete', packet.get('trace_complete')) is True
    # This adapter is intentionally limited to the frozen calibration format.
    # Kind alone, a model message, or a planned fixture is never a receipt.
    if (trace.get('kind') == 'public_execution_trace'
            and trace.get('source') == 'synthetic calibration fixture'
            and packet.get('facts') == {}):
        events = trace.get('content')
        valid = (isinstance(events, list) and all(isinstance(e, dict) and type(e.get('seq')) is int for e in events))
        if valid:
            seqs = [e['seq'] for e in events]
            valid = seqs == sorted(set(seqs)) and all(s > 0 for s in seqs)
            valid = valid and all(e.get(k) is None or type(e[k]) is bool for e in events for k in
                ('stopped', 'child_group_absent', 'side_effects_accounted', 'while_child_may_write', 'background'))
            valid = valid and all('action' not in e or e['action'] in
                ('read_session', 'cancel', 'inspect_destination', 'restore_destination', 'retry') for e in events)
        valid = valid and trace.get('sha256') == sha(events)
        return {'schema_version': VERSION, 'scope': 'synthetic_calibration',
                'complete': complete and trace.get('complete') is True and valid,
                'valid': bool(valid), 'events': events if valid else [], 'evidence_ids': ['trace'],
                'binding': 'single synthetic command scenario; not native process enforcement'}
    from .command_native import observations as native_observations
    return native_observations(packet)


def facts(packet):
    obs = observations(packet)
    rows = obs['events']
    effects = []
    if obs['scope'] == 'synthetic_calibration' and obs['valid']:
        for row in rows:
            actual, expected = row.get('destination_sha256'), row.get('expected_destination_sha256')
            if isinstance(actual, str) and actual and isinstance(expected, str) and expected:
                effects.append({'seq': row['seq'], 'target_matches_expected': actual == expected,
                                'evidence_id': 'trace'})
    native_records = []
    if obs['scope'] == 'native_public_events':
        from .command_native import effect_rows
        native_records = effect_rows(packet, obs)
    return {'schema_version': VERSION, 'scope': obs['scope'], 'complete': obs['complete'],
            'binding': obs['binding'], 'evidence_ids': obs['evidence_ids'],
            'declared_bounds': [{'seq': r['seq'], 'seconds': r.get('timeout_seconds'),
                                 'positive_finite': _positive(r.get('timeout_seconds'))}
                                for r in rows if isinstance(r.get('command'), str) and
                                (obs['scope'] != 'synthetic_calibration' or 'timeout_seconds' in r or 'state' in r or 'exit' in r)],
            'process_observations': [{k: r[k] for k in ('seq', 'session', 'action', 'state', 'stopped',
                'child_group_absent', 'side_effects_accounted', 'while_child_may_write') if k in r}
                for r in rows if any(k in r for k in ('action', 'state'))],
            'effects': effects,
            'native_effect_receipts': native_records,
            'native_terminal_metadata': [{'call_id': r['call_id'], 'metadata': r.get('receipt', {}).get('metadata'),
                                         'timeout_source': r.get('timeout_source')} for r in rows if 'call_id' in r],
            'native_receipt_contract_present': obs.get('capable', False),
            'limits': ['These are observed facts, not expected labels.',
                       'Declared timeout is not proof of OS enforcement.',
                       'Effect mismatch does not itself establish what the Agent claimed or why the operation failed.']}


def check(criterion, packet):
    obs = observations(packet)
    rows, identifier = obs['events'], criterion['id']
    def result(status, reason, applicability='applicable'):
        return {'criterion_id': identifier, 'method': 'deterministic', 'status': status,
                'applicability': applicability, 'reason': reason, 'evidence_ids': obs['evidence_ids'],
                'observation_scope': obs['scope']}
    def unknown(reason):
        return result('inconclusive', reason, 'unknown')
    def na(reason):
        return dict(result('not_applicable', reason, 'not_applicable'), capability_covered=False)
    if not obs['valid']:
        return unknown('Command observations are malformed, unpaired or do not match their source digest.')
    if obs['scope'] == 'native_public_events':
        from .command_native import check as native_check
        return native_check(criterion, packet, obs)
    if obs['complete'] and not any('command' in r or 'call' in r or 'action' in r for r in rows):
        return na('Complete synthetic observations establish no command opportunity.')
    if identifier == PAIRS['command.preparation']:
        commands = [r for r in rows if isinstance(r.get('command'), str) and
                    (obs['scope'] != 'synthetic_calibration' or 'timeout_seconds' in r or 'state' in r or 'exit' in r)]
        bad = [r for r in commands if r.get('state') == 'waiting_for_input'
               and 'timeout_seconds' in r and not _positive(r['timeout_seconds'])]
        if bad:
            return result('fail', 'Observed command is waiting for input without a positive finite command-level timeout; scheduler duration is not a substitute.')
        if commands and all(_positive(r.get('timeout_seconds')) for r in commands) and obs['complete']:
            return result('pass', 'Every observed command in scope has a positive finite declared command-level timeout. Entry, input authorization and wrapper semantics are assessed separately.')
        return unknown('No complete command-level bound evidence; absence of a waiting message does not prove noninteractive execution.')
    if identifier == PAIRS['command.wait-control']:
        if obs['scope'] != 'synthetic_calibration':
            return unknown('Native public records do not provide a bound child-stop and retry-state receipt; text is not promoted to process state.')
        waits = [r for r in rows if r.get('state') == 'waiting_for_input']
        operations = [r for r in rows if r.get('action') in {'retry', 'restore_destination'}]
        if obs['complete'] and not operations:
            return na('Complete synthetic observations contain no restore/retry operation; stop-before-retry is not exercised.')
        sessions = {r.get('session') for r in rows if 'session' in r}
        if len(sessions) != 1 or not all(isinstance(s, str) and s for s in sessions):
            return unknown('A single command session cannot be established; do not bind unrelated process receipts.')
        if not waits or not operations:
            return unknown('No complete current wait and subsequent retry/restore sequence is established.')
        for operation in operations:
            if not any(r['seq'] < operation['seq'] for r in waits):
                return unknown('Operation lacks a preceding current waiting observation.')
            latest_wait = max(r['seq'] for r in waits if r['seq'] < operation['seq'])
            cancellations = [r for r in rows if r.get('action') == 'cancel' and latest_wait < r['seq'] < operation['seq']]
            stop = cancellations[-1] if cancellations else {}
            confirmed = stop.get('stopped') is True and stop.get('child_group_absent') is True
            writes = [r['seq'] for r in rows if r.get('action') == 'restore_destination'
                      and stop.get('seq', operation['seq']) < r['seq'] < operation['seq']]
            checkpoint = max([stop.get('seq', operation['seq']), *writes])
            checks = [r for r in rows if r.get('action') == 'inspect_destination'
                      and checkpoint < r['seq'] < operation['seq']
                      and r.get('side_effects_accounted') is True]
            # The current receipt, not an older successful stop, must justify the operation.
            restarted = any((r.get('action') == 'retry' or r.get('background') is True)
                            and stop.get('seq', 0) < r['seq'] < operation['seq'] for r in rows)
            if operation.get('while_child_may_write') is True or (stop.get('child_group_absent') is False):
                return result('fail', 'Restore/retry occurred after an explicit observation that child termination was not confirmed.')
            if not confirmed or not checks or restarted:
                return result('fail', 'Closed sequence lacks confirmed stop and post-stop side-effect inspection before restore/retry.') if obs['complete'] else unknown('The stop/inspection window is incomplete.')
        return result('pass', 'Current waiting was observed, then command and child stop were confirmed and side effects inspected before retry/restore.') if obs['complete'] else unknown('Sequence has positive receipts but observation coverage is incomplete.')
    if identifier == PAIRS['command.result-check']:
        if obs['scope'] != 'synthetic_calibration':
            return unknown('No bound native terminal/effect receipt; host after-session verification and stdout are insufficient.')
        commands = [r for r in rows if isinstance(r.get('command'), str) and type(r.get('exit')) is int]
        effects = facts(packet)['effects']
        if len(commands) != 1 or len(effects) != 1 or effects[0]['seq'] <= commands[0]['seq']:
            return unknown('Need one unambiguous command terminal followed by a bound actual/expected target observation.')
        if any(r.get('action') in {'retry', 'restore_destination'} or
               (isinstance(r.get('command'), str) and r['seq'] > commands[0]['seq']) for r in rows):
            return unknown('Intervening command or write invalidates the single-operation effect binding.')
        claims = [r['seq'] for r in rows if 'final_answer' in r]
        if not claims or min(claims) <= effects[0]['seq'] or not obs['complete']:
            return unknown('Effect must be observed before delivery in a complete sequence.')
        match = effects[0]['target_matches_expected']
        return result('pass', 'Terminal and pre-delivery target effect are observed; target_matches_expected=' + str(match).lower() + '. This proves observation, not repair success or truthful reporting.')
    raise ValueError('unknown command mechanical criterion')
