# service

이 디렉토리에는 참가자가 접속할 KOTH 서비스 파일을 넣습니다.

필수:

- `Dockerfile`
- 서비스 소스코드
- 필요한 설정 파일
- 팀 토큰 입력/검증 연동 코드
- `GET /internal/koth/scores` 공통 점수 API 코드

참가자에게 노출할 포트는 `info.yaml`의 `deployment.containers[].ports`에 적습니다.

## 구현해야 하는 기능

참가자용:

- 팀 토큰 입력 화면 또는 API
- `/internal/koth/team_tokens/verify` 호출
- 플랫폼 API 호출 시 `X-Internal-Token` 헤더 사용
- 검증 성공 시 `team_id` 기준 세션 생성
- 검증 실패 시 `valid: false`로 처리하고 세션 생성 금지
- 잘못된 팀 토큰 입력을 참가자 세션 또는 IP 단위로 제한 (연속 실패 시 해당 세션/IP만 일시 차단, 3회 연속 실패 시 30초 락)
- 팀별 상태 저장

운영자/백엔드용:

- `GET /healthz` 생존 확인 엔드포인트, 9090 포트, 인증 없이 200 반환 (참가자 진입점인 8080과 다른 포트입니다. `info.yaml`의 `deployment.healthcheck`와 포트·경로가 같아야 함)
- `GET /internal/koth/scores`
- `X-KOTH-Internal-Token` 인증
- `period_id`, `scored_at` query 처리
- 팀별 `metric_score`와 `period_rank` 반환 (지급 점수는 플랫폼이 계산하므로 반환하지 않음)
- 미제출·실패 팀은 `metric_score`를 0으로 반환하며 플랫폼 지급 순위에서 제외됨
- 결과가 없는 구간은 `data: null` 반환
- period별 결과 저장 및 재조회
- `code`, `message`, `data` 공통 응답 봉투 적용

브라우저가 보낸 `team_id`는 사용하지 말고, 팀 토큰 검증 API에서 받은 `team_id`만 사용합니다.

`team_id`와 `koth_challenge_id`는 UUID 문자열입니다. 정수 컬럼이나 정수 파싱을 쓰지 마세요.
