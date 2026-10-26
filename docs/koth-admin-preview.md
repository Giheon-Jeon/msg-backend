# 관리자 KOTH 게임 테스트

공개 예정 문제의 관리자 미리보기에서 게임 테스트 열기를 누르면 실제 게임을 5분 동안 열 수 있습니다
문제 상태와 공개 일정은 바뀌지 않으며, 게임 자체의 팀 토큰 로그인은 그대로 필요합니다

Backend의 KOTH_ADMIN_PREVIEW_ORIGINS에 준비된 게임의 HTTPS 원점만 쉼표로 구분해 등록합니다
기본값은 비어 있어 발급이 차단됩니다

관리자 API는 해당 관리자·문제·원점에 묶인 서명을 발급합니다
게임 게이트웨이는 매 요청마다 서명 만료와 현재 관리자 권한을 Backend에서 확인합니다
관리자 역할 삭제, 문제 상태·주소 변경, 허용 원점 제거 시 기존 서명도 거절됩니다

발급 URL의 접속 정보는 fragment에만 담습니다
게이트웨이 소유의 deploy/koth-admin-preview.html은 fragment를 즉시 지우고 POST로 교환합니다
응답은 Secure·HttpOnly·SameSite=Lax 호스트 쿠키를 설정합니다
게이트웨이는 이 쿠키를 게임 컨테이너로 전달하지 않아야 합니다
일반 참가자 접속과 채점 전용 호스트는 기존 시간·네트워크 제한을 유지합니다

서명 발급은 POST /api/v1/admin/koth/challenges/{challenge_id}/preview_sessions를 사용합니다
게이트웨이 확인은 /api/v1/koth/preview_sessions/authorize를 사용하며 일반 Access JWT는 받지 않습니다
게이트웨이가 고정한 X-KOTH-Preview-Challenge와 X-KOTH-Preview-Origin 헤더가 서명과 같아야 합니다
POST는 ticket 교환용이고 GET은 호스트 쿠키 확인용입니다

채점 API·공식 점수는 이 기능의 대상이 아닙니다
테스트 팀과 게임 기록은 운영자가 구분해서 관리해야 합니다
