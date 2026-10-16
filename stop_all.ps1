[CmdletBinding()]
param([string]$ConfigFile = '')
. (Join-Path $PSScriptRoot 'scripts/stack-common.ps1')
if (-not $ConfigFile) { $ConfigFile = Join-Path $PSScriptRoot '.env.stack' }
try {
    if (-not (Test-Path -LiteralPath $ConfigFile)) {
        Write-Host 'This checkout has not been started; nothing to stop.'
        exit 0
    }
    Initialize-StackContext -Root $PSScriptRoot -ConfigFile $ConfigFile
    Write-Host 'Stopping this project only. Database records, downloads and AI models will be kept.' -ForegroundColor Cyan
    Invoke-StackCompose -ComposeArgs @('stop', '--timeout', '30')
    Write-Host 'Stopped. Run start_all to resume.' -ForegroundColor Green
    exit 0
} catch {
    Write-Host "Stop failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
