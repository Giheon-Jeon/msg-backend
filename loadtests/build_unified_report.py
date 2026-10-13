from __future__ import annotations

import csv
import html
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
OUTPUT = RESULTS / "MSG_CTF_Board_KOTH_부하테스트_통합결과_20260927.html"


def read_csv(name: str) -> list[dict[str, str]]:
    with (RESULTS / name).open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


staged = read_csv("board-koth-staged-summary-20260927.csv")
worker = read_csv("worker-2-4-8-comparison-20260927.csv")
cpu = read_csv("cpu-2v4v-worker-comparison-20260927.csv")
steady = read_csv("board-koth-steady-load-20260927.csv")


def fnum(value: str, digits: int = 1) -> str:
    return f"{float(value):,.{digits}f}"


def status_class(p95: float, failures: float) -> tuple[str, str]:
    if p95 <= 3000 and failures == 0:
        return "pass", "통과"
    if failures == 0:
        return "warn", "지연"
    return "fail", "실패"


api_labels = {
    "board_get": "GET /board",
    "board_me": "GET /board/me",
    "cell_current": "GET /board/cell/current",
    "opened_challenges": "GET /board/opened_challenges",
    "chance_catalog": "GET /board/chance/catalog",
    "dice_status": "GET /board/dice/status",
    "koth_clubs": "GET /koth/clubs",
    "koth_club_detail": "GET /koth/clubs/{id}",
    "koth_me": "GET /koth/me",
    "koth_leaderboard": "GET /koth/leaderboard",
    "koth_team_token": "GET /koth/team_token",
    "koth_verify_token": "POST /koth/team_tokens/verify",
    "koth_internal_teams": "GET /internal/teams",
    "dice_roll": "POST /board/dice/roll",
    "dice_confirm": "POST /board/dice/confirm",
    "airport_move": "POST /board/airport/move",
    "chance_now": "POST /board/chance/now",
    "chance_discard": "POST /board/chance/discard",
    "chance_use": "POST /board/chance/use",
    "chance_confirm": "POST /board/chance/confirm",
    "cell_open": "POST /board/cell/open",
    "roulette_spin": "POST /board/roulette/spin",
}


steady_agg = [r for r in steady if r["api"] == "Aggregated"]

burst_1000 = [r for r in staged if r["users"] == "1000"]
burst_requests = sum(int(r["requests"]) for r in burst_1000)
burst_timeouts = sum(int(r["timeouts"]) for r in burst_1000)
burst_timeout_pct = 100 * burst_timeouts / burst_requests
burst_weighted_avg_s = (
    sum(float(r["avg_ms"]) * int(r["requests"]) for r in burst_1000)
    / burst_requests
    / 1000
)
burst_p95_min_s = min(float(r["p95_ms"]) for r in burst_1000) / 1000
burst_p95_max_s = max(float(r["p95_ms"]) for r in burst_1000) / 1000
steady_rows = "".join(
    f"""
    <tr>
      <td class="num strong">{int(r['users']):,}</td>
      <td class="num">{int(r['requests']):,}</td>
      <td class="num">{float(r['avg_ms'])/1000:.2f}s</td>
      <td class="num strong">{float(r['p95_ms'])/1000:.1f}s</td>
      <td class="num">{float(r['max_ms'])/1000:.1f}s</td>
      <td class="num">{float(r['failure_pct']):.1f}%</td>
      <td class="num">{float(r['rps']):.1f}</td>
      <td><span class="badge {status_class(float(r['p95_ms']), float(r['failure_pct']))[0]}">{status_class(float(r['p95_ms']), float(r['failure_pct']))[1]}</span></td>
    </tr>"""
    for r in steady_agg
)


by_api: dict[str, dict[int, dict[str, str]]] = {}
for row in staged:
    by_api.setdefault(row["scenario"], {})[int(row["users"])] = row

api_rows = []
for scenario, points in by_api.items():
    p100 = points[100]
    p1000 = points[1000]
    threshold = next(
        (
            u
            for u in (100, 300, 500, 1000)
            if float(points[u]["p95_ms"]) > 3000 or float(points[u]["failure_pct"]) > 0
        ),
        None,
    )
    cls, label = status_class(float(p100["p95_ms"]), float(p100["failure_pct"]))
    api_rows.append(
        f"""
        <tr>
          <td class="endpoint"><code>{html.escape(api_labels.get(scenario, p100['api']))}</code></td>
          <td class="num">{float(p100['p95_ms'])/1000:.2f}s</td>
          <td class="num">{float(p1000['p95_ms'])/1000:.1f}s</td>
          <td class="num">{str(threshold) + '명' if threshold else '없음'}</td>
          <td class="num">{float(p1000['failure_pct']):.1f}%</td>
          <td><span class="badge {cls}">{label}</span></td>
        </tr>"""
    )


worker_1000 = [r for r in worker if r["users"] == "1000"]
worker_timeout = {2: 1966, 4: 1549, 8: 2409}
worker_cards = "".join(
    f"<div class='bar-row'><span>{w}워커</span><div class='bar-track'><div class='bar {('best' if w == 4 else '')}' style='width:{v/max(worker_timeout.values())*100:.1f}%'></div></div><strong>{v:,}건</strong></div>"
    for w, v in worker_timeout.items()
)


steady_chart_points = [
    (int(r["users"]), float(r["p95_ms"]) / 1000, float(r["rps"])) for r in steady_agg
]


def polyline(
    values: list[float], ymax: float, width: int = 680, height: int = 250
) -> str:
    left, top, right, bottom = 56, 20, 20, 42
    plot_w, plot_h = width - left - right, height - top - bottom
    pts = []
    for i, val in enumerate(values):
        x = left + (plot_w * i / max(1, len(values) - 1))
        y = top + plot_h * (1 - val / ymax)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


p95_values = [p[1] for p in steady_chart_points]
rps_values = [p[2] for p in steady_chart_points]
p95_poly = polyline(p95_values, 24)
rps_poly = polyline(rps_values, 50)
x_positions = [56 + (604 * i / 3) for i in range(4)]
p95_marks = "".join(
    f"<circle cx='{x_positions[i]:.1f}' cy='{20 + 188*(1-v/24):.1f}' r='5'/><text x='{x_positions[i]:.1f}' y='{10 + 188*(1-v/24):.1f}' text-anchor='middle'>{v:.1f}s</text>"
    for i, v in enumerate(p95_values)
)
rps_marks = "".join(
    f"<circle cx='{x_positions[i]:.1f}' cy='{20 + 188*(1-v/50):.1f}' r='5'/><text x='{x_positions[i]:.1f}' y='{10 + 188*(1-v/50):.1f}' text-anchor='middle'>{v:.1f}</text>"
    for i, v in enumerate(rps_values)
)
x_labels = "".join(
    f"<text x='{x_positions[i]:.1f}' y='238' text-anchor='middle'>{u:,}명</text>"
    for i, (u, _, _) in enumerate(steady_chart_points)
)


doc = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MSG CTF Board/KOTH 부하테스트 통합 결과</title>
<style>
:root{{--bg:#f4f6fa;--paper:#fff;--ink:#172033;--muted:#667085;--line:#dde3ec;--navy:#173b63;--blue:#2878d0;--blue2:#74a9e3;--green:#15803d;--green-bg:#e9f7ee;--amber:#a15c00;--amber-bg:#fff4d7;--red:#b42318;--red-bg:#feeceb;--shadow:0 12px 32px rgba(23,59,99,.08)}}
*{{box-sizing:border-box}} html{{scroll-behavior:smooth}} body{{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,"Pretendard","Noto Sans KR",system-ui,sans-serif;line-height:1.58}} a{{color:inherit}} code{{font-family:"Cascadia Code",Consolas,monospace;font-size:.9em}}
.hero{{background:linear-gradient(135deg,#102d4c,#245d96);color:#fff;padding:58px 24px 50px}} .wrap{{max-width:1180px;margin:auto}} .eyebrow{{letter-spacing:.08em;text-transform:uppercase;font-size:13px;opacity:.78}} h1{{font-size:clamp(30px,5vw,50px);line-height:1.12;margin:10px 0 14px}} .subtitle{{max-width:820px;font-size:18px;opacity:.9;margin:0}} .meta{{display:flex;gap:10px;flex-wrap:wrap;margin-top:24px}} .meta span{{background:rgba(255,255,255,.12);padding:7px 11px;border-radius:999px;font-size:13px}}
main{{max-width:1180px;margin:-28px auto 60px;padding:0 20px}} section{{background:var(--paper);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);padding:30px;margin-bottom:22px}} h2{{font-size:25px;margin:0 0 18px;color:var(--navy)}} h3{{font-size:18px;margin:24px 0 10px}} p{{margin:8px 0}} .lead{{font-size:18px}}
.verdict{{border-left:7px solid var(--red);padding-left:22px}} .verdict h2{{color:var(--red)}} .summary-grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-top:22px}} .metric{{background:#f7f9fc;border:1px solid var(--line);border-radius:14px;padding:17px}} .metric .label{{color:var(--muted);font-size:13px}} .metric .value{{font-size:25px;font-weight:700;line-height:1.25;margin-top:5px}} .metric .hint{{color:var(--muted);font-size:12px;margin-top:4px}}
.scenario-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:18px}} .scenario-card{{border:1px solid var(--line);border-radius:15px;padding:20px;background:#fbfcfe}} .scenario-card.burst{{border-top:5px solid var(--red)}} .scenario-card.steady{{border-top:5px solid var(--blue)}} .scenario-card h3{{margin:2px 0 5px;font-size:20px}} .scenario-card .definition{{color:var(--muted);font-size:13px;min-height:42px}} .number-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:14px}} .number{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:11px}} .number span{{display:block;color:var(--muted);font-size:12px}} .number strong{{display:block;font-size:21px;margin-top:2px}} .trend{{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;align-items:end;height:150px;margin-top:18px;padding-top:20px;border-bottom:1px solid var(--line)}} .trend-col{{display:flex;flex-direction:column;justify-content:flex-end;align-items:center;height:100%;gap:4px}} .trend-value{{font-size:12px;font-weight:700}} .trend-bar{{width:min(48px,72%);background:var(--blue);border-radius:7px 7px 0 0;min-height:8px}} .trend-label{{font-size:12px;color:var(--muted);white-space:nowrap}} .failure-line{{font-size:12px;margin-top:10px;color:var(--muted);text-align:center}} .failure-line strong{{color:var(--red)}}
.key-points{{display:grid;grid-template-columns:1fr 1fr;gap:12px 28px;padding:0;margin:18px 0 0;list-style:none}} .key-points li{{position:relative;padding-left:24px}} .key-points li:before{{content:"✓";position:absolute;left:0;color:var(--green);font-weight:700}}
.toc{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:22px}} .toc a{{text-decoration:none;background:#fff;border:1px solid var(--line);border-radius:999px;padding:8px 13px;font-size:13px;color:var(--navy)}}
.two-col{{display:grid;grid-template-columns:1fr 1fr;gap:20px}} .chart{{border:1px solid var(--line);border-radius:14px;padding:14px;overflow:hidden}} .chart-title{{font-weight:700;margin:0 0 8px}} svg{{width:100%;height:auto;display:block}} svg text{{fill:var(--muted);font-size:12px}} svg .grid{{stroke:var(--line);stroke-width:1}} svg .series{{fill:none;stroke:var(--blue);stroke-width:4;stroke-linejoin:round;stroke-linecap:round}} svg circle{{fill:var(--paper);stroke:var(--blue);stroke-width:3}} svg .slo{{stroke:var(--red);stroke-width:2;stroke-dasharray:6 5}} svg .slo-label{{fill:var(--red)}}
.table-wrap{{overflow-x:auto}} table{{width:100%;border-collapse:collapse;font-size:14px}} th{{text-align:left;color:var(--muted);font-weight:600;background:#f7f9fc}} th,td{{padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:middle}} tbody tr:hover{{background:#f9fbfe}} .num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}} .strong{{font-weight:700}} .endpoint{{min-width:250px}} .badge{{display:inline-block;padding:3px 9px;border-radius:999px;font-size:12px;font-weight:700;white-space:nowrap}} .pass{{color:var(--green);background:var(--green-bg)}} .warn{{color:var(--amber);background:var(--amber-bg)}} .fail{{color:var(--red);background:var(--red-bg)}}
.callout{{background:#f1f7fd;border-left:4px solid var(--blue);padding:15px 17px;border-radius:0 10px 10px 0;margin:16px 0}} .danger{{background:var(--red-bg);border-left-color:var(--red)}} .warning{{background:var(--amber-bg);border-left-color:#d98200}}
.bar-row{{display:grid;grid-template-columns:62px 1fr 72px;gap:10px;align-items:center;margin:14px 0}} .bar-track{{height:18px;background:#edf1f6;border-radius:999px;overflow:hidden}} .bar{{height:100%;background:var(--blue2);border-radius:999px}} .bar.best{{background:var(--green)}}
.phase-grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}} .phase{{border-top:4px solid var(--blue);background:#f7f9fc;border-radius:10px;padding:14px}} .phase b{{display:block;margin-bottom:5px}} .phase span{{font-size:13px;color:var(--muted)}}
.race-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}} .race-item{{border:1px solid var(--line);border-radius:12px;padding:14px}} .race-item b{{display:block}} .race-item span{{font-size:13px;color:var(--muted)}}
.actions{{counter-reset:item;padding:0;list-style:none}} .actions li{{counter-increment:item;display:grid;grid-template-columns:38px 1fr;gap:10px;margin:14px 0}} .actions li:before{{content:counter(item);display:grid;place-items:center;width:30px;height:30px;border-radius:50%;background:var(--navy);color:#fff;font-weight:700}}
.foot{{color:var(--muted);font-size:13px;text-align:center;padding:8px 20px 30px}}
@media(max-width:850px){{.summary-grid{{grid-template-columns:1fr 1fr}}.scenario-grid,.two-col{{grid-template-columns:1fr}}.phase-grid{{grid-template-columns:1fr 1fr}}.race-grid{{grid-template-columns:1fr 1fr}}.key-points{{grid-template-columns:1fr}}}}
@media(max-width:520px){{main{{padding:0 10px}}section{{padding:20px 15px}}.summary-grid,.phase-grid,.race-grid{{grid-template-columns:1fr}}h1{{font-size:32px}}}}
@media print{{body{{background:#fff}}.hero{{padding:32px 20px}}main{{margin:0 auto}}section{{box-shadow:none;break-inside:avoid}}.toc{{display:none}}}}
</style>
</head>
<body>
<header class="hero"><div class="wrap">
  <div class="eyebrow">Final consolidated report · 2026-09-27</div>
  <h1>MSG CTF Board/KOTH<br>부하테스트 통합 결과</h1>
  <p class="subtitle">순간 동시요청, 점진적 지속부하, Race Condition, Gunicorn 워커 수, CPU 조건을 한 문서에서 비교한 최종 보고서</p>
  <div class="meta"><span>기준 커밋 b1db2aa</span><span>PostgreSQL 16</span><span>Redis 7</span><span>Gunicorn sync</span><span>로컬 Docker Desktop</span></div>
</div></header>
<main>
  <nav class="toc"><a href="#summary">맨앞 요약</a><a href="#method">테스트 범위</a><a href="#steady">지속부하</a><a href="#burst">API별 결과</a><a href="#race">경합 검사</a><a href="#workers">워커·CPU</a><a href="#cause">원인과 조치</a></nav>

  <section id="summary">
    <div class="eyebrow" style="color:var(--navy);opacity:1">Executive summary</div>
    <h2>두 가지 부하 시나리오 핵심 수치</h2>
    <p class="lead">아래 두 테스트는 조건이 다릅니다. <strong>순간 동시요청</strong>은 API 하나씩 1,000명이 같은 순간에 호출한 최악 조건이고, <strong>점진적 지속부하</strong>는 Board/KOTH 조회를 섞어 사용자를 늘린 현실적인 조건입니다.</p>
    <div class="scenario-grid">
      <article class="scenario-card burst">
        <div class="eyebrow" style="color:var(--red);opacity:1">Scenario A · 순간 동시요청</div>
        <h3>22개 API 각각 1,000명 동시 시작</h3>
        <p class="definition">각 API를 별도 회차로 시험했습니다. 22개 API를 한 번에 섞어 22,000명이 요청한 테스트가 아닙니다.</p>
        <div class="number-grid">
          <div class="number"><span>전체 요청</span><strong>{burst_requests:,}건</strong></div>
          <div class="number"><span>가중 평균 응답</span><strong>{burst_weighted_avg_s:.2f}초</strong></div>
          <div class="number"><span>API별 p95 범위</span><strong>{burst_p95_min_s:.1f}~{burst_p95_max_s:.0f}초</strong></div>
          <div class="number"><span>시간초과</span><strong>{burst_timeouts:,}건 · {burst_timeout_pct:.1f}%</strong></div>
        </div>
        <div class="callout danger"><strong>3초 기준 통과: 0/22개 API</strong><br>가장 빠른 Board 공개 조회도 p95 9.1초였습니다.</div>
      </article>
      <article class="scenario-card steady">
        <div class="eyebrow" style="color:var(--blue);opacity:1">Scenario B · 점진적 지속부하</div>
        <h3>100 → 300 → 500 → 1,000명</h3>
        <p class="definition">초당 50명씩 투입하고, 각 구간을 3분간 유지했습니다. 사용자는 Board/KOTH 조회 후 1~2초를 기다렸습니다.</p>
        <div class="trend" role="img" aria-label="점진적 부하 p95: 100명 2.9초, 300명 10초, 500명 16초, 1000명 22초">
          <div class="trend-col"><span class="trend-value">2.9초</span><div class="trend-bar" style="height:13%"></div><span class="trend-label">100명</span></div>
          <div class="trend-col"><span class="trend-value">10초</span><div class="trend-bar" style="height:45%"></div><span class="trend-label">300명</span></div>
          <div class="trend-col"><span class="trend-value">16초</span><div class="trend-bar" style="height:73%"></div><span class="trend-label">500명</span></div>
          <div class="trend-col"><span class="trend-value">22초</span><div class="trend-bar" style="height:100%"></div><span class="trend-label">1,000명</span></div>
        </div>
        <div class="failure-line">실패율: 100명 <strong>0%</strong> · 300명 <strong>0%</strong> · 500명 <strong>0%</strong> · 1,000명 <strong>85.9%</strong></div>
      </article>
    </div>
    <h3>점진적 지속부하 단계별 숫자</h3>
    <div class="table-wrap"><table><thead><tr><th>동시 사용자</th><th class="num">3분간 요청</th><th class="num">평균</th><th class="num">p95</th><th class="num">최악</th><th class="num">실패율</th><th class="num">처리량</th><th>3초 기준</th></tr></thead><tbody>{steady_rows}</tbody></table></div>
    <div class="summary-grid">
      <div class="metric"><div class="label">100명 점진 부하</div><div class="value">p95 2.9초</div><div class="hint">5,518건 · 실패 0건</div></div>
      <div class="metric"><div class="label">300명 점진 부하</div><div class="value">p95 10초</div><div class="hint">처리량 31.1 RPS에서 정체</div></div>
      <div class="metric"><div class="label">1,000명 점진 부하</div><div class="value">p95 22초</div><div class="hint">실패율 85.9%</div></div>
      <div class="metric"><div class="label">Race Condition</div><div class="value">이상 0건</div><div class="hint">6종 데이터 불변식 유지</div></div>
    </div>
    <div class="callout danger"><strong>수치에 따른 해석:</strong> 100명 점진 부하는 p95 2.9초로 3초 목표를 충족했습니다. 300명부터 p95가 10초로 증가했고, 1,000명에서는 요청의 85.9%가 시간초과되어 현재 구성으로 지속적인 1,000명 트래픽을 처리하기 어렵습니다.</div>
    <ul class="key-points">
      <li>기능 오류나 중복 저장보다 CPU·sync 워커 대기열이 먼저 한계에 도달했습니다.</li>
      <li>가장 큰 병목은 KOTH 클럽 목록·상세·리더보드입니다.</li>
      <li>2 vCPU에서는 4워커가 가장 안정적이고 8워커는 경합 때문에 악화됐습니다.</li>
      <li>로컬 4 vCPU 결과는 DB와 부하발생기까지 같은 PC를 써 운영 서버 증설 효과 판단에 사용할 수 없습니다.</li>
    </ul>
  </section>

  <section id="method">
    <h2>테스트 범위와 조건</h2>
    <div class="phase-grid">
      <div class="phase"><b>① 단일·기초 확인</b><span>API 기능, 인증, 단일 요청 지연과 SQL 특성 확인</span></div>
      <div class="phase"><b>② 순간 동시요청</b><span>22개 API × 100·300·500·1,000명, 총 41,800건</span></div>
      <div class="phase"><b>③ 경합 검사</b><span>동일/서로 다른 키 100건 동시 실행, 불변식 확인</span></div>
      <div class="phase"><b>④ 자원 비교</b><span>워커 2·4·8개와 2·4 vCPU 조건 비교</span></div>
      <div class="phase"><b>⑤ 점진적 지속부하</b><span>초당 50명 투입, 각 단계 3분, 요청 간격 1~2초</span></div>
    </div>
    <div class="callout"><strong>기준 환경:</strong> API 2 vCPU·2 GiB, Gunicorn sync 2 또는 4워커. PostgreSQL·Redis·API·Locust가 같은 8코어 노트북을 공유했습니다. 클라이언트 제한시간은 20초이며 초과는 실패로 집계했습니다.</div>
  </section>

  <section id="steady">
    <h2>현실적인 점진적 지속부하 결과</h2>
    <p>사용자를 한 번에 투입하지 않고 초당 50명씩 늘렸습니다. 100명에서는 목표를 만족했지만 300명부터 처리량 증가 없이 응답 대기만 길어졌습니다.</p>
    <div class="two-col">
      <div class="chart"><div class="chart-title">사용자 증가에 따른 p95 응답시간</div>
        <svg viewBox="0 0 680 250" role="img" aria-label="100명 2.9초, 300명 10초, 500명 16초, 1000명 22초">
          <line class="grid" x1="56" y1="208" x2="660" y2="208"/><line class="grid" x1="56" y1="114" x2="660" y2="114"/><line class="slo" x1="56" y1="184.5" x2="660" y2="184.5"/><text class="slo-label" x="60" y="178">SLO 3초</text>
          <polyline class="series" points="{p95_poly}"/>{p95_marks}{x_labels}
        </svg>
      </div>
      <div class="chart"><div class="chart-title">처리량 정체</div>
        <svg viewBox="0 0 680 250" role="img" aria-label="처리량이 100명 30.5에서 500명 33.2 RPS 수준으로 정체">
          <line class="grid" x1="56" y1="208" x2="660" y2="208"/><line class="grid" x1="56" y1="114" x2="660" y2="114"/>
          <polyline class="series" points="{rps_poly}"/>{rps_marks}{x_labels}
        </svg>
      </div>
    </div>
    <div class="table-wrap"><table><thead><tr><th>동시 사용자</th><th class="num">요청</th><th class="num">평균</th><th class="num">p95</th><th class="num">최악</th><th class="num">실패율</th><th class="num">RPS</th><th>판정</th></tr></thead><tbody>{steady_rows}</tbody></table></div>
    <div class="callout danger"><strong>1,000명:</strong> p95 22초, 최악 39.6초, 시간초과 85.9%. 처리 가능한 요청보다 들어오는 요청이 많아 워커 대기열이 계속 누적된 결과입니다.</div>
  </section>

  <section id="burst">
    <h2>22개 API 순간 동시요청 결과</h2>
    <p>각 API에 100·300·500·1,000명이 동시에 한 번씩 요청했습니다. 총 41,800건 중 시간초과 4,545건, 4xx·5xx는 0건이었습니다. p95 3초와 실패 0건을 함께 적용하면 100명에서 18/22개, 300명 1/22개, 500·1,000명 0/22개가 통과했습니다.</p>
    <div class="callout warning"><strong>해석:</strong> 순간 부하는 최악 상황을 보는 시험입니다. 단일 요청은 2~88ms였지만 2개 sync 워커 뒤에 요청이 줄을 서면서 수초~20초로 늘었습니다.</div>
    <div class="table-wrap"><table><thead><tr><th>API</th><th class="num">100명 p95</th><th class="num">1,000명 p95</th><th class="num">최초 기준 초과</th><th class="num">1,000명 실패율</th><th>100명 판정</th></tr></thead><tbody>{''.join(api_rows)}</tbody></table></div>
  </section>

  <section id="race">
    <h2>Race Condition 및 데이터 무결성</h2>
    <p>100개의 동시 요청으로 동일 행과 상태를 경합시켰으며, 최종 데이터에서 중복 생성이나 범위 이탈을 검사했습니다.</p>
    <div class="race-grid">
      <div class="race-item"><b>주사위 동일 멱등 키</b><span>200 × 100, 실제 주사위·멱등 레코드 각 1개</span></div>
      <div class="race-item"><b>주사위 서로 다른 키</b><span>200 × 1, 409 × 99 · 중복 대기 굴림 0</span></div>
      <div class="race-item"><b>룰렛</b><span>200 × 1, 409 × 99 · 중복 보상 0</span></div>
      <div class="race-item"><b>찬스 카드</b><span>200 × 1, 409 × 99 · 중복 카드 0</span></div>
      <div class="race-item"><b>문제 선택</b><span>200 × 1, 409 × 99 · 중복 선택 0</span></div>
      <div class="race-item"><b>KOTH 팀 토큰</b><span>200 × 100 · 팀당 토큰 1개 제약 유지</span></div>
    </div>
    <div class="callout"><strong>판정:</strong> 검사한 경합 시나리오에서는 Race Condition에 의한 데이터 무결성 위반이 발견되지 않았습니다. 부하 시 주요 위험은 중복 저장보다 응답 지연과 시간초과입니다.</div>
  </section>

  <section id="workers">
    <h2>Gunicorn 워커 수와 CPU 비교</h2>
    <div class="two-col">
      <div><h3>2 vCPU에서 워커별 1,000명 시간초과</h3>{worker_cards}<p class="callout"><strong>4워커 권장:</strong> 2워커보다 시간초과가 21.2% 감소했습니다. 8워커는 CPU 경합과 문맥 전환 비용으로 다시 악화됐습니다.</p></div>
      <div><h3>4 vCPU 로컬 결과의 의미</h3><p>1,000명 대표 API 6종 시간초과 합계는 2 vCPU/4W 1,549건, 4 vCPU/4W 2,539건, 4 vCPU/8W 1,876건이었습니다.</p><div class="callout warning">이 노트북에서는 vCPU 제한만 늘었고 실제 CPU가 추가되지 않았습니다. API가 CPU를 더 사용하면서 같은 호스트의 DB와 Locust 여유가 줄었습니다. 따라서 독립된 4 vCPU 서버가 더 느리다는 뜻이 아닙니다.</div></div>
    </div>
    <p><strong>KOTH는 워커만 늘려도 해결되지 않았습니다.</strong> 1,000명 KOTH 클럽 목록 시간초과는 2W 65.6%, 4W 74.9%, 8W 80.8%로 오히려 증가했습니다.</p>
  </section>

  <section id="cause">
    <h2>원인과 권장 조치</h2>
    <div class="two-col">
      <div><h3>확인된 원인</h3><ul>
        <li>Gunicorn sync 워커가 동시에 처리할 수 있는 요청 수가 적어 대기열이 빠르게 증가합니다.</li>
        <li><code>koth/clubs</code>는 단일 호출도 약 88ms·7 SQL이며 소유자 계산, 정렬, 직렬화 비용이 큽니다.</li>
        <li>리더보드 약 151KB, 내부 팀 목록 약 80KB를 요청마다 생성합니다.</li>
        <li><code>board/me</code>, <code>cell/current</code>, <code>dice/status</code>는 조회 중에도 상태 갱신과 행 잠금을 사용합니다.</li>
        <li>메모리 고갈이나 DB lock wait보다 API CPU와 워커 큐 적체가 먼저 발생했습니다.</li>
      </ul></div>
      <div><h3>실행 우선순위</h3><ol class="actions">
        <li><div><strong>KOTH 캐시와 페이지네이션</strong><br>클럽 목록·상세·리더보드를 짧게 캐시하고 필요한 행만 조회합니다.</div></li>
        <li><div><strong>Board 조회 쿼리 축소</strong><br>반복 SQL을 합치고 읽기 요청의 상태 갱신·행 잠금을 분리합니다.</div></li>
        <li><div><strong>운영 워커 4개 명시</strong><br>연결 제한, API별 rate limit, 타임아웃으로 무한 대기열을 방지합니다.</div></li>
        <li><div><strong>부하발생기와 DB 분리 재시험</strong><br>독립 4 vCPU API 서버의 실제 확장 효과를 검증합니다.</div></li>
        <li><div><strong>동일 시나리오 회귀 테스트</strong><br>100→300→500→1,000명과 순간 1,000명을 다시 비교합니다.</div></li>
      </ol></div>
    </div>
    <div class="callout"><strong>임시 서버 구성안:</strong> 최적화 전 최소 API 4 vCPU·4 GiB, Gunicorn sync 4워커, PostgreSQL 4 vCPU·8 GiB, Redis 1 vCPU·1 GiB를 분리합니다. 1,000명을 목표로 하면 API 인스턴스 2개 이상으로 수평 확장 가능한 구조가 필요합니다. 이 사양은 보장이 아니라 재시험의 시작 조건입니다.</div>
  </section>

  <section>
    <h2>팀원 테스트보다 느려 보인 이유</h2>
    <p>팀원 테스트는 관리자·마이페이지·로그인 중심의 비교적 가벼운 API였고 100명/4워커에서 약 67.9 RPS였습니다. 이번 테스트는 KOTH의 고비용 목록·상세·리더보드를 포함해 p95 3초 기준 처리 한계가 약 30 RPS였습니다. 팀원 결과도 1,000명에서는 평균 약 10.8초였으므로 모든 구간이 3초 이내였던 것은 아닙니다.</p>
  </section>
</main>
<footer class="foot">MSG CTF Backend · Board/KOTH load test · 생성일 2026-09-27 · 테스트용 환경은 종료되었으며 기존 개발 컨테이너는 유지됨</footer>
</body></html>"""

OUTPUT.write_text(doc, encoding="utf-8")
print(OUTPUT)
