param(
    [int]$Users = 1000,
    [int]$SpawnRate = 1000,
    [string]$ReadDuration = "60s",
    [string]$WriteDuration = "45s"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$compose = Join-Path $root "docker-compose.loadtest.yml"
$results = Join-Path $PSScriptRoot ("results\\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force -Path $results | Out-Null

$readScenarios = @(
    "board_get", "board_me", "cell_current", "opened_challenges", "chance_catalog", "dice_status",
    "koth_clubs", "koth_club_detail", "koth_me", "koth_leaderboard",
    "koth_verify_token", "koth_internal_teams"
)
$writeScenarios = @(
    "dice_roll", "dice_confirm", "airport_move", "chance_now", "chance_discard",
    "chance_use", "chance_confirm", "cell_open", "roulette_spin", "koth_team_token"
)

function Invoke-Scenario([string]$scenario, [string]$duration, [bool]$prepare) {
    Write-Host "Running $scenario with $Users simultaneous users"
    if ($prepare -and $scenario -ne "koth_team_token") {
        docker compose -f $compose exec -T api python loadtests/prepare_data.py scenario $scenario
        if ($LASTEXITCODE -ne 0) { throw "Fixture preparation failed: $scenario" }
    }

    $stopFile = Join-Path $results "$scenario.stop"
    $monitorFile = Join-Path $results "$scenario-resources.jsonl"
    Remove-Item -LiteralPath $stopFile -ErrorAction SilentlyContinue
    $monitor = Start-Process -FilePath "python" -ArgumentList @(
        (Join-Path $PSScriptRoot "monitor.py"), $monitorFile, $stopFile
    ) -PassThru -WindowStyle Hidden
    try {
        docker compose -f $compose --profile loadgen run --rm `
            -e "LOADTEST_SCENARIO=$scenario" loadgen `
            --headless -u $Users -r $SpawnRate -t $duration `
            --only-summary --csv "/loadtests/results/$(Split-Path $results -Leaf)/$scenario"
        if ($LASTEXITCODE -ne 0) { throw "Locust failed: $scenario" }
    }
    finally {
        New-Item -ItemType File -Force -Path $stopFile | Out-Null
        Wait-Process -Id $monitor.Id -Timeout 20 -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $stopFile -ErrorAction SilentlyContinue
    }
}

foreach ($scenario in $readScenarios) { Invoke-Scenario $scenario $ReadDuration $false }
foreach ($scenario in $writeScenarios) { Invoke-Scenario $scenario $WriteDuration $true }

Write-Host "Results: $results"
