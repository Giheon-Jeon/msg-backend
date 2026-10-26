# KOTH 이미지 전달 티켓

POST /internal/v1/koth_environments/{challenge_id}/image_export

VM별 Bearer 인증으로 현재 application_id, version_id, image_ref를 보냅니다
백엔드는 현재 적용 요청에 포함된 승인 이미지에만 5분짜리 티켓을 발급합니다
관리자 JWT, 다른 VM의 인증값, 다른 이미지나 이미 교체된 요청은 거부합니다

응답에는 url, ticket, image_ref, expires_at만 들어갑니다
expires_at은 UTC 날짜이며 레지스트리 계정·비밀번호와 문제 비밀값은 응답하지 않습니다
티켓은 특정 digest의 읽기만 허용하며 다른 이미지 요청이나 만료된 티켓은 관리 서버에서 거절합니다

KOTH_IMAGE_EXPORT_URL과 KOTH_IMAGE_EXPORT_SIGNING_KEY를 운영 환경과 GitHub Secrets에 등록합니다
관리 서버의 OCI 전달 서비스 설정과 같은 주소·전용 키를 사용합니다
전용 VM에는 image_export_url만 등록하고 티켓 서명 키나 레지스트리 인증값은 배포하지 않습니다

공유 계약은 koth-image-transfer.v1.json을 참고합니다
