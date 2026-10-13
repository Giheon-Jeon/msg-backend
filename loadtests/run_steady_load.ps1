param(
    [int[]]$Stages = @(100, 300, 500, 1000),
    [int]$DurationSeconds = 180,
    [int]$SpawnRate = 50,
    [int]$Workers = 4,
    [double]$AppCpus = 2.0,
    [int]$Threads = 1,
    [ValidateSet('sync', 'gthread')][string]$WorkerClass = 'sync',
    [int]$KeepAliveSeconds = 2
)

$ErrorActionPreference = "Stop"
$env:LOADTEST_WORKERS = $Workers.ToString()
$env:LOADTEST_THREADS = $Threads.ToString()
$env:LOADTEST_WORKER_CLASS = $WorkerClass
$env:LOADTEST_KEEPALIVE = $KeepAliveSeconds.ToString()
$env:APP_CPUS = $AppCpus.ToString([System.Globalization.CultureInfo]::InvariantCulture)
$root = Split-Path -Parent $PSScriptRoot
$compose = Join-Path $root "docker-compose.loadtest.yml"
$modeSuffix = if ($Threads -eq 1 -and $WorkerClass -eq 'sync') { '' } else { "-$WorkerClass$Threads" }
if ($WorkerClass -ne 'sync' -and $KeepAliveSeconds -ne 2) { $modeSuffix += "-ka$KeepAliveSeconds" }
$runName = "steady-cpu$($AppCpus)-workers$Workers$modeSuffix-" + (Get-Date -Format "yyyyMMdd-HHmmss")
$results = Join-Path $PSScriptRoot "results\$runName"
New-Item -ItemType Directory -Force -Path $results | Out-Null

docker compose -f $compose up -d db redis api | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Load-test stack failed to start" }
docker compose -f $compose exec -T api python loadtests/prepare_data.py tokens
if ($LASTEXITCODE -ne 0) { throw "Token refresh failed" }

foreach ($users in $Stages) {
    $label = "mixed-$users"
    Write-Host "STEADY $label"
    docker compose -f $compose restart api | Out-Null
    $deadline = (Get-Date).AddSeconds(90)
    do {
        Start-Sleep -Seconds 2
        $health = docker inspect --format "{{.State.Health.Status}}" msg-loadtest-api-1
    } while ($health -ne "healthy" -and (Get-Date) -lt $deadline)
    if ($health -ne "healthy") { throw "API did not become healthy before $label" }

    $stopFile = Join-Path $results "$label.stop"
    $resourceFile = Join-Path $results "$label-resources.jsonl"
    Remove-Item -LiteralPath $stopFile -ErrorAction SilentlyContinue
    $monitor = Start-Process -FilePath "python" -ArgumentList @(
        (Join-Path $PSScriptRoot "monitor.py"), $resourceFile, $stopFile
    ) -PassThru -WindowStyle Hidden
    try {
        docker compose -f $compose --profile loadgen run --rm `
            --entrypoint locust `
            -e "LOADTEST_REQUEST_TIMEOUT=20" loadgen `
            -f /loadtests/locust_mixed.py --headless `
            -u $users -r $SpawnRate -t "$($DurationSeconds)s" `
            --stop-timeout 5 --only-summary --csv-full-history `
            --csv "/loadtests/results/$runName/$label"
        if ($LASTEXITCODE -notin @(0, 1)) { throw "Locust process failed: $label" }
    }
    finally {
        New-Item -ItemType File -Force -Path $stopFile | Out-Null
        Wait-Process -Id $monitor.Id -Timeout 20 -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $stopFile -ErrorAction SilentlyContinue
    }
    python (Join-Path $PSScriptRoot "recovery_probe.py") `
        "http://127.0.0.1:58080/healthz" 15 `
        (Join-Path $results "$label-recovery.json")
}

Write-Host "Steady results: $results"
