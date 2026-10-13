param(
    [int]$Users = 1000,
    [int]$SpawnRate = 1000,
    [int]$RequestTimeoutSeconds = 20,
    [string[]]$Scenarios = @(
        "board_get", "board_me", "cell_current", "opened_challenges", "chance_catalog", "dice_status",
        "koth_clubs", "koth_club_detail", "koth_me", "koth_leaderboard", "koth_team_token",
        "koth_verify_token", "koth_internal_teams",
        "dice_roll", "dice_confirm", "airport_move", "chance_now", "chance_discard",
        "chance_use", "chance_confirm", "cell_open", "roulette_spin"
    )
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$compose = Join-Path $root "docker-compose.loadtest.yml"
$runName = Get-Date -Format "yyyyMMdd-HHmmss"
$results = Join-Path $PSScriptRoot "results\$runName"
New-Item -ItemType Directory -Force -Path $results | Out-Null
$duration = ($RequestTimeoutSeconds + 10).ToString() + "s"
$writeScenarios = @(
    "dice_roll", "dice_confirm", "airport_move", "chance_now", "chance_discard",
    "chance_use", "chance_confirm", "cell_open", "roulette_spin"
)

foreach ($scenario in $Scenarios) {
    Write-Host "BURST $scenario : $Users simultaneous one-shot users"
    docker compose -f $compose restart api | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "API restart failed" }
    $deadline = (Get-Date).AddSeconds(90)
    do {
        Start-Sleep -Seconds 2
        $health = docker inspect --format "{{.State.Health.Status}}" msg-loadtest-api-1
    } while ($health -ne "healthy" -and (Get-Date) -lt $deadline)
    if ($health -ne "healthy") { throw "API did not recover before $scenario" }

    if ($writeScenarios -contains $scenario) {
        docker compose -f $compose exec -T api python loadtests/prepare_data.py scenario $scenario
        if ($LASTEXITCODE -ne 0) { throw "Fixture preparation failed: $scenario" }
    }

    docker compose -f $compose --profile loadgen run --rm `
        -e "LOADTEST_SCENARIO=$scenario" `
        -e "LOADTEST_ONESHOT=1" `
        -e "LOADTEST_REQUEST_TIMEOUT=$RequestTimeoutSeconds" loadgen `
        --headless -u $Users -r $SpawnRate -t $duration `
        --stop-timeout 3 --only-summary `
        --csv "/loadtests/results/$runName/$scenario"
    if ($LASTEXITCODE -notin @(0, 1)) { throw "Locust process failed: $scenario" }
}

Write-Host "Results: $results"
