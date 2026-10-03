# legacy-db-modernizer: one-command startup
# Starts Oracle and Postgres containers, then waits until Oracle genuinely
# accepts a real connection (not just checking log text, which breaks for
# an already-running container whose "ready" message scrolled out of the
# recent log tail hours ago).

Write-Host "Starting Oracle and Postgres containers..." -ForegroundColor Cyan
docker start oracle-xe pg-target

Write-Host "Waiting for Oracle to accept connections (this can take a minute on a fresh start)..." -ForegroundColor Cyan

$ready = $false
$maxAttempts = 30
$attempt = 0

while (-not $ready -and $attempt -lt $maxAttempts) {
    $attempt++
    Start-Sleep -Seconds 5
    $result = python -c "import oracledb; from config import ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN; oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN).close(); print('OK')" 2>&1
    if ($result -match "OK") {
        $ready = $true
    } else {
        Write-Host "  ...still starting (attempt $attempt/$maxAttempts)" -ForegroundColor DarkGray
    }
}

if ($ready) {
    Write-Host "Oracle is ready!" -ForegroundColor Green
} else {
    Write-Host "Oracle did not become reachable within the expected time - check 'docker logs oracle-xe' manually." -ForegroundColor Yellow
}

Write-Host "`nContainer status:" -ForegroundColor Cyan
docker ps --filter "name=oracle-xe" --filter "name=pg-target"
