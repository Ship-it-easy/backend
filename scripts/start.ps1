param(
    [switch]$Build,
    [switch]$SkipFrontend
)

$ErrorActionPreference = 'Stop'
$backendRoot = Split-Path -Parent $PSScriptRoot
$frontendRoot = Join-Path (Split-Path -Parent $backendRoot) 'frontend'
Set-Location $backendRoot

if (-not (Test-Path '.env.docker')) {
    Copy-Item '.env.docker.example' '.env.docker'
    Write-Host 'Created .env.docker from the example. Review it before sharing this machine.'
}

$feed = Join-Path $backendRoot 'data/gtfs/moscow'
$requiredFeedFiles = @('agency.txt', 'calendar.txt', 'routes.txt', 'stops.txt', 'stop_times.txt', 'trips.txt')
$feedReady = (Test-Path $feed) -and (@($requiredFeedFiles | Where-Object { -not (Test-Path (Join-Path $feed $_)) }).Count -eq 0)
if (-not $feedReady) {
    $archive = Get-ChildItem (Join-Path $backendRoot 'data/gtfs') -Filter '*.zip' -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($archive) {
        uv run python scripts/prepare_gtfs.py $archive.FullName
    } else {
        Write-Warning 'Moscow GTFS feed is missing. Transit routing will use its fallback until you add data/gtfs/moscow.'
    }
}

$composeArgs = @('compose', '--env-file', '.env.docker', 'up', '-d')
if ($Build) { $composeArgs += '--build' }
& docker @composeArgs
if ($LASTEXITCODE -ne 0) { throw 'Docker Compose failed to start.' }

if (-not $SkipFrontend -and (Test-Path (Join-Path $frontendRoot 'package.json'))) {
    if (-not (Test-Path (Join-Path $frontendRoot 'node_modules'))) {
        Push-Location $frontendRoot
        try { npm install } finally { Pop-Location }
        if ($LASTEXITCODE -ne 0) { throw 'npm install failed.' }
    }
    Start-Process 'npm' -ArgumentList 'run', 'dev', '--', '--host', '0.0.0.0' -WorkingDirectory $frontendRoot
}

Write-Host 'Backend: http://localhost:8000/docs'
if (-not $SkipFrontend) { Write-Host 'Frontend: http://localhost:5173' }
Write-Host 'Stop backend services with: docker compose --env-file .env.docker down'
