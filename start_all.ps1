[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$NoInstall,
    [switch]$SkipAI,
    [ValidateSet('none', 'ollama', 'groq')][string]$AiProvider = 'none',
    [int]$WaitSeconds = 1200,
    [string]$ConfigFile = ''
)
. (Join-Path $PSScriptRoot 'scripts/stack-common.ps1')
if (-not $ConfigFile) { $ConfigFile = Join-Path $PSScriptRoot '.env.stack' }
$startupLock = $null
try {
    $lockDirectory = Join-Path $PSScriptRoot '.runtime'
    New-Item -ItemType Directory -Path $lockDirectory -Force | Out-Null
    try {
        $startupLock = [IO.File]::Open((Join-Path $lockDirectory 'startup.lock'),
                                      [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite,
                                      [IO.FileShare]::None)
    } catch { throw 'Another start_all is already running in this checkout. Wait for it to finish.' }
    Write-Host 'Legacy DB Moderniser - start all' -ForegroundColor Cyan
    New-StackConfig -Path $ConfigFile -Provider $AiProvider
    Update-StackConfig -Path $ConfigFile
    Initialize-StackContext -Root $PSScriptRoot -ConfigFile $ConfigFile
    $provider = Get-StackSetting 'STACK_AI_PROVIDER' 'ollama'
    if ($provider -notin @('none', 'ollama', 'groq')) { throw 'STACK_AI_PROVIDER must be none, ollama or groq.' }
    if ($provider -eq 'none') { $SkipAI = $true }
    if (-not $SkipAI -and $provider -eq 'groq' -and -not (Get-StackSetting 'STACK_GROQ_API_KEY' '')) {
        throw 'Add STACK_GROQ_API_KEY to .env.stack, or use STACK_AI_PROVIDER=ollama. An API account/key cannot be created automatically.'
    }
    Ensure-StackDocker -NoInstall:$NoInstall
    Invoke-StackCompose -ComposeArgs @('config', '--quiet')

    Write-Host '1/6 Building the app and installing its Python dependencies...' -ForegroundColor Cyan
    Invoke-StackCompose -ComposeArgs @('build', 'bootstrap')
    Write-Host '2/6 Downloading/starting databases and waiting for real readiness...' -ForegroundColor Cyan
    $services = @('oracle', 'postgres', 'queue')
    if ($provider -eq 'ollama' -and -not $SkipAI) { $services += 'ollama' }
    Invoke-StackCompose -ComposeArgs (@('up', '-d', '--wait', '--wait-timeout', "$WaitSeconds") + $services)

    if ($provider -eq 'ollama' -and -not $SkipAI) {
        Write-Host '3/6 Checking the AI model cache...' -ForegroundColor Cyan
        $model = Get-StackSetting 'STACK_OLLAMA_MODEL' 'qwen2.5-coder:1.5b'
        & docker @script:ComposeBase exec -T ollama sh -c 'ollama show "$1" >/dev/null 2>&1' sh $model
        if ($LASTEXITCODE -eq 0) {
            Write-Host '   Model already downloaded; reusing the local cache.'
        } else {
            Invoke-StackCompose -ComposeArgs @('exec', '-T', 'ollama', 'ollama', 'pull', $model)
        }
    } else { Write-Host '3/6 AI model download not required for this mode.' }

    Write-Host '4/6 Initialising sample data only if the demo schema is empty...' -ForegroundColor Cyan
    Invoke-StackCompose -ComposeArgs @('run', '--rm', '--no-deps', 'bootstrap')
    Write-Host '5/6 Checking authenticated database queries, encoding, migration state and AI...' -ForegroundColor Cyan
    $health = @('run', '--rm', '--no-deps', 'bootstrap', 'python', 'runtime_stack.py', 'health')
    if ($SkipAI) { $health += '--skip-ai' }
    Invoke-StackCompose -ComposeArgs $health

    Write-Host '6/6 Starting the API and website and checking HTTP health...' -ForegroundColor Cyan
    Invoke-StackCompose -ComposeArgs @('up', '-d', '--wait', '--wait-timeout', "$WaitSeconds", 'worker', 'api', 'web')
    Test-StackHttp
    $webPort = Get-StackSetting 'STACK_WEB_PORT' '18501'
    $apiPort = Get-StackSetting 'STACK_API_PORT' '18000'
    Write-Host "`nReady: http://localhost:$webPort" -ForegroundColor Green
    Write-Host "API documentation: http://localhost:$apiPort/docs"
    Write-Host 'Sign in as admin using STACK_ADMIN_PASSWORD from your private .env.stack file.'
    Write-Host 'Stop with stop_all.bat or ./stop_all.ps1. Data, downloads and model files are retained.'
    if ($SkipAI) { Write-Warning 'AI checks/model setup were explicitly skipped; procedure translation is not ready unless separately configured.' }
    if (-not $NoBrowser) { Start-Process "http://localhost:$webPort" }
    exit 0
} catch {
    Write-Host "`nStartup stopped: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Data is retained. Fix the issue and rerun start_all. To stop only this project, run stop_all.'
    Write-Host 'Container diagnostics: docker compose --env-file .env.stack --profile local-ai logs --tail 80'
    exit 1
} finally {
    if ($null -ne $startupLock) { $startupLock.Dispose() }
}
