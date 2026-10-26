# Windows EXE 접수와 실행 연결

백엔드 #167의 EXE 접수에 참가자 기록 조회, 관리자 설정, 작업별 파일·비밀값 제공과 결과 수신을 추가한다
총 실행 횟수 제한은 없으며 기본 요청 간격은 팀별 30초다 관리자가 간격을 바꿀 수 있다

## 현재 범위

실제 EXE 체커의 저장소와 주소는 아직 미정이다
스케줄러의 Windows 준비 확인·배정 API와 실제 Azure VM 실행도 연결되지 않았다
이 변경만으로 Windows 실행이 가능해지는 것은 아니다 두 실행 플래그의 기본값은 False로 유지한다

검증한 범위는 격리 PostgreSQL·Redis의 접수·조회·설정과 브라우저 연결이다
테스트용 결과 callback은 실제 EXE 실행 증거로 사용하지 않는다
출제자 드라이버, 참가자 파일과 풀이 코드는 변경하지 않는다

## 참가자 API

| 작업 | 경로 | 동작 |
| --- | --- | --- |
| 파일 접수 | POST /api/v1/challenges/{challenge_id}/exe-submissions | multipart file 하나와 UUID Idempotency-Key를 받는다 새 접수는 202, 같은 파일·키의 재시도는 200이다 |
| 팀 기록 | GET /api/v1/challenges/{challenge_id}/exe-submissions | 현재 팀의 기록만 조회한다 idempotency_key로 응답을 잃은 접수도 찾을 수 있다 |
| 개별 기록 | GET /api/v1/challenges/{challenge_id}/exe-submissions/{submission_id} | 실행 상태, 실제 출력과 정리 상태를 조회한다 |

- 파일은 x64 PE32+ EXE, 최대 10 MiB다 실행 제한은 최대 180초다
- 같은 팀·문제의 새 요청은 마지막 접수 시각과 현재 요청 간격으로 제한한다 설정을 바꿔도 접수 시각을 덮어쓰지 않는다
- 진행 중이거나 정리·반납이 끝나지 않은 작업이 있으면 새 실행을 받지 않는다
- 접수 때 팀 소속을 잠금으로 확인하고, 실행 설정과 승인된 릴리스·비밀값 참조를 고정한다
- 기존 접수 조회는 접수를 꺼둔 뒤에도 가능하다 로그 조회와 체커 결과는 정답·점수·마일리지를 지급하지 않는다

## 관리자 API

- PATCH /api/v1/admin/challenges/{challenge_id}/remote_run
- GET /api/v1/admin/challenges/{challenge_id}/remote_runs

관리자는 실행 환경 안내, 크기, 시간, 요청 간격과 접수를 설정한다
검증한 VM 릴리스, 개인 저장 경로와 체커 설정을 준비한 뒤 접수를 켠다
대기·준비·실행·시작 여부 불명과 정리·반납을 기다리는 작업은 진행 중 필터에 남긴다
파일 바이트와 비밀값은 관리자 목록에도 넣지 않는다

## 체커 연결 계약 v1

아래 내부 계약은 실제 체커 담당자와 맞춰야 한다 기존 동기 /v1/attempts API와 호환된다고 가정하지 않는다
경로의 하이픈은 기존 #167과 10월 25일 체커 명세를 따른다 JSON 필드·쿼리·경로 변수는 snake_case로 유지한다

1. 백엔드 워커가 저장한 submission_id로 스케줄러에 배정을 요청한다
2. 유효한 배정, 만료 시각과 30초 이내의 Windows 게스트 준비 증거를 확인한다 VM IP나 전원 상태로 READY를 추정하지 않는다
3. 운영 설정의 worker UUID에 등록한 HTTPS 체커만 직접 호출한다 스케줄러 응답의 주소를 HTTP 호출 대상으로 쓰지 않는다
4. POST /v1/checker-submissions 에 고정한 식별정보, 실행 시간·출력 제한과 승인한 버전을 보낸다 파일·플래그·URL·토큰은 JSON에 넣지 않는다
5. 체커는 작업별 토큰으로 아래 파일·비밀값을 읽고 STARTED·RESULT·CLEANUP 결과를 보낸다 Windows 게스트에는 서비스 토큰과 백엔드 서명 키를 전달하지 않는다

| 내부 경로 | 인증과 조건 |
| --- | --- |
| GET /internal/v1/checker-submissions/{submission_id}/payload | 배정·파일 해시에 묶인 작업 토큰, 유효한 준비 상태와 배정이 필요하다 실제 파일 해시도 다시 확인한다 |
| GET /internal/v1/checker-submissions/{submission_id}/secret | 같은 조건에서 접수 때 고정한 비밀값만 제공한다 |
| POST /internal/v1/checker-submissions/{submission_id}/events | event_id 중복을 확인하고 같은 결과만 재생한다 다른 파일·배정·버전, 늦은 상태 되돌리기는 거절한다 |

실제 출력과 실행 시작 여부는 체커가 보고해야 한다 출력은 각 64 KiB이며 DB 저장 전에 제어문자를 정리한다
RESULT가 도착해도 CLEANUP이 확인되기 전에는 배정을 반납하지 않는다 반납 ACK를 정리 증거로 쓰지 않는다
정리와 반납까지 확인한 실행 파일은 지운다 삭제 실패는 워커가 재시도하며 상태·해시·결과 기록은 보존한다
POST 응답을 잃거나 전송 중 워커가 재시작되면 같은 작업의 GET만 재시도한다 404·timeout을 근거로 다른 작업·호스트에서 재실행하지 않는다
체커의 GET은 접수 확인에 사용한다 최종 결과와 정리는 callback으로 보고해야 한다
실행 여부가 끝내 불명확하면 새 접수와 자동 반납을 막고 운영자가 실제 이력을 확인한다

## 운영 설정

EXE_CHECKERS_JSON의 키는 브로커 worker UUID다 각 값에는 다음 항목을 등록한다

- base_url: 인증서 검증이 가능한 HTTPS origin
- service_token_env: EXE_CHECKER_로 시작하고 _TOKEN으로 끝나는 전용 환경변수 이름
- checker_commit_sha, vm_template_sha256, driver_sha256: 승인한 실제 배포 해시
- config_version: 체커에서 확인할 구성 버전

EXE_CHECKER_SIGNING_KEY는 백엔드에만 두며 최소 32자다
EXE_SUBMISSION_STORAGE_ROOT는 웹에서 제공하지 않는 별도의 영속 볼륨으로 지정한다
인증 토큰, 암호문과 실행 파일을 일반 API 응답·CI 산출물·요청 로그에 넣지 않는다

실제 Azure·게스트 준비 확인·체커 callback을 검증한 뒤 EXE_SUBMISSIONS_ENABLED와 EXE_DISPATCH_ENABLED를 켜고 관리자에서 접수를 허용한다
워커는 dispatch_exe_submissions 명령을 주기 실행한다 한 번에 처리할 수는 --limit으로 제한한다
일반 문제와 KOTH의 실행 경로는 사용하지 않는다
