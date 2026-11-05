"""Read-only check of the five published experiment documents before collection."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen


def get(api, path):
    with urlopen(api.rstrip('/') + path, timeout=30) as response:
        return json.load(response)


def verify_versions(environment, docs, api):
    verified = []
    for expected in environment['published_versions']:
        query = urlencode({'policy_id': expected['policy_id'], 'scope': expected['scope']})
        matches = [v for v in get(api, '/api/versions?' + query)['versions']
                   if v['version_label'] == expected['version_label']]
        if len(matches) != 1:
            raise ValueError(f"Expected one version: {expected['policy_id']}/{expected['version_label']}")
        actual = matches[0]
        for key in ('policy_id', 'version_label', 'scope', 'valid_from', 'valid_to'):
            if actual.get(key) != expected.get(key):
                raise ValueError(f"Version mismatch: {expected['policy_id']} {key}")
        if actual.get('job_status') != 'ready' or actual.get('publication_status') != 'published':
            raise ValueError('Every experiment version must be ready and published')
        filename = actual['filename']
        if filename not in environment['documents'] or Path(filename).name != filename:
            raise ValueError('Unexpected document filename')
        digest = hashlib.sha256((docs / filename).read_bytes()).hexdigest()
        if digest != environment['documents'][filename] or actual.get('sha256') != digest:
            raise ValueError(f'Document hash mismatch: {filename}')
        if not actual.get('store_id') or not actual.get('version_id'):
            raise ValueError('Missing version/store identity')
        verified.append({**expected, 'version_id': actual['version_id'],
                         'store_id': actual['store_id'], 'filename': filename, 'sha256': digest})
    if {v['filename'] for v in verified} != set(environment['documents']):
        raise ValueError('Published versions do not cover the document inventory')
    return verified


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--environment', type=Path, required=True)
    p.add_argument('--docs', type=Path, default=Path('dataset/docs'))
    p.add_argument('--api', default='http://127.0.0.1:8089')
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    env = json.loads(a.environment.read_text(encoding='utf-8'))
    # Version/store IDs are read from the API. Other environment facts are supplied by the operator.
    manual = {k: v for k, v in env.items() if k != 'published_versions'}
    if 'FILL_FROM_YOUR_SERVER' in json.dumps(manual):
        raise ValueError('Fill the actual environment values first; do not copy the example run IDs')
    env['published_versions'] = verify_versions(env, a.docs, a.api)
    env['preflight_note'] = ('Read-only metadata/hash check. Model, software and configuration '
                             'values are operator-provided, not remotely attested.')
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open('x', encoding='utf-8') as f:
        json.dump(env, f, ensure_ascii=False, indent=2)
    print(f'Verified {len(env["published_versions"])} published versions: {a.output}')


if __name__ == '__main__':
    main()
