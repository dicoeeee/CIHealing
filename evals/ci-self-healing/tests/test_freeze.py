"""New freeze defaults and legacy snapshot immutability; no runtime/model access."""
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import EvalError, digest, json_bytes, read_json, snapshot, write_json, write_new
from freeze import freeze


class FreezeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        self.root = self.repo / 'evals/ci-self-healing'
        self.previous = self.root / 'runs/previous'
        self.cache = self.previous / 'cache'
        write_new(self.cache / 'synthetic.jar', 'not a real dependency')
        write_new(self.root / 'eval.py', '# synthetic engine\n')
        self.runtime = {'backend': 'native', 'maven_repository': str(self.cache), 'timeout_seconds': 600,
                        'max_tool_calls': 80, 'model_profile': '/external/selected-profile'}
        self.config = {'backend': 'native', 'runtime': str(self.previous / 'runtime.json'),
                       'max_agent_invocations': 6, 'max_wall_seconds': 5400,
                       'reviewer': {'timeout_seconds': 900}}
        write_json(self.previous / 'freeze-manifest.json', {'maven_cache_sha256': digest(json_bytes(snapshot(self.cache, skip_git=False))),
                   'scope': ['synthetic'], 'exclusions': [], 'external_dependencies': []})
        write_json(self.previous / 'runtime.json', self.runtime)
        self.before = snapshot(self.previous, skip_git=False)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target, value in [('freeze.REPO', self.repo), ('freeze.ROOT', self.root)]:
            self.stack.enter_context(mock.patch(target, value))
        for target, value in [('freeze.load_config', self.config), ('freeze.load_runtime', self.runtime),
                              ('freeze.skill_from_dir', {'SKILL.md': b'---\nname: test\n---\n'}),
                              ('freeze.engine_digest', 'synthetic-engine'), ('freeze.command', b'unchanged-head\n')]:
            # Return copies to model the real loaders and detect actual disk mutation.
            self.stack.enter_context(mock.patch(target, side_effect=lambda *a, v=value, **kw: v.copy() if isinstance(v, dict) else v))

    def test_new_freeze_uses_three_workers_and_900_seconds_without_changing_budget_or_old_run(self):
        output = self.root / 'runs/new'
        result = freeze(self.previous, output)
        runtime = read_json(output / 'snapshot/config/runtime.json')
        config = read_json(output / 'snapshot/config/batch.json')
        self.assertEqual(runtime['timeout_seconds'], 900)
        self.assertEqual(config['max_parallel_trials'], 3)
        self.assertEqual(config['max_agent_invocations'], 6)
        self.assertEqual(config['max_wall_seconds'], 5400)
        self.assertEqual(config['reviewer'], self.config['reviewer'])
        self.assertEqual(runtime['max_tool_calls'], 80)
        self.assertEqual(runtime['model_profile'], self.runtime['model_profile'])
        self.assertFalse(result['models_called'])
        self.assertEqual(snapshot(self.previous, skip_git=False), self.before)

    def test_explicit_serial_and_timeout_overrides_are_frozen(self):
        output = self.root / 'runs/serial'
        freeze(self.previous, output, repair_timeout_seconds=700, max_parallel_trials=1)
        self.assertEqual(read_json(output / 'snapshot/config/runtime.json')['timeout_seconds'], 700)
        self.assertEqual(read_json(output / 'snapshot/config/batch.json')['max_parallel_trials'], 1)

    def test_invalid_limits_do_not_create_partial_freeze(self):
        output = self.root / 'runs/invalid'
        for kw in ({'repair_timeout_seconds': 0}, {'repair_timeout_seconds': True}, {'repair_timeout_seconds': 1801},
                   {'max_parallel_trials': 4}, {'max_parallel_trials': False}):
            with self.subTest(kw=kw), self.assertRaises(EvalError):
                freeze(self.previous, output, **kw)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
