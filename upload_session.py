"""Upload the exact input ZIP, with its external ID, byte length and SHA-256."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('archive', type=Path)
    parser.add_argument('--server', default='http://127.0.0.1:8000')
    args = parser.parse_args()
    with zipfile.ZipFile(args.archive) as archive:
        manifests = [n for n in archive.namelist() if n.endswith('/session.json')]
        if len(manifests) != 1:
            parser.error('Expected exactly one session.json')
        sid = json.loads(archive.read(manifests[0]))['session_id']
    with args.archive.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
        source.seek(0)
        # CLI deliberately accepts no token argument, so secrets stay out of shell history.
        with httpx.Client(timeout=120, trust_env=False) as client:
            response = client.post(args.server.rstrip('/') + '/api/v1/sessions',
                data={'session_id': sid, 'client_zip_size_bytes': str(args.archive.stat().st_size), 'client_zip_sha256': digest},
                files={'zip': (args.archive.name, source, 'application/zip')})
    print(json.dumps({'http_status': response.status_code, 'body': response.json()}, ensure_ascii=False, indent=2))
    response.raise_for_status()


if __name__ == '__main__':
    main()
