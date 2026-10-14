# 문제 실행 설정과 비밀값

프론트는 문제 ID만 보냅니다
백엔드는 활성 릴리스의 환경변수와 비밀값 참조를 골라 스케줄러에 전달합니다
비밀값 원문은 런타임 worker만 조회할 수 있습니다

| 단계 | 입력과 처리 |
| --- | --- |
| 출제자 → CI | env는 일반 값, secret_env는 백엔드에 등록된 비밀값 이름 |
| CI → 백엔드 | 새 필드가 있으면 artifact schema 2.1로 발행 |
| 관리자 → 백엔드 | 기존 2.0 릴리스의 이미지·포트는 유지하고 env·secret_env만 새 설정 버전으로 복제 |
| 릴리스 등록 | 이름을 해당 문제의 최신 비밀값 버전으로 고정, 누락되면 등록 실패 |
| 릴리스 활성화 | approved_at 기록, 이전 승인 릴리스도 reset을 위해 조회 허용 |
| 백엔드 → 스케줄러 | env·컨테이너별 secret_ref·release_id 전달, 새 설정은 POST /api/v2/instances 사용 |
| 런타임 → 백엔드 | 전용 서비스 토큰으로 참조·컨테이너 이름·이미지 digest를 확인하고 값 조회 |

## 운영자가 준비할 것

1) 마이그레이션을 적용하고 RUNTIME_SECRET_ENCRYPTION_KEYS를 설정합니다
   쉼표로 구분한 Fernet 키 목록이며 첫 키로 암호화하고 나머지 키로 이전 값을 복호화합니다
2) RUNTIME_SECRET_API_TOKEN을 런타임 전용 토큰으로 설정합니다
   참가자 JWT, 관리자 JWT, 스케줄러 토큰과 각각 분리합니다
3) 관리자 API에 문제별 비밀값을 등록한 뒤 기존 릴리스를 복제하거나 새 릴리스를 등록해 활성화합니다
   flag 값은 해당 문제의 채점 hash와 일치해야 합니다

| API | 권한 | 요청 | 응답 |
| --- | --- | --- | --- |
| POST /api/v1/admin/challenges/{challenge_id}/runtime-secrets | 관리자 JWT | name, value | secret_id UUID, name, version |
| GET /api/v1/admin/challenges/{challenge_id}/runtime-secrets | 관리자 JWT | 없음 | 비밀값 이름, 버전, 저장 시각과 최신 여부 |
| POST /api/v1/admin/challenges/{challenge_id}/releases/{release_id}/derive | 관리자 JWT | containers[].name/env/secret_env | 원본 이미지·포트·격리를 유지한 새 릴리스 |
| POST /internal/v1/runtime-secrets/resolve | 런타임 전용 Bearer | secret_ref UUID, container, image | data.env에 값 반환, Cache-Control: no-store |

관리자 릴리스 조회의 컨테이너별 secret_bindings는 주입 이름, 저장 이름, 연결 버전과 최신 여부를 반환합니다
다른 문제의 비밀값은 연결된 참조가 있어도 조회하지 않으며 저장값 없음으로 표시합니다
관리자 메타데이터 조회는 값을 복호화하지 않고 원문·hash·암호문을 응답에 포함하지 않습니다
연결 상태는 릴리스 설정을 뜻하며 실제 컨테이너 주입 검증 결과와는 구분합니다

비밀값은 인증된 암호화로 DB에 저장합니다
새 등록은 새 버전을 만들며 기존 릴리스의 참조는 바꾸지 않습니다
릴리스 조회·스케줄러 요청에는 원문을 담지 않습니다
키를 잃으면 값을 복구할 수 없으므로 DB 백업과 별도로 키 보관·교체 절차가 필요합니다
운영에서는 DEBUG를 끄고 요청·응답 body 수집을 비활성화해야 합니다

## 검수 기준

| 확인할 것 | 기대 결과 |
| --- | --- |
| 참가자가 실행 설정을 추가 | 문제 ID 외 입력 거절 |
| FLAG·비밀번호·토큰을 일반 env에 입력 | 거절, secret_env로 등록 |
| 다른 문제의 비밀값 이름 사용 | 참조를 찾지 못해 릴리스 등록 실패 |
| 미승인 릴리스, 다른 컨테이너·digest로 조회 | 값 반환 없이 404 |
| 릴리스 전환 후 기존 인스턴스 reset | 원래 revision·env·비밀값 버전 유지 |
| 같은 발행 번호로 설정 복제 후 재연결 | 스케줄러 release_id로 정확한 버전 선택, ID가 없어 모호하면 실패 |
| 암호화 키 누락·잘못된 키·암호문 교체 | 값 반환 없이 503 |
| 구형 schema 2.0에 새 필드 추가 | 빈 값도 거절, 조용한 누락 방지 |

이 변경은 고정된 비밀값 버전을 지원합니다
이미지에 플래그가 들어 있거나 프로그램이 환경변수 대신 파일을 읽는 경우에는 관리자 주입 설정만으로 해당 값이 사라지거나 동작이 바뀌지 않습니다
인스턴스별 값 생성, 비밀값 폐기 API, 채점 hash의 릴리스별 버전 관리는 추가 구현이 필요합니다
FLAG를 교체하면 기존 hash 검사도 바뀌므로 기존 인스턴스와 reset의 처리 기준을 함께 정해야 합니다

일반 env와 비밀값은 컨테이너당 합계 128개, 값당 UTF-8 4096바이트, 이름과 값을 합쳐 16384바이트까지 허용합니다
이름은 64자 이하의 대문자 식별자이며 값은 문자열만 허용합니다
비밀값 이름은 64자 이하의 소문자 식별자입니다

관련 검사: [실행 설정 테스트](../apps/instances/tests_execution_settings.py)

## 관리자 .env 가져오기

관리자는 릴리스와 컨테이너를 선택하고 .env 파일을 가져옵니다
미리보기에서 각 항목의 일반 설정·비밀값 구분과 필수 여부를 확인한 뒤 새 설정 버전을 저장합니다
파일에 없는 항목, 다른 컨테이너의 설정과 비밀값 버전은 원본 릴리스를 그대로 유지합니다
저장 후 릴리스를 활성화해야 새 인스턴스에 적용됩니다

| API | 처리 |
| --- | --- |
| POST /api/v1/admin/challenges/{challenge_id}/releases/{release_id}/environment_files/preview | 파일 검사와 변경 내용 확인, DB에 쓰지 않음 |
| POST /api/v1/admin/challenges/{challenge_id}/releases/{release_id}/environment_files/import | 확인한 분류에 따라 비밀값과 새 릴리스를 한 트랜잭션으로 저장 |

두 API 모두 관리자 JWT가 필요하며 응답에 no-store를 적용합니다
요청·응답 body는 수집하지 않습니다
파일 원문은 DB에 저장하지 않으며 비밀값을 미리보기·저장 응답에 포함하지 않습니다

미리보기 요청에는 container와 content를 보냅니다
새 항목은 비밀값으로 분류하며, 원본에 일반 값으로 등록된 항목은 기존 분류를 사용합니다
FLAG, DB_PASS, 비밀번호·토큰·키 이름과 기존 비밀값 연결은 가져오기에서 일반 값으로 바꿀 수 없습니다
이름 검사는 보조 수단이며 문제별 env_rules에 종류와 필수 여부를 저장합니다
필수 항목을 비우거나 직접 편집에서 삭제해도 백엔드가 거절합니다

분류를 수정할 때는 entries에 name, kind(env 또는 secret), required를 보냅니다
비밀값에는 secret_name과 use_existing을 추가합니다
기존 값을 쓰려면 파일의 해당 값을 비우고 use_existing을 true로 보냅니다
미리보기가 반환한 secret_id를 함께 보내면 그 버전에 고정됩니다
예를 들어 FLAG=는 원본 릴리스의 flag 연결을 유지할 수 있으며, 새 문제에 등록된 값이 없으면 저장할 수 없습니다
파일에 값이 있는 비밀값은 새 버전을 만들고, 해당 컨테이너의 새 릴리스에만 연결합니다

저장 요청에는 검토한 entries와 UUID 형식 request_id가 필수입니다
같은 request_id와 같은 내용의 재시도는 처음 저장한 릴리스를 반환합니다
같은 request_id에 다른 내용을 보내면 409를 반환합니다
비교용 지문은 서버 키를 사용하는 HMAC이며 API로 공개하지 않습니다
처리 중 검증·암호화·릴리스 저장에 실패하면 새 비밀값까지 전부 취소합니다

### 파일 문법

- UTF-8, 선택적 BOM, LF·CRLF 줄바꿈, 파일 크기 64KiB 이하
- 한 줄에 NAME=VALUE, 이름은 기존 환경변수 계약과 같은 대문자 식별자
- 빈 줄과 공백 뒤 #으로 시작하는 주석 줄 무시
- 첫 =에서 이름과 값을 나누고 양끝 공백·탭 제거
- 값 양끝에 같은 따옴표 한 쌍이 있으면 그 한 쌍만 제거하고 안쪽 공백 보존
- $NAME, ${NAME}, #, =, 역슬래시는 값 그대로 보존, 치환·이스케이프 해석·명령 실행 없음
- export 접두사, 여러 줄 값, 중복 이름, NUL, 잘못된 UTF-8은 거절
- 값 뒤의 #은 주석이 아니므로 주석은 별도 줄에 작성
- 일반 값의 빈 문자열은 required가 false일 때 허용, 비밀값의 빈 문자열 저장은 거절

현재 런타임의 적용 대상은 Kubernetes 컨테이너입니다
일반 VM·Windows VM의 시작 과정에 값을 전달하는 기능과 파일·DB에 들어 있는 플래그의 전환은 이번 가져오기 기능에 포함되지 않습니다
환경변수로 읽는 문제는 기존 소스·이미지를 유지하고 실행 시 주입합니다

관련 검사: [파일 가져오기 테스트](../apps/instances/tests_environment_files.py)

참가자 ZIP도 배포 버전별로 연결할 수 있습니다
환경변수를 수정한 새 버전은 원본의 파일 선택을 유지합니다
자세한 동작과 API는 [배포 버전별 참가자 파일](release-files.md)에 정리했습니다

## 관리자 배포 테스트

배포 설정에서 버전을 고른 뒤 배포 테스트를 실행합니다
선택한 버전의 이미지·환경변수·비밀값 참조를 사용하며 현재 적용 버전은 바꾸지 않습니다
실행되면 접속 주소가 나오고 관리자가 직접 문제 화면과 기능을 확인합니다
RUNNING은 컨테이너 실행 상태이며 문제 기능 검수의 합격 판정은 아닙니다

| API | 처리 |
| --- | --- |
| GET /api/v1/admin/challenges/{challenge_id}/deployment_tests | 이 문제의 내 테스트 최근 10건과 진행 중인 테스트 조회 |
| POST /api/v1/admin/challenges/{challenge_id}/releases/{release_id}/deployment_tests | 선택한 버전으로 테스트 접수, body는 UUID request_id만 허용 |
| GET /api/v1/admin/challenges/{challenge_id}/deployment_tests/{test_id} | 상태와 접속 주소 조회 |
| DELETE /api/v1/admin/challenges/{challenge_id}/deployment_tests/{test_id} | 실행 중인 테스트 종료 |

관리자 권한이 필요하고 각 관리자는 본인 테스트만 조회·종료할 수 있습니다
관리자당 테스트 하나를 실행할 수 있으며 TTL과 hard timeout은 모두 15분입니다
준비 중에는 스케줄러가 종료 요청을 지원하지 않으므로 준비 완료 후 종료합니다
화면을 닫아도 만료와 자원 정리는 스케줄러가 처리합니다
정리가 끝나기 전에는 다음 테스트를 접수하지 않습니다

[스케줄러 #63](https://github.com/MSG-CTF/instance-scheduler/pull/63)과 [런타임 #47](https://github.com/MSG-CTF/secure-provisioner/pull/47)을 함께 적용하면 생성 실패 뒤 자원이 삭제됐는지 확인하고 정리를 완료합니다
자원 소유권이나 생성 작업의 종료를 확인할 수 없으면 정리 중으로 남으며 운영자 확인이 필요합니다
백엔드는 스케줄러가 정리 완료를 확인한 뒤 다음 테스트를 허용합니다

테스트마다 별도 scheduler user_id를 사용하고 참가자 계정·팀·Instance 행을 만들거나 바꾸지 않습니다
같은 request_id를 재전송하면 기존 테스트를 반환합니다 다른 버전에 재사용하면 409입니다
접수 응답이 유실되면 테스트 user_id로 상태를 복구하며 생성 요청을 다시 전송하지 않습니다

미승인 릴리스는 테스트가 유효한 동안에만 런타임의 비밀값 조회를 허용합니다
approved_at과 current_release는 그대로 유지하며 종료·만료 뒤에는 이 임시 허용이 사라집니다
요청 시 테스트 예약을 먼저 커밋한 뒤 스케줄러를 호출해야 하므로 생성 뷰는 ATOMIC_REQUESTS에서 제외합니다
상태 응답에는 환경변수, 비밀값, 런타임 원문 로그를 포함하지 않습니다
실패 사유는 허용된 오류 코드로 표시하며 배포 실패 뒤 정리가 완료되어도 원인을 유지합니다

Kubernetes·Docker 컨테이너 릴리스를 기존 참가자 실행과 같은 스케줄러 경로로 전달합니다
실제 실행 환경은 스케줄러가 자원을 배정하면서 정합니다 VM·Windows 릴리스는 이 경로에서 거절합니다
검사: [배포 테스트 API](../apps/instances/tests_deployment_tests.py)
