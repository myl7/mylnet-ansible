#!/usr/bin/env python3
"""Migrate a running CLIProxyAPI through its v8 API and verify local health."""

import argparse
import copy
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--compatible-model', action='append', default=[],
                        help='Preserve Claude thinking history for this model name or alias')
    args = parser.parse_args()
    management_key = os.environ['CLIPROXY_MANAGEMENT_KEY']
    client_key = os.environ['CLIPROXY_CLIENT_KEY']
    server_version = None

    def request(path, key, method='GET', body=None):
        nonlocal server_version
        data = None if body is None else json.dumps(body).encode()
        req = Request(args.url + path, data=data, method=method, headers={
            'Authorization': 'Bearer ' + key,
            'Content-Type': 'application/json',
        })
        with urlopen(req, timeout=10) as response:
            version = response.headers.get('X-CPA-VERSION')
            if version:
                server_version = version
            return json.load(response)

    for attempt in range(30):
        try:
            before = request('/v8/management/config', management_key)
            break
        except URLError as exc:
            if isinstance(exc, HTTPError) or attempt == 29:
                raise
            time.sleep(1)

    assert before['config-version'] == 8, 'Server does not expose v8 configuration'
    expected = copy.deepcopy(before)
    logs = expected.setdefault('observability', {}).setdefault('logs', {})
    logging_changed = logs.get('logging-to-file') is not True
    logs['logging-to-file'] = True
    compatibility_changed = False
    for model_name in args.compatible_model:
        matches = [
            model
            for provider in expected.get('api-keys', {}).get('openai-compatibility', [])
            for model in provider.get('models', [])
            if model_name in (model.get('name'), model.get('alias'))
        ]
        assert matches, 'Compatible model is not configured: ' + model_name
        for model in matches:
            if model.get('is-compat') is not True:
                model['is-compat'] = True
                compatibility_changed = True
    current = Path(args.config).read_text()
    migrated = not re.search(r'^config-version:\s*8\s*(?:#.*)?$', current, re.MULTILINE)
    changed = migrated or before != expected
    if changed:
        patch = {
            'observability': {'logs': {'logging-to-file': True}},
        }
        if compatibility_changed:
            patch['api-keys'] = {
                'openai-compatibility': expected['api-keys']['openai-compatibility'],
            }
        result = request('/v8/management/config', management_key, 'PATCH', patch)
        assert result.get('status') == 'ok' and result.get('config-version') == 8

    after = request('/v8/management/config', management_key)
    assert after == expected, 'Configuration changed beyond logging and model compatibility'
    assert re.search(r'^config-version:\s*8\s*(?:#.*)?$', Path(args.config).read_text(), re.MULTILINE)
    models = request('/v1/models', client_key)
    assert models.get('object') == 'list' and models.get('data'), 'No models available'
    report = request('/v8/management/observability/logs?limit=2', management_key)
    assert report.get('line-count', 0) > 0, 'File log has no entries'
    credentials = request('/v8/management/credentials', management_key)
    print('Migrated config to v8' if migrated else 'Updated file logging' if logging_changed else 'Config already uses v8')
    if compatibility_changed:
        print('Updated compatible models:', ', '.join(args.compatible_model))
    if server_version:
        print('Running version:', server_version)
    print('Verified authenticated models, management API, and nonempty file logs')
    print('Model count:', len(models['data']))
    print('Credential count:', len(credentials.get('files', [])))


if __name__ == '__main__':
    try:
        main()
    except HTTPError as exc:
        print('CLIProxyAPI verification failed: HTTP', exc.code, file=sys.stderr)
        sys.exit(1)
    except (URLError, AssertionError, KeyError, ValueError, OSError) as exc:
        # Responses and environment values can contain credentials. Report only
        # the error category instead of dumping their contents.
        print('CLIProxyAPI verification failed:', type(exc).__name__, file=sys.stderr)
        sys.exit(1)
