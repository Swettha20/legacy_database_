[CmdletBinding()]
param()
$root=Split-Path $PSScriptRoot -Parent
. (Join-Path $root 'scripts/stack-common.ps1')
$original=Join-Path $root '.env.stack'
$folder=Join-Path $root '.runtime'
$stamp=Get-Date -Format 'yyyyMMddHHmmss'
$config=Join-Path $folder "clean-$stamp.env"
New-StackConfig -Path $config -Provider none
Update-StackConfig -Path $config
function Get-FreePort {
    $listener=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
    $listener.Start()
    try { return $listener.LocalEndpoint.Port } finally { $listener.Stop() }
}
$apiPort=Get-FreePort
$webPort=Get-FreePort
$content=[IO.File]::ReadAllText($config)
$content=$content.Replace('STACK_PROJECT_NAME=legacy-moderniser',"STACK_PROJECT_NAME=moderniser-clean-$stamp")
$content=$content.Replace('STACK_API_PORT=18000',"STACK_API_PORT=$apiPort").Replace('STACK_WEB_PORT=18501',"STACK_WEB_PORT=$webPort")
[IO.File]::WriteAllText($config,$content,(New-Object Text.UTF8Encoding($false)))
Initialize-StackContext -Root $root -ConfigFile $original
Invoke-StackCompose -ComposeArgs @('stop')
try {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'start_all.ps1') -ConfigFile $config -NoBrowser -NoInstall
    if ($LASTEXITCODE -ne 0) { throw 'Clean deployment startup failed' }
    $check=Invoke-RestMethod "http://127.0.0.1:$apiPort/health/ready"
    if ($check.status -ne 'ready') { throw 'Clean deployment is not ready' }
    $settings=@{}
    foreach($line in Get-Content $config){if($line -match '^([A-Z_]+)=(.*)$'){$settings[$Matches[1]]=$Matches[2]}}
    $session=New-Object Microsoft.PowerShell.Commands.WebRequestSession
    $body=@{username='admin';password=$settings['STACK_ADMIN_PASSWORD']} | ConvertTo-Json
    Invoke-RestMethod -Uri "http://127.0.0.1:$apiPort/v1/auth/login" -Method Post -ContentType application/json -Body $body -WebSession $session | Out-Null
    $job=Invoke-RestMethod -Uri "http://127.0.0.1:$apiPort/v1/jobs" -Method Post -ContentType application/json -Body '{"source_schema":"DEMO"}' -WebSession $session
    $deadline=(Get-Date).AddMinutes(3)
    do {
        Start-Sleep -Seconds 1
        $state=Invoke-RestMethod "http://127.0.0.1:$apiPort/v1/jobs/$($job.job_id)" -WebSession $session
    } while ($state.status -in @('queued','running') -and (Get-Date) -lt $deadline)
    if (-not $state.verification.passed -or -not $state.verification.export_restore.passed) { throw 'Clean deployment migration/restore failed' }
    $result=@{project="moderniser-clean-$stamp";fresh_named_volumes=$true;rules_only=$true;readiness=$true;verified_migration=$true;job_id=$job.job_id;oracle_download_cached=$true;host_os_install_tested=$false}
    $result | ConvertTo-Json | Set-Content (Join-Path $root 'benchmarks/results/clean-deployment.json')
    Write-Host 'PASS Clean named volumes, startup, authentication, migration and restored download.'
} finally {
    Initialize-StackContext -Root $root -ConfigFile $config
    Invoke-StackCompose -ComposeArgs @('stop')
    Initialize-StackContext -Root $root -ConfigFile $original
    $services=@('oracle','postgres','queue','worker','api','web')
    if ((Get-StackSetting 'STACK_AI_PROVIDER' 'none') -eq 'ollama') { $services+='ollama' }
    Invoke-StackCompose -ComposeArgs (@('up','-d','--wait','--wait-timeout','240')+$services)
    Write-Host 'Original deployment restarted. Isolated test volumes and private settings retained for inspection.'
}
