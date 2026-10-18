# GitHub 발행본 가져오기

관리자 문제 목록에서 GitHub에 발행된 항목을 고르고 내용을 확인한 뒤 가져옵니다
처음 등록하는 문제는 당시 커밋의 info.yaml에서 제목·지문·분야·플래그를 읽습니다
기존 문제는 이 값을 덮어쓰지 않으며 채점 플래그가 다르면 가져오기를 막습니다

| 가져오는 내용 | 저장 후 상태 |
| --- | --- |
| 새 문제 | 비공개, 선택한 난이도, 초기 1000점·최소 600점·감소 계수 70 |
| 실행 이미지 | 같은 발행번호가 없으면 새 배포 버전 등록, 현재 적용 버전 유지 |
| 참가자 파일 | 같은 실행에서 발행된 user-files.zip 검증 후 설정된 저장소에 저장 |
| 플래그 | 채점용 해시와 암호화된 비밀값 flag 저장, API 응답에는 원문 제외 |
| 환경변수 | 이미지 발행본에 담긴 설정만 등록, 저장소 .env는 배포 설정에서 확인 후 가져오기 |

실행 문제의 파일은 배포 버전의 발행번호에 맞춰 연결합니다
파일 풀이 문제는 기존 다운로드 규칙대로 가장 높은 발행번호의 파일을 사용하므로 가져오기 화면에 변경 가능성을 안내합니다
Windows 문제 pwn-msgdriver와 VM 실행 방식은 이 경로에서 가져오지 않습니다

## API

세 경로 모두 관리자 JWT가 필요하며 응답은 no-store입니다
경로 앞에는 /api/v1을 붙입니다

| 요청 | 본문 또는 쿼리 | 응답 data |
| --- | --- | --- |
| GET /admin/published_challenges | page, 기본 1 | items, next_page, repository, source_ref |
| POST /admin/published_challenges/preview | artifact_id 정수 | 문제 정보, 발행번호, 파일 요약, can_import, blocked_reason, preview_token |
| POST /admin/published_challenges/import | preview_token, difficulty | challenge_id, challenge_created, release_id, release_status, files_status, registry_revision |

목록은 한 요청에 GitHub artifact 최대 3페이지를 확인합니다
같은 문제·workflow run의 이미지와 파일을 한 행으로 묶으며 더 오래된 기록은 next_page로 이어서 조회합니다
GitHub run 목록 100개로 확인된 실패·진행 중 기록은 선택할 수 없습니다
목록의 상태를 확인하지 못한 경우에도 미리보기에서 실제 run의 성공 여부를 검사합니다
메타데이터 캐시는 60초이며 플래그·ZIP 원문은 캐시하지 않습니다
파일 정보가 있는 것과 ZIP이 있는 것은 다릅니다 실제 ZIP 유무와 크기는 미리보기에서 반환합니다

미리보기 토큰은 관리자·artifact·내용 지문에 묶이며 20분간 유효합니다
내용 지문은 서버 키로 HMAC을 만들어 플래그에 대한 단순 사전 대입에 쓰이지 않게 합니다
저장할 때 고정된 소스 커밋과 같은 run의 파일을 다시 확인합니다
허용 브랜치, 성공한 run, SHA, 발행번호, 문제 식별자, 이미지 digest, ZIP 해시·크기·경로를 검사합니다

저장 요청은 Idempotency-Key가 필요합니다
응답을 못 받으면 본문과 키를 그대로 재사용합니다
다른 본문에 같은 키를 쓰면 409이며, 성공한 재시도는 GitHub 재조회 없이 처음 응답을 반환합니다
같은 버전을 다른 키로 가져와도 문제·릴리스·파일을 중복 등록하지 않습니다
현재 DB 기록과 발행 내용이 충돌하면 409입니다

저장소·비밀값 암호화 설정 오류는 503이며 DB 기록과 요청 키를 되돌려 재시도할 수 있습니다
파일 저장 뒤 DB 저장이 실패하면 저장소에 아직 연결되지 않은 ZIP이 남을 수 있습니다
이 파일은 내용 해시 경로로 저장되며 재시도 때 재사용합니다

## 서버 설정

RELEASE_POLL_REPO, RELEASE_POLL_SOURCE_REF, RELEASE_POLL_GITHUB_TOKEN을 기존 수집기와 공유합니다
토큰은 서버에만 두며 저장소 Contents와 Actions artifact를 읽을 수 있어야 합니다
파일은 기본 Django storage를 사용하므로 GCS를 켠 환경에서는 GCS 쓰기 권한도 필요합니다
RUNTIME_SECRET_ENCRYPTION_KEYS가 없으면 등록을 완료하지 않습니다

## 확인한 범위

PostgreSQL에서 권한, 원본 대조, 비밀값 응답 제외, 중복 요청, 파일 저장 실패 후 복구를 검사합니다
실제 GitHub 발행본으로 실행 문제·파일 풀이 문제 각각 ZIP 있음·없음을 가져와 HTTP로 다운로드했습니다
기존 제목·지문·공개 상태·현재 배포 버전은 유지됩니다
실제 GCS 업로드와 문제 컨테이너 배포는 이 검사에 포함하지 않습니다
