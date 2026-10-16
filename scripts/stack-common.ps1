Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function New-StackSecret {
    $bytes = New-Object byte[] 12
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
}

function New-StackConfig {
    param([string]$Path, [string]$Provider)
    if (Test-Path -LiteralPath $Path) { return }
    $key = [Environment]::GetEnvironmentVariable('GROQ_API_KEY')
    if ($null -eq $key) { $key = '' }
    $content = @"
# Generated locally by start_all. Keep this file private and retain it with your data volumes.
STACK_PROJECT_NAME=legacy-moderniser
STACK_ORACLE_PASSWORD=$(New-StackSecret)
STACK_DEMO_PASSWORD=$(New-StackSecret)
STACK_PG_PASSWORD=$(New-StackSecret)
STACK_API_PORT=18000
STACK_WEB_PORT=18501
STACK_AI_PROVIDER=$Provider
STACK_OLLAMA_MODEL=qwen2.5-coder:1.5b
STACK_GROQ_API_KEY=$key
STACK_GROQ_MODEL=openai/gpt-oss-120b
"@
    [IO.File]::WriteAllText($Path, $content, (New-Object Text.UTF8Encoding($false)))
    Write-Host "Created private stack settings: $Path"
}

function Initialize-StackContext {
    param([string]$Root, [string]$ConfigFile)
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        $knownDocker = Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin'
        if (Test-Path -LiteralPath (Join-Path $knownDocker 'docker.exe')) {
            $env:PATH = "$knownDocker;$env:PATH"
        }
    }
    if (-not (Test-Path -LiteralPath $ConfigFile)) {
        throw 'Stack settings do not exist. Run start_all first.'
    }
    $script:StackRoot = $Root
    $script:StackConfig = [IO.Path]::GetFullPath($ConfigFile)
    $script:StackValues = @{}
    foreach ($line in [IO.File]::ReadAllLines($script:StackConfig)) {
        if ($line -match '^\s*([A-Z][A-Z0-9_]*)\s*=(.*)$') {
            $value = $Matches[2].Trim()
            if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or
                                         ($value.StartsWith("'") -and $value.EndsWith("'")))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $script:StackValues[$Matches[1]] = $value
        }
    }
    foreach ($required in @('STACK_ORACLE_PASSWORD', 'STACK_DEMO_PASSWORD', 'STACK_PG_PASSWORD')) {
        if (-not $script:StackValues.ContainsKey($required) -or -not $script:StackValues[$required]) {
            throw "Missing $required in stack settings. Do not regenerate passwords for existing database volumes."
        }
    }
    $script:ComposeBase = @('compose', '--project-directory', $Root, '--env-file', $script:StackConfig,
                           '-f', (Join-Path $Root 'compose.yaml'), '--profile', 'local-ai', '--profile', 'tools')
}

function Update-StackConfig {
    param([string]$Path)
    $content = [IO.File]::ReadAllText($Path)
    foreach ($key in @('STACK_META_PASSWORD','STACK_ADMIN_PASSWORD','STACK_READER_PASSWORD')) {
        if ($content -notmatch "(?m)^$key=") {
            [IO.File]::AppendAllText($Path, "`n$key=$(New-StackSecret)`n", (New-Object Text.UTF8Encoding($false)))
        }
    }
}

function Get-StackSetting {
    param([string]$Name, [string]$Default)
    if ($script:StackValues.ContainsKey($Name) -and $script:StackValues[$Name]) {
        return $script:StackValues[$Name]
    }
    return $Default
}

function Invoke-StackCompose {
    param([string[]]$ComposeArgs)
    & docker @script:ComposeBase @ComposeArgs
    if ($LASTEXITCODE -ne 0) {
        # Never print the resolved Compose config: it contains passwords.
        throw "Docker Compose '$($ComposeArgs[0])' failed (exit $LASTEXITCODE)."
    }
}

function Get-StackDockerEngine {
    # Windows PowerShell 5 treats redirected native stderr as error records.
    # A stopped daemon is expected here, not a terminating setup error.
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & docker info --format '{{.OSType}}' 2>$null
        return @{ Engine = ("$output").Trim(); ExitCode = $LASTEXITCODE }
    } finally { $ErrorActionPreference = $previous }
}

function Ensure-StackDocker {
    param([switch]$NoInstall, [int]$WaitSeconds = 300)
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        $knownDocker = Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin'
        if (Test-Path -LiteralPath (Join-Path $knownDocker 'docker.exe')) {
            $env:PATH = "$knownDocker;$env:PATH"
        }
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        if ($NoInstall) { throw 'Docker is missing. Install Docker Desktop, then rerun start_all.' }
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            throw 'Install Docker Desktop from https://docs.docker.com/desktop/setup/install/windows-install/ then rerun start_all. Windows App Installer (winget) is not available.'
        }
        Write-Host 'Installing Docker Desktop. Windows may request administrator approval or a reboot.'
        & winget install --id Docker.DockerDesktop --exact --accept-source-agreements
        if ($LASTEXITCODE -ne 0) {
            throw 'Docker installation did not finish. Complete the installer/Windows restart, then rerun start_all.'
        }
        $env:PATH = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:PATH
        $knownDocker = Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin'
        $env:PATH = "$knownDocker;$env:PATH"
    }
    $composeVersion = & docker compose version 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose v2+ is required; update Docker Desktop.' }
    Write-Host $composeVersion
    $probe = Get-StackDockerEngine
    if ($probe.ExitCode -ne 0) {
        $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
        if (-not (Test-Path -LiteralPath $desktop)) {
            throw 'Docker is installed but its engine is unavailable. Start Docker and rerun start_all.'
        }
        Write-Host 'Starting Docker Desktop. Complete any Docker/WSL first-run prompts.'
        Start-Process -FilePath $desktop -WindowStyle Hidden
        $deadline = (Get-Date).AddSeconds($WaitSeconds)
        do {
            Start-Sleep -Seconds 5
            $probe = Get-StackDockerEngine
            if ($probe.ExitCode -eq 0) { break }
            Write-Host '  Waiting for the Docker engine...'
        } while ((Get-Date) -lt $deadline)
        if ($probe.ExitCode -ne 0) {
            throw 'Docker is not ready. Enable hardware virtualization/WSL 2 if requested, finish first-run setup or reboot, then rerun start_all.'
        }
    }
    if ($probe.Engine -ne 'linux') { throw 'Switch Docker Desktop to Linux containers, then rerun start_all.' }
}

function Test-StackHttp {
    $apiPort = Get-StackSetting 'STACK_API_PORT' '18000'
    $webPort = Get-StackSetting 'STACK_WEB_PORT' '18501'
    $api = Invoke-RestMethod -Uri "http://127.0.0.1:$apiPort/health/live" -TimeoutSec 15
    if ($api.service -ne 'legacy-db-modernizer') { throw 'API port answers with a different application.' }
    $ready = Invoke-RestMethod -Uri "http://127.0.0.1:$apiPort/health/ready" -TimeoutSec 25
    if ($ready.status -ne 'ready') { throw 'The API reports an unavailable database.' }
    $web = Invoke-WebRequest -Uri "http://127.0.0.1:$webPort/health/live" -UseBasicParsing -TimeoutSec 15
    if ($web.StatusCode -ne 200) { throw 'The website health check failed.' }
    Write-Host 'PASS  API identity, database readiness and website HTTP health.' -ForegroundColor Green
}
