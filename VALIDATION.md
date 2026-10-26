# 검증 기록 — 2026-10-23 (한국 시간)

## 입력과 결과

- 실제 입력: `0.5.1_array_sample.zip`, 30,661,636 bytes.
- SHA-256: `e87e810013b49756ab450349baa10ef939f7a1ddd0f68e4308fc5144efcf0735`.
- session_id: `d405_1792750256071`.
- ZIP 238항목, 해제 선언량46,173,139 bytes.
- Depth33프레임, RGB33장, IMU2530행. manifest 개수와 일치.
- Depth480×640 float32(m), valid_mask bool. 33개 모두 FramePacket/배열/해시 검증 통과.
- 프레임 ID는 0,11,15,...처럼 비연속입니다. 인덱스를 기준으로 처리하며 연속 번호를 요구하지 않습니다.
- `metadata.json` 기준 NPZ 경로는 프레임 상대경로, raw/RGB는 세션 상대경로로 구분했습니다.
- ns는 문자열을 유지하고 변환하지 않습니다. null pose/specimen/target_mask 및 unverified sync는 그대로 보존합니다.
- 추가 rgb_depth_evidence/raw_capture 파일은 보존합니다. 해당 파일의 의미 검증 완료를 뜻하지 않습니다.

## 발견한 차이와 대응

33개 RGB 모두 인덱스 선언 RGB8 / 실제 PNG RGBA입니다.
서버는 원본을 수정하지 않고 `RGB_ENCODING_CHANNEL_MISMATCH`, count33을 반환합니다.
이는 로컬 호환 정책이며 팀 경고 수락 승인 완료를 의미하지 않습니다.
배열·session ID·파일 참조·manifest 개수에서 차단되는 구조 불일치는 이 샘플에서 발견하지 않았습니다.

## 자동 검증

unittest 10개: 기본 저장/목록/원본 반환/중복/재시작,
서로 다른 원본 ID충돌, 크기/SHA/ID/한도 거부, 악성/손상 ZIP·JSON 거부,
동일 시각 정렬/커서/페이지 경계, 동시 동일 업로드,
Bearer/404/OpenAPI, multipart 수신 한도, NPY 헤더 shape 불일치,
실제33프레임 샘플 왕복.

별도 Uvicorn 프로세스와 실제 TCP HTTP를 사용한 검증:

- 업로드201, 동일 파일 재전송200, 최초 stored_at 유지.
- 목록200, 정확한 PC 호환 record, 마지막 next_cursor=null.
- 두 번 다운로드 후 길이/SHA 일치.
- Content-Length/X-Session-ID/X-Archive-SHA256 각각 단일 헤더.
- application/zip, Content-Encoding/Transfer-Encoding 없음.
- 404 SESSION_NOT_FOUND, 400 INVALID_PAGINATION 오류 외피 확인.
- 서버 종료 후 임시 DB/다운로드 시험 환경 정리.

실제 응답은 `WIRE_VERIFIED.json`에 보관했습니다. 제공받은 시험 fixture 응답을 실제 서버 응답으로 오인하지 않았습니다.
Windows 테스트의 SQLite 연결 핸들 미종료 문제를 발견해 명시적 close로 수정했습니다.
JSON 지수 overflow의 비유한값과 NPY 사전 할당 위험을 입력 검증에서 차단했습니다.

## 검증 범위

현재 PC Python3.11.1, NumPy2.4.6, FastAPI0.141.1, Uvicorn0.48.0에서 검증했습니다.
공통 문서의 NumPy2.3.5 기준과 다른 현재 실행 버전이며, 해당 기준 환경의 별도 재검증은 하지 않았습니다.
TestClient 사용 시 설치된 Starlette의 httpx deprecation 경고가 출력될 수 있으며 테스트 실패는 아닙니다.

실제 PC GUI 제품 코드, 휴대폰 업로드 어댑터, LAN/HTTPS/운영 인증, 장애복구/전원차단,
RGB/Depth 정렬·IMU 동기화·실제 부피/중량 정확도는 이번 시험 범위가 아닙니다.
