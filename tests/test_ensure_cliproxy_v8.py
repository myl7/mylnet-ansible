import contextlib
import copy
import importlib.util
import io
import json
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'ensure_cliproxy_v8.py'
spec = importlib.util.spec_from_file_location('ensure_cliproxy_v8', MODULE_PATH)
assert spec is not None and spec.loader is not None
ensure_cliproxy_v8 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ensure_cliproxy_v8)


class Response(io.BytesIO):
    headers: dict[str, str]


class CompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.config: dict[str, Any] = {
            'config-version': 8,
            'observability': {'logs': {'logging-to-file': True, 'request-log': False}},
            'plugins': {'configs': {'jshandler': {'enabled': True}}},
            'api-keys': {'openai-compatibility': [
                {'name': 'cityu', 'keys': [{'api-key': 'test-key'}], 'models': [
                    {'name': 'CS/DeepSeek-V4-Flash-FP8', 'alias': 'deepseek-v4-flash'},
                    {'name': 'another-model'},
                ]},
                {'name': 'other-provider', 'models': [{'name': 'other-model'}]},
            ]},
        }
        self.patches = []

    def request(self, req, timeout):
        path = req.full_url.split('localhost', 1)[1]
        if req.get_method() == 'PATCH':
            patch = json.loads(req.data)
            self.patches.append(patch)
            self.config['observability']['logs'].update(patch['observability']['logs'])
            if 'api-keys' in patch:
                self.config['api-keys'].update(patch['api-keys'])
            result = {'status': 'ok', 'config-version': 8}
        elif path == '/v8/management/config':
            result = copy.deepcopy(self.config)
        elif path == '/v1/models':
            result = {'object': 'list', 'data': [{'id': 'deepseek-v4-flash'}]}
        elif path.startswith('/v8/management/observability/logs'):
            result = {'line-count': 1}
        elif path == '/v8/management/credentials':
            result = {'files': []}
        else:
            raise AssertionError(path)
        response = Response(json.dumps(result).encode())
        response.headers = {}
        return response

    def run_check(self, model='deepseek-v4-flash'):
        with (
            mock.patch('sys.argv', ['ensure_cliproxy_v8.py', '--config', '/test/config.yaml',
                                   '--url', 'http://localhost', '--compatible-model', model]),
            mock.patch.dict('os.environ', {'CLIPROXY_MANAGEMENT_KEY': 'test-management',
                                         'CLIPROXY_CLIENT_KEY': 'test-client'}),
            mock.patch.object(ensure_cliproxy_v8, 'urlopen', side_effect=self.request),
            mock.patch.object(Path, 'read_text', return_value='config-version: 8\n'),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            ensure_cliproxy_v8.main()

    def test_enables_matching_model_and_preserves_other_settings(self):
        expected = copy.deepcopy(self.config)
        expected['api-keys']['openai-compatibility'][0]['models'][0]['is-compat'] = True
        self.run_check()
        self.assertEqual(self.config, expected)
        self.assertEqual(len(self.patches), 1)

    def test_is_idempotent(self):
        self.config['api-keys']['openai-compatibility'][0]['models'][0]['is-compat'] = True
        self.run_check()
        self.assertEqual(self.patches, [])

    def test_missing_model_fails_before_patching(self):
        with self.assertRaisesRegex(AssertionError, 'Compatible model is not configured'):
            self.run_check('typo')
        self.assertEqual(self.patches, [])


if __name__ == '__main__':
    unittest.main()
