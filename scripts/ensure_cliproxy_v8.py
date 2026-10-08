#!/usr/bin/env python3
"""Migrate a running CLIProxyAPI through its v8 API and verify local health."""

import argparse
import copy
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--url', required=True)
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
    logs['logging-to-file'] = True
    current = Path(args.config).read_text()
    migrated = not re.search(r'^config-version:\s*8\s*(?:#.*)?$', current, re.M)
    changed = migrated or before != expected
    if changed:
        result = request('/v8/management/config', management_key, 'PATCH', {
            'observability': {'logs': {'logging-to-file': True}},
        })
        assert result.get('status') == 'ok' and result.get('config-version') == 8

    after = request('/v8/management/config', management_key)
    assert after == expected, 'Configuration changed beyond enabling file logging'
    assert re.search(r'^config-version:\s*8\s*(?:#.*)?$', Path(args.config).read_text(), re.M)
    models = request('/v1/models', client_key)
    assert models.get('object') == 'list' and models.get('data'), 'No models available'
    report = request('/v8/management/observability/logs?limit=2', management_key)
    assert report.get('line-count', 0) > 0, 'File log has no entries'
    credentials = request('/v8/management/credentials', management_key)
    print('Migrated config to v8' if migrated else 'Updated file logging' if changed else 'Config already uses v8')
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
