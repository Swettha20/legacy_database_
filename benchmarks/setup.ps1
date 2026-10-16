[CmdletBinding()]
param()
$root = Split-Path $PSScriptRoot -Parent
. (Join-Path $root 'scripts/stack-common.ps1')
Initialize-StackContext -Root $root -ConfigFile (Join-Path $root '.env.stack')
$bench = Join-Path $root '.runtime/benchmark'
New-Item -ItemType Directory -Force $bench | Out-Null
$pin = '6660bad68c07bd143430ace58565b3f727e17263'
$source = Join-Path $bench "db-sample-schemas-$pin"
if (-not (Test-Path -LiteralPath $source)) {
    Invoke-WebRequest "https://codeload.github.com/oracle-samples/db-sample-schemas/zip/$pin" -OutFile (Join-Path $bench 'schemas.zip')
    Expand-Archive (Join-Path $bench 'schemas.zip') -DestinationPath $bench
}
$cl = Join-Path $bench 'sqlcl.zip'
if (-not (Test-Path -LiteralPath $cl)) {
    Invoke-WebRequest 'https://download.oracle.com/otn_software/java/sqldeveloper/sqlcl-latest.zip' -OutFile $cl
}
# Fail closed if the moving vendor link has changed; update this pin only after review.
if ((Get-FileHash $cl -Algorithm SHA256).Hash -ne 'EB0DCA2BECC30E9AA1C700AE71E5DF6EFDE8A33D098D7D23405B6558BBB81A81') {
    throw 'SQLcl download differs from the validated 26.3 release. Review the new version and update the checksum before installing.'
}
if (-not (Test-Path (Join-Path $bench 'sqlcl/bin/sql'))) { Expand-Archive $cl -DestinationPath $bench }
& docker @script:ComposeBase run --rm --no-deps --env PYTHONPATH=/app --volume "${bench}:/bench" bootstrap python benchmarks/verify_source.py
if ($LASTEXITCODE -eq 0) { Write-Host 'Existing SH sample validated and retained.'; exit 0 }
if ($LASTEXITCODE -ne 3) { throw 'Existing SH sample is incomplete or unavailable. No data was overwritten.' }
$original = Get-Content (Join-Path $source 'sales_history/sh_install.sql') -Raw
$adapted = [regex]::Replace($original, '(?m)^ACCEPT pass .*$', ('DEFINE pass = '+(Get-StackSetting 'STACK_DEMO_PASSWORD' '')))
$adapted = [regex]::Replace($adapted, '(?m)^ACCEPT tbs .*$', 'DEFINE tbs = USERS')
$adapted = [regex]::Replace($adapted, '(?m)^ACCEPT overwrite_schema .*$', 'DEFINE overwrite_schema = no')
$adapted = $adapted.Replace('@@sh_populate.sql','@@sh_populate_automated.sql')
Set-Content (Join-Path $source 'sales_history/sh_install_automated.sql') $adapted
$populate = Get-Content (Join-Path $source 'sales_history/sh_populate.sql') -Raw
$populate = [regex]::Replace($populate, '(?s)CREATE INDEX sup_text_idx.*?nopopulate''\);', '-- SUP_TEXT_IDX omitted: Oracle Text is unavailable in the slim benchmark image.')
Set-Content (Join-Path $source 'sales_history/sh_populate_automated.sql') $populate
$project = Get-StackSetting 'STACK_PROJECT_NAME' 'legacy-moderniser'
& docker run --rm --network "${project}_default" --env-file $script:StackConfig --mount "type=bind,source=$bench,target=/bench" --workdir "/bench/db-sample-schemas-$pin/sales_history" eclipse-temurin:21-jre bash -c 'printf ''connect system/"%s"@oracle:1521/FREEPDB1\n@sh_install_automated.sql\n'' "$STACK_ORACLE_PASSWORD" | bash /bench/sqlcl/bin/sql -s /nolog'
if ($LASTEXITCODE -ne 0) { throw 'SH installation failed. Inspect the private installer log in .runtime/benchmark.' }
Invoke-StackCompose -ComposeArgs @('run','--rm','--no-deps','--env','PYTHONPATH=/app','--volume',"${bench}:/bench",'bootstrap','python','benchmarks/verify_source.py')
Copy-Item (Join-Path $bench 'installation.json') (Join-Path $PSScriptRoot 'results/source-installation.json') -Force
Write-Host 'Sales History validated: 1,063,396 base-table rows. Oracle Text index omission recorded.'
