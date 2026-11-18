"""Calibration material integrity; these tests do not measure judge accuracy."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import calibration, grading
from harness.catalog import Catalog
from harness.core import EvaluationError, sha


class EvidenceCalibrationSamplesTests(unittest.TestCase):
    def setUp(self):
        self.samples = calibration.samples('evidence-semantics-v1')
        self.rows = {row['id']: row for row in self.samples}
        self.catalog = Catalog()

    def content(self, row, reference):
        return row['packet']['evidence'][reference]['content']

    def test_six_samples_cover_three_atomic_criteria_with_agent_checked_pairs(self):
        self.assertEqual(len(self.samples), 6)
        counts = Counter()
        for row in self.samples:
            self.assertEqual(len(row['case']['criteria_ids']), 1)
            criterion = row['case']['criteria_ids'][0]
            expected = row['expected'][criterion]
            self.assertEqual(expected['applicability'], 'applicable')
            counts[(criterion, expected['status'])] += 1
            self.assertEqual(row['labels_provenance']['kind'], 'agent_checked')
            self.assertFalse(row['labels_provenance']['reviewed'])
            for evidence in row['packet']['evidence'].values():
                self.assertEqual(evidence['sha256'], sha(evidence['content']))
        self.assertEqual(counts, Counter({(criterion, status): 1
            for criterion in ('contract.target-evidence', 'delivery.outcome-meaning', 'delivery.observation-honesty')
            for status in ('pass', 'fail')}))

    def test_review_prompts_do_not_disclose_sample_names_or_prelabels(self):
        for row in self.samples:
            prompt = grading.review_prompt(row['case'], row['packet'], self.catalog.for_case(row['case'], 'semantic'))
            self.assertNotIn(row['id'], prompt)
            self.assertNotIn(row['labels_provenance']['basis'], prompt)
            self.assertNotIn('labels_provenance', prompt)
            self.assertNotIn('label_basis', prompt)
            self.assertNotIn('"expected"', prompt)
            self.assertEqual(row['case']['id'], row['packet']['case_id'])

    def test_direction_pair_changes_only_current_contract_evidence(self):
        negative = self.rows['direction-underconstrained']['packet']['evidence']
        positive = self.rows['direction-supported']['packet']['evidence']
        self.assertEqual(set(negative), set(positive))
        changed = {key for key in negative if negative[key] != positive[key]}
        self.assertEqual(changed, {'current-contract'})
        self.assertEqual(negative['report']['content']['result'], 'unresolved')
        self.assertFalse(negative['execution']['content']['source_changed'])

    def test_outcome_pair_distinguishes_agent_checks_from_later_host_results(self):
        for identifier, actual in [('outcome-late-host-checks', {'compile'}),
                                   ('outcome-agent-receipts', {'compile', 'target-behavior', 'legacy-interface'})]:
            row = self.rows[identifier]
            events = self.content(row, 'execution')['events']
            delivery = next(e['seq'] for e in events if e['action'] == 'deliver_report')
            closed = next(e['seq'] for e in events if e['action'] == 'agent_session_closed')
            receipts = [e for e in events if e['action'] == 'run_check']
            self.assertEqual({e['check'] for e in receipts if e['seq'] < delivery and e['receipt_returned_to_agent']}, actual)
            candidate = self.content(row, 'candidate')['sha256']
            self.assertTrue(all(e['candidate_sha256'] == candidate for e in receipts))
            host = self.content(row, 'host-verification')
            self.assertGreater(host['seq'], closed)
            self.assertFalse(host['available_to_agent_before_delivery'])
            self.assertEqual(host['candidate_sha256'], candidate)
        self.assertEqual(self.content(self.rows['outcome-late-host-checks'], 'report'),
                         self.content(self.rows['outcome-agent-receipts'], 'report'))

    def test_readback_versions_and_actual_ranges_are_consistent(self):
        for identifier, first_line in [('readback-final-partial', 5), ('readback-final-full', 1)]:
            row = self.rows[identifier]
            versions = self.content(row, 'file-versions')
            by_hash = {v['sha256']: v['body'] for v in versions.values()}
            for digest, body in by_hash.items():
                self.assertEqual(digest, hashlib.sha256(body.encode()).hexdigest())
            events = self.content(row, 'execution')['events']
            edit = next(e for e in events if e['action'] == 'edit')
            reads = [e for e in events if e['action'] == 'read']
            for event in reads:
                lines = by_hash[event['sha256']].splitlines(keepends=True)
                span = event['range']
                self.assertEqual(len(lines), span['total_lines'])
                self.assertEqual(''.join(lines[span['first_line'] - 1:span['last_line']]), event['returned_text'])
            self.assertLess(reads[0]['seq'], edit['seq'])
            self.assertGreater(reads[1]['seq'], edit['seq'])
            self.assertEqual(reads[1]['sha256'], versions['final']['sha256'])
            self.assertEqual(reads[1]['range']['first_line'], first_line)
        self.assertEqual(self.content(self.rows['readback-final-partial'], 'answer'),
                         self.content(self.rows['readback-final-full'], 'answer'))

    def test_original_suite_and_unknown_suite_are_not_silently_replaced(self):
        self.assertEqual(calibration.samples(), calibration.samples('initial-v1'))
        counts = Counter(next(iter(row['expected'].values()))['status'] for row in calibration.samples())
        self.assertEqual(counts, {'pass': 2, 'fail': 2, 'inconclusive': 2})
        with self.assertRaises(EvaluationError):
            calibration.samples('unregistered-suite')


if __name__ == '__main__':
    unittest.main()
