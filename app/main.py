"""Mobile -> immutable SQLite archive -> PC. Run with python -m uvicorn app.main:app."""
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .validation import ID_PATTERN, MAX_ARCHIVE, InvalidArchive, inspect_archive

CHUNK = 65536


def error(status, code, message):
    raise HTTPException(status, detail={'code': code, 'message': message})


class RequestGate:
    """Bound multipart body before the framework finishes spooling it to disk."""
    def __init__(self, app, token):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        if self.token and scope['path'].startswith('/api/'):
            supplied = [v for k, v in scope['headers'] if k == b'authorization']
            expected = ('Bearer ' + self.token).encode('ascii')
            if len(supplied) != 1 or not hmac.compare_digest(supplied[0], expected):
                return await JSONResponse({'error': {'code': 'UNAUTHORIZED', 'message': 'Bearer authentication required'}}, 401)(scope, receive, send)
        size = 0

        async def bounded_receive():
            nonlocal size
            message = await receive()
            size += len(message.get('body', b''))
            if size > MAX_ARCHIVE + 1024**2:
                error(413, 'LIMIT_EXCEEDED', 'Request exceeds upload limit')
            return message

        await self.app(scope, bounded_receive, send)


def create_app(data_dir=None, token=None):
    data = Path(data_dir or os.environ.get('INJIDONG_DATA_DIR', Path(__file__).resolve().parents[1] / 'data'))
    data.mkdir(parents=True, exist_ok=True)
    database = data / 'sessions.sqlite3'
    token = os.environ.get('INJIDONG_API_TOKEN', '') if token is None else token
    if token and not all(33 <= ord(c) <= 126 for c in token):
        raise ValueError('Bearer token must use ASCII 33..126')

    def connect():
        conn = sqlite3.connect(database, timeout=60)
        conn.row_factory = sqlite3.Row
        return conn

    with closing(connect()) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('''CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY, session_id TEXT UNIQUE NOT NULL,
            zip_size_bytes INTEGER NOT NULL, zip_sha256 TEXT NOT NULL,
            stored_at TEXT NOT NULL, warnings TEXT NOT NULL, archive BLOB NOT NULL)''')
        conn.execute('CREATE INDEX IF NOT EXISTS sessions_order ON sessions(stored_at DESC, session_id ASC)')
        conn.commit()

    api = FastAPI(title='Injidong Session API', version='1.0.0',
                  description='Mobile 0.5.1 array ZIP upload and PC original archive download. See README for provisional policies.')
    api.add_middleware(RequestGate, token=token)

    @api.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        detail = exc.detail if isinstance(exc.detail, dict) else {'code': 'HTTP_ERROR', 'message': str(exc.detail)}
        return JSONResponse({'error': detail}, status_code=exc.status_code)

    @api.exception_handler(RequestValidationError)
    async def bad_request(request, exc):
        return JSONResponse({'error': {'code': 'INVALID_REQUEST', 'message': 'Invalid request fields'}}, status_code=400)

    @api.exception_handler(Exception)
    async def internal_error(request, exc):
        return JSONResponse({'error': {'code': 'INTERNAL_ERROR', 'message': 'Server could not complete request'}}, status_code=500)

    def record(row):
        return {'session_id': row['session_id'], 'storage_status': 'stored',
                'zip_size_bytes': row['zip_size_bytes'], 'zip_sha256': row['zip_sha256'],
                'stored_at': row['stored_at'],
                'download_path': '/api/v1/sessions/' + quote(row['session_id'], safe='') + '/archive',
                'warnings': json.loads(row['warnings'])}

    @api.get('/health')
    def health():
        with closing(connect()) as conn:
            conn.execute('SELECT 1 FROM sessions LIMIT 1').fetchone()
        return {'status': 'ok'}

    @api.post('/api/v1/sessions', status_code=201)
    def upload_session(session_id: str = Form(...), client_zip_size_bytes: str = Form(...),
                       client_zip_sha256: str = Form(...), zip: UploadFile = File(...)):
        if not re.fullmatch(ID_PATTERN, session_id):
            error(400, 'INVALID_SESSION_ID', 'Use 1..128 ASCII letters, digits, underscore or hyphen')
        if not re.fullmatch(r'[1-9][0-9]{0,19}', client_zip_size_bytes):
            error(400, 'INVALID_REQUEST', 'ZIP size must be a positive decimal integer')
        size_expected = int(client_zip_size_bytes)
        if size_expected > MAX_ARCHIVE:
            error(413, 'LIMIT_EXCEEDED', 'ZIP exceeds 128 MiB')
        if not re.fullmatch(r'[0-9a-f]{64}', client_zip_sha256):
            error(400, 'INVALID_REQUEST', 'Lowercase SHA-256 required')
        # Isolated temporary file is cleaned on every path, including duplicate/conflict.
        with tempfile.TemporaryDirectory(prefix='upload-', dir=data) as temp:
            path = Path(temp) / 'archive.zip'
            digest, size = hashlib.sha256(), 0
            with path.open('xb') as target:
                while chunk := zip.file.read(CHUNK):
                    size += len(chunk)
                    if size > MAX_ARCHIVE:
                        error(413, 'LIMIT_EXCEEDED', 'ZIP exceeds 128 MiB')
                    digest.update(chunk)
                    target.write(chunk)
            if size != size_expected:
                error(400, 'SIZE_MISMATCH', 'Uploaded ZIP size differs from client size')
            if digest.hexdigest() != client_zip_sha256:
                error(400, 'HASH_MISMATCH', 'Uploaded ZIP SHA-256 differs from client SHA-256')
            try:
                summary = inspect_archive(path, session_id)
            except InvalidArchive as exc:
                error(422, 'ZIP_REJECTED', str(exc))
            # Archive bytes and metadata commit together. UNIQUE ID serializes simultaneous uploads.
            with closing(connect()) as conn:
                try:
                    conn.execute('BEGIN IMMEDIATE')
                    existing = conn.execute('SELECT id,session_id,zip_size_bytes,zip_sha256,stored_at,warnings FROM sessions WHERE session_id=?', (session_id,)).fetchone()
                    if existing:
                        if existing['zip_size_bytes'] != size or existing['zip_sha256'] != client_zip_sha256:
                            error(409, 'SESSION_CONFLICT', 'Session ID already stores different archive bytes')
                        return JSONResponse({'disposition': 'existing', 'session': record(existing)}, status_code=200)
                    stored_at = datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
                    cursor = conn.execute('''INSERT INTO sessions
                        (session_id,zip_size_bytes,zip_sha256,stored_at,warnings,archive)
                        VALUES (?,?,?,?,?,zeroblob(?))''',
                        (session_id, size, client_zip_sha256, stored_at, json.dumps(summary['warnings']), size))
                    with conn.blobopen('sessions', 'archive', cursor.lastrowid) as blob, path.open('rb') as source:
                        while chunk := source.read(CHUNK):
                            blob.write(chunk)
                    row = conn.execute('SELECT id, session_id,zip_size_bytes,zip_sha256,stored_at,warnings FROM sessions WHERE id=?', (cursor.lastrowid,)).fetchone()
                    conn.commit()
                    return {'disposition': 'created', 'session': record(row)}
                except BaseException:
                    conn.rollback()
                    raise

    @api.get('/api/v1/sessions')
    def list_sessions(limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
        condition, params = '', []
        if cursor is not None:
            try:
                decoded = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))
                if not isinstance(decoded, list) or len(decoded) != 2 or not all(isinstance(v, str) for v in decoded):
                    raise ValueError()
                stamp, sid = decoded
                with closing(connect()) as conn:
                    if not conn.execute('SELECT 1 FROM sessions WHERE stored_at=? AND session_id=?', (stamp, sid)).fetchone():
                        raise ValueError()
            except (ValueError, TypeError, UnicodeError):
                error(400, 'INVALID_PAGINATION', 'Invalid cursor')
            condition = 'WHERE stored_at < ? OR (stored_at = ? AND session_id > ?)'
            params = [stamp, stamp, sid]
        with closing(connect()) as conn:
            rows = conn.execute('SELECT id,session_id,zip_size_bytes,zip_sha256,stored_at,warnings FROM sessions ' + condition +
                                ' ORDER BY stored_at DESC, session_id ASC LIMIT ?', (*params, limit + 1)).fetchall()
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = page[-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps([last['stored_at'], last['session_id']]).encode()).decode()
        return {'items': [record(row) for row in page], 'next_cursor': next_cursor}

    @api.get('/api/v1/sessions/{session_id}/archive', response_class=StreamingResponse,
             responses={200: {'content': {'application/zip': {}}}})
    def download(session_id: str):
        with closing(connect()) as conn:
            row = conn.execute('SELECT id,session_id,zip_size_bytes,zip_sha256,stored_at,warnings FROM sessions WHERE session_id=?', (session_id,)).fetchone()
        if row is None:
            error(404, 'SESSION_NOT_FOUND', 'Session not found')

        def chunks():
            with closing(connect()) as conn:
                with conn.blobopen('sessions', 'archive', row['id'], readonly=True) as blob:
                    while chunk := blob.read(CHUNK):
                        yield chunk

        return StreamingResponse(chunks(), media_type='application/zip', headers={
            'Content-Length': str(row['zip_size_bytes']), 'X-Session-ID': row['session_id'],
            'X-Archive-SHA256': row['zip_sha256'], 'Cache-Control': 'no-transform',
            'Content-Disposition': f'attachment; filename="{row["session_id"]}.zip"'})

    return api


app = create_app()
