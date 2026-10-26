"""Real Uvicorn + TCP test. No TestClient; report includes only public wire data."""
import argparse
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


def run(sample):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='injidong-wire-') as temp:
        env = dict(os.environ, INJIDONG_DATA_DIR=temp, INJIDONG_API_TOKEN='')
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
                '--port', str(port)], env=env, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            try:
                origin = f'http://127.0.0.1:{port}'
                with httpx.Client(base_url=origin, timeout=60, trust_env=False) as client:
                    for _ in range(100):
                        try:
                            if client.get('/health').status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if process.poll() is not None:
                            raise RuntimeError('Uvicorn exited before becoming ready')
                        time.sleep(.1)
                    else:
                        raise RuntimeError('Server startup timed out')
                    size = sample.stat().st_size
                    with sample.open('rb') as source:
                        digest = hashlib.file_digest(source, 'sha256').hexdigest()
                    fields = {'session_id': 'd405_1792750256071', 'client_zip_size_bytes': str(size), 'client_zip_sha256': digest}
                    with sample.open('rb') as source:
                        result = client.post('/api/v1/sessions', data=fields, files={'zip': (sample.name, source, 'application/zip')})
                    assert result.status_code == 201, result.text
                    created = result.json()
                    with sample.open('rb') as source:
                        repeat = client.post('/api/v1/sessions', data=fields, files={'zip': (sample.name, source, 'application/zip')})
                    assert repeat.status_code == 200
                    assert repeat.json()['session'] == created['session']
                    page = client.get('/api/v1/sessions?limit=50')
                    assert page.status_code == 200 and page.json() == {'items': [created['session']], 'next_cursor': None}
                    headers = None
                    for _ in range(2):
                        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=60)
                        try:
                            conn.request('GET', created['session']['download_path'], headers={'Accept': 'application/zip', 'Accept-Encoding': 'identity'})
                            response = conn.getresponse()
                            assert response.status == 200
                            headers = response.getheaders()
                            for name in ('content-length', 'x-session-id', 'x-archive-sha256'):
                                assert sum(k.lower() == name for k, v in headers) == 1
                            assert response.getheader('Transfer-Encoding') is None
                            assert response.getheader('Content-Encoding') is None
                            assert response.getheader('Content-Type') == 'application/zip'
                            assert response.getheader('X-Session-ID') == fields['session_id']
                            assert response.getheader('X-Archive-SHA256') == digest
                            assert int(response.getheader('Content-Length')) == size
                            received, hasher = 0, hashlib.sha256()
                            while block := response.read(65536):
                                received += len(block)
                                hasher.update(block)
                            assert received == size and hasher.hexdigest() == digest
                        finally:
                            conn.close()
                    missing = client.get('/api/v1/sessions/no_such_session/archive')
                    assert missing.status_code == 404
                    bad_cursor = client.get('/api/v1/sessions?cursor=wrong')
                    assert bad_cursor.status_code == 400
                    return {'sample_bytes': size, 'sample_sha256': digest, 'upload_status': result.status_code,
                        'duplicate_status': repeat.status_code, 'first_page': page.json(),
                        'download_headers': dict(headers), 'identical_downloads_verified': 2,
                        'not_found': missing.json(), 'invalid_cursor': bad_cursor.json(),
                        'scope': 'Real Uvicorn loopback HTTP. Android upload and actual PC GUI not exercised.'}
            finally:
                process.terminate()
                process.wait(timeout=15)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('sample', type=Path)
    parser.add_argument('--output', type=Path, default=Path('WIRE_VERIFIED.json'))
    args = parser.parse_args()
    report = run(args.sample)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
