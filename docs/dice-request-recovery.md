# 브라우저·기기 간 주사위 요청 복구

같은 계정의 다른 브라우저에서는 localStorage가 공유되지 않아 새 요청 키로 주사위가 다시 사용될 수 있다
팀·사용자별 미확인 요청을 서버에 보관하고, 결과를 확인한 뒤에만 다음 요청을 발급한다

1. POST /api/v1/board/dice/request, 본문 없음 → data.request_key
2. 받은 키를 Idempotency-Key로 사용해 기존 POST /api/v1/board/dice/roll 호출
3. 결과와 최신 보드 상태를 받은 뒤 POST /api/v1/board/dice/request/ack, 본문 {"request_key":"…"}

GET /api/v1/board/dice/status의 unconfirmed_roll_request_key는 현재 계정의 미확인 키 또는 null이다
새 브라우저는 이 값을 복구해 사용자가 직접 재시도할 때 같은 요청을 보낸다
요청 준비와 상태 조회만으로 주사위를 사용하지 않는다

결과 확인 요청이 유실되면 서버에 남은 결과를 다시 확인할 수 있다
늦게 도착한 이전 확인 요청은 새 요청을 지우지 않는다
서버가 발급한 키는 확인 후 일반 재시도 기록이 정리돼도 새 굴림으로 실행되지 않는다

기존 클라이언트의 임의 키는 서버에 미확인 요청이 없을 때 종전대로 처리한다
미확인 서버 요청이 있으면 다른 키의 새 굴림은 DICE_REQUEST_UNCONFIRMED로 막는다
참가자 일반 계정은 팀장용 요청 준비·확인 API를 호출할 수 없다

위 굴림 요청 복구를 처음 도입할 때는 백엔드 마이그레이션과 API를 먼저 배포한다

## 이동 확정 요청

이동 확정 키는 각 PendingDiceRoll의 confirmation_id에 연결된다
서버가 발급한 pending_confirm_request_key를 Idempotency-Key로 보내야 하며, 브라우저마다 임의 키를 새로 만들지 않는다

| API | 확정 키 |
| --- | --- |
| POST /api/v1/board/dice/roll | pending_confirm이 true인 응답에 pending_confirm_request_key 포함 |
| GET /api/v1/board/dice/status | 현재 이동의 pending_confirm_request_key 또는 null |
| POST /api/v1/board/dice/confirm | 본문 없이 해당 키를 Idempotency-Key로 전송 |

같은 회차를 두 브라우저에서 확정해도 원래 응답을 돌려주고 이동과 보상은 한 번만 반영한다
이전 회차의 요청을 늦게 재시도해도 새 대기 이동을 확정하지 않는다
이전 재시도 기록이 정리됐다면 DICE_CONFIRM_REQUEST_EXPIRED로 거절한다

이번 이동 확정 변경은 프론트를 먼저 배포한 뒤 백엔드의 board 0011 마이그레이션과 코드를 적용한다
새 프론트는 키를 제공하지 않는 이전 백엔드에서만 기존 방식으로 동작한다
백엔드 적용 후 임의 키는 거절하므로 이전 프론트가 열린 탭은 새로고침해야 한다
