# GCS 참가자 파일 검증

Backend CI를 수동 실행하면서 verify_gcs를 켜면 등록된 GCP_WORKLOAD_IDENTITY_PROVIDER·GCP_SERVICE_ACCOUNT로 인증합니다
서비스 배포는 실행하지 않으며 GitHub runner의 임시 PostgreSQL과 실제 GCS를 사용합니다

검증용 ZIP 두 개를 실제 파일 저장 경로인 dev/participant-files 아래 qa-gcs-smoke-실행번호 이름으로 저장합니다
문제 원본·플래그·운영 DB는 사용하지 않습니다

- 수집 코드로 ZIP 업로드, 같은 파일 재등록 시 중복 방지
- 관리자와 참가자의 HTTP 다운로드, SHA-256·ZIP CRC·응답 헤더 확인
- 배포 버전 변경·되돌리기와 직접 선택한 파일 적용
- 로그인하지 않은 요청, 접근 권한이 없는 참가자, 다른 버전 파일 차단
- GCS 객체의 인증 없는 직접 다운로드 차단
- 삭제된 객체의 404 응답과 검증용 객체 정리

실행 결과는 gcs-smoke artifact에 저장합니다 인증 정보와 응답 원문은 기록하지 않습니다
실패하면 failure의 단계·오류 종류와 cleanup을 확인합니다
Workload Identity 정책이나 GCS 권한 때문에 실패한 경우에는 통과로 처리하지 않습니다

tools/gcs_smoke.py의 --local-storage 옵션은 검증 코드만 점검하는 용도이며 GCS 검증 결과로 사용하지 않습니다
GitHub 발행본 가져오기는 별도 검증 대상이고 이 검사는 수집 이후 저장·다운로드 경로를 확인합니다
