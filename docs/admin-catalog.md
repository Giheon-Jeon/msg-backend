# 관리자 문제 확인

GET /api/v1/admin/challenges/{challenge_id}/preview는 저장된 제목·지문·현재 배포 버전의 참가자 파일을 반환합니다
참가자 상세 화면과 같은 필드를 쓰며 is_published와 release_version을 추가합니다
관리자만 조회할 수 있고 팀 개방·제출·배포 테스트를 만들지 않습니다
플래그·비밀값·환경변수는 응답에 포함하지 않습니다
다운로드는 기존 관리자 파일 다운로드 API를 사용합니다

GET /api/v1/admin/board/readiness는 실제 CHALLENGE 칸과 공개된 BoardChallenge를 난이도별로 비교합니다
difficulties의 required_count·published_count·shortfall_count와 난이도 미정 수를 반환합니다
ready는 각 난이도의 필요 수가 충족되었음을 뜻하며 문제 풀이·배포 성공 여부까지 보장하지 않습니다

python manage.py ensure_signature_drafts로 행사 6개 동아리의 비공개 초안을 만듭니다
반복 실행해도 기존 동아리·문제·플래그를 변경하지 않습니다
초안의 플래그는 사용할 수 없는 해시이며 실제 정답이 아닙니다
관리자 목록·수정 응답의 missing_fields로 미등록 항목을 확인합니다
비공개 문제는 지문을 비워 저장할 수 있지만 공개 요청은 지문·플래그가 모두 있어야 성공합니다
준비되지 않은 공개 요청은 409 SIGNATURE_NOT_READY를 반환합니다

RUNNING 인스턴스의 접속 주소가 여러 개이고 원래 release의 healthcheck가 별도 포트를 가리키면
해당 endpoint에 healthcheck_path를 추가합니다
프론트는 이를 상태 확인으로 표시하고 같은 호스트의 해당 경로로 연결합니다
DB의 주소와 네트워크 공개 설정은 변경하지 않습니다

<!-- QA B0 baseline -->
