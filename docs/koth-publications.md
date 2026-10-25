# KOTH 발행본 가져오기

출제자가 main에 수정한 뒤 CI가 성공하면 기존 릴리스 수집 작업이 KOTH 발행본도 등록합니다
같은 커밋의 info.yaml, env/<컨테이너>.env, 이미지 digest, 참가자 ZIP을 한 버전으로 묶습니다
등록만으로 경기를 공개하거나 실행 VM을 바꾸지는 않습니다

관리자는 배포 버전에서 내용을 확인하고 환경변수 탭에서 적용합니다
VM 에이전트가 이미지와 실제 환경변수를 확인해 APPLIED를 보내면 실행 소스, 지문과 참가자 파일을 함께 전환합니다
실패하거나 응답이 늦게 도착한 요청은 현재 버전의 지문과 파일을 바꾸지 않습니다
이전 발행본도 같은 방식으로 다시 적용할 수 있습니다

운영 토큰, 잠근 값, 관리자가 직접 지정한 값과 선택한 FLAG는 새 출제자 기본값으로 덮지 않습니다
새로 필요한 비밀값은 관리자 화면에서 채우며 일반 응답과 감사 기록에 원문을 남기지 않습니다
컨테이너 구성이나 자원·포트 설정이 바뀌면 자동 적용을 막고 운영 프로필 확인을 요구합니다
처음 연결하는 VM은 실행 중인 발행본부터 등록해야 이후 프로필을 대조할 수 있습니다

| API | 용도 |
| --- | --- |
| GET, POST /api/v1/admin/koth/challenges | 목록과 신규 등록 |
| GET, PATCH /api/v1/admin/koth/challenges/{challenge_id} | 공개 전 지문을 포함한 상세와 수정 |
| GET /api/v1/admin/koth/repository | KOTH 폴더의 제목·지문·연결 상태 |
| PUT /api/v1/admin/koth/challenges/{challenge_id}/repository | 폴더 연결과 선택한 제목·지문 가져오기 |
| GET /api/v1/admin/koth/published?page=1&slug=koth-jenga | 해당 문제의 GitHub 발행 이력 |
| GET /api/v1/admin/koth/challenges/{challenge_id}/publications | 가져온 버전과 설정·파일 상태 |
| POST /api/v1/admin/koth/challenges/{challenge_id}/publications/import | artifact_id로 미리보기, preview_token과 reason을 함께 보내 등록 |
| GET, PUT /api/v1/admin/koth/challenges/{challenge_id}/files | 현재 파일 확인과 제공 여부 변경 |
| GET /api/v1/admin/koth/challenges/{challenge_id}/files?publication_id=... | 가져온 발행본의 파일 미리보기 |
| GET /api/v1/admin/koth/challenges/{challenge_id}/files/download | 관리자 ZIP 다운로드, publication_id로 발행본 지정 가능 |
| GET /api/v1/admin/koth/challenges/{challenge_id}/periods | 점수 수집 이력 |
| POST /api/v1/admin/koth/challenges/{challenge_id}/periods/{period_id}/retry | 종료된 미반영 구간 재수집 |
| POST /api/v1/admin/koth/challenges/{challenge_id}/connection_test | 저장된 문제·점수 조회 주소의 연결 확인 |

모든 관리자 API는 Bearer 인증과 ADMIN 권한이 필요합니다
참가자 지문·파일·접속 주소는 기존 ACTIVE 조건을 유지합니다
저장소 연결은 metadata_revision만 갱신하며 실행 중인 source_revision을 바꾸지 않습니다
파일 제공 설정은 현재 발행 ZIP을 선택하거나 숨기며 main 파일을 따로 다시 묶지 않습니다

새 점수 서버의 주소는 KOTH_SCORE_API_ALLOWED_ORIGINS에 먼저 등록합니다
접속 시험은 저장된 주소와 KOTH 전용 토큰 이름으로만 진행하며 임의 주소로 서버 비밀값을 보내지 않습니다
관리자 수정 사유는 1~500자이며 수집·적용 이력과 분리해 기록합니다

VM 에이전트의 allow_publications는 기본 false입니다
운영자가 켜면 성공한 CI에서 등록된 발행본에 한해 기존 컨테이너와 같은 GHCR 저장소의 digest로 전환합니다
네임스페이스, 컨테이너 이름, 격리 설정, 자원 한도와 볼륨은 기존 프로필을 유지합니다
