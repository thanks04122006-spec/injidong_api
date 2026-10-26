# 인지동 세션 API

Python 3.11 이상 / FastAPI. Android 0.5.1 array 세션 ZIP을 원본 그대로 보관하고 PC에 제공합니다.
중량 추론 서버는 포함하지 않습니다. 기존 Android 앱의 `SessionTransport`는 별도 네트워크 구현이 필요합니다.

## VS Code 실행

이 폴더를 VS Code에서 열고 Python / Python Debugger 확장을 사용합니다.

```powershell
cd D:\injidong_api
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

또는 Python 인터프리터를 `.venv\Scripts\python.exe`로 선택하고 F5 → `Session API: debug localhost:8000`을 실행합니다.
API 문서: http://127.0.0.1:8000/docs, OpenAPI: http://127.0.0.1:8000/openapi.json

현재 PC의 준비된 환경은 기존 설치 패키지를 참조하는 `--system-site-packages` venv입니다.
다른 PC에서 독립 환경을 만들 때:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Python 3.11 `sqlite3.blobopen`으로 큰 ZIP을 청크 단위 저장/반환합니다.
vendored 공통 계약 검증기와 5개 schema는 기존 `injidong_contract_v1`에서 복사했으며 원본 규격은 수정하지 않았습니다.

## API 계약

| 메서드 | 경로 | 성공 |
|---|---|---|
| GET | `/health` | 200, DB 접근 확인 |
| POST | `/api/v1/sessions` | 새 저장 201 / 동일 원본 재전송 200 |
| GET | `/api/v1/sessions?limit=50&cursor=...` | 200, `{items,next_cursor}` |
| GET | `/api/v1/sessions/{session_id}/archive` | 200, 원본 ZIP |

업로드는 multipart/form-data의 `session_id`, `client_zip_size_bytes`, `client_zip_sha256`, `zip`을 사용합니다.
클라이언트 크기는 양의 10진 문자열, SHA는 소문자 64자리입니다. 바이너리 파일은 base64 변환하지 않습니다.

업로드 응답은 `{disposition: "created" 또는 "existing", session: SessionRecord}`입니다.
SessionRecord는 `session_id`, `storage_status="stored"`, `zip_size_bytes`(integer),
`zip_sha256`, `stored_at`(UTC Z, 소수초 6자리), `download_path`, `warnings`(array)를 포함합니다.
`stored_at`은 최초 저장 완료 시각이며 재전송으로 바뀌지 않습니다.
동일 session_id에 다른 원본은 409 `SESSION_CONFLICT`로 거부하며 기존 ZIP을 덮어쓰지 않습니다.

목록 기본 limit=50, 범위 1..200. `stored_at DESC, session_id ASC` 정렬.
불투명 cursor는 그대로 다음 요청에 전달합니다. 마지막 페이지의 `next_cursor`는 null입니다.
커서에 만료는 없고 기존 record의 정렬 경계를 가리킵니다. 페이지 간 snapshot은 제공하지 않으므로
조회 도중 새 업로드는 새로고침 후 나타납니다. 삭제/수정 API는 없습니다.

다운로드는 단일 `Content-Length`, `X-Session-ID`, `X-Archive-SHA256` 헤더를 제공합니다.
`application/zip`, 원본 길이/해시 그대로, gzip/Transfer-Encoding 없이 스트리밍합니다.
Range 다운로드나 리다이렉트는 제공하지 않습니다. 프록시를 붙일 때도 원본 응답의 identity/framing 계약을 유지해야 합니다.

오류 외피: `{ "error": { "code": "...", "message": "..." } }`.
대표 오류: 400 `INVALID_REQUEST`, `INVALID_SESSION_ID`, `INVALID_PAGINATION`, `SIZE_MISMATCH`, `HASH_MISMATCH`;
401 `UNAUTHORIZED`; 404 `SESSION_NOT_FOUND`; 409 `SESSION_CONFLICT`; 413 `LIMIT_EXCEEDED`;
422 `ZIP_REJECTED`; 500 `INTERNAL_ERROR`.

## 합의와 이번 구현에서 선택한 정책

근거: 2026-10-22 `PC_SERVER_CONNECTION_HANDOFF_ko.md`와 `WIRE_EXAMPLES_OBSERVED.json`.
GET 2개 경로, multipart 4개 필드, disposition/storage_status 구분, 최초 저장 시각,
목록 limit/정렬/cursor, 다운로드 3개 헤더는 전달 자료의 합의 범위를 따릅니다.

아래는 이번 서버의 명시적 구현 정책이며 기존에 팀 최종 합의가 끝났다는 의미는 아닙니다.

- POST 경로, 201/200/409 정책, 정확한 JSON 외피/경고/오류 형식은 이번 서버 계약으로 채택했습니다.
- session_id는 ASCII 영숫자/underscore/hyphen 1..128자로 제한합니다.
  PC 임시 파서는 더 넓은 ID를 받지만 이 서버는 비ASCII 헤더 표현 미확정으로 이 범위만 발급/접수합니다.
- RGB8 선언 + RGBA PNG는 원본 보존 후 `RGB_ENCODING_CHANNEL_MISMATCH` 경고로 접수합니다.
  경고 수락의 팀 최종 승인을 의미하지 않습니다. 단위/배열/해시 불일치는 거부합니다.
- 서버 한도는 PC 기본값에 맞춘 로컬 개발 정책입니다: ZIP 128 MiB, 항목4096,
  개별 해제32 MiB, 총 해제256 MiB, 압축률200, Depth128프레임/프레임4M픽셀/전체16M픽셀,
  RGB당4M픽셀, NPY헤더10000바이트. multipart 본문은 ZIP한도+1MiB로 제한합니다.
- 기본 인증은 로컬 개발용 미설정. `INJIDONG_API_TOKEN`이 있으면 `/api/`에 Bearer 인증을 적용합니다.
  `/health`, `/docs`, `/openapi.json`은 공개합니다. 실제 인증·HTTPS·운영 한도는 후속 합의 대상입니다.

## 샘플 업로드

서버를 실행한 상태에서 다른 PowerShell에서:

```powershell
.\.venv\Scripts\python.exe upload_session.py "C:\Users\KIKA\AppData\Local\Temp\0.5.1_array_sample.zip"
```

업로드 도구는 무인증 로컬 개발용입니다. ZIP을 변경하거나 재압축하지 않습니다.
PC의 서버 주소에는 `http://127.0.0.1:8000`을 입력합니다.
휴대폰에서 접속하려면 PC LAN IP/방화벽/바인딩과 Android 업로드 어댑터 구현이 추가로 필요합니다.

## 검증

```powershell
$env:INJIDONG_SAMPLE_ZIP="C:\Users\KIKA\AppData\Local\Temp\0.5.1_array_sample.zip"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe tests/wire_check.py $env:INJIDONG_SAMPLE_ZIP
```

상세 결과: `VALIDATION.md`, 실제 loopback 응답: `WIRE_VERIFIED.json`.
wire_check는 해당 33프레임 array 샘플의 session_id/해시를 기준으로 작성됐습니다.
실제 PC GUI/휴대폰 업로드 종단은 별도 공동 연결 검증 대상입니다.

## 저장과 검증 범위

기본 저장 위치는 `data/sessions.sqlite3`이고 `INJIDONG_DATA_DIR`로 변경할 수 있습니다.
메타데이터와 ZIP BLOB을 하나의 SQLite 트랜잭션으로 저장합니다. 중간 실패는 rollback하며
동시 동일 ID 요청은 직렬화합니다. 원본은 재압축/삭제/교체하지 않습니다.
프로세스 정상 경로에서 upload 임시 폴더는 정리합니다. 강제 종료 시 임시 폴더가 남을 수 있습니다.
디스크 quota, 전역 동시 업로드 제한, 강제 종료/전원 장애 주입시험, 운영 백업/복구 시험은 포함하지 않습니다.

검사: ZIP 경로/심볼릭링크/중복/크기/압축률, JSON 중복키·비유한값, manifest/index/count/ID,
NPZ SHA 및 NPY 헤더·shape·dtype·payload, 공통 FramePacket 검증, raw SHA, PNG SHA·크기·채널.
IMU는 JSON 객체와 개수만 검사합니다. raw_capture/rgb_depth_evidence 등 추가 보조 파일은
ZIP 안전성 검사 후 원본으로 보존하며 보조 파일 전체 의미·보정 정확도는 검사하지 않습니다.
RGB/Depth 정렬·동기화·물리 부피/중량 정확도를 검증하거나 입력의 unverified/null을 변경하지 않습니다.
