"""Bounded, read-only mobile ZIP validation. Never re-encode the archive."""
import hashlib
import io
import json
import math
import re
import stat
import sys
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vendor/injidong_contract/src'))
from contract import ContractError, FramePacket, validate_frame, validate_json

MAX_ARCHIVE = 128 * 1024**2
MAX_FILE = 32 * 1024**2
MAX_TOTAL = 256 * 1024**2
ID_PATTERN = r'[A-Za-z0-9_-]{1,128}'


class InvalidArchive(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise InvalidArchive(message)


def safe_path(value):
    require(isinstance(value, str) and value and '\\' not in value and ':' not in value,
            'Invalid relative path')
    p = PurePosixPath(value)
    require(not p.is_absolute() and all(x not in ('', '.', '..') for x in value.rstrip('/').split('/')),
            'Unsafe archive path')
    return value


def json_value(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Duplicate JSON key')
            result[key] = value
        return result
    def constant(value):
        raise InvalidArchive('Nonfinite JSON number')
    def finite_float(value):
        result = float(value)
        require(math.isfinite(result), 'Nonfinite JSON number')
        return result
    return json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_float)


def checked_members(z, total_limit=MAX_TOTAL):
    infos = z.infolist()
    require(len(infos) <= 4096, 'Too many ZIP entries')
    require(len({i.filename.casefold() for i in infos}) == len(infos), 'Duplicate ZIP names')
    require(sum(i.file_size for i in infos) <= total_limit, 'Expanded ZIP too large')
    for i in infos:
        safe_path(i.filename)
        require(not stat.S_ISLNK(i.external_attr >> 16), 'Symlink forbidden')
        require(not i.flag_bits & 1, 'Encrypted entry forbidden')
        require(i.file_size <= MAX_FILE, 'ZIP entry too large')
        require(i.file_size / max(1, i.compress_size) <= 200, 'Compression ratio exceeds limit')
    return {i.filename: i for i in infos if not i.is_dir()}


def inspect_archive(path, session_id):
    require(bool(re.fullmatch(ID_PATTERN, session_id)), 'ASCII session ID required by server profile')
    try:
        return _inspect(path, session_id)
    except InvalidArchive:
        raise
    except ContractError as exc:
        raise InvalidArchive(exc.code) from exc
    except (ValueError, KeyError, TypeError, OSError, OverflowError, EOFError, zipfile.BadZipFile,
            NotImplementedError, RuntimeError, Image.DecompressionBombError) as exc:
        raise InvalidArchive('Invalid mobile ZIP structure or payload') from exc


def _inspect(path, session_id):
    warnings = Counter()
    with zipfile.ZipFile(path) as z:
        members = checked_members(z)
        root = session_id + '/'
        require(all(n.startswith(root) for n in z.namelist()), 'ZIP root/session_id mismatch')
        require(z.testzip() is None, 'ZIP CRC mismatch')

        def read(relative):
            name = root + safe_path(relative)
            require(name in members, 'Referenced file missing')
            with z.open(name) as stream:
                data = stream.read(MAX_FILE + 1)
            require(len(data) == members[name].file_size and len(data) <= MAX_FILE, 'Entry size mismatch')
            return data

        def obj(relative):
            return json_value(read(relative))

        def rows(relative):
            return [json_value(line) for line in read(relative).splitlines() if line.strip()]

        session = obj('session.json')
        require(session['session_id'] == session_id and session['status'] == 'complete', 'Session not complete or ID mismatch')
        require(session['manifest_version'] == '1.0.0', 'Unsupported manifest version')
        files = session['files']
        require(files['depth_index'] == 'frames.jsonl' and files['images_index'] == 'images.jsonl'
                and files['imu_samples'] == 'imu.jsonl', 'Unsupported manifest paths')
        frames, images, imu = rows('frames.jsonl'), rows('images.jsonl'), rows('imu.jsonl')
        actual = {'depth_frames': len(frames), 'rgb_images': len(images), 'imu_samples': len(imu)}
        require(all(type(session['counts'][k]) is int and session['counts'][k] == v for k, v in actual.items()), 'Session counts mismatch')
        require(0 < len(frames) <= 128, 'Depth frame count exceeds limits')
        seen = set()
        total_pixels = 0
        for entry in frames:
            require(set(entry) == {'frame_id', 'metadata_path'}, 'Invalid frame index')
            fid = entry['frame_id']
            require(isinstance(fid, str) and fid not in seen, 'Duplicate frame ID')
            seen.add(fid)
            meta = obj(entry['metadata_path'])
            validate_json(meta, 'frame')
            require(meta['session_id'] == session_id and meta['frame_id'] == fid, 'Frame identity mismatch')
            shape = (meta['depth']['height'], meta['depth']['width'])
            pixels = shape[0] * shape[1]
            total_pixels += pixels
            require(pixels <= 4_000_000 and total_pixels <= 16_000_000, 'Depth pixel limit exceeded')
            storage = meta['storage']
            require(storage['depth_key'] == 'depth_m' and storage['valid_mask_key'] == 'valid_mask'
                    and storage['target_mask_key'] in (None, 'target_mask'), 'Unsupported NPZ key mapping')
            parent = str(PurePosixPath(entry['metadata_path']).parent)
            arr_path = safe_path(storage['file']) if parent == '.' else parent + '/' + safe_path(storage['file'])
            payload = read(arr_path)
            require(hashlib.sha256(payload).hexdigest() == storage['sha256'], 'NPZ SHA mismatch')
            expected = {'depth_m', 'valid_mask'} | ({'target_mask'} if storage['target_mask_key'] else set())
            with zipfile.ZipFile(io.BytesIO(payload)) as nested:
                nested_members = checked_members(nested)
                require(set(nested_members) == {k + '.npy' for k in expected}, 'Unexpected NPZ arrays')
                # Validate NPY headers before NumPy allocation, including hostile declared shapes.
                for name, info in nested_members.items():
                    with nested.open(name) as stream:
                        version = np.lib.format.read_magic(stream)
                        require(version in ((1, 0), (2, 0)), 'Unsupported NPY version')
                        parser = np.lib.format.read_array_header_1_0 if version == (1, 0) else np.lib.format.read_array_header_2_0
                        arr_shape, _, dtype = parser(stream, max_header_size=10000)
                        require(arr_shape == shape and not dtype.hasobject, 'NPY shape/object mismatch')
                        require(dtype == (np.dtype('float32') if name == 'depth_m.npy' else np.dtype('bool')), 'NPY dtype mismatch')
                        require(info.file_size - stream.tell() == pixels * dtype.itemsize, 'NPY payload size mismatch')
            with np.load(io.BytesIO(payload), allow_pickle=False) as arrays:
                packet = FramePacket(meta, arrays['depth_m'], arrays['valid_mask'], arrays['target_mask'] if 'target_mask' in expected else None)
                validate_frame(packet)
            raw = meta['provenance']['raw_file']
            if raw is not None:
                require(hashlib.sha256(read(raw)).hexdigest() == meta['provenance']['raw_sha256'], 'Raw SHA mismatch')

        image_paths = set()
        for image in images:
            require(image['session_id'] == session_id and image['frame_id'] in seen, 'RGB identity mismatch')
            require(image['file'] not in image_paths, 'Duplicate RGB path')
            image_paths.add(image['file'])
            data = read(image['file'])
            require(hashlib.sha256(data).hexdigest() == image['sha256'], 'RGB SHA mismatch')
            with Image.open(io.BytesIO(data)) as png:
                require(png.format == 'PNG' and png.width * png.height <= 4_000_000, 'Invalid RGB PNG')
                require(type(image['width']) is int and type(image['height']) is int
                        and png.size == (image['width'], image['height']), 'RGB dimensions mismatch')
                png.load()
                if image['encoding'] == 'RGB8' and png.mode == 'RGBA':
                    warnings['RGB_ENCODING_CHANNEL_MISMATCH'] += 1
                else:
                    require((image['encoding'], png.mode) in (('RGB8', 'RGB'), ('RGBA8', 'RGBA')), 'Unsupported RGB encoding')
        require(all(isinstance(row, dict) for row in imu), 'Invalid IMU rows')
        return {'counts': actual, 'warnings': [{'code': k, 'count': v} for k, v in sorted(warnings.items())]}
