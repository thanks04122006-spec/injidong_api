"""Serve existing review arrays; never reconstruct or generate point clouds."""
import json
import os
import struct
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

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
