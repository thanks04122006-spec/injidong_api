"""Serve existing review arrays; never reconstruct or generate point clouds."""
import json
import os
import struct
import sqlite3
from contextlib import closing
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, JSONResponse

MAX_POINTS = 250_000


def router(data):
    routes = APIRouter()

    @routes.get('/api/v1/pointclouds/review')
    def review():
        source = os.environ.get('INJIDONG_REVIEW_FILE')
        if not source:
            config = Path(data) / 'pointcloud-source.json'
            if config.is_file():
                source = json.loads(config.read_text('utf-8'))['file']
        if not source or not Path(source).is_file():
            raise HTTPException(404, detail={'code': 'POINTCLOUD_NOT_FOUND', 'message': 'Review file is not configured or unavailable'})
        return encode(source)

    @routes.get('/api/v1/sessions/{session_id}/pointcloud')
    def session_cloud(session_id: str):
        import re
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session_id):
            raise HTTPException(400, detail={'code': 'INVALID_SESSION_ID', 'message': 'Invalid session ID'})
        with closing(sqlite3.connect(Path(data) / 'sessions.sqlite3')) as connection:
            found = connection.execute('SELECT 1 FROM sessions WHERE session_id=?', (session_id,)).fetchone()
        if not found:
            raise HTTPException(404, detail={'code': 'SESSION_NOT_FOUND', 'message': 'Session is not uploaded'})
        source = Path(data) / 'pointclouds' / (session_id + '.npz')
        if not source.is_file():
            return JSONResponse({'session_id': session_id, 'status': 'pending'}, status_code=202,
                                headers={'Retry-After': '3', 'Cache-Control': 'no-store'})
        response = encode(source)
        response.headers['X-Session-Id'] = session_id
        response.headers['X-Pointcloud-Id'] = session_id
        return response

    def encode(source):
        try:
            with np.load(source, allow_pickle=False) as archive:
                xyz = archive['points_m']
                if xyz.ndim != 2 or xyz.shape[1] != 3 or not 0 < len(xyz) <= MAX_POINTS:
                    raise ValueError('Unsupported point count or shape')
                if not np.isfinite(xyz).all():
                    raise ValueError('Non-finite coordinates')
                rgb = archive['rgb_u8'] if 'rgb_u8' in archive else None
                if rgb is not None:
                    if rgb.shape != xyz.shape or rgb.dtype != np.uint8:
                        raise ValueError('Invalid RGB')
                    rgb = rgb.astype(np.float32) / 255
                    if 'color_candidate' in archive:
                        mask = archive['color_candidate']
                        if mask.shape != (len(xyz),) or mask.dtype != np.bool_:
                            raise ValueError('Invalid color mask')
                        rgb[~mask] = .57
                packed = np.empty((len(xyz), 6), dtype='<f4')
                packed[:, :3] = xyz
                packed[:, 3:] = rgb if rgb is not None else .57
                if not np.isfinite(packed).all():
                    raise ValueError('Coordinates exceed float32')
        except (ValueError, KeyError, OSError):
            raise HTTPException(422, detail={'code': 'INVALID_POINTCLOUD', 'message': 'Unsupported review arrays'})
        # IPC1 + uint32 count + count * (XYZ metres, RGB 0..1), all little-endian.
        body = b'IPC1' + struct.pack('<I', len(packed)) + packed.tobytes()
        return Response(body, media_type='application/octet-stream', headers={
            'Cache-Control': 'no-store', 'X-Pointcloud-Id': 'review',
            'X-Pointcloud-Unit': 'm', 'X-Pointcloud-Count': str(len(packed))})

    return routes
