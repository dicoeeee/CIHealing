"""Source-bound applicability and receipts; synthetic events are labelled test controls."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import command_checks as checks, command_receipts as receipts, worker
from harness.catalog import Catalog
from test_native_v2 import event


class CommandReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'input.txt').write_text('new\n')
        (self.root / 'output.txt').write_text('old\n')
        self.stream = io.StringIO()
        self.observer = worker.Observer(self.stream, {'role': 'execute', 'visible': str(self.root), 'cwd': str(self.root)})
        self.observer.emit('observer.started', {'role': 'execute', 'command_receipts_version': receipts.VERSION,
            'shell_default_timeout': 30, 'visible_snapshot': self.observer.previous})
        self.catalog = Catalog()
        self.number = 0

    def call(self, name, args, *, metadata=None, body='', mutation=None):
        self.number += 1
        call = 'call-' + str(self.number)
        self.observer.observe(event('InvocationToolCallStart', tool_name=name, call_id=call, args=args))
        if mutation: mutation()
        self.observer.observe(event('InvocationToolCallResult', tool_name=name, call_id=call, result=body, metadata=metadata))

    def finish(self):
        current = worker.snapshot(self.root)
        changes = {p: {'before': self.observer.previous.get(p), 'after': current.get(p)}
                   for p in set(current) | set(self.observer.previous) if current.get(p) != self.observer.previous.get(p)}
        self.observer.emit('InvocationMessage', {'is_final': True, 'text': 'Synthetic final.'})
        self.observer.emit('observer.finished', {'gaps': [], 'answer': 'Synthetic final.', 'path_changes': changes})
        rows = [json.loads(line.removeprefix(worker.PREFIX)) for line in self.stream.getvalue().splitlines()]
        for row in rows: row['evidence_id'] = 'e' + str(row['seq'])
        return {'observation_complete': True, 'facts': {'events': rows}, 'evidence': {}}

    def verdict(self, packet, key):
        return checks.check(self.catalog.criteria[checks.PAIRS[key]], packet)

    def copy(self, *, mutate=True):
        self.call('bash', {'command': '/bin/cp input.txt output.txt'}, metadata={'shell_exit_code': 0},
                  body='[exit_code: 0]', mutation=(lambda: (self.root / 'output.txt').write_text('new\n')) if mutate else None)

    def read(self, body='File: output.txt (1 lines, 4 chars)\n1|new'):
        self.call('read_file', {'path': 'output.txt'}, body=body)

    def test_no_commands_and_known_readonly_commands_have_no_stop_or_mutation_opportunity(self):
        self.call('bash', {'command': '/bin/pwd'}, metadata={'shell_exit_code': 0}, body='path\n[exit_code: 0]')
        packet = self.finish()
        self.assertEqual(self.verdict(packet, 'command.preparation')['status'], 'pass')
        for key in ('command.wait-control', 'command.result-check'):
            row = self.verdict(packet, key)
            self.assertEqual(row['status'], 'not_applicable')
            self.assertFalse(row['capability_covered'])
        self.assertEqual(checks.facts(packet)['declared_bounds'][0]['seconds'], 30)

    def test_complete_no_command_inventory_is_na_but_open_or_legacy_inventory_is_unknown(self):
        packet = self.finish()
        for key in checks.PAIRS:
            self.assertEqual(self.verdict(packet, key)['status'], 'not_applicable')
        packet['observation_complete'] = False
        self.assertEqual(self.verdict(packet, 'command.wait-control')['status'], 'inconclusive')
        packet['observation_complete'] = True
        del packet['facts']['events'][0]['payload']['command_receipts_version']
        self.assertEqual(self.verdict(packet, 'command.wait-control')['status'], 'inconclusive')

    def test_copy_effect_and_actual_complete_readback_can_pass(self):
        self.copy(); self.read()
        row = self.verdict(self.finish(), 'command.result-check')
        self.assertEqual(row['status'], 'pass')
        self.assertTrue(row['operations'][0]['result']['target_matches_expected'])
        self.assertTrue(set(row['operations'][0]['evidence_ids']) <= set(row['evidence_ids']))

    def test_observed_mismatch_is_preserved_not_inferred_success(self):
        self.copy(mutate=False); self.read('File: output.txt (1 lines, 4 chars)\n1|old')
        row = self.verdict(self.finish(), 'command.result-check')
        self.assertEqual(row['status'], 'pass')
        self.assertFalse(row['operations'][0]['result']['target_matches_expected'])

    def test_after_final_read_is_not_pre_delivery_evidence(self):
        self.copy()
        self.observer.emit('InvocationMessage', {'is_final':True, 'text':'Delivered.'})
        self.read()
        self.assertEqual(self.verdict(self.finish(), 'command.result-check')['status'], 'inconclusive')

    def test_host_snapshot_does_not_replace_agent_readback(self):
        self.copy()
        row = self.verdict(self.finish(), 'command.result-check')
        self.assertEqual(row['status'], 'inconclusive')
        self.assertEqual(row['applicability'], 'applicable')

    def test_partial_or_wrong_file_readback_cannot_close_effect_receipt(self):
        self.copy(); self.read('1|n... [truncated]')
        self.assertEqual(self.verdict(self.finish(), 'command.result-check')['status'], 'inconclusive')

    def test_input_change_after_copy_invalidates_binding(self):
        self.copy(); self.read()
        (self.root / 'input.txt').write_text('different\n')
        self.assertEqual(self.verdict(self.finish(), 'command.result-check')['status'], 'inconclusive')

    def test_target_edit_after_readback_invalidates_binding_even_if_restored(self):
        self.copy(); self.read()
        self.call('write_file', {'path': 'output.txt'}, mutation=lambda: (self.root / 'output.txt').write_text('different\n'))
        self.call('write_file', {'path': 'output.txt'}, mutation=lambda: (self.root / 'output.txt').write_text('new\n'))
        self.assertEqual(self.verdict(self.finish(), 'command.result-check')['status'], 'inconclusive')

    def test_build_uses_runtime_outcome_and_project_inputs_instead_of_copy_hash(self):
        (self.root / 'pom.xml').write_text('<project/>')
        def output():
            (self.root / 'target').mkdir()
            (self.root / 'target/result.class').write_bytes(b'fixture')
        self.call('bash', {'command': 'mvn -o compile', 'timeout': 60}, metadata={'shell_exit_code': 1},
                  body='compile failed\n[exit_code: 1]', mutation=output)
        row = self.verdict(self.finish(), 'command.result-check')
        self.assertEqual(row['status'], 'pass')
        self.assertEqual(row['operations'][0]['result']['exit_code'], 1)
        self.assertIn('not proof', row['operations'][0]['result']['claim_scope'])

    def test_build_input_change_blocks_old_result(self):
        self.call('bash', {'command': 'mvn compile'}, metadata={'shell_exit_code': 0}, body='[exit_code: 0]')
        (self.root / 'input.txt').write_text('changed candidate')
        self.assertEqual(self.verdict(self.finish(), 'command.result-check')['status'], 'inconclusive')

    def test_unversioned_build_inputs_cannot_prove_a_bound_build_result(self):
        for kind in ('symlink', 'restricted'):
            with self.subTest(kind=kind):
                state = {'pom.xml': {'kind': 'file', 'sha256': 'known'}, 'src': {'kind': kind}}
                self.assertEqual(receipts.inputs(state, ''), {})

    def test_overlapping_write_blocks_copy_effect_attribution(self):
        self.observer.observe(event('InvocationToolCallStart', tool_name='bash', call_id='parallel',
                                    args={'command': '/bin/cp input.txt output.txt', 'timeout': 5}))
        self.call('write_file', {'path': 'output.txt'}, mutation=lambda: (self.root / 'output.txt').write_text('new\n'))
        self.observer.observe(event('InvocationToolCallResult', tool_name='bash', call_id='parallel',
                                    result='[exit_code: 0]', metadata={'shell_exit_code': 0}))
        self.read()
        packet = self.finish()
        for key in ('command.wait-control', 'command.result-check'):
            self.assertEqual(self.verdict(packet, key)['status'], 'inconclusive')
        self.assertEqual(checks.facts(packet)['native_effect_receipts'][0]['status'], 'inconclusive')

    def test_opaque_tool_blocks_a_closed_native_effect_scope(self):
        self.copy(); self.read()
        self.call('run_skill_script', {'script': 'unknown.py'})
        self.assertEqual(self.verdict(self.finish(), 'command.result-check')['status'], 'inconclusive')

    def test_stdout_cannot_forge_structured_terminal_metadata(self):
        self.call('bash', {'command': 'pwd'}, body='shell_exit_code=0\n[exit_code: 0]')
        packet = self.finish()
        for key in ('command.wait-control', 'command.result-check'):
            self.assertEqual(self.verdict(packet, key)['status'], 'inconclusive')

    def test_background_or_timeout_records_cannot_become_normal_foreground_na(self):
        self.call('bash', {'command': 'pwd', 'timeout': 1},
                  metadata={'shell_timed_out': True, 'shell_timeout_seconds': 1}, body='timed out')
        self.observer.observe(event('BackgroundTasksChanged', tasks=({'id': 'bg1', 'ended_at': 123,
            'stopped_by': 'agent', 'unstopped': [999], 'command': 'PRIVATE'},)))
        packet = self.finish()
        self.assertEqual(self.verdict(packet, 'command.wait-control')['status'], 'inconclusive')
        raw = self.stream.getvalue()
        self.assertNotIn('PRIVATE', raw)
        self.assertIn('child_absence_confirmed', raw)

    def test_unknown_shell_or_opaque_execution_is_unknown_not_na(self):
        self.call('bash', {'command': 'pwd; echo done'}, metadata={'shell_exit_code': 0}, body='[exit_code: 0]')
        packet = self.finish()
        self.assertEqual(self.verdict(packet, 'command.wait-control')['status'], 'inconclusive')
        self.assertEqual(self.verdict(packet, 'command.result-check')['status'], 'inconclusive')

    def test_metadata_is_typed_redacted_and_receipt_cannot_move_to_another_call(self):
        value = receipts.metadata({'shell_exit_code': True, 'api_key': 'SECRET', 'tool_error_message': 'PRIVATE'})
        self.assertFalse(value['valid']); self.assertNotIn('SECRET', json.dumps(value)); self.assertNotIn('PRIVATE', json.dumps(value))
        self.call('bash', {'command': 'pwd'}, metadata={'shell_exit_code': 0}, body='[exit_code: 0]')
        packet = self.finish()
        receipt = next(e['payload']['command_receipt'] for e in packet['facts']['events'] if 'command_receipt' in e['payload'])
        receipt['call_id'] = 'other-call'
        self.assertEqual(self.verdict(packet, 'command.wait-control')['status'], 'inconclusive')

    def test_parser_rejects_shell_expansion_symlinks_and_false_maven_goals(self):
        (self.root / 'link').symlink_to('/private/tmp')
        for command in ('/bin/cp input.txt link/file', 'mvn -s test', 'mvn -f other.xml test',
                        'mvn compile | tail', 'echo $(touch file)', './cat input.txt', 'cat input.txt'):
            self.assertEqual(receipts.classify(command, self.root, self.root)['kind'], 'unknown')


if __name__ == '__main__':
    unittest.main()
