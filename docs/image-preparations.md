# Backend image preparation (#139)

Runtime #70의 B안 계약을 사용하는 기본 비활성 기능이다. 실제 Runtime API,
노드 에이전트, 인증 범위가 배포됐다는 의미가 아니다. Linux K3s의 AMD64/ARM64만
대상이며 Windows, KOTH, Docker 및 플랫폼 이미지는 이 경로에서 다루지 않는다.

## 흐름

- 기존 `/activate`는 release 승인과 활성화를 함께 처리한다. 이 동작을 유지한다.
- 별도 `/approve`는 승인 시각과 감사 기록만 저장한다. active, 준비 요청, 기존
  pending 활성화 의도 및 세대는 변경하지 않는다. Runtime이나 Scheduler를 호출하지 않는다.
- 기능이 켜져 있으면 기존 활성화 트랜잭션에 이미지 준비 요청을 저장한다. 같은 release,
  고정 요청 내용, Runtime origin의 자동 요청은 재사용한다. HTTP는 호출하지 않는다.
- 워커가 커밋된 요청을 읽어 Runtime에 전송하고 이후 operation을 조회한다.
- 모든 컨테이너의 digest 이미지, 선언 포트, architecture, isolation profile,
  release ID, 운영자가 지정한 target ID를 요청 시점에 고정한다.
- 환경변수, 비밀값, FLAG, Registry 인증 정보와 kubeconfig는 요청에 넣지 않는다.
- 기본 모드에서는 준비 기능은 최적화다. 접수 실패나 미준비 상태로 활성화 또는
  인스턴스 생성을 차단하지 않는다. 선택적 활성화 대기 모드는 아래와 같다.
  이미 실행 중인 참가자 인스턴스의 고정 release는 변경하지 않는다.
- 작업의 SUCCEEDED는 과거 실행 결과다. 현재 노드 캐시 준비 상태를 의미하지 않는다.
  활성화 대기 모드의 워커는 별도 현재 readiness 조회로 다시 확인한다.

## 관리자 API

모든 경로는 `/api/v1` 아래이며 관리자 인증이 필요하다. 조회는 DB 기록만 반환한다.

| Method | 경로 | 용도 |
| --- | --- | --- |
| POST | `/admin/challenges/{challenge_id}/releases/{release_id}/approve` | 활성화 없는 승인, 본문 `{}` 또는 `{"reason":"검토 사유"}` |
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

명령은 대기 중인 최대 20개를
한 번 처리하고 종료한다. 상태 갱신도 다음 실행이 필요하다. 비활성일 때는 요청 DB
조회나 HTTP 없이 종료한다. 워커는 DB 트랜잭션 밖에서 실행해야 한다.

## 활성화 없는 승인과 준비

새 미승인 release는 `/approve`로 승인한 뒤 기존 `/image_preparations`에 별도로 접수한다.
승인 API는 기존 활성화와 같은 배포 가능 여부 및 미완성 저장소 환경변수 검사를 수행한다.
`reason`은 선택이며 null 또는 공백 제거 후 1~500자 문자열이다. 그 외 필드는 거절한다.
동일 release의 반복 승인은 최초 승인 시각을 유지하고 감사 기록을 중복 생성하지 않는다.
최초 승인과 `CHALLENGE_RELEASE_APPROVED` 감사 기록은 같은 트랜잭션으로 저장한다.
릴리스 목록의 `approved_at`은 미승인이면 null이다. 승인 응답은 release 식별자,
승인 시각, current_release_id, is_current를 반환하며 환경변수나 비밀값은 포함하지 않는다.

Backend 승인은 Runtime의 정확한 digest 정책 승인이나 실제 이미지 다운로드 완료를
대신하지 않는다. 승인 뒤에도 Runtime의 정책·플랫폼·노드 검증을 통과해야 한다.
준비만 할 때에는 `/activate`를 호출하지 않는다. 특히 준비 후 활성화 모드가 꺼져 있으면
기존 `/activate`는 즉시 active를 바꾼다. 기존 참가자 인스턴스는 승인 API가 변경하지 않는다.

`adminpanel.0012_admin_event_release_approved`는 승인 감사 이벤트의 type 선택지만 추가한다.
기존 release를 자동 승인하거나 데이터·파일·active를 변경하지 않는다.

`instances.0014_imagepreparationrequest`는 신규 테이블만 추가하며 기존 데이터는
수정·삭제하지 않는다. 요청이 참조하는 release와 부모 요청은 삭제로부터 보호한다.
이미 요청이 저장된 뒤 migration을 되돌리면 추적 기록이 사라지므로 보존해야 한다.
다른 열린 PR의 instances migration과 합칠 때는 merge migration 필요 여부를 확인한다.

## 주기 처리 설치 (명시적 선택)

`deploy/msg-image-preparation.service`와 `.timer`가 별도 oneshot 워커를 실행한다.
배치 기본값은 20개이며 CLI `--limit`은 1~100만 허용한다. 이전 실행이 끝난 뒤
15초 후 다음 실행을 예약하므로 systemd가 같은 service의 실행을 겹치지 않는다.
다른 프로세스가 같은 요청을 처리해도 DB 행 잠금과 timeout+30초의 영속 lease가
중복 HTTP를 방지한다. 프로세스 종료 시 lease 만료 뒤 같은 요청 ID로 복구한다.

배포 이미지에는 Dockerfile의 전체 소스 COPY로 `deploy/`와 unit 원본이 포함된다.
서버의 검토된 동일 revision 소스 checkout에서 다음 명령을 실행한다. 별도 pipeline
수정이나 설치 명령 없이 기존 release 배포만으로 timer를 자동 활성화하지 않는다.

```sh
# 먼저 지원 Runtime API/전용 권한/노드 agent/현재 정책과 모든 target을 검증한다.
# Backend 환경에서 IMAGE_PREPARATION_ENABLED=True와 전용 설정을 적용하고 재기동한다.
docker compose --project-name msg-dev --file /opt/msg-dev/compose.yml --env-file /opt/msg-dev/.env exec -T backend python manage.py migrate --noinput
sudo python3 deploy/install_image_preparation.py --check
sudo python3 deploy/install_image_preparation.py
systemctl status msg-image-preparation.timer
journalctl -u msg-image-preparation.service
```

설치기는 `process_image_preparations --check-config`와 migration 적용 여부를 먼저
확인한다. `--check`는 HTTP, 요청 처리, unit 생성 및 systemctl 변경 없이 종료한다.
활성화 설정이 꺼졌거나 오래된 Backend 이미지면 설치하지 않는다. unit은 root 소유의
일반 파일, 쓰기 제한, 정확한 내용이어야 한다. symlink/hardlink, 다른 내용, systemd
override/drop-in은 덮어쓰지 않고 중단한다. 동일 설치는 파일을 다시 쓰지 않는다.
검색 경로는 `systemd-analyze --system unit-paths`의 기본 목록과 실행 중인 system
manager의 `UnitPath`를 함께 조회한다. 조회 실패, 모호한 경로, 설치 대상 경로를
사용하지 않는 manager 설정에서는 설치를 중단한다. `/usr/local/lib`와 generator,
attached, control 및 manager 전용 경로도 검사한다. 정확한 이름의 drop-in 외에
`msg-image-.service.d`, `msg-.service.d`, `service.d`처럼 적용되는 접두어·타입별
drop-in도 운영자 검토 대상으로 중단한다. timer도 같은 규칙을 적용한다.
고정 plain unit이므로 무관한 `@` template의 drop-in은 적용되지 않는다.
다른 unit 이름의 symlink가 이 unit 이름을 가리키는 alias도 중단한다. 대상 파일이
아직 없거나 다른 검색 경로의 같은 이름을 가리켜도 alias의 설정이 함께 적용될 수
있으므로 이름과 검색 경로를 기준으로 검사한다. 이 unit 자체의 `.wants`,
`.requires`, `.upholds` 디렉터리도 추가 의존성에 대한 운영자 검토 대상으로 중단한다.
일반 enable이 만드는 `timers.target.wants`의 timer 링크는 유지한다.
이 조회는 읽기 전용이며 unit 작성 전, 원자적 작성 후, daemon reload 후에 다시
검사한다. 검사와 활성화 사이에 다른 root 운영자가 구성을 변경하지 않도록 해야 한다.
지원하지 않는 Runtime에 연결해도 설치기가 실제 API 지원을 보증하지는 않는다.
Runtime rollout 검증이 끝난 뒤에만 명시적으로 설치한다.

중지: `sudo systemctl disable --now msg-image-preparation.timer`. 진행 중인 bounded
service가 끝나면 추가 요청은 전송되지 않는다. DB 이력과 pending intent는 보존한다.

## 준비 후 활성화 (별도 기본 비활성 정책)

`IMAGE_PREPARATION_ACTIVATE_WHEN_READY=True`를 명시적으로 켜면 관리자 기존
`POST /admin/challenges/{challenge_id}/releases/{release_id}/activate`가 release를
검증·승인하고 준비 요청과 활성화 의도를 하나의 트랜잭션에 저장한다. 요청 경로에서
네트워크를 호출하지 않으며 현재 release를 유지한 채 202를 반환한다. 응답 예:

```json
{"code":"SUCCESS","data":{"challenge_id":"<uuid>","release_id":"<uuid>","current_release_id":"<previous-uuid-or-null>","activation_status":"PENDING","preparation":{"request_id":"<uuid>","activation_status":"PENDING"}}}
```

실제 `preparation`에는 기존 상세 상태 필드도 들어간다. 같은 release에 대한 pending
요청은 같은 request ID를 반환한다. 이미 현재인 release 요청은 기존과 같이 200이며
다른 pending 활성화는 취소한다. 더 새로운 활성화 요청은 이전 pending을 SUPERSEDED로
만든다. 실패 뒤 다시 activate하면 새 준비 요청을 기록한다. `/retry`의 FAILED/PARTIAL
child 요청은 부모 이력을 보존하며, 운영자가 다른 활성화를 선택하지 않았으면 같은
활성화 의도를 이어간다. retry 실행자는 child의 actor로 기록된다.

워커는 operation SUCCEEDED와 상세 이력을 확인한 다음 실행에서
`POST /v1/image-readiness/query`를 호출한다. 주 이미지와 보조 이미지의 전체 immutable
참조 집합(1~32개), architecture, 고정 target 목록(1~50개)을 보낸다. 현재 응답에서
다음 조건을 **모두** 만족해야만 현재 release를 바꾼다.

- canonical image_set_id와 architecture 일치, 정확한 target 전체가 중복 없이 존재.
- 각 target의 ready=true 및 ready_node_count=eligible_node_count>0.
- observed_at은 미래가 아니며 `IMAGE_PREPARATION_READINESS_MAX_AGE_SECONDS` 이내.
  기본 60초, 허용 1~300초. valid_until이 현재 시각 및 observed_at보다 미래여야 한다.
- 현재 설정의 origin/target 전체와 승인 release snapshot이 요청 시점과 일치.
  Runtime은 현재 create_enabled 정책 검사를 반영해야 한다.

Runtime 계약의 ready=true는 한 노드 이상을 뜻하지만 이 선택적 활성화 정책은 모든
eligible 노드의 준비를 추가로 요구한다. 누락, 0대, 부분 준비, 오래되거나 미래인
관측, 응답 schema 오류, 권한·정책 오류는 성공으로 보지 않는다. readiness 실패는
기존 MAX_ATTEMPTS(기본 3) 및 poll exponential backoff로 제한한 뒤 FAILED와 분류
오류를 기록한다. SUCCEEDED 이력은 과거 결과로 계속 보존한다.

전환 직전에 Challenge와 runtime config를 잠그고 예상 이전 release 및 활성화 세대를
다시 비교한다. 따라서 HTTP 중 운영자의 더 새로운 전환을 오래된 워커가 덮어쓰지
못한다. 증거 만료도 잠금 획득 뒤 다시 확인한다. 실제 전환과 기존 감사 이벤트는
하나의 트랜잭션으로 정확히 한 번 기록한다. 기존 인스턴스는 계속 원래 release를 쓴다.
새 `instances.0015`는 세대 및 활성화 의도 필드만 추가하며 기존 release를 변경하지 않는다.

지원되지 않는 Windows/KOTH/Docker release는 이 모드에서 명확히 400으로 거절한다.
잘못된 준비 설정 또는 disabled 준비 기능은 503이며 승인/활성화 의도도 rollback한다.
설정을 false로 되돌리면 이후 activate는 기존 즉시 전환 동작이다. 진행 중인 pending
활성화는 워커가 비활성 정책을 확인하면 FAILED로 멈추며 자동으로 즉시 전환하지 않는다.
이미 준비 기능 전체를 껐다면 워커는 DB/HTTP 없이 종료하므로 이력은 그대로 대기한다.

## 연동 전 남은 계약

- Runtime #70 API·워커·노드 관찰 구현 및 권한 배포 확인.
- 전용 인증 scope와 trusted target 목록의 관리 주체.
- operation/request 멱등 기록의 보존 기간과 GET 404의 복구 절차.
- FAILED 결과의 필수 오류 코드 및 분류표. 미지정/미확인 코드는
  `UNCLASSIFIED_RUNTIME_FAILURE`로 표시하며 성공으로 해석하지 않는다.
- operation ID 길이와 형식 통일. Backend는 128자 이하, 경로 구분자 없는 ASCII를 받는다.
- NO_ELIGIBLE_NODES에서 재시도 대상 UID가 없는 경우의 계약. 현재는 child retry를
  거절하고 새 root 요청으로 원래 대상군을 다시 확인하도록 한다.

Backend는 실제 노드 캐시를 직접 확인하지 않는다. 전용 Runtime 준비 credential은
준비 요청/상태 조회와 readiness query 권한을 모두 가져야 하며 CI/full 기존 토큰에
묵시적으로 의존하지 않는다. token 원문, 환경변수, FLAG 또는 registry credential을
요청/상태/로그에 저장하지 않는다. 실제 Runtime·노드 관측·현재 정책의 검증과 운영
대상 전체 scope 확인은 rollout의 선행 조건이다. Scheduler의 배치 선호 최적화는 별도다.
