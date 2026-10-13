# 저장소 환경변수 가져오기

관리자는 출제자가 설정한 환경변수 이름과 실제 값을 불러와 새 설정 버전으로 저장합니다
문제 저장소의 env/컨테이너명.env를 읽습니다
예를 들어 info.yaml에 이름이 web인 컨테이너가 있으면 env/web.env가 원본입니다
이 파일이 있으면 Dockerfile·Compose·코드에서 다른 값을 다시 모으지 않습니다
아직 이관하지 않은 문제에만 기존 파일 탐색을 사용합니다

플래그·비밀번호·키도 원본 값을 가져옵니다
브라우저에는 원문을 보내지 않고, 서버가 고정된 커밋에서 다시 읽어 암호화 저장합니다
원본값이 있으면 기본 선택은 출제자 원본값 사용입니다
원본에서 값을 확인할 수 없고 기존 연결이 있으면 그 비밀값 버전을 유지합니다
이 기능은 기존 Kubernetes 실행 릴리스에서 사용하며 Windows 문제 pwn-msgdriver는 제외합니다
파일·DB에 들어 있는 정답의 주입 방식까지 바꾸지는 않습니다

## API

아래 경로는 /api/v1/admin/challenges/{challenge_id}/releases/{release_id} 아래에 있습니다
모두 관리자 JWT가 필요하며 응답에 no-store를 적용합니다

| 메서드·경로 | 하는 일 |
| --- | --- |
| GET /repository_environment?container=web | 출제자의 환경변수·일반 값·출처 조회, 비밀값 원문 제외 |
| POST /repository_environment/preview | 선택한 항목 검사, DB에 쓰지 않음 |
| POST /repository_environment/import | 비밀값과 새 설정 버전을 한 트랜잭션으로 저장 |

GET은 source_token·source_revision·build_context·source_files·entries·warnings를 반환합니다
각 항목의 resolution은 configured·code_default·author_example·needs_value·conflict 중 하나입니다
확인할 수 없는 값을 빈 문자열이나 임의 기본값으로 바꾸지 않습니다
source_url은 해당 커밋의 GitHub 주소이며 아직 올리지 않은 로컬 커밋은 null입니다
source_token은 문제·릴리스·컨테이너·저장소·커밋에 묶이며 1시간 동안 유효합니다
미리보기와 저장은 이 커밋을 다시 읽으므로 그 사이 브랜치가 바뀌어도 다른 값을 가져오지 않습니다

POST에는 container·source_token·선택한 entries를 보냅니다
entries는 name·kind·required를 포함합니다
비밀값은 secret_name·use_existing과, 기존 버전을 유지할 때 secret_id를 함께 보냅니다
관리자가 바꾼 값만 overrides의 이름=값으로 보냅니다
저장에는 UUID request_id가 추가로 필요합니다

같은 request_id와 내용으로 재시도하면 처음 저장한 릴리스를 반환합니다
선택하지 않은 기존 값, 다른 컨테이너, 이미지·포트·자원은 원본에서 유지합니다
출제자 원본값을 선택한 비밀값은 새 버전으로 저장하고, 기존 값 사용을 선택하면 연결 버전을 유지합니다
저장 후 별도 활성화가 필요하며 기존 인스턴스는 생성 당시 버전을 유지합니다
비밀값 원문은 조회·미리보기·저장 응답에 넣지 않으며 암호화 저장합니다

상세 형식은 [OpenAPI](repository-environment.openapi.json)와 [기존 .env 가져오기](backend-execution-settings.md#관리자-env-가져오기)를 참고해주세요

## 파일과 설정

.env는 64KiB·256개 선언까지 읽되, 주입 가능한 항목은 기존 값을 합쳐 컨테이너당 128개입니다
읽은 값은 기존 .env 저장 경로로 전달합니다
통일한 .env의 값은 Compose와 관리자에서 같은 방식으로 읽습니다
달러 기호를 값 자체로 쓰려면 작은따옴표로 감쌉니다
실행 중 생성되는 값은 선언하지 않아 프로그램이 원래대로 생성하게 둡니다
원래 외부 입력이 필요했던 항목은 기존 필수값 표현을 유지하며 서버 환경변수로 채우지 않습니다
아직 이관하지 않은 문제의 Compose는 같은 빌드 경로의 서비스를 찾고 environment → env_file → Dockerfile ENV 순으로 적용합니다
Compose의 .env는 참조된 값만 사용하며 서버 프로세스의 환경변수를 섞지 않습니다
저장소 안에서 확인되는 변수 참조와 기본값 표현은 처리하지만 프로그램과 셸 명령은 실행하지 않습니다
원본에 없는 값·계산되는 값·서로 충돌하는 값은 확인이 필요한 상태로 표시합니다
코드의 상수 기본값은 출처를 표시하고, 출제자의 예시 파일만 있는 항목은 기본 선택에서 제외합니다
빈 일반 값은 필수 항목이 아닐 때 보존하며 빈 비밀값은 새 값으로 저장하지 않습니다
중복 이름·YAML alias는 거절하고 여러 줄 값은 별도 확인 대상으로 둡니다

우선순위와 치환 기준: [Docker 환경변수 우선순위](https://docs.docker.com/compose/how-tos/environment-variables/envvars-precedence/) · [Compose 변수 치환](https://docs.docker.com/reference/compose-file/interpolation/)

| 설정 | 기본값·용도 |
| --- | --- |
| RELEASE_POLL_REPO | MSG-CTF/2026_MSG_CTF, 읽을 문제 저장소 |
| RELEASE_POLL_GITHUB_TOKEN | 비공개 저장소 읽기 권한이 있는 서버 토큰 |
| REPOSITORY_ENV_REF | main, 새로 불러올 때 읽을 브랜치 또는 커밋 |
| REPOSITORY_ENV_GIT_DIR | 기본은 빈 문자열, 로컬 검증용 Git 체크아웃 경로 |

경로·저장소·브랜치는 서버 설정이며 요청에서 바꿀 수 없습니다
GitHub API 호스트는 고정하고 심볼릭 링크와 서브모듈은 읽지 않습니다
로컬 모드는 커밋된 파일만 읽으며 작업 중인 파일은 반영하지 않습니다
저장소 코드를 실행하거나 파일을 수정하지 않습니다

검사: [원본 환경변수·관리자 API 테스트](../apps/instances/tests_repository_environment.py)
