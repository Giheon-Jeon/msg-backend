# Local API load tests

This suite targets only the isolated services in `docker-compose.loadtest.yml`.
It never uses the repository's normal PostgreSQL or Redis containers.

1. Start: `docker compose -f docker-compose.loadtest.yml up -d --build db redis api`
2. Seed: `docker compose -f docker-compose.loadtest.yml exec api python loadtests/prepare_data.py seed`
3. Prepare a write scenario when needed: `... python loadtests/prepare_data.py scenario dice_roll`
4. Run Locust headless using `powershell -File loadtests/run_suite.ps1`.
5. Run exact same-team races using `loadtests/race_test.py`.

Generated JWTs, internal tokens, and results are ignored by Git.

The default suite runs 28 scenarios: 18 reads and 10 writes or token requests.
The reads include `healthz`, `timer`, `ranking`, `ranking_me`, `ranking_member`,
and `leaderboard`. These six scenarios also run by default in the burst and
staged runners:

```powershell
powershell -File loadtests/run_burst_suite.ps1
powershell -File loadtests/run_staged_bursts.ps1
```

Each burst scenario runs once with the configured simultaneous users. Each
staged scenario runs at the configured user stages, by default 100, 300, 500,
and 1000 users. To limit either runner to the six added reads, invoke it from
PowerShell with the supported `Scenarios` parameter:

```powershell
& ./loadtests/run_burst_suite.ps1 -Scenarios healthz,timer,ranking,ranking_me,ranking_member,leaderboard
& ./loadtests/run_staged_bursts.ps1 -Scenarios healthz,timer,ranking,ranking_me,ranking_member,leaderboard
```

`run_steady_load.ps1` uses the separate weighted Board/KOTH profile in
`locust_mixed.py`; it does not replace the per-scenario coverage above.
