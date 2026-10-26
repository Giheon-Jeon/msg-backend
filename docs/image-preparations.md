# Backend image preparation (#139)

Runtime #70의 B안 제안 계약을 사용하는 기본 비활성 골격이다. 실제 Runtime API,
노드 에이전트, 인증 범위가 배포됐다는 의미가 아니다. Linux K3s의 AMD64/ARM64만
대상이며 Windows, KOTH, Docker 및 플랫폼 이미지는 이 경로에서 다루지 않는다.

## 흐름

- 현재 main의 승인은 release 활성화와 함께 기록된다. 이 동작을 유지한다.
- 기능이 켜져 있으면 활성화 트랜잭션에 이미지 준비 요청을 저장한다. 같은 release,
  고정 요청 내용, Runtime origin의 자동 요청은 재사용한다. HTTP는 호출하지 않는다.
- 워커가 커밋된 요청을 읽어 Runtime에 전송하고 이후 operation을 조회한다.
- 모든 컨테이너의 digest 이미지, 선언 포트, architecture, isolation profile,
  release ID, 운영자가 지정한 target ID를 요청 시점에 고정한다.
- 환경변수, 비밀값, FLAG, Registry 인증 정보와 kubeconfig는 요청에 넣지 않는다.
- 준비 기능은 최적화다. 접수 실패나 미준비 상태로 활성화 또는 인스턴스 생성을
  차단하지 않는다. 이미 실행 중인 참가자 인스턴스도 변경하지 않는다.
- 작업의 SUCCEEDED는 과거 실행 결과다. 현재 노드 캐시 준비 상태를 의미하지 않는다.
  Scheduler의 readiness 조회 및 Runtime의 노드 배치는 이번 변경에 포함하지 않는다.

## 관리자 API

모든 경로는 `/api/v1` 아래이며 관리자 인증이 필요하다. 조회는 DB 기록만 반환한다.

| Method | 경로 | 용도 |
| --- | --- | --- |
| GET | `/admin/challenges/{challenge_id}/releases/{release_id}/image_preparations` | 이력, `page` 기본 1, 20개씩 |
| POST | 위와 동일 | 승인된 release의 새 준비 요청, 본문 `{}` |
| GET | `/admin/challenges/{challenge_id}/image_preparations/{request_id}` | 요청, operation, 노드·이미지 결과 |
| POST | 위 경로 + `/refresh` | 중단된 전송/조회의 다음 워커 처리를 예약, 본문 `{}` |
| POST | 위 경로 + `/retry` | FAILED/PARTIAL의 새 child 작업 접수 |

새 요청과 retry에는 `Idempotency-Key`가 필요하다. 같은 키의 재요청은 저장된 접수를
반환하므로, 진행 결과는 별도 GET으로 조회한다. retry 본문은 `{}` 또는
`{"node_uids":["original-failed-node-uid"]}`이다. 성공 노드나 새로운 UID는 허용하지 않는다.
다른 내용에 같은 키를 사용하면 409다. 부모 operation과 결과는 덮어쓰지 않는다.

`delivery_status`는 Backend 전송 상태, `operation_status`는 Runtime 작업 상태다.
operation ID를 받은 뒤에는 GET만 수행한다. 응답 유실로 operation ID를 모를 때는
같은 request ID와 고정 본문으로 POST를 재시도한다. 이는 Runtime의 멱등 계약에
의존하며, GitHub 발행 요청의 재호출 정책과는 다르다.

일시 통신 오류는 기본 연속 3회 후 중단한다. 관리자 `/refresh`는 같은 요청으로
복구를 예약하며 새 operation을 만들지 않는다. GET 404도 기존 ID를 보존하며 자동
재생성하지 않는다. 인증·정책·멱등 충돌은 오류 코드를 남겨 검토 대상으로 표시한다.
상위 시스템의 오류 본문·자유 형식 메시지는 저장하거나 응답에 노출하지 않는다.

## 설정과 실행

`.env.example`의 `IMAGE_PREPARATION_*` 설정을 사용한다. 기본값은 비활성이다.
활성화 전에 전용 API 토큰과 허용 target ID 목록을 운영 담당자가 확인해야 한다.
기존 Scheduler 토큰이나 GitHub PAT으로 자동 대체하지 않는다.

- `IMAGE_PREPARATION_RUNTIME_URL`: HTTPS origin만 허용. 경로·후행 `/`·쿼리·userinfo 금지.
- `IMAGE_PREPARATION_TARGET_IDS`: 중복 없는 target ID 1~50개를 쉼표로 구분.
- timeout 1~30초(기본 5), poll 1~300초(기본 15), 연속 오류 한도 1~10회(기본 3).
- Runtime origin이 바뀌거나 요청 대상이 허용 target 목록에서 빠지면 기존 요청을
  변경해 전송하지 않고 검토 대상으로 멈춘다.
- 토큰은 DB에 저장하지 않는다. 토큰 교체 시 동일한 principal scope 유지 여부는
  운영자가 확인해야 한다. scope가 달라지면 기존 request ID의 멱등 보장이 달라질 수 있다.

```powershell
python manage.py migrate --plan
python manage.py migrate
python manage.py process_image_preparations --limit 20
```

주기 실행은 검증 후 운영 측에서 별도로 등록한다. 명령은 대기 중인 최대 20개를
한 번 처리하고 종료한다. 상태 갱신도 다음 실행이 필요하다. 비활성일 때는 요청 DB
조회나 HTTP 없이 종료한다. 워커는 DB 트랜잭션 밖에서 실행해야 한다.

`instances.0014_imagepreparationrequest`는 신규 테이블만 추가하며 기존 데이터는
수정·삭제하지 않는다. 요청이 참조하는 release와 부모 요청은 삭제로부터 보호한다.
이미 요청이 저장된 뒤 migration을 되돌리면 추적 기록이 사라지므로 보존해야 한다.
다른 열린 PR의 instances migration과 합칠 때는 merge migration 필요 여부를 확인한다.

## 연동 전 남은 계약

- Runtime #70 API·워커·노드 관찰 구현 및 권한 배포 확인.
- 전용 인증 scope와 trusted target 목록의 관리 주체.
- operation/request 멱등 기록의 보존 기간과 GET 404의 복구 절차.
- FAILED 결과의 필수 오류 코드 및 분류표. 미지정/미확인 코드는
  `UNCLASSIFIED_RUNTIME_FAILURE`로 표시하며 성공으로 해석하지 않는다.
- operation ID 길이와 형식 통일. Backend는 128자 이하, 경로 구분자 없는 ASCII를 받는다.
- NO_ELIGIBLE_NODES에서 재시도 대상 UID가 없는 경우의 계약. 현재는 child retry를
  거절하고 새 root 요청으로 원래 대상군을 다시 확인하도록 한다.

본 변경은 이미지의 실제 노드 저장·현재 준비 보장·배포 전 준비 게이트를 구현하지 않는다.
이 세 항목은 Runtime/Scheduler 계약과 실제 환경 검증 후 연결해야 한다.
