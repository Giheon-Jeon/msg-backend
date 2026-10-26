# Windows EXE 접수와 실행 연결

백엔드 #167의 EXE 접수에 참가자 기록 조회, 관리자 설정, 작업별 파일·비밀값 제공과 결과 수신을 추가한다
총 실행 횟수 제한은 없으며 기본 요청 간격은 팀별 30초다 관리자가 간격을 바꿀 수 있다

## 현재 범위

실제 EXE 체커의 저장소와 주소는 아직 미정이다
스케줄러의 Windows 준비 확인·배정 API와 실제 Azure VM 실행도 연결되지 않았다
이 변경만으로 Windows 실행이 가능해지는 것은 아니다 두 실행 플래그의 기본값은 False로 유지한다

체커 호출 주체는 백엔드다 스케줄러는 VM 준비 확인·배정·예약 반납을 맡는다
백엔드는 유효한 배정을 받은 뒤 런타임의 체커를 직접 호출하고 실행 결과도 직접 받는다
스케줄러가 EXE 실행 요청이나 실행 로그를 중계하는 구조로 연결하지 않는다

검증한 범위는 격리 PostgreSQL·Redis의 접수·조회·설정과 브라우저 연결이다
테스트용 결과 callback은 실제 EXE 실행 증거로 사용하지 않는다
출제자 드라이버, 참가자 파일과 풀이 코드는 변경하지 않는다

### 접수와 팀 변경의 동시 처리

EXE 접수는 문제 → 접수 전 확인한 팀 → 사용자 순서로 잠근다
일반 플래그 제출의 문제 → 팀 순서와 관리자 팀 변경의 팀 → 사용자 순서를 함께 유지한다
문제 잠금을 기다리는 동안 팀이나 사용자 잠금을 먼저 잡지 않는다

잠금 후 사용자의 현재 팀과 정지 상태, 문제 접근 권한을 다시 확인한다
접수 전 확인한 팀에서 다른 팀으로 바뀌면 TEAM_CHANGED로 거절하며 새 팀의 권한으로 접수를 이어가지 않는다
팀에서 제외되거나 계정이 삭제되면 접수하지 않으며, 이미 풀이한 문제도 새 실행을 예약하지 않는다
같은 파일·요청 키 재시도와 팀별 실행 간격 검사는 이 잠금 범위 안에서 유지한다

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
접수 활성화 전 비공개 절대 저장 경로가 MEDIA_ROOT 밖에 있는지, Scheduler 토큰이 설정됐는지, 최신 flag 비밀값을 복호화해 현재 채점 값과 일치하는지도 확인한다
누락 시 can_enable·available은 False이며 활성화 요청은 409 RELEASE_NOT_READY로 거절한다 접수 비활성화와 기존 이력 조회는 가능하다
이 검사는 파일 생성이나 외부 호출 없이 설정만 확인한다 실제 디스크 쓰기·Scheduler 인증 성공·VM 준비 완료를 의미하지 않는다
대기·준비·실행·시작 여부 불명과 정리·반납을 기다리는 작업은 진행 중 필터에 남긴다
파일 바이트와 비밀값은 관리자 목록에도 넣지 않는다
관리자 실행 목록의 last_error_code에는 고정된 처리 오류 코드를 표시한다 참가자 목록·상세에는 이 필드를 추가하지 않는다
반납 404 WINDOWS_ASSIGNMENT_NOT_FOUND는 release_pending과 requires_review로 표시하고 주기 워커의 자동 반납 재시도를 보류한다 실제 실행 결과·출력·정리 보고는 변경하지 않는다
반납 502 BROKER_CALL_FAILED와 응답 유실·잘못된 ACK는 실패 코드를 DB에 남기고 동일 lease_id로 재시도한다 늦은 CLEANUP 보고만으로 반납 오류를 지우지 않는다
정확한 lease_id와 COMPLETED·EXPIRED ACK를 확인한 뒤에만 반납 오류를 지운다 반납 실패는 실행 실패 건수로 합산하지 않으며 미반납 작업은 ACTIVE 목록에 남는다

## 체커 연결 계약 v1

아래 내부 계약은 실제 체커 담당자와 맞춰야 한다 기존 동기 /v1/attempts API와 호환된다고 가정하지 않는다
경로의 하이픈은 기존 #167과 10월 25일 체커 명세를 따른다 JSON 필드·쿼리·경로 변수는 snake_case로 유지한다

1) 백엔드 워커가 저장한 submission_id로 스케줄러에 배정을 요청한다
2) 유효한 배정, 만료 시각과 30초 이내의 Windows 게스트 준비 증거를 확인한다 VM IP나 전원 상태로 READY를 추정하지 않는다
3) 운영 설정의 worker UUID에 등록한 HTTPS 체커만 직접 호출한다 스케줄러 응답의 주소를 HTTP 호출 대상으로 쓰지 않는다
4) POST /v1/checker-submissions 에 고정한 식별정보, 실행 시간·출력 제한과 승인한 버전을 보낸다 파일·플래그·URL·토큰은 JSON에 넣지 않는다
5) 체커는 작업별 토큰으로 아래 파일·비밀값을 읽고 STARTED·RESULT·CLEANUP 결과를 보낸다 Windows 게스트에는 서비스 토큰과 백엔드 서명 키를 전달하지 않는다

| 내부 경로 | 인증과 조건 |
| --- | --- |
| GET /internal/v1/checker-submissions/{submission_id}/payload | 배정·파일 해시에 묶인 작업 토큰, 유효한 준비 상태와 배정이 필요하다 실제 파일 해시도 다시 확인한다 |
| GET /internal/v1/checker-submissions/{submission_id}/secret | 같은 조건에서 접수 때 고정한 비밀값만 제공한다 |
| POST /internal/v1/checker-submissions/{submission_id}/events | event_id 중복을 확인하고 같은 결과만 재생한다 다른 파일·배정·버전, 늦은 상태 되돌리기는 거절한다 |

실제 출력과 실행 시작 여부는 체커가 보고해야 한다 출력은 각 64 KiB이며 DB 저장 전에 제어문자를 정리한다
RESULT와 별도 CLEANUP(cleanup_complete=true) 보고가 모두 확인되기 전에는 배정을 반납하지 않는다
RESULT의 cleanup_complete는 참고 정보로만 검증하며 정리 완료 상태를 갱신하지 않는다
CLEANUP 완료 보고를 원자적으로 저장한 서버 수신 시각(cleanup_confirmed_at)이 있어야 반납·파일 삭제를 허용한다 반납 ACK를 정리 증거로 쓰지 않는다
두 보고의 도착 순서는 달라도 되지만 하나만 도착한 상태에서는 반납하지 않는다
정리와 반납까지 확인한 실행 파일은 지운다 삭제 실패는 워커가 재시도하며 상태·해시·결과 기록은 보존한다
POST 응답을 잃거나 전송 중 워커가 재시작되면 같은 작업의 GET만 재시도한다 404·timeout을 근거로 다른 작업·호스트에서 재실행하지 않는다
체커의 GET은 접수 확인에 사용한다 최종 결과와 정리는 callback으로 보고해야 한다
실행 여부가 끝내 불명확하면 새 접수와 자동 반납을 막고 운영자가 실제 이력을 확인한다

### 배정 후 준비 증거 갱신

아직 체커에 보내지 않은 ASSIGNED·dispatch_state=PENDING 작업에서 준비 증거가 없거나
30초를 넘으면 기존 POST /api/windows-assignments를 같은 submission_id·본문으로 다시 호출한다
별도 준비 상태 조회 API를 가정하지 않는다 유효한 READY 증거가 이미 있으면 추가 호출은 생략한다

- 응답은 원래 lease_id·worker_id와 같아야 한다 이미 확인한 vm_id도 바꿀 수 없다
- READY이면 실제 수신한 readiness_check_id·readiness_checked_at을 저장한다 조회 시각으로 증거 시각을 새로 만들지 않는다
- UNKNOWN·NOT_READY 또는 통신 실패이면 이전 준비 증거를 무효화하고 같은 배정을 유지한 채 다음 워커 실행에서 재조회한다
- 조회로 기존 임대 만료 시각을 연장하지 않는다 실행 제한 시간과 반납 여유가 부족하면 실행하지 않는다
- WINDOWS_ASSIGNMENT_ENDED·WINDOWS_ASSIGNMENT_CONFLICT나 배정/VM 식별자 변경은 START_UNKNOWN으로 보류한다 파일·예약을 보존하며 새 submission_id·VM으로 자동 재실행하지 않는다
- 체커로 전송한 이력이 있으면 Scheduler 준비 재조회와 체커 POST를 하지 않는다 기존 체커 GET 복구만 유지한다

준비 재조회는 다른 워커의 실행 요청과 겹치지 않도록 해당 제출 행 잠금 안에서 제한 시간 있는
Scheduler 호출을 수행한다 대회 규모에서 잠금 대기가 문제가 되면 별도 워커 lease로 전환한다
현재 Scheduler #81은 vm_readiness=UNKNOWN만 제공하며 Broker·Runtime의 게스트 증거 연결은 별도 구현 대기다
따라서 이 보완은 실제 Windows 실행·준비 증거 생성 완료를 의미하지 않으며 실행 설정의 기본값은 바꾸지 않는다

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

## Scheduler 배정·반납 계약 보완

- 배정 재시도는 저장된 submission_id·팀·release·파일 해시를 그대로 사용한다 Scheduler도 이 식별정보를 고정하고 같은 ID의 다른 본문은 거절해야 한다
- 409 WINDOWS_ASSIGNMENT_ENDED는 자동 재배정하지 않는다 START_UNKNOWN으로 옮겨 FIFO에서 제외하고 예약·파일을 보존한다 실제 이력 확인 전에는 해당 팀의 새 실행을 막는다
- 409 WINDOWS_ASSIGNMENT_CONFLICT도 START_UNKNOWN과 requires_review로 분리한다 같은 submission_id의 고정 정보 충돌은 자동 재시도로 해결하지 않으며 새 ID나 VM으로 바꿔 실행하지 않는다 다른 팀의 FIFO 처리는 계속한다
- 배정 응답의 status는 필수다 누락·null·알 수 없는 값은 배정으로 저장하지 않으며 파일을 보존하고 같은 submission_id로 재조회한다 status가 LEASED인 유효한 배정만 받는다 HTTP 200 응답도 status가 COMPLETED·EXPIRED이거나 expires_at이 지났으면 종료된 배정으로 처리한다
- VM 부족·502·응답 유실은 같은 submission_id로 재시도한다 새 제출을 자동 생성하지 않는다
- 반납은 응답의 lease_id가 일치하고 status가 COMPLETED 또는 EXPIRED일 때만 확인한다 빈 응답·404·502는 반납 확인이 아니며 파일을 삭제하지 않는다 502·응답 유실은 같은 lease_id로 자동 재시도하고 명시적인 WINDOWS_ASSIGNMENT_NOT_FOUND 404는 운영자 확인 전까지 주기 재시도를 보류한다 운영자가 재확인할 때도 원래 lease_id만 사용한다
- 임대 기간은 기본 2시간으로 가정하지 않고 expires_at을 따른다 연장 API는 없다 새 실행은 설정된 실행 제한 시간에 dispatch·결과 수집·반납 여유 30초를 더한 시간이 남아 있어야 한다 실제 연동 전 이 여유의 적정성을 확인한다
- 임대가 끝나도 기존 실행의 결과를 추측하지 않는다 접수 여부가 불명확한 체커 작업은 같은 ID로 조회만 하며 자동 재실행·반납하지 않는다

스케줄러 제공 API와 맞출 필드·응답은 windows-assignments.openapi.json에 별도 초안으로 남긴다 이 파일이 실제 제공 구현이나 팀 간 합의를 대신하지 않는다

## 별도 정리 보고 적용 시 주의

challenge.0011_exesubmission_cleanup_confirmed_at을 배포 전에 적용한다 이 migration은 nullable 확인 시각만 추가하며 데이터나 파일을 삭제하지 않는다
기존 cleanup_complete=true 값이나 과거 이벤트의 해시만으로 별도 CLEANUP 완료 보고를 추정해 채우지 않는다
확인 시각 없는 기존 작업은 자동 반납·파일 삭제를 보류한다 유효한 새 CLEANUP 완료 보고 또는 운영자 확인이 필요하며 기존 이벤트 ID 재생만으로 확인 시각을 만들지 않는다
체커 인증 경계와 실제 준비 증거 생성·임대 만료·VM 초기화 통지는 별도 계약 대기다
배정 재조회로 준비 증거를 갱신하는 Backend 경로만 위 절과 같이 연결한다

## 실제 제공 구현과 남은 연결

2026년 10월 26일 확인한 저장소 기준이다 아래 경로는 운영 연결 완료를 뜻하지 않는다

| 구간 | 확인한 구현 | 남은 작업 |
| --- | --- | --- |
| 백엔드 → 스케줄러 | 이 PR의 /api/windows-assignments 호출과 Scheduler #81의 배정·반납 구현 | 실제 배포·연동 확인과 게스트 준비 증거 전달은 대기 중이다 |
| 스케줄러 → 브로커 | 브로커의 /v1/windows-worker-leases와 /{lease_id}/complete | submission_id를 Idempotency-Key로 전달하고 팀·release·파일 해시는 스케줄러가 고정해야 한다 |
| 브로커 → Windows | 현재 Windows 풀은 GCP zonal MIG 기반 | Azure VM용 등록·배정·복구 경로를 추가해야 한다 일반 Azure VM 어댑터만으로 연결된 것으로 보지 않는다 |
| 준비 확인 | 브로커 READY는 MIG의 RUNNING·NONE·HEALTHY와 IP로 판정 | 런타임의 게스트·드라이버 점검 결과를 별도로 받아야 한다 READY를 그대로 vm_readiness로 복사하지 않는다 |
| 백엔드 → 런타임 | /v1/checker-submissions 접수·조회 계약 | 런타임 dev에는 Windows EXE 체커가 없다 실제 체커 구현·HTTPS 배포·주소 등록 필요 |
| 반납·초기화 | 브로커 complete는 작업을 RECREATING으로 바꾸고 재생성을 예약 | COMPLETED·EXPIRED 응답은 게스트 초기화 완료 증거가 아니다 체커의 정리 확인과 브로커의 재사용 준비 확인을 구분한다 |

대조한 커밋

- 스케줄러: https://github.com/MSG-CTF/instance-scheduler/tree/886d9e8b52a1099fe8cc0aa138e79bfe6a921ad1
- 브로커: https://github.com/MSG-CTF/resource-broker/tree/a785c9ad08b499f7abda693163aeb72133e9a94e
- 런타임: https://github.com/MSG-CTF/secure-provisioner/tree/a73838ebcf46b23e699ebe0d46f8378a5731f317
- 호출 주체 근거: https://app.notion.com/p/4030f19c72be81709d18c9af6356fb0f

노션 초안의 총 3회 제한은 이후 사용자 결정으로 폐기했다 이 PR은 총 횟수 제한 없이 팀별 요청 간격을 적용한다
실제 Azure 배정·게스트 준비 확인·체커 실행·초기화·반납을 검증하기 전에는 운영 접수를 켜지 않는다
