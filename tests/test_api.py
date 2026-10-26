import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient
from app.main import create_app

FIXTURE = Path(__file__).parent / 'fixtures'


def archive(sid='test_1', mutation=None):
    metadata = json.loads((FIXTURE / 'metadata.json').read_text('utf-8'))
    metadata['session_id'] = sid
    files = {'frames.jsonl': json.dumps({'frame_id': metadata['frame_id'], 'metadata_path': 'frame/metadata.json'}).encode(),
             'images.jsonl': b'', 'imu.jsonl': b'', 'frame/metadata.json': json.dumps(metadata).encode(),
             'frame/arrays.npz': (FIXTURE / 'arrays.npz').read_bytes(),
             'session.json': json.dumps({'manifest_version': '1.0.0', 'session_id': sid, 'status': 'complete',
                'counts': {'depth_frames': 1, 'rgb_images': 0, 'imu_samples': 0},
                'files': {'depth_index': 'frames.jsonl', 'images_index': 'images.jsonl', 'imu_samples': 'imu.jsonl'}}).encode()}
    if mutation:
        mutation(files)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, payload in files.items():
            z.writestr(sid + '/' + name, payload)
    return stream.getvalue()


def upload(client, data, sid='test_1', **overrides):
    fields = {'session_id': sid, 'client_zip_size_bytes': str(len(data)),
              'client_zip_sha256': hashlib.sha256(data).hexdigest()}
    fields.update(overrides)
    return client.post('/api/v1/sessions', data=fields, files={'zip': ('session.zip', data, 'application/zip')})


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(self.temp.name, token=''))

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_upload_list_download_duplicate_restart(self):
        data = archive()
        result = upload(self.client, data)
        self.assertEqual(result.status_code, 201, result.text)
        record = result.json()['session']
        repeat = upload(self.client, data)
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat.json(), {'disposition': 'existing', 'session': record})
        page = self.client.get('/api/v1/sessions').json()
        self.assertEqual(page, {'items': [record], 'next_cursor': None})
        with TestClient(create_app(self.temp.name, token='')) as restarted:
            response = restarted.get(record['download_path'])
        self.assertEqual(response.content, data)
        self.assertEqual(response.headers['x-archive-sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(int(response.headers['content-length']), len(data))
        self.assertEqual(response.headers['x-session-id'], 'test_1')
        self.assertNotIn('transfer-encoding', response.headers)
        self.assertEqual(list(Path(self.temp.name).glob('upload-*')), [])

    def test_conflict_keeps_original(self):
        first = archive()
        self.assertEqual(upload(self.client, first).status_code, 201)
        other = archive(mutation=lambda f: f.update({'extra.json': b'{}'}))
        self.assertEqual(upload(self.client, other).status_code, 409)
        self.assertEqual(self.client.get('/api/v1/sessions/test_1/archive').content, first)

    def test_hash_size_id_limits(self):
        data = archive()
        for fields, status, code in [({'client_zip_sha256': '0' * 64}, 400, 'HASH_MISMATCH'),
                ({'client_zip_size_bytes': str(len(data) + 1)}, 400, 'SIZE_MISMATCH'),
                ({'client_zip_size_bytes': '134217729'}, 413, 'LIMIT_EXCEEDED'),
                ({'session_id': '../escape'}, 400, 'INVALID_SESSION_ID')]:
            with self.subTest(fields=fields):
                response = upload(self.client, data, **fields)
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(response.json()['error']['code'], code)
        self.assertEqual(self.client.get('/api/v1/sessions').json()['items'], [])

    def test_malformed_archive_rejected(self):
        mutations = [lambda f: f.update({'../escape': b'x'}),
                     lambda f: f.update({'frame/arrays.npz': b'broken'}),
                     lambda f: f.update({'session.json': b'{"session_id":1,"session_id":2}'}),
                     lambda f: f.update({'imu.jsonl': b'NaN'}),
                     lambda f: f.update({'imu.jsonl': b'{"value":1e999}'}),
                     lambda f: f.update({'frames.jsonl': f['frames.jsonl'] + b'\n' + f['frames.jsonl']})]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                response = upload(self.client, archive(mutation=mutation))
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.get('/api/v1/sessions').json()['items'], [])

    def test_pagination_tie_sort_invalid_cursor(self):
        for sid in ('test_3', 'test_1', 'test_2'):
            self.assertEqual(upload(self.client, archive(sid), sid).status_code, 201)
        with closing(sqlite3.connect(Path(self.temp.name) / 'sessions.sqlite3')) as conn:
            conn.execute("UPDATE sessions SET stored_at='2026-10-23T00:00:00.000000Z'")
            conn.commit()
        ids, cursor = [], None
        for _ in range(3):
            params = {'limit': 1}
            if cursor is not None:
                params['cursor'] = cursor
            page = self.client.get('/api/v1/sessions', params=params).json()
            ids.extend(x['session_id'] for x in page['items'])
            cursor = page['next_cursor']
        self.assertEqual(ids, ['test_1', 'test_2', 'test_3'])
        self.assertIsNone(cursor)
        for params in ({'cursor': ''}, {'cursor': 'wrong'}, {'limit': 201}, {'limit': 0}):
            self.assertEqual(self.client.get('/api/v1/sessions', params=params).status_code, 400)

    def test_concurrent_duplicate_uploads(self):
        data = archive()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: upload(self.client, data).status_code, range(2)))
        self.assertEqual(sorted(results), [200, 201])
        self.assertEqual(len(self.client.get('/api/v1/sessions').json()['items']), 1)

    def test_auth_errors_and_openapi(self):
        with TestClient(create_app(self.temp.name, token='test-only')) as client:
            self.assertEqual(client.get('/api/v1/sessions').status_code, 401)
            self.assertEqual(client.get('/api/v1/sessions', headers={'Authorization': 'Bearer test-only'}).status_code, 200)
            self.assertEqual(client.get('/health').status_code, 200)
        response = self.client.get('/api/v1/sessions/missing/archive')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['error']['code'], 'SESSION_NOT_FOUND')
        self.assertEqual(self.client.get('/openapi.json').status_code, 200)

    def test_multipart_body_limit(self):
        # Exercise receive limit without allocating a production-size 129 MiB body.
        with patch('app.main.MAX_ARCHIVE', 10):
            response = upload(self.client, b'x' * (1024**2 + 100))
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(response.json()['error']['code'], 'LIMIT_EXCEEDED')

    def test_npy_shape_rejected_before_loading(self):
        import numpy as np
        def mutation(files):
            stream = io.BytesIO()
            np.savez(stream, depth_m=np.ones((1, 1), dtype=np.float32), valid_mask=np.ones((1, 1), dtype=bool))
            payload = stream.getvalue()
            meta = json.loads(files['frame/metadata.json'])
            meta['storage']['sha256'] = hashlib.sha256(payload).hexdigest()
            files['frame/metadata.json'] = json.dumps(meta).encode()
            files['frame/arrays.npz'] = payload
        response = upload(self.client, archive(mutation=mutation))
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()['error']['message'], 'NPY shape/object mismatch')

    @unittest.skipUnless(os.environ.get('INJIDONG_SAMPLE_ZIP'), 'Set INJIDONG_SAMPLE_ZIP to real sample')
    def test_real_array_sample(self):
        data = Path(os.environ['INJIDONG_SAMPLE_ZIP']).read_bytes()
        sid = 'd405_1792750256071'
        response = upload(self.client, data, sid)
        self.assertEqual(response.status_code, 201, response.text)
        record = response.json()['session']
        self.assertEqual(record['warnings'], [{'code': 'RGB_ENCODING_CHANNEL_MISMATCH', 'count': 33}])
        self.assertEqual(record['zip_sha256'], 'e87e810013b49756ab450349baa10ef939f7a1ddd0f68e4308fc5144efcf0735')
        self.assertEqual(self.client.get(record['download_path']).content, data)


if __name__ == '__main__':
    unittest.main()
