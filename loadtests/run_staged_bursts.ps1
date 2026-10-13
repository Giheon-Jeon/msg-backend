param(
    [int[]]$Stages = @(100, 300, 500, 1000),
    [int]$RequestTimeoutSeconds = 20,
    [int]$RecoveryLimitSeconds = 15,
    [int]$Workers = 2,
    [double]$AppCpus = 2.0,
    [int]$Threads = 1,
    [ValidateSet('sync', 'gthread')][string]$WorkerClass = 'sync',
    [int]$KeepAliveSeconds = 2,
    [string]$RunTag = "staged",
    [string[]]$Scenarios = @(
        "board_get", "board_me", "cell_current", "opened_challenges", "chance_catalog", "dice_status",
        "koth_clubs", "koth_club_detail", "koth_me", "koth_leaderboard", "koth_team_token",
        "koth_verify_token", "koth_internal_teams",
        "dice_roll", "dice_confirm", "airport_move", "chance_now", "chance_discard",
        "chance_use", "chance_confirm", "cell_open", "roulette_spin"
    )
)

$ErrorActionPreference = "Stop"
$env:LOADTEST_WORKERS = $Workers.ToString()
$env:LOADTEST_THREADS = $Threads.ToString()
$env:LOADTEST_WORKER_CLASS = $WorkerClass
$env:LOADTEST_KEEPALIVE = $KeepAliveSeconds.ToString()
$env:APP_CPUS = $AppCpus.ToString([System.Globalization.CultureInfo]::InvariantCulture)
$root = Split-Path -Parent $PSScriptRoot
$compose = Join-Path $root "docker-compose.loadtest.yml"
$runName = $RunTag + "-" + (Get-Date -Format "yyyyMMdd-HHmmss")
$results = Join-Path $PSScriptRoot "results\$runName"
New-Item -ItemType Directory -Force -Path $results | Out-Null
# Leave enough time for the final 1,000-request result to reach the CSV writer.
# The expected-request listener already exits 3 seconds after the last request.
$duration = ($RequestTimeoutSeconds + 15).ToString() + "s"
$writeScenarios = @(
    "dice_roll", "dice_confirm", "airport_move", "chance_now", "chance_discard",
    "chance_use", "chance_confirm", "cell_open", "roulette_spin"
)

docker compose -f $compose up -d db redis api | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Load-test stack failed to start" }

foreach ($scenario in $Scenarios) {
    docker compose -f $compose exec -T api python loadtests/prepare_data.py tokens
    if ($LASTEXITCODE -ne 0) { throw "Token refresh failed: $scenario" }
    foreach ($users in $Stages) {
        $label = "$scenario-$users"
        Write-Host "STAGE $label"
        docker compose -f $compose restart api | Out-Null
        $deadline = (Get-Date).AddSeconds(90)
        do {
            Start-Sleep -Seconds 2
            $health = docker inspect --format "{{.State.Health.Status}}" msg-loadtest-api-1
        } while ($health -ne "healthy" -and (Get-Date) -lt $deadline)
        if ($health -ne "healthy") { throw "API did not become healthy before $label" }

        if ($writeScenarios -contains $scenario) {
            docker compose -f $compose exec -T api python loadtests/prepare_data.py scenario $scenario
            if ($LASTEXITCODE -ne 0) { throw "Fixture preparation failed: $label" }
        }

        $stopFile = Join-Path $results "$label.stop"
        $resourceFile = Join-Path $results "$label-resources.jsonl"
        Remove-Item -LiteralPath $stopFile -ErrorAction SilentlyContinue
        $monitor = Start-Process -FilePath "python" -ArgumentList @(
            (Join-Path $PSScriptRoot "monitor.py"), $resourceFile, $stopFile
        ) -PassThru -WindowStyle Hidden
        try {
            docker compose -f $compose --profile loadgen run --rm `
                -e "LOADTEST_SCENARIO=$scenario" `
                -e "LOADTEST_ONESHOT=1" `
                -e "LOADTEST_EXPECTED_REQUESTS=$users" `
                -e "LOADTEST_REQUEST_TIMEOUT=$RequestTimeoutSeconds" loadgen `
                --headless -u $users -r $users -t $duration `
                --stop-timeout 3 --only-summary --csv-full-history `
                --csv "/loadtests/results/$runName/$label"
            if ($LASTEXITCODE -notin @(0, 1)) { throw "Locust process failed: $label" }
        }
        finally {
            New-Item -ItemType File -Force -Path $stopFile | Out-Null
            Wait-Process -Id $monitor.Id -Timeout 20 -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $stopFile -ErrorAction SilentlyContinue
        }

        python (Join-Path $PSScriptRoot "recovery_probe.py") `
            "http://127.0.0.1:58080/healthz" $RecoveryLimitSeconds `
            (Join-Path $results "$label-recovery.json")
    }
}

Write-Host "Staged results: $results"
