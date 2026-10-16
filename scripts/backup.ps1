[CmdletBinding()]
param([switch]$RestoreTest)
$root=Split-Path $PSScriptRoot -Parent
. (Join-Path $root 'scripts/stack-common.ps1')
Initialize-StackContext -Root $root -ConfigFile (Join-Path $root '.env.stack')
# Stop new HTTP writes; active work must already have finished.
Invoke-StackCompose -ComposeArgs @('exec','-T','api','python','-c',"from moderniser.store import store; assert not any(j['status'] in ('queued','running') for j in store.list('job',limit=10000)), 'Wait for active jobs before backup'")
Invoke-StackCompose -ComposeArgs @('stop','web','api','worker')
try {
    $stamp=Get-Date -Format 'yyyyMMdd-HHmmss'
    $path="/app/logs/backups/$stamp"
    Invoke-StackCompose -ComposeArgs @('run','--rm','--no-deps','bootstrap','python','-m','moderniser.backup','create','--path',$path)
    if ($RestoreTest) {
        Invoke-StackCompose -ComposeArgs @('run','--rm','--no-deps','bootstrap','python','-m','moderniser.backup','restore-test','--path',$path)
    }
    Write-Host "Backup stored in the logs volume: $path"
    Write-Host 'Copy backups off this device for disaster recovery. Keep the private .env.stack separately.'
} finally {
    Invoke-StackCompose -ComposeArgs @('up','-d','worker','api','web')
}
