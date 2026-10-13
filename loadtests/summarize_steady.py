import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
directory = sorted(RESULTS.glob("steady-cpu2-workers4-*"))[-1]
STAGES = [100, 300, 500, 1000]

rows, aggregate = [], []
for users in STAGES:
    source = list(
        csv.DictReader((directory / f"mixed-{users}_stats.csv").open(encoding="utf-8"))
    )
    recovery = json.loads(
        (directory / f"mixed-{users}-recovery.json").read_text(encoding="utf-8")
    )
    for item in source:
        row = {
            "users": users,
            "api": item["Name"],
            "requests": int(item["Request Count"]),
            "failures": int(item["Failure Count"]),
            "failure_pct": round(
                int(item["Failure Count"]) / max(int(item["Request Count"]), 1) * 100, 2
            ),
            "avg_ms": round(float(item["Average Response Time"]), 2),
            "p95_ms": round(float(item["95%"]), 2),
            "max_ms": round(float(item["Max Response Time"]), 2),
            "rps": round(float(item["Requests/s"]), 2),
        }
        if item["Name"] == "Aggregated":
            row.update(
                {
                    "recovered": recovery["recovered"],
                    "recovery_seconds": round(recovery["recovery_seconds"], 2),
                }
            )
            aggregate.append(row)
        else:
            rows.append(row)

csv_path = RESULTS / "board-koth-steady-load-20260927.csv"
with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
    writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)

md = [
    "# Board/KOTH 점진적 지속 부하테스트",
    "",
    "2 vCPU·2GiB·Gunicorn sync 4워커에서 사용자를 초당 50명씩 투입하고 각 단계를 3분 유지했습니다. 각 사용자는 요청 사이에 1~2초 간격을 두며, Board/KOTH 조회 비중을 섞었습니다.",
    "",
    "## 결론",
    "",
    "- 100명은 전체 p95 2.9초, 실패 0건으로 3초 기준을 통과했습니다.",
    "- 300명부터 처리량이 약 31 RPS에서 정체되고 p95가 10초로 증가했습니다.",
    "- 500명은 p95 16초, 1,000명은 p95 22초와 시간초과 85.9%였습니다.",
    "- 순간 동시 요청을 제거해도 300명부터 느려지는 이유는 KOTH 고비용 요청이 sync 워커를 오래 점유하기 때문입니다.",
    "- 현재 Board+KOTH 혼합 트래픽의 p95 3초 기준 처리 한계는 약 30 RPS입니다.",
    "",
    "## 단계별 결과",
    "",
    "| 사용자 | 총 요청 | 평균 | p95 | 최악 | 실패율 | RPS | 복구 |",
    "|---:|---:|---:|---:|---:|---:|---:|---:|",
]
for row in aggregate:
    rec = f"{row['recovery_seconds']:.1f}s" if row["recovered"] else "15s+"
    md.append(
        f"| {row['users']} | {row['requests']:,} | {row['avg_ms']/1000:.2f}s | {row['p95_ms']/1000:.1f}s | {row['max_ms']/1000:.1f}s | {row['failure_pct']:.1f}% | {row['rps']:.1f} | {rec} |"
    )
md += [
    "",
    "## 팀원 결과와 다른 이유",
    "",
    "팀원 테스트의 100명 회차는 관리자·마이페이지·로그인 중심의 비교적 가벼운 API였고 4워커 처리량은 67.9 RPS였습니다. 이번 혼합 트래픽에는 KOTH 목록·상세·리더보드가 포함되어 sync 워커가 오래 점유됐고, 3초 기준 처리 한계가 약 30 RPS로 낮아졌습니다.",
]
md_path = RESULTS / "점진적_지속부하_분석_20260927.md"
md_path.write_text("\n".join(md), encoding="utf-8")

payload = json.dumps(
    {"aggregate": aggregate, "rows": rows}, ensure_ascii=False
).replace("</", "<\\/")
html_path = RESULTS / "점진적_지속부하_그래프_20260927.html"
html_path.write_text(
    f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Board/KOTH 지속 부하</title><style>:root{{--bg:#f4f4f1;--card:#fff;--line:#ddd;--text:#111;--sub:#666;--blue:#2a78d6;--orange:#eb6834;--red:#c83d3d;--green:#23845b}}@media(prefers-color-scheme:dark){{:root{{--bg:#111;--card:#1a1a19;--line:#35342f;--text:#fff;--sub:#aaa;--blue:#3987e5;--orange:#e36c36;--red:#ee6666;--green:#4bc28a}}}}*{{box-sizing:border-box}}body{{margin:0;padding:28px 16px;background:var(--bg);color:var(--text);font:15px/1.55 system-ui,"Malgun Gothic"}}main{{max-width:1000px;margin:auto}}h1{{margin:0;font-size:1.6rem}}.sub{{color:var(--sub)}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}}.card,section{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px}}.card b{{display:block;font-size:1.45rem}}small{{color:var(--sub)}}section{{margin:16px 0}}svg{{width:100%;height:auto}}.grid{{stroke:var(--line)}}.axis{{fill:var(--sub);font-size:11px}}.lab{{fill:var(--text);font-size:12px}}table{{border-collapse:collapse;width:100%;font-size:.84rem}}th,td{{padding:7px;border-bottom:1px solid var(--line);text-align:right}}th:first-child,td:first-child{{text-align:left}}.bad{{color:var(--red);font-weight:650}}select{{padding:7px;background:var(--card);color:var(--text);border:1px solid var(--line)}}</style></head><body><main><h1>Board/KOTH 점진적 지속 부하</h1><p class="sub">2 vCPU · sync 4워커 · 초당 50명 투입 · 단계별 3분 · 사용자 요청 간격 1~2초</p><div class="cards">{''.join(f'<div class="card"><small>{x["users"]}명</small><b>{x["p95_ms"]/1000:.1f}s</b><small>p95 · {x["failure_pct"]:.1f}% 실패</small></div>' for x in aggregate)}</div><section><h2>사용자 증가에 따른 응답시간</h2><svg id="chart" viewBox="0 0 900 330"></svg></section><section><h2>API별 결과</h2><select id="stage">{''.join(f'<option {"selected" if u==100 else ""}>{u}</option>' for u in STAGES)}</select><table><thead><tr><th>API</th><th>요청</th><th>평균</th><th>p95</th><th>최악</th><th>실패율</th><th>RPS</th></tr></thead><tbody id="body"></tbody></table></section><section><h2>판정</h2><p><b>100명은 p95 2.9초로 통과했습니다.</b> 300명부터 처리량은 약 31 RPS에서 증가하지 않고 대기시간만 늘었습니다. 순간 동시 요청을 제거해도 KOTH가 sync 워커를 점유하므로, 캐시·쿼리 최적화 없이는 300명 이상에서 3초를 지키기 어렵습니다.</p></section></main><script>const D={payload},fmt=x=>(x/1000).toFixed(1)+'s';function chart(){{let s=document.querySelector('#chart'),r=D.aggregate,W=900,H=330,L=60,R=30,T=20,B=45,pw=W-L-R,ph=H-T-B,max=Math.max(...r.map(x=>x.p95_ms),3000),X=i=>L+i*pw/3,Y=v=>T+ph-v/max*ph;s.innerHTML='';[0,3000,max/2,max].sort((a,b)=>a-b).forEach(v=>s.innerHTML+=`<line class="grid" x1="${{L}}" x2="${{W-R}}" y1="${{Y(v)}}" y2="${{Y(v)}}"/><text class="axis" x="${{L-8}}" y="${{Y(v)+4}}" text-anchor="end">${{fmt(v)}}</text>`);[['avg_ms','var(--blue)'],['p95_ms','var(--orange)']].forEach(([k,c])=>{{let pts=r.map((x,i)=>`${{X(i)}},${{Y(x[k])}}`).join(' ');s.innerHTML+=`<polyline points="${{pts}}" fill="none" stroke="${{c}}" stroke-width="3"/>`}});r.forEach((x,i)=>s.innerHTML+=`<text class="lab" x="${{X(i)}}" y="${{H-16}}" text-anchor="middle">${{x.users}}명</text>`)}}function table(){{let u=+document.querySelector('#stage').value,b=document.querySelector('#body');b.innerHTML='';D.rows.filter(x=>x.users===u).sort((a,b)=>b.avg_ms-a.avg_ms).forEach(x=>{{let tr=document.createElement('tr');tr.innerHTML=`<td>${{x.api}}</td><td>${{x.requests}}</td><td>${{fmt(x.avg_ms)}}</td><td>${{fmt(x.p95_ms)}}</td><td>${{fmt(x.max_ms)}}</td><td class="${{x.failure_pct?'bad':''}}">${{x.failure_pct.toFixed(1)}}%</td><td>${{x.rps.toFixed(1)}}</td>`;b.append(tr)}})}}document.querySelector('#stage').onchange=table;chart();table();</script></body></html>""",
    encoding="utf-8",
)

print(csv_path)
print(md_path)
print(html_path)
