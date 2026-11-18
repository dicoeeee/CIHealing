"""Strict review syntax/structure and the unchanged semantic validation boundary."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import calibration, grading, review_schema
from harness.core import EvaluationError


class ReviewSchemaTests(unittest.TestCase):
    def setUp(self):
        self.sample = calibration.samples('evidence-semantics-v1')[0]
        self.criterion = grading._criterion_map()['contract.target-evidence']
        self.criteria = [self.criterion]
        self.packet = self.sample['packet']
        self.raw = {'checks': [{'criterion_id': self.criterion['id'], 'applicability': 'applicable',
                               'status': 'fail', 'evidence_ids': ['current-contract', 'report'],
                               'reason': 'The current contract does not support the definite direction.'}]}

    def validate(self, raw):
        return grading.validate_review(raw, self.criteria, self.packet)

    def assert_rejected(self, raw, keyword=None):
        result = self.validate(raw)
        self.assertTrue(all(c['status'] == 'inconclusive' and c['applicability'] == 'unknown'
                            and c.get('validation_error') for c in result), result)
        if keyword:
            self.assertIn(keyword, [d['keyword'] for d in result[0]['validation_errors']], result)
        return result

    def test_shared_schema_validates_and_is_sent_as_strict_format(self):
        schema = review_schema.response_schema()
        review_schema.require_validator().validate(self.raw)
        self.assertEqual(self.validate(json.dumps(self.raw))[0]['status'], 'fail')
        fmt = review_schema.response_format()
        self.assertEqual(fmt['type'], 'json_schema')
        self.assertIs(fmt['json_schema']['strict'], True)
        self.assertEqual(fmt['json_schema']['schema'], schema)
        self.assertIs(schema['additionalProperties'], False)
        self.assertIs(schema['properties']['checks']['items']['additionalProperties'], False)
        fmt['json_schema']['schema']['additionalProperties'] = True
        self.assertIs(review_schema.response_schema()['additionalProperties'], False)

    def test_extra_title_null_is_rejected_with_precise_safe_diagnostics(self):
        self.raw['checks'][0]['title'] = None
        result = self.assert_rejected(self.raw, 'additionalProperties')
        self.assertIn({'path': '/checks/0', 'keyword': 'additionalProperties',
                       'unexpected_fields': ['title']}, result[0]['validation_errors'])
        self.assertIn('title', result[0]['reason'])
        self.raw['checks'][0]['title'] = 'SYNTHETIC_PRIVATE_VALUE_DO_NOT_ECHO'
        self.assertNotIn('SYNTHETIC_PRIVATE_VALUE_DO_NOT_ECHO', json.dumps(self.validate(self.raw)))

    def test_per_review_ids_are_shared_by_prompt_provider_and_local_validation(self):
        contract = review_schema.contract_for(self.criteria, self.packet)
        prompt = grading.review_prompt(self.sample['case'], self.packet, self.criteria)
        payload = json.loads(prompt.split('\n', 1)[1])
        self.assertEqual(payload['allowed_ids'], contract)
        self.assertEqual({r['id']: r['kind'] for r in payload['evidence_index']},
                         {k: v['kind'] for k, v in self.packet['evidence'].items()})
        fmt = review_schema.response_format(contract)
        self.assertEqual(review_schema.require_validator(contract).schema, fmt['json_schema']['schema'])
        properties = fmt['json_schema']['schema']['properties']['checks']['items']['properties']
        self.assertEqual(properties['criterion_id']['enum'], contract['criterion_ids'])
        self.assertEqual(properties['evidence_ids']['items']['enum'], contract['evidence_ids'])
        self.assertEqual(review_schema.validate_structure(self.raw, contract), [])
        self.assertNotIn('expected', payload)

    def test_kind_is_not_an_id_even_when_structure_is_valid(self):
        self.raw['checks'][0]['evidence_ids'] = ['trusted_task', 'public_execution_trace']
        self.assertEqual(review_schema.validate_structure(self.raw), [])
        result = self.assert_rejected(self.raw, 'enum')
        self.assertEqual(result[0]['validation_error_category'], 'reference')
        self.assertNotIn('trusted_task', json.dumps(result))

    def test_reason_note_null_is_not_removed_or_repaired(self):
        self.raw['checks'][0]['reason_note'] = None
        original = copy.deepcopy(self.raw)
        result = self.assert_rejected(self.raw, 'additionalProperties')
        self.assertEqual(self.raw, original)
        self.assertEqual(result[0]['validation_error_category'], 'schema')

    def test_contract_does_not_accept_empty_duplicate_or_unknown_allowlists(self):
        good = review_schema.contract_for(self.criteria, self.packet)
        for bad in [None, [], ['x', 'x'], [None], [' ']]:
            for key in good:
                with self.subTest(key=key, bad=bad), self.assertRaises(EvaluationError):
                    review_schema.response_format({**good, key: bad})
        with self.assertRaises(EvaluationError):
            review_schema.response_format({**good, 'expected': 'pass'})

    def test_top_level_extra_or_missing_required_fields_are_rejected(self):
        raw = copy.deepcopy(self.raw)
        raw['title'] = None
        self.assert_rejected(raw, 'additionalProperties')
        self.assert_rejected({}, 'required')
        for field in self.raw['checks'][0]:
            with self.subTest(field=field):
                raw = copy.deepcopy(self.raw)
                del raw['checks'][0][field]
                self.assert_rejected(raw, 'required')

    def test_unknown_field_names_are_redacted_and_diagnostics_are_bounded(self):
        secret_key = 'SYNTHETIC_PRIVATE_TEXT_AS_KEY'
        self.raw['checks'][0].update({secret_key + str(i): 'not echoed' for i in range(1000)})
        result = self.assert_rejected(self.raw, 'additionalProperties')
        detail = result[0]['validation_errors'][0]
        self.assertEqual(detail['unexpected_field_count'], 1000)
        self.assertTrue(detail['fields_truncated'])
        self.assertLessEqual(len(detail['unexpected_fields']), 8)
        self.assertNotIn(secret_key, json.dumps(result))
        self.assertNotIn('not echoed', json.dumps(result))

    def test_wrong_types_do_not_reach_set_or_dictionary_operations(self):
        for field in ['criterion_id', 'applicability', 'status', 'reason']:
            for bad in [None, [], {}, 1, True]:
                with self.subTest(field=field, value=bad):
                    raw = copy.deepcopy(self.raw)
                    raw['checks'][0][field] = bad
                    self.assert_rejected(raw, 'type')
        for bad in [None, {}, 'report', [None], [{}], [1]]:
            raw = copy.deepcopy(self.raw)
            raw['checks'][0]['evidence_ids'] = bad
            self.assert_rejected(raw, 'type')
        for bad in [[], None, '"text"', {'checks': {}}, {'checks': [None]}]:
            self.assert_rejected(bad, 'type')

    def test_empty_fields_and_unknown_enums_are_rejected(self):
        for field, bad, keyword in [('criterion_id', '', 'minLength'), ('reason', '', 'minLength'),
                                     ('evidence_ids', [], 'minItems'), ('evidence_ids', [''], 'minLength'),
                                     ('status', 'success', 'enum'), ('applicability', 'yes', 'enum')]:
            raw = copy.deepcopy(self.raw)
            raw['checks'][0][field] = bad
            self.assert_rejected(raw, keyword)
        self.assert_rejected({'checks': []}, 'minItems')
        self.raw['checks'][0]['reason'] = ' \n\t'
        self.assert_rejected(self.raw)

    def test_duplicate_keys_nonfinite_values_and_trailing_text_are_invalid_json(self):
        valid = json.dumps(self.raw)
        examples = [valid.replace('"status": "fail"', '"status": "pass", "status": "fail"'),
                    '{"checks":[],"checks":' + json.dumps(self.raw['checks']) + '}',
                    '```json\n' + valid + '\n```', valid + '{}']
        for number in ['NaN', 'Infinity', '-Infinity', '1e999']:
            examples.append(valid.replace('"status": "fail"', '"status": ' + number))
        for raw in examples:
            with self.subTest(prefix=raw[:70]):
                self.assert_rejected(raw, 'json_parse')
        # Valid JSON arrays may parse on newer Python or hit an older parser's
        # nesting limit. Neither path may accept them as a review object.
        nested = self.assert_rejected('[' * 2000 + ']' * 2000)
        self.assertIn(nested[0]['validation_errors'][0]['keyword'], {'type', 'json_parse'})

    def test_schema_does_not_replace_evidence_and_applicability_checks(self):
        for change in [{'criterion_id': 'not-selected'}, {'evidence_ids': ['not-observed']},
                       {'applicability': 'unknown', 'status': 'pass'},
                       {'applicability': 'not_applicable', 'status': 'pass'},
                       {'applicability': 'not_applicable', 'status': 'not_applicable', 'evidence_ids': ['report']}]:
            raw = copy.deepcopy(self.raw)
            raw['checks'][0].update(change)
            self.assertEqual(review_schema.validate_structure(raw), [])
            self.assert_rejected(raw)
        raw = copy.deepcopy(self.raw)
        raw['checks'] *= 2
        self.assert_rejected(raw)
        raw = copy.deepcopy(self.raw)
        raw['checks'][0]['status'] = 'pass'
        self.packet['evidence']['report']['complete'] = False
        self.assert_rejected(raw)

    def test_one_invalid_item_invalidates_the_whole_review_without_partial_acceptance(self):
        other = copy.deepcopy(self.criterion)
        other['id'] = 'other-semantic-criterion'
        self.criteria.append(other)
        self.raw['checks'].append({**self.raw['checks'][0], 'criterion_id': other['id'], 'title': None})
        result = self.assert_rejected(self.raw, 'additionalProperties')
        self.assertEqual(len(result), 2)

    def test_missing_dependency_is_an_explicit_error_not_silent_validation_bypass(self):
        with patch.dict(sys.modules, {'jsonschema': None}):
            with self.assertRaisesRegex(EvaluationError, 'requires jsonschema'):
                review_schema.require_validator()


if __name__ == '__main__':
    unittest.main()
