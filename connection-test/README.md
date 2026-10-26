# 팀원 API 연결 시험

이 폴더만 Vercel 프로젝트로 배포합니다. 기존 세션 API, ZIP, SQLite DB,
테스터 파일은 포함하지 않습니다. 상태를 저장하지 않는 접속 시험 전용입니다.

## 배포

Vercel CLI가 설치된 PC에서 이 폴더로 이동한 뒤 로그인하고 배포합니다.

```powershell
cd D:\injidong_api\connection-test
vercel login
vercel deploy
```

Framework는 FastAPI, Root Directory는 이 폴더입니다.
팀원에게 공유할 배포 URL이 Vercel 로그인 화면으로 연결되면 프로젝트의
Deployment Protection 설정을 확인하세요. 공개 시험 API에는 민감한 값을 보내지 마세요.

## 팀원 시험

아래 주소를 실제 배포 URL로 바꿉니다.

```powershell
$apiBase = 'https://YOUR-PROJECT.vercel.app'
Invoke-RestMethod "$apiBase/health"
$echoBody = @{client='team-member-1'; message='연결 확인'} | ConvertTo-Json
Invoke-RestMethod "$apiBase/api/v1/echo" -Method Post -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($echoBody))
```

성공 기준: GET 200 / status=ok, POST 200 / 보낸 client와 message가 그대로 반환됨.
각 팀원은 client 값을 바꾸고 상태 코드와 응답을 확인합니다.
브라우저에서는 `/docs`에서 POST 요청을 실행할 수 있습니다.
잘못된 입력은 422를 반환합니다. 브라우저 JSON 요청을 위해 공개 CORS를 허용합니다.
ZIP 업로드·조회·다운로드 및 실제 Android 앱 연동은 이 시험 범위에 포함되지 않습니다.

공식 배포 가이드: https://vercel.com/docs/frameworks/backend/fastapi
