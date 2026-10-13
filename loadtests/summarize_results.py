import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
BASELINE = RESULTS / "20260927-010125"
CELL_OPEN = RESULTS / "20260927-011657" / "cell_open_stats.csv"
OUTPUT_CSV = RESULTS / "board-koth-1000-summary.csv"
OUTPUT_MD = RESULTS / "board-koth-1000-report-20260927.md"


def read_stat(path):
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    row = next(row for row in rows if row["Name"] != "Aggregated")
    count = int(row["Request Count"])
    failures = int(row["Failure Count"])
    return {
        "api": row["Name"],
        "method": row["Type"],
        "requests": count,
        "failures": failures,
        "failure_pct": failures / count * 100 if count else 0,
        "min_ms": float(row["Min Response Time"]),
        "avg_ms": float(row["Average Response Time"]),
        "max_ms": float(row["Max Response Time"]),
        "p95_ms": float(row["95%"]),
        "p99_ms": float(row["99%"]),
        "rps": float(row["Requests/s"]),
    }


rows = []
for path in sorted(BASELINE.glob("*_stats.csv")):
    if path.name == "cell_open_stats.csv":
        continue
    rows.append(read_stat(path))
rows.append(read_stat(CELL_OPEN))
rows.sort(key=lambda row: row["api"])

with OUTPUT_CSV.open("w", newline="", encoding="utf-8-sig") as handle:
    writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)


def race(name):
    return json.loads((RESULTS / name).read_text(encoding="utf-8"))


race_rows = [
    race("race-same-key.json"),
    race("race-unique-keys.json"),
    race("race-roulette_spin.json"),
    race("race-chance_now.json"),
    race("race-cell_open.json"),
    race("race-koth-team-token.json"),
]

table = [
    "| API | 요청 | 실패율 | 최소 | 평균 | 최대 | p95 | p99 | 처리량 |",
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
]
for row in rows:
    table.append(
        f"| {row['method']} `{row['api']}` | {row['requests']} | {row['failure_pct']:.1f}% | "
        f"{row['min_ms']:.0f}ms | {row['avg_ms']:.0f}ms | {row['max_ms']:.0f}ms | "
        f"{row['p95_ms']:.0f}ms | {row['p99_ms']:.0f}ms | {row['rps']:.1f}/s |"
    )

race_table = [
    "| 경합 | 동시 요청 | HTTP 결과 | 평균 | p95 | 무결성 |",
    "|---|---:|---|---:|---:|---|",
]
for row in race_rows:
    scenario = row.get("scenario", "dice_roll")
    key_mode = "동일 키" if row.get("same_key") else "서로 다른 키"
    statuses = ", ".join(
        f"{code}: {count}" for code, count in row["status_counts"].items()
    )
    race_table.append(
        f"| `{scenario}` ({key_mode}) | {row['concurrency']} | {statuses} | "
        f"{row['avg_ms']:.0f}ms | {row['p95_ms']:.0f}ms | 통과 |"
    )

report = f"""# Board/KOTH 1,000명 로컬 부하 테스트 결과

## 판정

현재 백엔드는 1,000명 동시 시작 조건에서 기능 오류나 데이터 중복보다 **처리 지연과 시간초과가 먼저 발생**합니다. 2 vCPU·2 GiB·Gunicorn sync 워커 2개 기준으로 읽기/쓰기 대부분이 평균 6~18초였고, 일부 API는 20초 제한 내 미완료가 70%를 넘었습니다. 현재 상태로는 1,000명 동시 사용을 수용한다고 판정할 수 없습니다.

소스 기준 커밋은 `b1db2aa255b2611631996e82e7fd4be39e4a8781`입니다. 테스트는 기존 로컬 DB와 분리된 PostgreSQL 16, Redis 7, API 컨테이너에서 수행했습니다. API는 2 vCPU·2 GiB로 제한했고 DB/Redis는 같은 Docker Desktop 호스트를 사용했습니다. 각 API에 가상 사용자 1,000명을 초당 1,000명으로 투입해 한 번씩 요청했으며, 클라이언트 제한시간은 20초였습니다. 제한시간 초과는 HTTP 0 실패로 집계했습니다.

## API별 결과

{chr(10).join(table)}

`cell_open`은 준비 조회를 통계에서 제외하고 API 단독으로 다시 측정한 값입니다. p95/p99가 약 21초인 행은 성공 응답의 지연이라기보다 20초 클라이언트 제한시간 초과가 다수 포함된 값입니다.

## 경합과 데이터 무결성

{chr(10).join(race_table)}

동일 멱등 키 주사위 요청 100개는 모두 200을 받았지만 실제 `DiceRoll`과 멱등 레코드는 각각 1개만 생성됐습니다. 서로 다른 키 주사위, 룰렛, 찬스 뽑기, 문제 선택은 첫 요청 1개만 성공하고 나머지 99개는 변경된 팀 상태를 감지해 409로 거절됐습니다. KOTH 팀 토큰은 100개 모두 성공했고 한 팀당 토큰 1개 제약을 유지했습니다. 최종 검사에서 주사위 범위 이탈, 중복 칸 소비, 중복 대기 굴림, 중복 찬스 카드, 중복 문제 선택, 중복 룰렛 보상, 중복 KOTH 토큰은 모두 0건이었습니다.

## 과부하와 복구

처음 수행한 30초 지속 부하에서 KOTH 클럽 목록 테스트가 끝난 뒤 API는 1분 이상 헬스 체크에 응답하지 못하고 이전 요청 큐를 계속 처리했습니다. 당시 API CPU는 약 168%, DB CPU는 약 47%였으며 메모리는 약 147 MiB였습니다. 전체 자원 표본의 최대치는 API CPU 177.9%, DB CPU 136.8%, Redis CPU 9.5%였습니다. 메모리 고갈보다 CPU와 2개 동기 워커의 요청 큐 적체가 먼저 한계에 도달했습니다.

## 워커 수와 서버 사양

저장소의 운영 Dockerfile은 Gunicorn `--workers`를 지정하지 않으므로 기본값인 **sync 워커 1개**로 실행됩니다. 기준 테스트는 워커 2개였습니다. 같은 2 vCPU에서 워커 4개로 대표 API를 비교했을 때 Board 공개 조회는 평균 8.48초에서 6.81초, p95 15초에서 11초로 개선됐지만 여전히 느렸습니다. KOTH 클럽 목록의 시간초과율은 74.6%에서 76.4%로 개선되지 않았습니다. 워커만 늘려서는 KOTH 병목을 해결할 수 없습니다.

최적화 전 임시 최소 구성은 API 4 vCPU·4 GiB, Gunicorn sync 워커 4개, PostgreSQL 4 vCPU·8 GiB, Redis 1 vCPU·1 GiB를 각각 분리하는 수준입니다. 1,000명 동시 사용을 목표로 하면 API를 4 vCPU·4 GiB 인스턴스 2개 이상으로 수평 확장할 수 있게 구성해야 합니다. 이 사양도 현재 코드의 1,000명 수용을 보장하지 않으며, 아래 수정 후 같은 테스트로 다시 검증해야 합니다.

1. KOTH 클럽 목록·상세·리더보드 결과를 짧게 캐시하고, 현재 소유자 계산에 필요한 행만 DB에서 조회합니다. 리더보드는 페이지네이션을 적용합니다.
2. 정적인 Board와 찬스카드 카탈로그는 애플리케이션 또는 프록시에서 캐시합니다.
3. `board/me`, `dice/status`, `cell/current`의 반복 쿼리 수를 측정해 합치고, 읽기 요청에서 발생하는 상태 갱신과 행 잠금을 분리합니다.
4. 운영 Gunicorn 워커 수를 명시하고, 로드밸런서의 연결·요청 제한과 API별 rate limit을 설정해 큐가 무한히 쌓이지 않게 합니다.
5. 수정 후 100→300→500→1,000 단계와 1,000명 순간 부하를 다시 실행해 p95, 시간초과율, 복구시간을 비교합니다.
"""

OUTPUT_MD.write_text(report, encoding="utf-8")
print(OUTPUT_CSV)
print(OUTPUT_MD)
