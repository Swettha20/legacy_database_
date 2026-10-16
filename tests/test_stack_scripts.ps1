# Run with powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/test_stack_scripts.ps1
. (Join-Path (Split-Path $PSScriptRoot -Parent) 'scripts/stack-common.ps1')
$testRoot = Split-Path $PSScriptRoot -Parent
$scratch = Join-Path $testRoot '.runtime'
New-Item -ItemType Directory -Force -Path $scratch | Out-Null
$settings = Join-Path $scratch ('test-' + [Guid]::NewGuid().ToString('N') + '.env')
try {
    New-StackConfig -Path $settings -Provider ollama
    $first = (Get-FileHash -LiteralPath $settings -Algorithm SHA256).Hash
    New-StackConfig -Path $settings -Provider groq
    if ($first -ne (Get-FileHash -LiteralPath $settings -Algorithm SHA256).Hash) {
        throw 'Rerunning setup overwrote private settings.'
    }
    Initialize-StackContext -Root $testRoot -ConfigFile $settings
    if ((Get-StackSetting 'STACK_AI_PROVIDER' '') -ne 'ollama') { throw 'Existing provider was changed.' }
    if ((Get-StackSetting 'MISSING_VALUE' 'fallback') -ne 'fallback') { throw 'Default setting failed.' }
    $secrets = @('STACK_ORACLE_PASSWORD','STACK_DEMO_PASSWORD','STACK_PG_PASSWORD') |
        ForEach-Object { Get-StackSetting $_ '' }
    foreach ($secret in $secrets) {
        if ($secret -notmatch '^[a-f0-9]{24}$') { throw 'Invalid generated password format.' }
    }
    if (($secrets | Select-Object -Unique).Count -ne 3) { throw 'Generated passwords are not independent.' }
    if ($script:ComposeBase -notcontains $testRoot) { throw 'Project path was lost or split.' }
    if ($script:ComposeBase -notcontains $settings) { throw 'Settings path was lost or split.' }
    Write-Host 'PASS  Settings preservation, independent secrets, provider persistence and paths with spaces.'
} finally {
    if (Test-Path -LiteralPath $settings) { Remove-Item -LiteralPath $settings }
}
