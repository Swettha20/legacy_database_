[CmdletBinding()]
param([switch]$SkipAI, [string]$ConfigFile = '')
. (Join-Path $PSScriptRoot 'scripts/stack-common.ps1')
if (-not $ConfigFile) { $ConfigFile = Join-Path $PSScriptRoot '.env.stack' }
try {
    Initialize-StackContext -Root $PSScriptRoot -ConfigFile $ConfigFile
    Invoke-StackCompose -ComposeArgs @('ps')
    $health = @('run', '--rm', '--no-deps', 'bootstrap', 'python', 'runtime_stack.py', 'health')
    if ($SkipAI) { $health += '--skip-ai' }
    Invoke-StackCompose -ComposeArgs $health
    Test-StackHttp
    exit 0
} catch {
    Write-Host "Health check failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
