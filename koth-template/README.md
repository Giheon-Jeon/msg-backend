# koth-problem-name

## 출제자

- author-name

## 문제 세팅 방법

문제 디렉토리 이름은 다른 분야와 같은 규칙을 씁니다. `분야-문제명` 형태이므로 KOTH 문제는 `koth-문제명`으로 만듭니다. 소문자와 하이픈만 사용합니다.

`info.yaml`은 일반 문제 양식에 `deployment.healthcheck`만 추가한 형태입니다. 그 외 KOTH 전용 필드는 없습니다. 팀 토큰 인증과 점수 API 규칙은 이 README와 `prob/for_organizer/admin.md`가 담당합니다.

로컬 테스트(출제자 편의용, 실제 배포는 `info.yaml`의 `deployment`가 담당):

```bash
cd prob/for_organizer
docker compose up --build
# service: http://localhost:8080
# score:   http://localhost:8080/internal/koth/scores
```

실제 대회 배포는 플랫폼이 `info.yaml`의 `deployment.containers` 명세로 쿠버네티스 리소스를 생성합니다.
출제자는 컨테이너별 Dockerfile, KOTH 문제 서버 코드, `GET /internal/koth/scores` 공통 API 코드, `info.yaml`을 정확히 작성하면 됩니다.

`club_id`와 `koth_challenge_id`는 플랫폼이 발급해서 전달하는 값입니다. `info.yaml`에 적지 않고, 문제 서버에는 환경 변수로 주입받습니다.

## 출제 지문

참가자에게 공개될 문제 설명을 적습니다.

예시:
참가자는 KOTH 문제 서버에 접속해 팀 토큰으로 인증한 뒤 목표 상태를 만들고 유지해야 합니다.
플랫폼 백엔드는 15분마다 문제 서버의 공통 점수 API를 조회하고, 해당 구간에서 각 팀이 획득한 점수를 누적합니다.

접속: `http://<주어진 주소>:8080`

참가자는 플랫폼에서 확인한 KOTH 팀 토큰을 문제 서버에 입력합니다.
문제 서버는 팀 토큰을 직접 신뢰하지 않고 플랫폼 백엔드의 검증 API로 확인해야 합니다.

## KOTH 구현 규칙

KOTH 문제는 일반 web 문제와 달리 팀 토큰 인증과 15분 점수 API가 필요합니다.

### 팀 토큰 인증

1. 참가자가 문제 서버에서 팀 토큰을 입력합니다.
2. 문제 서버가 플랫폼 백엔드의 `/internal/koth/team_tokens/verify`를 호출합니다.
3. 검증 성공 시 응답의 `team_id`, `team_name`, `koth_division`을 세션 또는 서버 상태에 저장합니다.
4. 이후 모든 팀 상태, 제출 코드, 점유 정보, 성능 측정값은 `team_id` 기준으로 관리합니다.
5. 브라우저가 직접 보낸 `team_id`는 사용하지 않습니다.

Method: `POST`

URL: `/internal/koth/team_tokens/verify`

Header:

```json
{
  "X-Internal-Token": "<INTERNAL_TOKEN>",
  "Content-Type": "application/json"
}
```

검증 요청:

```json
{
  "koth_challenge_id": "018f3f1e-0700-7a91-a30b-630000000010",
  "team_token": "<TEAM_TOKEN>"
}
```

검증 성공 응답:

```json
{
  "code": "SUCCESS",
  "message": "성공",
  "data": {
    "valid": true,
    "team_id": "018f3f1e-0100-7a91-a30b-630000000003",
    "team_name": "MJSEC",
    "koth_challenge_id": "018f3f1e-0700-7a91-a30b-630000000010"
  }
}
```

검증 실패 응답:

```json
{
  "code": "SUCCESS",
  "message": "성공",
  "data": {
    "valid": false,
    "team_id": null,
    "team_name": null,
    "koth_challenge_id": "018f3f1e-0700-7a91-a30b-630000000010"
  }
}
```

`koth_challenge_id`와 `team_id`는 UUID 문자열입니다. 정수가 아니므로 정수로 파싱하지 마세요.

주의사항:

- 출제자에게 전체 팀 토큰 목록을 전달하지 않습니다.
- 원본 팀 토큰은 저장하거나 로그에 남기지 않습니다.
- 잘못된 팀 토큰이 들어오면 문제 서버는 참가자 세션을 만들지 않아야 합니다.
- 문제 서버가 플랫폼을 호출할 때 쓰는 `X-Internal-Token`과 플랫폼이 문제 서버를 호출할 때 쓰는 `X-KOTH-Internal-Token`은 서로 다른 값입니다.
- 잘못된 팀 토큰 연속 제출은 문제 서버가 참가자 세션 또는 IP 단위로 제한합니다. 자세한 정책은 `prob/for_organizer/admin.md` 참고.

### 팀 상태

출제자는 문제별로 다음 내용을 작성해야 합니다.

- `team_id`별로 저장하는 값: 예시 `제출 코드`, `점유 상태`, `성능 측정값`, `마지막 갱신 시각`
- 팀 토큰 검증 성공 후 처리: 세션 생성, 팀별 작업 공간 생성, 기존 상태 복구 등
- 잘못된 토큰 처리: `valid: false` 응답 시 세션 생성 금지, 에러 메시지 표시 등
- 팀 상태 초기화 조건: 수동 초기화인지, 재시작 후 유지해야 하는지

### 15분 점수 API

문제 서버는 아래 API를 구현해야 합니다.
문제 서버에서 플랫폼으로 점수를 보내는 기능은 만들지 않습니다.
플랫폼 백엔드가 15분마다 문제 서버를 조회합니다.

Method: `GET`

URL: `/internal/koth/scores`

Header:

```json
{
  "X-KOTH-Internal-Token": "<문제별 내부 인증값>"
}
```

Query:

```json
{
  "period_id": "2026-07-31T10:15:00Z",
  "scored_at": "2026-07-31T10:15:00Z"
}
```

성공 응답:

```json
{
  "code": "SUCCESS",
  "message": "성공",
  "data": {
    "koth_challenge_id": "018f3f1e-0700-7a91-a30b-630000000010",
    "period_id": "2026-07-31T10:15:00Z",
    "results": [
      {
        "team_id": "018f3f1e-0100-7a91-a30b-630000000003",
        "period_rank": 1,
        "metric_score": 98.73
      },
      {
        "team_id": "018f3f1e-0100-7a91-a30b-630000000004",
        "period_rank": 2,
        "metric_score": 92.15
      }
    ],
    "total_count": 2
  }
}
```

결과가 없는 구간 응답:

```json
{
  "code": "SUCCESS",
  "message": "결과가 없습니다.",
  "data": null
}
```

필수 규칙:

1. `period_id`는 15분 구간 시작 시각이며 분은 `00`, `15`, `30`, `45`만 허용합니다.
2. `scored_at`은 함께 받지만 계산 기준은 `period_id`입니다.
3. 같은 `period_id`로 다시 요청하면 같은 결과를 반환합니다.
4. 이미 저장된 구간은 다시 계산하지 않고 저장된 값을 그대로 반환합니다.
5. 지난 구간 결과도 대회 종료 전까지 다시 조회할 수 있어야 합니다.
6. 한 번 저장한 구간 결과는 이후 등수가 바뀌어도 수정하지 않습니다.
7. `team_id`는 팀 토큰 검증 API에서 받은 값만 사용합니다.
8. 응답 body는 항상 `code`, `message`, `data` 3개 키를 포함합니다.
9. 목록은 `data.results`로 감쌉니다.
10. 점수는 반환하지 않습니다. 문제 서버는 등수까지만 계산하고, 등수에 배점을 적용하는 것은 플랫폼 백엔드가 합니다.

### 등수 계산

출제자는 다음 내용을 작성해야 합니다.

- 등수 기준 값: 예시 `처리량`, `생존 시간`, `점유 시간`, `최적화 점수`
- 정렬 방향: 높은 값이 좋은지, 낮은 값이 좋은지
- 순위 제외 조건: 인증 실패, 서비스 장애, 제출 코드 오류 등

동점 처리는 출제자 재량이 아니라 공통 규칙입니다. 같은 등수 부여 시 다음 등수는 동점 팀 수만큼 건너뜁니다 (예: 공동 1등이 2팀이면 다음 등수는 3등). 배점표의 동점 합산 규칙이 이 방식을 전제로 계산되어 있습니다.

등수별 지급 점수는 출제자가 정하지 않습니다. 6개 문제의 배점 스케일을 맞추기 위해 플랫폼이 배점표를 관리하며, 문제 서버는 등수까지만 계산해서 반환합니다.

`metric_score`는 등수 계산에 사용한 성능 측정값입니다. 타입은 Double입니다.
`period_rank`는 해당 15분 구간의 등수입니다. 타입은 Long입니다. 대회 전체 누적 등수는 플랫폼이 따로 계산하므로 이름이 다릅니다.

`metric_score`가 0 이하인 결과는 미제출·실패로 간주되어 플랫폼 지급 순위에서 제외되고 0점을 받습니다. 플랫폼은 `koth_division`(`INTERNAL` 또는 `EXTERNAL`)별로 순위를 분리해 배점합니다.

## 문제 풀이 (writeup)

출제자가 의도한 점유 방법, 유지 전략, 최적화 방향, 방어 포인트를 적습니다.

1. 팀 토큰 인증을 완료한다.
2. 팀별 상태를 `team_id` 기준으로 저장한다.
3. 목표 상태 또는 성능 최적화 조건을 만족한다.
4. 15분 구간마다 등수 계산 결과가 안정적으로 반환되는지 확인한다.

자세한 익스플로잇은 `exploit/solve.py` 참고.

## 플래그

일반 플래그의 경우 전체 플래그를, 다이나믹 플래그의 경우 secret에 들어가는 값을 넣어주세요.

```txt
msgctf2026{replace_this}
```
