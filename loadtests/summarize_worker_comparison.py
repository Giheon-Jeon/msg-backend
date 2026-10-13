import csv
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
SCENARIOS = [
    "board_get",
    "board_me",
    "koth_clubs",
    "koth_leaderboard",
    "dice_roll",
    "chance_now",
]
STAGES = [100, 300, 500, 1000]
LABELS = {
    "board_get": "GET /api/v1/board",
    "board_me": "GET /api/v1/board/me",
    "koth_clubs": "GET /api/v1/koth/clubs",
    "koth_leaderboard": "GET /api/v1/koth/leaderboard",
    "dice_roll": "POST /api/v1/board/dice/roll",
    "chance_now": "POST /api/v1/board/chance/now",
}


def locust_row(path):
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    return next(row for row in rows if row["Name"] != "Aggregated")


baseline = {
    (row["scenario"], int(row["users"])): row
    for row in csv.DictReader(
        (RESULTS / "board-koth-staged-summary-20260927.csv").open(encoding="utf-8-sig")
    )
}
dirs = {workers: sorted(RESULTS.glob(f"workers{workers}-*"))[-1] for workers in (4, 8)}

rows = []
for scenario in SCENARIOS:
    for users in STAGES:
        source = baseline[(scenario, users)]
        rows.append(
            {
                "scenario": scenario,
                "api": LABELS[scenario],
                "users": users,
                "workers": 2,
                "avg_ms": float(source["avg_ms"]),
                "p95_ms": float(source["p95_ms"]),
                "max_ms": float(source["max_ms"]),
                "failure_pct": float(source["failure_pct"]),
                "rps": float(source["rps"]),
            }
        )
        for workers in (4, 8):
            item = locust_row(dirs[workers] / f"{scenario}-{users}_stats.csv")
            if int(item["Request Count"]) != users:
                raise SystemExit(
                    f"Incomplete result: workers={workers} {scenario}-{users}"
                )
            rows.append(
                {
                    "scenario": scenario,
                    "api": LABELS[scenario],
                    "users": users,
                    "workers": workers,
                    "avg_ms": round(float(item["Average Response Time"]), 2),
                    "p95_ms": round(float(item["95%"]), 2),
                    "max_ms": round(float(item["Max Response Time"]), 2),
                    "failure_pct": round(int(item["Failure Count"]) / users * 100, 2),
                    "rps": round(float(item["Requests/s"]), 2),
                }
            )

for scenario in SCENARIOS:
    for users in STAGES:
        group = [
            row for row in rows if row["scenario"] == scenario and row["users"] == users
        ]
        winner = min(
            group, key=lambda row: (row["failure_pct"], row["p95_ms"], row["avg_ms"])
        )["workers"]
        for row in group:
            row["best"] = row["workers"] == winner

csv_path = RESULTS / "worker-2-4-8-comparison-20260927.csv"
with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
    writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)

wins = {
    workers: sum(row["best"] for row in rows if row["workers"] == workers)
    for workers in (2, 4, 8)
}
at_1000 = [row for row in rows if row["users"] == 1000]
timeouts_1000 = {
    workers: round(
        sum(row["failure_pct"] * 10 for row in at_1000 if row["workers"] == workers)
    )
    for workers in (2, 4, 8)
}

md = [
    "# Gunicorn 워커 2·4·8개 비교",
    "",
    "동일한 API 2 vCPU·2GiB 조건에서 워커 수만 변경하고, 대표 Board/KOTH API 6개에 100·300·500·1,000명 동시 요청을 보냈습니다.",
    "",
    "## 결론",
    "",
    f"- 24개 API·인원 조합 중 4워커가 {wins[4]}개에서 가장 좋았고, 2워커가 {wins[2]}개, 8워커가 {wins[8]}개였습니다.",
    f"- 1,000명 대표 API 6종의 시간초과 합계는 2워커 {timeouts_1000[2]:,}건, 4워커 {timeouts_1000[4]:,}건, 8워커 {timeouts_1000[8]:,}건이었습니다. 4워커는 2워커보다 약 {(1-timeouts_1000[4]/timeouts_1000[2])*100:.1f}% 줄었습니다.",
    "- 4워커는 일반 Board 조회·상태 조회·쓰기에서 가장 안정적이었습니다.",
    "- 8워커는 2 vCPU에서 CPU 경합과 프로세스 전환 비용이 커져 대부분 4워커보다 느렸습니다.",
    "- KOTH 목록과 리더보드는 워커를 늘릴수록 악화됐습니다. 캐시·쿼리·직렬화 개선이 먼저 필요합니다.",
    "- 현재 로컬 2 vCPU 기준 권장은 Gunicorn sync 워커 4개입니다.",
    "",
    "## 1,000명 결과",
    "",
    "| API | 워커 | 평균 | p95 | 시간초과 | RPS |",
    "|---|---:|---:|---:|---:|---:|",
]
for scenario in SCENARIOS:
    for row in [x for x in at_1000 if x["scenario"] == scenario]:
        md.append(
            f"| `{row['api']}` | {row['workers']} | {row['avg_ms']/1000:.2f}s | {row['p95_ms']/1000:.1f}s | {row['failure_pct']:.1f}% | {row['rps']:.1f} |"
        )
md_path = RESULTS / "워커수_2_4_8_비교분석_20260927.md"
md_path.write_text("\n".join(md), encoding="utf-8")

payload = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
cards = "".join(
    f'<div class="card"><small>{w}워커 최적 횟수</small><b>{wins[w]}/24</b></div>'
    for w in (2, 4, 8)
)
html_path = RESULTS / "워커수_2_4_8_비교그래프_20260927.html"
html_path.write_text(
    f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Gunicorn 워커 비교</title>
<style>:root{{--bg:#f4f4f1;--card:#fff;--line:#ddd;--text:#111;--sub:#666;--b:#2a78d6;--o:#eb6834;--r:#c83d3d;--g:#23845b}}@media(prefers-color-scheme:dark){{:root{{--bg:#111;--card:#1a1a19;--line:#35342f;--text:#fff;--sub:#aaa;--b:#3987e5;--o:#e36c36;--r:#ee6666;--g:#4bc28a}}}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 system-ui,"Malgun Gothic";padding:28px 16px}}main{{max-width:1050px;margin:auto}}h1{{font-size:1.6rem;margin:0}}.sub,small{{color:var(--sub)}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:22px 0}}.card,section{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px}}.card b{{display:block;font-size:1.6rem}}section{{margin:16px 0}}select{{padding:7px;background:var(--card);color:var(--text);border:1px solid var(--line)}}table{{border-collapse:collapse;width:100%;font-size:.82rem}}th,td{{padding:7px;border-bottom:1px solid var(--line);text-align:right}}th:first-child,td:first-child{{text-align:left}}.best{{color:var(--g);font-weight:700}}.bad{{color:var(--r)}}svg{{width:100%;height:auto}}.grid{{stroke:var(--line)}}.axis{{fill:var(--sub);font-size:11px}}.lab{{fill:var(--text);font-size:12px}}</style></head><body><main><h1>Gunicorn sync 워커 2·4·8개 비교</h1><p class="sub">API 2 vCPU·2GiB 고정 · 대표 Board/KOTH API 6개 · 100/300/500/1,000명 동시 시작</p><div class="cards">{cards}</div>
<section><h2>1,000명 평균 응답시간</h2><svg id="chart" viewBox="0 0 960 470"></svg></section>
<section><h2>상세 결과</h2><p class="sub">실패율, p95, 평균 순으로 최적 워커를 판정했습니다.</p><select id="stage"><option>100</option><option>300</option><option>500</option><option selected>1000</option></select><table><thead><tr><th>API</th><th>워커</th><th>평균</th><th>p95</th><th>최악</th><th>시간초과</th><th>RPS</th></tr></thead><tbody id="body"></tbody></table></section>
<section><h2>판정</h2><p><b>2 vCPU에서는 4워커가 적정값입니다.</b> 1,000명 대표 API의 시간초과 합계가 2워커 {timeouts_1000[2]:,}건에서 4워커 {timeouts_1000[4]:,}건으로 줄었습니다. 8워커는 {timeouts_1000[8]:,}건으로 악화됐습니다. KOTH 목록·리더보드는 2워커가 더 나아 워커 확장보다 캐시와 쿼리 개선이 필요합니다.</p></section></main><script>const D={payload},colors={{2:'var(--b)',4:'var(--g)',8:'var(--o)'}},fmt=x=>(x/1000).toFixed(2)+'s';function draw(){{const s=document.querySelector('#chart'),r=D.filter(x=>x.users===1000),W=960,H=470,L=235,R=60,T=20,B=30,pw=W-L-R,step=(H-T-B)/r.length,max=Math.max(...r.map(x=>x.avg_ms));s.innerHTML='';[0,.25,.5,.75,1].forEach(q=>{{let x=L+pw*q;s.innerHTML+=`<line class="grid" x1="${{x}}" x2="${{x}}" y1="${{T}}" y2="${{H-B}}"/><text class="axis" x="${{x}}" y="${{H-8}}" text-anchor="middle">${{fmt(max*q)}}</text>`}});r.forEach((x,i)=>{{let y=T+i*step+3,w=pw*x.avg_ms/max;s.innerHTML+=`<text class="lab" x="${{L-8}}" y="${{y+14}}" text-anchor="end">${{x.scenario}} · ${{x.workers}}W</text><rect x="${{L}}" y="${{y}}" width="${{w}}" height="${{Math.max(8,step-6)}}" rx="3" fill="${{colors[x.workers]}}"/><text class="lab" x="${{L+w+7}}" y="${{y+14}}">${{fmt(x.avg_ms)}} · ${{x.failure_pct.toFixed(1)}}%</text>`}})}}function table(){{let u=+document.querySelector('#stage').value,b=document.querySelector('#body');b.innerHTML='';D.filter(x=>x.users===u).forEach(x=>{{let tr=document.createElement('tr');tr.innerHTML=`<td>${{x.api}}</td><td class="${{x.best?'best':''}}">${{x.workers}}${{x.best?' ✓':''}}</td><td>${{fmt(x.avg_ms)}}</td><td>${{fmt(x.p95_ms)}}</td><td>${{fmt(x.max_ms)}}</td><td class="${{x.failure_pct?'bad':''}}">${{x.failure_pct.toFixed(1)}}%</td><td>${{x.rps.toFixed(1)}}</td>`;b.append(tr)}})}}document.querySelector('#stage').onchange=table;draw();table();</script></body></html>""",
    encoding="utf-8",
)

print(csv_path)
print(md_path)
print(html_path)
