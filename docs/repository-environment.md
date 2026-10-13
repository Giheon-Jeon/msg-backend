# 저장소 환경변수 가져오기

관리자는 문제 저장소의 env/컨테이너이름.env.example을 불러와 새 설정 버전으로 저장합니다
info.yaml의 deployment.containers에 선언된 이름으로 파일을 찾습니다
Dockerfile·Compose·코드에서 값을 추측하지 않으며 파일이 없으면 필요한 경로를 안내합니다

~~~text
web-grade-tampering/info.yaml
web-grade-tampering/env/web.env.example
~~~

일반 기본값은 템플릿에 적고, 플래그·비밀번호·키는 비워둡니다
비밀값은 관리자 입력 또는 기존 비밀값 버전 연결로 채웁니다
이 기능은 기존 Kubernetes 실행 릴리스에서 사용하며 Windows 문제 pwn-msgdriver는 제외합니다
파일·DB에 들어 있는 정답의 주입 방식까지 바꾸지는 않습니다

## API

아래 경로는 /api/v1/admin/challenges/{challenge_id}/releases/{release_id} 아래에 있습니다
모두 관리자 JWT가 필요하며 응답에 no-store를 적용합니다

| 메서드·경로 | 하는 일 |
| --- | --- |
| GET /repository_environment?container=web | 템플릿 항목과 일반 기본값 조회, 비밀값 원문 제외 |
| POST /repository_environment/preview | 선택한 항목 검사, DB에 쓰지 않음 |
| POST /repository_environment/import | 비밀값과 새 설정 버전을 한 트랜잭션으로 저장 |

GET은 source_token·source_revision·template_path·entries·warnings를 반환합니다
source_url은 해당 커밋의 GitHub 주소이며 아직 올리지 않은 로컬 커밋은 null입니다
source_token은 문제·릴리스·컨테이너·저장소·커밋에 묶이며 1시간 동안 유효합니다
미리보기와 저장은 이 커밋을 다시 읽으므로 그 사이 브랜치가 바뀌어도 다른 값을 가져오지 않습니다

POST에는 container·source_token·선택한 entries를 보냅니다
entries는 name·kind·required를 포함합니다
비밀값은 secret_name·use_existing과, 기존 버전을 유지할 때 secret_id를 함께 보냅니다
관리자가 바꾼 값만 overrides의 이름=값으로 보냅니다
저장에는 UUID request_id가 추가로 필요합니다

같은 request_id와 내용으로 재시도하면 처음 저장한 릴리스를 반환합니다
선택하지 않은 기존 값, 다른 컨테이너, 이미지·포트·자원·비밀값 버전은 원본에서 유지합니다
저장 후 별도 활성화가 필요하며 기존 인스턴스는 생성 당시 버전을 유지합니다
비밀값 원문은 조회·미리보기·저장 응답에 넣지 않으며 암호화 저장합니다

상세 형식은 [OpenAPI](repository-environment.openapi.json)와 [기존 .env 가져오기](backend-execution-settings.md#관리자-env-가져오기)를 참고해주세요

## 파일과 설정

템플릿은 UTF-8이며 기존 .env 파서와 같은 한 줄 NAME=VALUE 문법을 사용합니다
64KiB·128개 선언까지 읽되, 주입 가능한 항목은 기존 값을 합쳐 컨테이너당 32개입니다
빈 비밀값은 새 값으로 저장할 수 없으며 기존 연결이 없으면 직접 채워야 합니다
일반 값의 빈 문자열은 필수 항목이 아닐 때만 허용합니다
중복 이름·여러 줄 값·YAML alias는 거절하며 변수 치환·명령 실행을 하지 않습니다

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

검사: [템플릿·관리자 API 테스트](../apps/instances/tests_repository_environment.py)
