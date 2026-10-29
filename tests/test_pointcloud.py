import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient
from app.main import create_app


class PointCloudTests(unittest.TestCase):
    def test_auth_and_missing_source(self):
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {'INJIDONG_REVIEW_FILE': ''}):
            with TestClient(create_app(data, token='test-only')) as client:
                self.assertEqual(client.get('/api/v1/pointclouds/review').status_code, 401)
                response = client.get('/api/v1/pointclouds/review', headers={'Authorization': 'Bearer test-only'})
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()['error']['code'], 'POINTCLOUD_NOT_FOUND')

    def test_existing_review_round_trip(self):
        source = os.environ.get('INJIDONG_REVIEW_TEST_FILE')
        if not source:
            self.skipTest('Set INJIDONG_REVIEW_TEST_FILE to existing review_arrays.npz')
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {'INJIDONG_REVIEW_FILE': ''}):
            (Path(data) / 'pointcloud-source.json').write_text(json.dumps({'file': source}), encoding='utf-8')
            with TestClient(create_app(data, token='test-only')) as client:
                response = client.get('/api/v1/pointclouds/review', headers={'Authorization': 'Bearer test-only'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content[:4], b'IPC1')
            count = struct.unpack('<I', response.content[4:8])[0]
            self.assertEqual(len(response.content), 8 + count * 24)
            points = np.frombuffer(response.content[8:], dtype='<f4').reshape(count, 6)
            with np.load(source, allow_pickle=False) as original:
                np.testing.assert_array_equal(points[:, :3], original['points_m'])
                colors = original['rgb_u8'].astype(np.float32)/255
                colors[~original['color_candidate']] = .57
                np.testing.assert_array_equal(points[:, 3:], colors)
            self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_invalid_source(self):
        with tempfile.TemporaryDirectory() as data:
            source = Path(data)/'invalid.npz'
            source.write_bytes(b'not a point cloud')
            with patch.dict(os.environ, {'INJIDONG_REVIEW_FILE': str(source)}):
                with TestClient(create_app(data, token='')) as client:
                    self.assertEqual(client.get('/api/v1/pointclouds/review').status_code, 422)
