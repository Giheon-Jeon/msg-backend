# 운영자 문서

이 문서는 참가자에게 공개하지 않습니다.

## 배포 전 확인

- `docker compose up --build`로 서비스가 정상 실행되는지 확인
- 팀 토큰 입력 화면이 있는지 확인
- 문제 서버가 `/internal/koth/team_tokens/verify`를 호출해 `team_id`를 받아오는지 확인
- 브라우저가 보낸 `team_id`를 사용하지 않는지 확인
- `GET /internal/koth/scores`가 정상 JSON을 반환하는지 확인
- 같은 `period_id`로 재요청했을 때 같은 결과가 반환되는지 확인
- 실제 운영 secret, 팀 토큰 원본, 내부 토큰이 레포나 로그에 들어가지 않았는지 확인

## 플랫폼팀 제공 항목

플랫폼팀은 출제자에게 다음 값을 제공해야 합니다.

- `club_id`: 동아리 식별값 (UUID)
- `koth_challenge_id`: 문제 식별값 (UUID)
- `category`: `WEB` / `PWN` / `CRYPTO` / `REV` / `FORENSIC` / `MISC` 중 하나, 문제의 도메인 분류. `info.yaml`의 최상위 `category: koth`(챌린지 종류)와는 다른 값이며, 출제자가 직접 정하지 않음
- `open_group`: 문제 공개 순번, 대회 전체 스케줄이라 플랫폼이 배정
- `status`: `SCHEDULED` / `ACTIVE` / `CLOSED`, 플랫폼이 관리하며 출제자가 지정하지 않음
- 팀 토큰 검증 API 주소: `/internal/koth/team_tokens/verify`
- 전체 팀 조회 API 주소: `/internal/teams`
- 문제 서버 내부 API 인증값: `X-KOTH-Internal-Token`
- 플랫폼 API 호출용 인증값: `X-Internal-Token`
- 15분 채점 구간 규칙: `period_id`, `scored_at` 형식

두 인증값은 모두 플랫폼이 발급합니다. 문제마다 따로 발급하며 출제자가 직접 만들지 않습니다.

## 팀 토큰 인증 흐름

KOTH 문제는 팀별 토큰으로 참가 팀을 식별합니다.
로그인 JWT와 KOTH 팀 토큰은 서로 다른 값입니다.

1. 참가자가 플랫폼에서 자기 팀 토큰을 확인합니다.
2. 참가자가 KOTH 문제 서버에 팀 토큰을 입력합니다.
3. 문제 서버가 플랫폼 백엔드의 `/internal/koth/team_tokens/verify`를 호출합니다.
4. 플랫폼 백엔드가 `valid`, `team_id`, `team_name`, `koth_division`, `koth_challenge_id`를 반환합니다.
5. 문제 서버는 반환받은 `team_id`를 기준으로 로그인 세션과 팀별 상태를 관리합니다.
6. 원본 팀 토큰은 저장하거나 로그에 남기지 않습니다.

Method: `POST`

URL: `/internal/koth/team_tokens/verify`

Header:

```json
{
  "X-Internal-Token": "<INTERNAL_TOKEN>",
  "Content-Type": "application/json"
}
```

검증 API 요청:

```json
{
  "koth_challenge_id": "018f3f1e-0700-7a91-a30b-630000000010",
  "team_token": "<TEAM_TOKEN>"
}
```

검증 API 응답:

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

### 검증 제한 정책

차단은 문제 서버가 참가자 세션 또는 IP 단위로 합니다. 팀 토큰 검증은 인증 전 단계라 플랫폼은 이 요청이 어느 참가자의 시도인지 구분하지 못합니다. 그 상태에서 플랫폼이 문제 단위로 차단하면, 공격자가 일부러 틀린 토큰을 반복 제출해서 그 문제에 접속하려는 다른 정상 팀까지 전부 막아버리는 길을 열어줍니다. 그래서 플랫폼은 차단하지 않고 집계만 합니다.

- 문제 서버는 참가자 세션 또는 IP 단위로 잘못된 팀 토큰 연속 제출을 제한합니다. 잠금 기준은 3회 연속 실패 시 30초 락으로 고정합니다. 출제자가 다른 값을 임의로 정하지 않습니다.
- 플랫폼은 `koth_challenge_id` 단위로 검증 실패 횟수를 기록합니다. 이 집계는 요청을 차단하지 않습니다.
- 짧은 시간 안에 실패가 비정상적으로 많이 쌓이면 운영자에게 알립니다.
- 검증 성공 여부와 무관하게 응답은 항상 정상적으로 반환됩니다 (`valid: true`/`false`).

필수 규칙:

- `team_id`, `club_id`, `koth_challenge_id`는 UUID 문자열입니다. 정수로 파싱하지 않습니다.
- 출제자에게 전체 팀 토큰 목록을 전달하지 않습니다.
- 브라우저가 보낸 `team_id`는 사용하지 않습니다.
- 차단된 팀 토큰은 검증에 실패해야 합니다.
- 문제 서버용 내부 인증값은 KOTH 문제마다 따로 발급합니다.
- 팀 토큰 추측을 통한 점수 조작 방어는 문제 서버의 참가자 세션/IP 제한이 1차 방어선입니다. 플랫폼의 문제 단위 집계는 운영자 모니터링용 2차 방어선입니다.

## 전체 팀 조회

채점/문제 서버가 점수 계산에 사용할 참가 팀 목록을 가져옵니다.

Method: `GET`

URL: `/internal/teams`

Header:

```json
{
  "X-Internal-Token": "<INTERNAL_TOKEN>"
}
```

Query:

```json
{
  "koth_challenge_id": "018f3f1e-0700-7a91-a30b-630000000010"
}
```

Response:

```json
{
  "code": "SUCCESS",
  "message": "성공",
  "data": {
    "teams": [
      {
        "team_id": "018f3f1e-0100-7a91-a30b-630000000003",
        "team_name": "MJSEC"
      },
      {
        "team_id": "018f3f1e-0100-7a91-a30b-630000000004",
        "team_name": "TEAM4"
      }
    ],
    "total_count": 2
  }
}
```

차단된 팀은 제외하며, 팀 토큰 원본은 반환하지 않습니다.
전체 팀 수가 75팀이어도 같은 응답 구조를 사용하고, 목록은 항상 `data.teams`로 감쌉니다.

## 문제 서버 공통 점수 API

플랫폼 백엔드는 15분마다 각 KOTH 문제 서버의 아래 API를 호출합니다.
문제 서버가 플랫폼으로 점수를 POST하는 방식은 사용하지 않습니다.

Method: `GET`

URL: `/internal/koth/scores`

Header:

```json
{
  "X-KOTH-Internal-Token": "<문제별 내부 인증값>"
}
```

결과가 없는 구간:

```json
{
  "code": "SUCCESS",
  "message": "결과가 없습니다.",
  "data": null
}
```

Query:

```json
{
  "period_id": "2026-07-28T10:15:00Z",
  "scored_at": "2026-07-28T10:15:00Z"
}
```

`period_id`는 15분 구간의 시작 시각을 ISO-8601 UTC(`Z`)로 보냅니다. 분 값은 `00`, `15`, `30`, `45` 중 하나여야 하며, `scored_at`과 값이 다르면 `period_id`를 기준으로 계산합니다.

Response:

```json
{
  "code": "SUCCESS",
  "message": "성공",
  "data": {
    "koth_challenge_id": "018f3f1e-0700-7a91-a30b-630000000010",
    "period_id": "2026-07-28T10:15:00Z",
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

## 점수 계산 규칙

출제자는 15분 채점 구간마다 팀별 결과를 계산할 수 있어야 합니다.

필수 결과 필드:

- `team_id`: 팀 토큰 검증으로 확인한 팀 ID(UUID)
- `period_rank`: 해당 구간 등수(Long)
- `metric_score`: 해당 구간 순위 산정에 사용한 원점수(Double)
- `total_count`: `results` 배열의 길이(Long). 전체 참가 팀 수가 아니라 이 구간에서 순위가 매겨진 팀 수입니다.

지급 점수는 반환하지 않습니다. 6문제의 배점 스케일을 맞추기 위해 플랫폼이 배점표를 관리하며, 문제 서버는 등수까지만 계산합니다.

필수 규칙:

1. 같은 `period_id` 요청에는 같은 결과를 반환합니다.
2. 이미 계산한 `period_id`는 다시 계산하지 않고 저장된 결과를 그대로 반환합니다.
3. 응답 전에 해당 구간 결과를 문제 서버에 저장합니다.
4. 지난 구간 결과를 대회 종료 전까지 다시 조회할 수 있게 합니다.
5. 구간이 끝났는데 순위를 매길 팀이 없으면 200 OK, `code: "SUCCESS"`, `data: null`로 응답합니다. 아직 구간이 끝나지 않아 계산 자체를 못 한 경우에만 409 `PERIOD_NOT_READY`를 씁니다. 두 상황을 섞지 않습니다.
6. 백엔드가 응답을 받지 못하면 같은 `period_id`로 재요청할 수 있습니다.
7. 한 번 저장한 구간 결과는 이후 등수가 바뀌어도 수정하지 않습니다.
8. `team_id`는 팀 토큰 검증 API에서 받은 값만 사용합니다.
9. 브라우저에서 받은 `team_id`는 사용하지 않습니다.
10. 모든 응답 body는 `code`, `message`, `data` 3개 키를 포함합니다.
11. `data`는 객체 또는 null이며, 배열을 최상위에 두지 않습니다.

## 백엔드 처리 기준

플랫폼 백엔드는 다음 순서로 동작합니다.

1. 15분마다 `ACTIVE` 상태인 KOTH 문제 서버를 조회합니다.
2. 응답을 못 받으면 같은 `period_id`로 재시도합니다.
3. 한 구간의 재시도 횟수와 간격은 플랫폼팀 정책을 따릅니다.
4. 받은 `period_rank`에 플랫폼 배점표를 적용해 구간별 지급 점수를 계산합니다.
5. 이미 처리한 `period_id`는 다시 점수에 반영하지 않습니다.
6. 해당 팀이 이 문제에서 처음 양수 점수를 받으면 KOTH SOLVE를 만듭니다.
7. 이후 점수는 기존 SOLVE의 `earned_score`에 더합니다.
8. 기존 SOLVE의 `solved_at`은 수정하지 않습니다.
9. 채점 응답 원본은 운영 확인용 로그로 저장합니다.

## 배점 로직

플랫폼은 등록 팀을 `koth_division`의 `INTERNAL`과 `EXTERNAL`로 나눠 각 그룹의 순위를 독립적으로 계산합니다. 각 그룹 전체 팀 수의 절반(`floor(team_count / 2)`, 단 1팀 그룹은 1팀)만 점수를 받습니다.

- 1위는 40점으로 고정합니다.
- 마지막 지급 순위는 1점입니다.
- 역제곱근 감소폭을 정수로 배분하고, 인접 순위에 최소 1점 차이를 강제해 모든 순위 점수를 다르게 만듭니다.
- `metric_score`가 0 이하인 팀은 미제출·실패로 간주해 지급 순위에서 제외하고 0점을 줍니다.
- 지급 대상 밖의 팀과 응답에 없는 팀도 0점입니다. 사용하지 않은 점수는 재분배하지 않습니다.

현재 참가 규모의 결과는 다음과 같습니다.

- 내부 25팀, 상위 12팀: `40, 28, 22, 18, 15, 12, 10, 8, 6, 4, 2, 1`
- 외부 49팀, 상위 24팀: `40, 33, 29, 26, 24, 22, 20, 18, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1`

동점 팀은 그 팀들이 차지한 연속 순위의 점수를 합산한 뒤 팀 수로 나누고 소수점 이하는 버립니다. 실격·인증 실패 팀은 `results` 배열에 포함하지 않습니다.

## API 테스트 항목

- 정상 `period_id` 첫 요청: 해당 구간 결과 반환
- 같은 `period_id` 재요청: 첫 요청과 같은 결과 반환
- 지난 `period_id` 요청: 저장된 과거 결과 반환
- 잘못된 내부 인증값: 오류 응답
- 잘못된 팀 토큰: `valid: false` 처리
- 문제 서버의 같은 참가자 세션/IP에서 잘못된 팀 토큰 3회 연속 시도: 문제 서버가 429 `TOO_MANY_ATTEMPTS`와 30초 락 적용
- 플랫폼 `/internal/koth/team_tokens/verify`는 실패 시 `valid: false`를 반환하며 문제 전체를 차단하지 않음
- 동점 팀 존재: 작성한 동점 기준대로 반환
- 문제 상태 오류 또는 채점 실패: 오류 응답

## 오류 처리

- 400 `INVALID_PERIOD`: 채점 구간 값 오류
- 401 `INVALID_INTERNAL_TOKEN`: 문제 서버 인증 실패
- 409 `PERIOD_NOT_READY`: 아직 구간이 끝나지 않아 계산을 못 함 (구간은 끝났는데 순위 매길 팀이 없는 경우는 200 + `data: null`)
- 500 `SCORING_FAILED`: 문제 서버 채점 실패

오류 응답 예시:

```json
{
  "code": "INVALID_PERIOD",
  "message": "채점 구간 값이 올바르지 않습니다.",
  "data": null
}
```

## 공개 API 연결

`info.yaml`은 일반 문제 양식에 `deployment.healthcheck`만 추가한 형태입니다. 그 외 값은 모두 플랫폼이 관리하고 출제자에게 전달합니다.

문제 정보는 동아리 아래 `challenges[]` 배열에 들어갑니다. 이번 대회는 동아리 6개에 동아리당 문제 1개, 총 6문제입니다.

- `club_id`, 동아리 이름 -> `/api/v1/koth/clubs[].club_id`, `/api/v1/koth/clubs[].name`
- `koth_challenge_id`, 문제 이름, 카테고리 -> `/api/v1/koth/clubs[].challenges[]`
- `open_group`, `status` -> `/api/v1/koth/clubs[].challenges[]`

`open_group`과 `status`는 대회 전체 스케줄이라 플랫폼이 배정합니다. 둘 다 동아리가 아니라 문제 단위로 관리됩니다.

전체 KOTH 등수는 백엔드가 KOTH SOLVE의 `earned_score`를 합산해 계산합니다.
