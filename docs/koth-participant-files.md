# KOTH 참가자 파일

문제 서버와 같은 소스 커밋의 참가자 ZIP을 내려줍니다
출제자 파일을 재작성하거나 문제 코드를 수정하지 않습니다

1. 관리자가 KOTH 문제의 repository_slug와 실행 이미지의 source_revision을 등록합니다
2. 기존 GitHub 파일 수집 작업이 main의 신뢰된 CI 발행본을 검증하고 GCS에 저장합니다
3. 새 파일은 이력으로 쌓이며 실행 중인 source_revision을 바꾸지 않습니다
4. 참가자에게는 ACTIVE 문제의 같은 커밋에 해당하는 파일만 공개합니다

| API | 권한 | 동작 |
| --- | --- | --- |
| GET /api/v1/koth/clubs | 로그인 | 공개 중인 문제에 description과 file 메타데이터 추가 |
| GET /api/v1/koth/clubs/{club_id} | 로그인 | 같은 메타데이터 제공 |
| GET /api/v1/koth/me | 팀이 있는 로그인 사용자 | 같은 메타데이터 제공 |
| GET /api/v1/koth/challenges/{challenge_id}/file | 로그인 | ACTIVE 문제의 ZIP 다운로드 |
| GET /api/v1/admin/koth/challenges/{challenge_id}/file | 관리자 | 공개 전에도 현재 실행 버전의 ZIP 확인 |

file은 file_name, size_bytes, sha256, source_revision을 담습니다
저장소 경로나 비밀값은 반환하지 않습니다
미공개·종료 상태이거나 같은 소스 커밋의 파일이 없으면 file은 null이며 참가자 다운로드는 404입니다
관리자 다운로드는 문제 상태를 변경하지 않습니다
파일이 원래 없는 문제도 정상이며 과거 ZIP으로 대체하지 않습니다

ZIP의 크기·해시·파일 수·압축 해제 크기와 경로 검증은 기존 일반 문제의 검사기를 함께 사용합니다
저장소 장애는 내부 경로나 인증값 없이 503으로 반환합니다
응답은 첨부 ZIP이며 브라우저와 프록시에 캐시하지 않습니다

기존에 미등록 문제로 처리된 CI 발행본은 문제 매핑 후 해당 artifact만 다시 수집해야 합니다
실행 이미지를 교체할 때 source_revision도 함께 갱신해야 새 버전의 참가자 파일이 선택됩니다
