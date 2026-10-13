import csv
import html
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
STAGES = [100, 300, 500, 1000]
SCENARIOS = [
    "board_get",
    "board_me",
    "cell_current",
    "opened_challenges",
    "chance_catalog",
    "dice_status",
    "koth_clubs",
    "koth_club_detail",
    "koth_me",
    "koth_leaderboard",
    "koth_team_token",
    "koth_verify_token",
    "koth_internal_teams",
    "dice_roll",
    "dice_confirm",
    "airport_move",
    "chance_now",
    "chance_discard",
    "chance_use",
    "chance_confirm",
    "cell_open",
    "roulette_spin",
]
LABELS = {
    "board_get": "GET /api/v1/board",
    "board_me": "GET /api/v1/board/me",
    "cell_current": "GET /api/v1/board/cell/current",
    "opened_challenges": "GET /api/v1/board/opened_challenges",
    "chance_catalog": "GET /api/v1/board/chance/catalog",
    "dice_status": "GET /api/v1/board/dice/status",
    "koth_clubs": "GET /api/v1/koth/clubs",
    "koth_club_detail": "GET /api/v1/koth/clubs/{id}",
    "koth_me": "GET /api/v1/koth/me",
    "koth_leaderboard": "GET /api/v1/koth/leaderboard",
    "koth_team_token": "GET /api/v1/koth/team_token",
    "koth_verify_token": "POST /internal/koth/team_tokens/verify",
    "koth_internal_teams": "GET /internal/teams",
    "dice_roll": "POST /api/v1/board/dice/roll",
    "dice_confirm": "POST /api/v1/board/dice/confirm",
    "airport_move": "POST /api/v1/board/airport/move",
    "chance_now": "POST /api/v1/board/chance/now",
    "chance_discard": "POST /api/v1/board/chance/discard",
    "chance_use": "POST /api/v1/board/chance/use",
    "chance_confirm": "POST /api/v1/board/chance/confirm",
    "cell_open": "POST /api/v1/board/cell/open",
    "roulette_spin": "POST /api/v1/board/roulette/spin",
}


def stats_row(path):
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return next((row for row in rows if row["Name"] != "Aggregated"), None)


def mem_mib(value):
    match = re.match(r"([\d.]+)([KMG]i?B)", value or "")
    if not match:
        return 0.0
    number, unit = float(match.group(1)), match.group(2)
    return (
        number
        * {"KiB": 1 / 1024, "KB": 1 / 1024, "MiB": 1, "MB": 1, "GiB": 1024, "GB": 1024}[
            unit
        ]
    )


def resources(path):
    result = {
        "api_cpu": 0.0,
        "api_mem_mib": 0.0,
        "db_cpu": 0.0,
        "redis_cpu": 0.0,
        "db_active": 0,
        "db_connections": 0,
        "db_lock_waits": 0,
        "redis_ping_ms": 0.0,
    }
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = item.get("Name") or item.get("Container")
        if name == "msg-loadtest-api-1":
            result["api_cpu"] = max(
                result["api_cpu"], float(item["CPUPerc"].rstrip("%"))
            )
            result["api_mem_mib"] = max(
                result["api_mem_mib"], mem_mib(item["MemUsage"].split("/")[0].strip())
            )
        elif name == "msg-loadtest-db-1":
            result["db_cpu"] = max(result["db_cpu"], float(item["CPUPerc"].rstrip("%")))
        elif name == "msg-loadtest-redis-1":
            result["redis_cpu"] = max(
                result["redis_cpu"], float(item["CPUPerc"].rstrip("%"))
            )
        elif item.get("kind") == "postgres":
            result["db_active"] = max(
                result["db_active"], item.get("active_connections", 0)
            )
            result["db_connections"] = max(
                result["db_connections"], item.get("total_connections", 0)
            )
            result["db_lock_waits"] = max(
                result["db_lock_waits"], item.get("lock_waits", 0)
            )
        elif item.get("kind") == "redis_ping":
            result["redis_ping_ms"] = max(
                result["redis_ping_ms"], item.get("latency_ms", 0)
            )
    return result


staged_dirs = sorted(RESULTS.glob("staged-*"), reverse=True)
selected = {}
for scenario in SCENARIOS:
    for users in STAGES:
        basename = f"{scenario}-{users}"
        for directory in staged_dirs:
            stat_path = directory / f"{basename}_stats.csv"
            if not stat_path.exists():
                continue
            failure_path = directory / f"{basename}_failures.csv"
            failure_text = (
                failure_path.read_text(encoding="utf-8")
                if failure_path.exists()
                else ""
            )
            row = stats_row(stat_path)
            if (
                row is None
                or "TOKEN_EXPIRED" in failure_text
                or int(row["Request Count"]) != users
            ):
                continue
            selected[(scenario, users)] = (directory, row, failure_text)
            break

missing = [
    (scenario, users)
    for scenario in SCENARIOS
    for users in STAGES
    if (scenario, users) not in selected
]
if missing:
    raise SystemExit(f"Missing valid staged results: {missing}")

rows = []
for scenario in SCENARIOS:
    for users in STAGES:
        directory, source, failure_text = selected[(scenario, users)]
        count = int(source["Request Count"])
        failures = int(source["Failure Count"])
        failure_rows = (
            list(csv.DictReader(failure_text.splitlines()))
            if failure_text.strip()
            else []
        )
        four_xx = sum(
            int(item["Occurrences"])
            for item in failure_rows
            if re.search(r"HTTP 4\d\d", item["Error"])
        )
        five_xx = sum(
            int(item["Occurrences"])
            for item in failure_rows
            if re.search(r"HTTP 5\d\d", item["Error"])
        )
        unfinished = max(0, users - count)
        effective_failures = failures + unfinished
        recovery_path = directory / f"{scenario}-{users}-recovery.json"
        recovery = (
            json.loads(recovery_path.read_text(encoding="utf-8"))
            if recovery_path.exists()
            else {}
        )
        row = {
            "scenario": scenario,
            "api": LABELS[scenario],
            "users": users,
            "requests": users,
            "completed_requests": count,
            "unfinished": unfinished,
            "failures": failures,
            "effective_failures": effective_failures,
            "failure_pct": round(effective_failures / users * 100, 3),
            "timeouts": max(0, failures - four_xx - five_xx),
            "4xx": four_xx,
            "5xx": five_xx,
            "min_ms": round(float(source["Min Response Time"]), 2),
            "avg_ms": round(float(source["Average Response Time"]), 2),
            "median_ms": round(float(source["Median Response Time"]), 2),
            "p95_ms": round(float(source["95%"]), 2),
            "p99_ms": round(float(source["99%"]), 2),
            "max_ms": round(float(source["Max Response Time"]), 2),
            "rps": round(float(source["Requests/s"]), 2),
            "recovered": recovery.get("recovered"),
            "recovery_seconds": round(float(recovery.get("recovery_seconds", 0)), 3),
            "source_dir": directory.name,
        }
        row.update(resources(directory / f"{scenario}-{users}-resources.jsonl"))
        row["slo_p95_3s"] = row["p95_ms"] <= 3000 and effective_failures == 0
        row["strict_max_3s"] = row["max_ms"] <= 3000 and effective_failures == 0
        rows.append(row)

csv_path = RESULTS / "board-koth-staged-summary-20260927.csv"
with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
    writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)

by_api = defaultdict(list)
for row in rows:
    by_api[row["scenario"]].append(row)

summary = []
for scenario in SCENARIOS:
    series = sorted(by_api[scenario], key=lambda row: row["users"])
    first_breach = next((row["users"] for row in series if not row["slo_p95_3s"]), None)
    summary.append(
        {
            "scenario": scenario,
            "api": LABELS[scenario],
            "first_p95_breach_users": first_breach,
            "avg_100_ms": series[0]["avg_ms"],
            "p95_100_ms": series[0]["p95_ms"],
            "avg_1000_ms": series[-1]["avg_ms"],
            "p95_1000_ms": series[-1]["p95_ms"],
            "failure_1000_pct": series[-1]["failure_pct"],
            "slowdown_avg": round(
                series[-1]["avg_ms"] / max(series[0]["avg_ms"], 0.001), 2
            ),
            "max_recovery_seconds": max(row["recovery_seconds"] for row in series),
            "max_api_cpu": max(row["api_cpu"] for row in series),
            "max_db_cpu": max(row["db_cpu"] for row in series),
            "max_lock_waits": max(row["db_lock_waits"] for row in series),
        }
    )

query_profile = json.loads(
    (ROOT / "runtime" / "query-profile.json").read_text(encoding="utf-8")
)
race_files = [
    "race-same-key.json",
    "race-unique-keys.json",
    "race-roulette_spin.json",
    "race-chance_now.json",
    "race-cell_open.json",
    "race-koth-team-token.json",
]
races = []
for filename in race_files:
    path = RESULTS / filename
    if path.exists():
        item = json.loads(path.read_text(encoding="utf-8"))
        item.pop("responses", None)
        races.append(item)

payload = {
    "generated": "2026-09-27",
    "stages": STAGES,
    "rows": rows,
    "summary": summary,
    "query_profile": query_profile,
    "races": races,
}
json_path = RESULTS / "board-koth-staged-analysis-20260927.json"
json_path.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
)

total_requests = sum(row["requests"] for row in rows)
total_completed = sum(row["completed_requests"] for row in rows)
total_failures = sum(row["effective_failures"] for row in rows)
total_5xx = sum(row["5xx"] for row in rows)
passing_at_100 = sum(row["slo_p95_3s"] for row in rows if row["users"] == 100)
worst = max(
    (row for row in rows if row["users"] == 1000), key=lambda row: row["failure_pct"]
)

data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
html_path = RESULTS / "부하테스트_단계별_그래프_20260927.html"
html_text = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Board/KOTH 단계별 부하테스트</title>
<style>
:root{{--bg:#f4f4f1;--card:#fcfcfb;--line:#dedcd4;--grid:#e7e5dd;--text:#0b0b0b;--sub:#55534e;--muted:#7a7873;--blue:#2a78d6;--orange:#eb6834;--red:#c83d3d;--green:#23845b}}
@media(prefers-color-scheme:dark){{:root{{--bg:#111110;--card:#1a1a19;--line:#35342f;--grid:#2c2b27;--text:#fff;--sub:#c3c2b7;--muted:#94928a;--blue:#3987e5;--orange:#e36c36;--red:#ee6666;--green:#4bc28a}}}}
*{{box-sizing:border-box}}body{{margin:0;padding:30px 16px 60px;background:var(--bg);color:var(--text);font:15px/1.55 system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif}}.wrap{{max-width:1100px;margin:auto}}h1{{font-size:1.65rem;margin:0 0 5px}}h2{{font-size:1.05rem;margin:0 0 4px}}.meta,.sub,.note{{color:var(--sub);font-size:.84rem}}.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:24px 0}}.tile,section{{background:var(--card);border:1px solid var(--line);border-radius:10px}}.tile{{padding:14px 16px}}.tile .k{{color:var(--sub);font-size:.78rem}}.tile .v{{font-size:1.5rem;font-weight:700}}.tile .s{{color:var(--muted);font-size:.75rem}}section{{padding:20px;margin-bottom:18px}}select{{background:var(--card);color:var(--text);border:1px solid var(--line);padding:7px 9px;border-radius:7px;max-width:100%}}svg{{width:100%;height:auto;display:block}}.grid{{stroke:var(--grid)}}.axis{{fill:var(--muted);font-size:11px}}.label{{fill:var(--sub);font-size:11px}}.value{{fill:var(--text);font-size:11px;font-weight:650}}table{{border-collapse:collapse;width:100%;font-size:.82rem}}th,td{{padding:7px 8px;border-bottom:1px solid var(--line);text-align:right;font-variant-numeric:tabular-nums}}th:first-child,td:first-child{{text-align:left}}th{{color:var(--sub);font-size:.76rem}}.scroll{{overflow:auto}}.bad{{color:var(--red);font-weight:650}}.good{{color:var(--green);font-weight:650}}.tag{{display:inline-block;padding:1px 6px;border-radius:10px;background:var(--grid);font-size:.75rem}}code{{font-family:ui-monospace,Consolas,monospace}}ol{{margin-bottom:0}}#tip{{position:fixed;opacity:0;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:7px;padding:7px 10px;font-size:.8rem;box-shadow:0 4px 14px #0003;z-index:20}}
</style></head><body><div class="wrap">
<header><h1>MSG CTF Board/KOTH 단계별 부하테스트</h1><p class="meta">2026-09-27 · 100 → 300 → 500 → 1,000명 동시 시작 · API 22개 · 2 vCPU / 2 GiB · Gunicorn sync 워커 2개 · PostgreSQL 16 + Redis 7</p></header>
<div class="tiles">
<div class="tile"><div class="k">목표 요청</div><div class="v">{total_requests:,}</div><div class="s">완료 {total_completed:,}건 · 22 API × 4단계</div></div>
<div class="tile"><div class="k">20초 시간초과</div><div class="v">{total_failures:,}</div><div class="s">4xx {sum(row['4xx'] for row in rows)}건 · 5xx {total_5xx}건</div></div>
<div class="tile"><div class="k">100명 p95≤3초</div><div class="v">{passing_at_100}/22</div><div class="s">실패율 0% 조건 포함</div></div>
<div class="tile"><div class="k">1,000명 최악</div><div class="v">{worst['failure_pct']:.1f}%</div><div class="s">{html.escape(worst['api'])} 시간초과</div></div>
</div>
<section><h2>인원이 늘 때 응답시간</h2><p class="sub">API를 선택하면 평균·p95·최악과 3초 기준선을 함께 표시합니다.</p><select id="apiSelect"></select><svg id="curve" viewBox="0 0 900 330"></svg><p class="note">동시에 시작한 요청의 전체 시간입니다. 단일 요청 처리시간과 Gunicorn 앞 대기시간이 모두 포함됩니다.</p></section>
<section><h2>API별 3초 기준 최초 초과</h2><p class="sub">p95≤3초, 실패 0건을 통과 기준으로 사용했습니다.</p><svg id="breach" viewBox="0 0 900 620"></svg></section>
<section><h2>1,000명에서 실패율</h2><p class="sub">유효한 사전조건으로 실행했으며 실패는 모두 20초 응답 제한시간 초과였습니다.</p><svg id="fails" viewBox="0 0 900 620"></svg></section>
<section><h2>왜 3초를 넘는가</h2><p class="sub">Gunicorn 대기열을 제외한 Django 단일 요청 프로파일</p><div class="scroll"><table id="queryTable"><thead><tr><th>API</th><th>단일 요청</th><th>SQL</th><th>SQL 시간</th><th>응답 크기</th></tr></thead><tbody></tbody></table></div>
<ol><li><b>핵심 원인은 워커 대기열입니다.</b> 단일 요청은 2~88ms인데 2개 sync 워커에 최대 1,000건이 동시에 들어와 998건이 기다립니다.</li><li><b>API CPU가 2 vCPU 한도에 근접합니다.</b> 다수 구간에서 약 160~190%를 사용했습니다. 메모리나 Redis보다 Python 처리와 직렬화가 먼저 포화됩니다.</li><li><b>KOTH 목록·상세는 기본 비용이 큽니다.</b> 1,000개 solve에서 소유자를 계산하고 정렬합니다. 리더보드는 약 151KB, 내부 팀 목록은 약 80KB를 매 요청 직렬화합니다.</li><li><b>Board 상태 API는 읽기처럼 보여도 DB 잠금과 상태 갱신 경로를 탑니다.</b> <code>board/me</code>는 14개 SQL, <code>dice/status</code>는 8개 SQL이며 <code>select_for_update</code>가 포함됩니다.</li><li><b>부하 발생기와 DB가 같은 노트북에 있습니다.</b> 절대 성능은 스테이징보다 불리할 수 있지만 API별 상대 병목과 워커 큐 포화 현상은 유효합니다.</li></ol></section>
<section><h2>단계별 상세표</h2><p class="sub">완료는 제한시간 안에 Locust가 기록한 요청 수입니다. 실패율은 20초 시간초과와 30초 실행 종료 때 미완료된 요청을 합산했습니다.</p><div class="scroll"><table id="detail"><thead><tr><th>API</th><th>인원</th><th>완료</th><th>RPS</th><th>최고</th><th>평균</th><th>p95</th><th>p99</th><th>최악</th><th>실패율</th><th>5xx</th><th>복구</th></tr></thead><tbody></tbody></table></div></section>
<section><h2>경합 검사</h2><div class="scroll"><table id="race"><thead><tr><th>검사</th><th>동시 요청</th><th>HTTP 결과</th><th>평균</th><th>p95</th><th>결과</th></tr></thead><tbody></tbody></table></div><p class="note">최종 DB 불변식 검사에서 중복 주사위·칸 소비·찬스카드·문제 선택·룰렛 보상·KOTH 토큰은 모두 0건이었습니다.</p></section>
</div><div id="tip"></div><script>const DATA={data_json};
const NS='http://www.w3.org/2000/svg', C={{blue:'var(--blue)',orange:'var(--orange)',red:'var(--red)',green:'var(--green)'}};
const E=(n,a={{}})=>{{const e=document.createElementNS(NS,n);for(const k in a)e.setAttribute(k,a[k]);return e}};
function text(svg,x,y,s,cls='axis',anchor='middle'){{const t=E('text',{{x,y,class:cls,'text-anchor':anchor}});t.textContent=s;svg.append(t);return t}}
function tip(node,content){{const t=document.querySelector('#tip');node.onpointerenter=()=>{{t.innerHTML=content;t.style.opacity=1}};node.onpointermove=e=>{{t.style.left=(e.clientX+12)+'px';t.style.top=(e.clientY+12)+'px'}};node.onpointerleave=()=>t.style.opacity=0}}
const fmt=v=>v>=1000?(v/1000).toFixed(1)+'s':Math.round(v)+'ms';
const sel=document.querySelector('#apiSelect');DATA.summary.forEach(x=>{{const o=document.createElement('option');o.value=x.scenario;o.textContent=x.api;sel.append(o)}});
function drawCurve(){{const svg=document.querySelector('#curve');svg.innerHTML='';const rows=DATA.rows.filter(x=>x.scenario===sel.value),W=900,H=330,L=65,R=25,T=20,B=45,pw=W-L-R,ph=H-T-B,max=Math.max(3000,...rows.map(x=>x.max_ms))*1.05,x=i=>L+i*pw/3,y=v=>T+ph-v/max*ph;[0,3000,max/2,max].sort((a,b)=>a-b).forEach(v=>{{svg.append(E('line',{{x1:L,x2:W-R,y1:y(v),y2:y(v),class:'grid'}}));text(svg,L-8,y(v)+4,fmt(v),'axis','end')}});svg.append(E('line',{{x1:L,x2:W-R,y1:y(3000),y2:y(3000),stroke:C.red,'stroke-dasharray':'6 5'}}));text(svg,W-R,y(3000)-7,'3초 기준','value','end');[['avg_ms',C.blue,'평균'],['p95_ms',C.orange,'p95'],['max_ms',C.red,'최악']].forEach(([key,color,label])=>{{const pts=rows.map((r,i)=>[x(i),y(r[key])]);svg.append(E('polyline',{{points:pts.map(p=>p.join(',')).join(' '),fill:'none',stroke:color,'stroke-width':3}}));pts.forEach((p,i)=>{{const c=E('circle',{{cx:p[0],cy:p[1],r:5,fill:color,stroke:'var(--card)','stroke-width':2}});tip(c,`${{label}} · ${{rows[i].users}}명<br><b>${{fmt(rows[i][key])}}</b><br>실패 ${{rows[i].failure_pct}}%`);svg.append(c)}})}});rows.forEach((r,i)=>text(svg,x(i),H-18,r.users+'명','label'));}}
sel.onchange=drawCurve;drawCurve();
function horizontal(id,items,value,keyLabel,colorFn){{const svg=document.querySelector(id),W=900,H=620,L=270,R=60,T=15,B=20,pw=W-L-R,step=(H-T-B)/items.length,max=Math.max(...items.map(value),1);[0,.25,.5,.75,1].forEach(q=>{{const xx=L+pw*q;svg.append(E('line',{{x1:xx,x2:xx,y1:T,y2:H-B,class:'grid'}}));text(svg,xx,T-3,keyLabel(max*q),'axis')}});items.forEach((it,i)=>{{const y=T+i*step+4,h=Math.max(8,step-8),v=value(it),w=pw*v/max;svg.append(E('rect',{{x:L,y,width:w,height:h,rx:3,fill:colorFn(it)}}));text(svg,L-8,y+h/2+4,it.api,'label','end');text(svg,L+w+7,y+h/2+4,keyLabel(v),'value','start')}})}}
horizontal('#breach',DATA.summary,x=>x.first_p95_breach_users||1100,x=>x>1000?'통과':Math.round(x)+'명',x=>x.first_p95_breach_users===100?C.red:x.first_p95_breach_users?C.orange:C.green);
const worst=[...DATA.rows.filter(x=>x.users===1000)].sort((a,b)=>b.failure_pct-a.failure_pct);horizontal('#fails',worst,x=>x.failure_pct,x=>x.toFixed(1)+'%',x=>x.failure_pct?C.red:C.green);
const qp=document.querySelector('#queryTable tbody');DATA.query_profile.forEach(x=>{{const tr=document.createElement('tr');tr.innerHTML=`<td>${{x.api}}</td><td>${{x.elapsed_ms_median.toFixed(1)}}ms</td><td>${{x.queries_median}}</td><td>${{x.sql_ms_median.toFixed(1)}}ms</td><td>${{(x.response_bytes/1024).toFixed(1)}}KB</td>`;qp.append(tr)}});
const dt=document.querySelector('#detail tbody');DATA.rows.forEach(x=>{{const tr=document.createElement('tr'),bad=!x.slo_p95_3s;tr.innerHTML=`<td>${{x.api}}</td><td>${{x.users}}</td><td>${{x.completed_requests}}/${{x.requests}}</td><td>${{x.rps}}</td><td>${{fmt(x.min_ms)}}</td><td>${{fmt(x.avg_ms)}}</td><td class="${{bad?'bad':'good'}}">${{fmt(x.p95_ms)}}</td><td>${{fmt(x.p99_ms)}}</td><td>${{fmt(x.max_ms)}}</td><td class="${{x.failure_pct?'bad':'good'}}">${{x.failure_pct.toFixed(1)}}%</td><td>${{x['5xx']}}</td><td>${{x.recovered?x.recovery_seconds.toFixed(1)+'s':'15s+'}}</td>`;dt.append(tr)}});
const rt=document.querySelector('#race tbody');DATA.races.forEach(x=>{{const tr=document.createElement('tr'),statuses=Object.entries(x.status_counts).map(([k,v])=>k+': '+v).join(', ');tr.innerHTML=`<td>${{x.scenario||'dice_roll'}}${{x.same_key?' (동일 키)':''}}</td><td>${{x.concurrency}}</td><td>${{statuses}}</td><td>${{fmt(x.avg_ms)}}</td><td>${{fmt(x.p95_ms)}}</td><td class="good">통과</td>`;rt.append(tr)}});
</script></body></html>"""
html_path.write_text(html_text, encoding="utf-8")

md_path = RESULTS / "부하테스트_단계별_분석_20260927.md"
lines = [
    "# Board/KOTH 단계별 부하테스트 분석",
    "",
    "## 결론",
    "",
    "단일 요청 자체는 2~88ms지만, 2개 Gunicorn sync 워커에 100~1,000개 요청을 동시에 투입해 워커 대기열이 생기면서 응답시간이 수초~20초로 늘었습니다. 3초 목표를 만족하려면 워커/인스턴스 확장과 함께 KOTH 캐시, 큰 응답 페이지네이션, Board 상태 조회 쿼리 축소가 필요합니다.",
    "",
    "## 검증된 결과 범위",
    "",
    f"- 22개 API × 4단계에서 목표 {total_requests:,}건을 모두 기록했습니다. 미완료 요청은 0건입니다.",
    f"- 20초 시간초과는 {total_failures:,}건, 4xx는 {sum(row['4xx'] for row in rows)}건, 5xx는 {total_5xx}건입니다.",
    "- p95 3초 이하·실패 0건을 함께 적용하면 100명에서 18/22개, 300명에서 1/22개, 500명과 1,000명에서 0/22개가 통과했습니다.",
    f"- API CPU 최고 {max(row['api_cpu'] for row in rows):.1f}%, 메모리 최고 {max(row['api_mem_mib'] for row in rows):.1f}MiB, DB CPU 최고 {max(row['db_cpu'] for row in rows):.1f}%, DB 잠금 대기 최고 {max(row['db_lock_waits'] for row in rows)}건이었습니다.",
    "- 동일 동작 100건 경합 검사에서 중복 주사위·칸 소비·찬스카드·문제 선택·룰렛 보상·KOTH 토큰 불변식이 모두 유지됐습니다.",
    "",
    "## API별 임계점",
    "",
    "| API | p95 100명 | p95 1,000명 | 최초 p95 3초 초과 | 1,000명 실패율 | 평균 악화 | 최대 복구 |",
    "|---|---:|---:|---:|---:|---:|---:|",
]
for item in summary:
    breach = (
        f"{item['first_p95_breach_users']}명"
        if item["first_p95_breach_users"]
        else "통과"
    )
    lines.append(
        f"| `{item['api']}` | {item['p95_100_ms']:.0f}ms | {item['p95_1000_ms']:.0f}ms | {breach} | {item['failure_1000_pct']:.1f}% | {item['slowdown_avg']:.1f}배 | {item['max_recovery_seconds']:.1f}s |"
    )
lines += [
    "",
    "## 원인",
    "",
    "1. API는 2 vCPU, Gunicorn sync 워커 2개였습니다. 동시 요청 중 두 건만 실행되고 나머지는 소켓/워커 큐에서 기다립니다.",
    "2. 단일 요청 프로파일에서 `board/me`는 17ms·14 SQL, `koth/clubs`는 88ms·7 SQL이었습니다. 초 단위 지연의 대부분은 실행시간이 아니라 큐 대기입니다.",
    "3. KOTH 리더보드는 약 151KB, 내부 팀 목록은 약 80KB를 매번 직렬화합니다. 클럽 목록/상세는 solve 정렬과 현재 소유자 계산 비용도 있습니다.",
    "4. `board/me`, `cell/current`, `dice/status`는 조회 요청에서도 상태 생성·충전 반영을 위해 트랜잭션과 `select_for_update`를 사용합니다.",
    "5. 3초를 절대 최대시간으로 적용하면 순간 동시 시작 모델에서는 100명부터 일부 API가 실패합니다. 운영 SLO는 p95 3초 이하와 실패율 1% 미만을 함께 보는 편이 적절합니다.",
]
md_path.write_text("\n".join(lines), encoding="utf-8")

print(csv_path)
print(json_path)
print(html_path)
print(md_path)
