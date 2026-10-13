import csv
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
CONFIGS = [
    ("2 vCPU / 4W", "workers4-*", 2, 4),
    ("4 vCPU / 4W", "cpu4-workers4-*", 4, 4),
    ("4 vCPU / 8W", "cpu4-workers8-*", 4, 8),
]


def one(path):
    return next(
        row
        for row in csv.DictReader(path.open(encoding="utf-8"))
        if row["Name"] != "Aggregated"
    )


rows = []
for config, pattern, cpus, workers in CONFIGS:
    directories = sorted(RESULTS.glob(pattern), reverse=True)
    for scenario in SCENARIOS:
        for users in STAGES:
            item = None
            for directory in directories:
                path = directory / f"{scenario}-{users}_stats.csv"
                if not path.exists():
                    continue
                candidate = one(path)
                if int(candidate["Request Count"]) == users:
                    item = candidate
                    break
            if item is None:
                raise SystemExit(
                    f"Missing complete result: {config} {scenario}-{users}"
                )
            rows.append(
                {
                    "config": config,
                    "cpus": cpus,
                    "workers": workers,
                    "scenario": scenario,
                    "users": users,
                    "avg_ms": round(float(item["Average Response Time"]), 2),
                    "p95_ms": round(float(item["95%"]), 2),
                    "max_ms": round(float(item["Max Response Time"]), 2),
                    "failures": int(item["Failure Count"]),
                    "failure_pct": round(int(item["Failure Count"]) / users * 100, 2),
                    "rps": round(float(item["Requests/s"]), 2),
                }
            )

csv_path = RESULTS / "cpu-2v4v-worker-comparison-20260927.csv"
with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
    writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)

timeouts = {
    name: sum(
        row["failures"]
        for row in rows
        if row["config"] == name and row["users"] == 1000
    )
    for name, *_ in CONFIGS
}
md = [
    "# 2 vCPU와 4 vCPU 로컬 비교",
    "",
    "대표 Board/KOTH API 6개에 100·300·500·1,000명 동시 요청을 보내 비교했습니다. API·PostgreSQL·Redis·Locust가 모두 같은 8코어 노트북에서 실행됐습니다.",
    "",
    "## 결론",
    "",
    f"- 1,000명 시간초과 합계: 2 vCPU/4W {timeouts['2 vCPU / 4W']:,}건, 4 vCPU/4W {timeouts['4 vCPU / 4W']:,}건, 4 vCPU/8W {timeouts['4 vCPU / 8W']:,}건.",
    "- 로컬 통합 환경에서는 2 vCPU/4워커가 가장 좋았습니다.",
    "- 4 vCPU 제한은 실제 CPU를 추가하지 않고 API가 같은 호스트 CPU를 더 차지하도록 허용합니다. DB와 Locust의 CPU 여유가 줄어 전체 결과가 악화됐습니다.",
    "- 따라서 이 결과로 독립된 4 vCPU 운영 서버가 더 느리다고 결론 내릴 수 없습니다.",
    "- 실제 4 vCPU 증설 효과는 부하 발생기를 다른 PC에서 실행하고, 가능하면 DB도 분리한 뒤 다시 측정해야 합니다.",
    "",
    "## 1,000명 결과",
    "",
    "| API | 구성 | 평균 | p95 | 시간초과 | RPS |",
    "|---|---|---:|---:|---:|---:|",
]
for scenario in SCENARIOS:
    for row in [x for x in rows if x["scenario"] == scenario and x["users"] == 1000]:
        md.append(
            f"| `{scenario}` | {row['config']} | {row['avg_ms']/1000:.2f}s | {row['p95_ms']/1000:.1f}s | {row['failure_pct']:.1f}% | {row['rps']:.1f} |"
        )
md_path = RESULTS / "CPU_2v_4v_비교분석_20260927.md"
md_path.write_text("\n".join(md), encoding="utf-8")

payload = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
html_path = RESULTS / "CPU_2v_4v_비교그래프_20260927.html"
html_path.write_text(
    f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>2 vCPU와 4 vCPU 비교</title><style>:root{{--bg:#f4f4f1;--card:#fff;--line:#ddd;--text:#111;--sub:#666;--good:#23845b;--bad:#c83d3d}}@media(prefers-color-scheme:dark){{:root{{--bg:#111;--card:#1a1a19;--line:#35342f;--text:#fff;--sub:#aaa;--good:#4bc28a;--bad:#ee6666}}}}*{{box-sizing:border-box}}body{{margin:0;padding:28px 16px;background:var(--bg);color:var(--text);font:15px/1.55 system-ui,"Malgun Gothic"}}main{{max-width:1050px;margin:auto}}h1{{margin:0;font-size:1.6rem}}.sub{{color:var(--sub)}}section,.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px;margin:16px 0}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}.card{{margin:0}}.card b{{display:block;font-size:1.5rem}}small{{color:var(--sub)}}table{{border-collapse:collapse;width:100%;font-size:.84rem}}th,td{{padding:7px;border-bottom:1px solid var(--line);text-align:right}}th:first-child,td:first-child{{text-align:left}}.bad{{color:var(--bad);font-weight:650}}select{{padding:7px;background:var(--card);color:var(--text);border:1px solid var(--line)}}</style></head><body><main><h1>2 vCPU와 4 vCPU 로컬 비교</h1><p class="sub">대표 API 6개 · 모든 서비스와 부하 발생기가 같은 8코어 노트북에서 실행</p><div class="cards">{''.join(f'<div class="card"><small>{name} · 1,000명</small><b>{timeouts[name]:,}건</b><small>20초 시간초과</small></div>' for name,*_ in CONFIGS)}</div><section><h2>해석</h2><p><b>로컬에서는 2 vCPU/4워커가 가장 좋았습니다.</b> 4 vCPU는 CPU를 추가한 것이 아니라 API가 같은 호스트 CPU를 더 사용하게 한 설정이라 DB와 Locust가 느려졌습니다. 독립된 4 vCPU 서버의 성능을 판단하려면 부하 발생기를 다른 컴퓨터로 분리해야 합니다.</p></section><section><h2>상세 결과</h2><select id="stage"><option>100</option><option>300</option><option>500</option><option selected>1000</option></select><table><thead><tr><th>API</th><th>구성</th><th>평균</th><th>p95</th><th>시간초과</th><th>RPS</th></tr></thead><tbody id="body"></tbody></table></section></main><script>const D={payload},fmt=x=>(x/1000).toFixed(2)+'s';function draw(){{let u=+document.querySelector('#stage').value,b=document.querySelector('#body');b.innerHTML='';D.filter(x=>x.users===u).forEach(x=>{{let tr=document.createElement('tr');tr.innerHTML=`<td>${{x.scenario}}</td><td>${{x.config}}</td><td>${{fmt(x.avg_ms)}}</td><td>${{fmt(x.p95_ms)}}</td><td class="${{x.failure_pct?'bad':''}}">${{x.failure_pct.toFixed(1)}}%</td><td>${{x.rps.toFixed(1)}}</td>`;b.append(tr)}})}}document.querySelector('#stage').onchange=draw;draw();</script></body></html>""",
    encoding="utf-8",
)

print(csv_path)
print(md_path)
print(html_path)
