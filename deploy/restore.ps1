<#
Restauracao.
  .\deploy\restore.ps1 verify BACKUP_DIR      restaura num Timescale DESCARTAVEL (sem rede, removido ao fim) e confere
                                              alembic_version e contagens por tabela contra o manifesto do backup
  .\deploy\restore.ps1 apply BACKUP_DIR -Container NOME -ConfirmDatabase NOME_DO_BANCO [-IStoppedServices]
                                              troca o banco por uma versao restaurada e VALIDADA (api/runner com 0 replicas)
Apply, em ordem: checksum + pg_restore --list -> confirmacao digitada -> servicos parados -> pg_dump de seguranca ->
restauracao num banco temporario <db>_restore_<ts> -> validacao -> troca por renomeacao (o antigo vira <db>_old_<ts> e so
e descartado por voce). Qualquer divergencia aborta (exit != 0) com o banco original intacto.
Opcoes: -AllowDrift (aceita contagens diferentes: backup feito com o sistema em uso); -IStoppedServices (pula a checagem
de replicas, p.ex. fora do Swarm). Variaveis: SAFETY_DIR (.\backups\pre-restore-<ts>) STACK_NAME DB_NAME DB_USER TOOLS_IMAGE.
O dump de seguranca e o diretorio do backup ficam com ACL restrita ao usuario atual.
#>
param(
    [Parameter(Mandatory, Position = 0)][ValidateSet("verify", "apply")][string]$Mode,
    [Parameter(Mandatory, Position = 1)][string]$Backup,
    [string]$Container = "",
    [string]$ConfirmDatabase = "",
    [switch]$AllowDrift,
    [switch]$IStoppedServices
)
$ErrorActionPreference = "Stop"

function Get-Setting([string]$Name, [string]$Default) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ($value) { return $value } else { return $Default }
}

# stderr vai para arquivo: no Windows PowerShell 5.1 o stderr de um nativo viraria excecao com ErrorActionPreference=Stop
function Invoke-DockerRaw {
    $errFile = [IO.Path]::GetTempFileName()
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & docker @args 2> $errFile
        $code = $LASTEXITCODE
        $err = @(Get-Content $errFile -ErrorAction SilentlyContinue)
    }
    finally {
        $ErrorActionPreference = $previous
        Remove-Item $errFile -Force -ErrorAction SilentlyContinue
    }
    return [pscustomobject]@{ Code = $code; Out = @($out | ForEach-Object { "$_" }); Err = $err }
}
function Invoke-Docker {
    $result = Invoke-DockerRaw @args
    if ($result.Code -ne 0) { throw "docker $($args -join ' ') falhou: $($result.Err -join ' ')" }
    return $result.Out
}
function Restrict-Acl([string]$Path) {
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls $Path /inheritance:r /grant:r "${me}:(OI)(CI)F" | Out-Null
}

$image = Get-Setting "TOOLS_IMAGE" "timescale/timescaledb:2.30.2-pg17"
$stack = Get-Setting "STACK_NAME" "cryptoinvest"
$db = Get-Setting "DB_NAME" "cryptoinvest"
$user = Get-Setting "DB_USER" "cryptoinvest"
if ($db -notmatch '^[a-z0-9_]+$') { throw "DB_NAME invalido: $db" }
$countsSql = "SELECT table_name || '|' || (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name), false, true, '')))[1]::text FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name"
$dump = Join-Path $Backup "db.dump"
if (-not (Test-Path $dump)) { throw "db.dump nao encontrado em $Backup" }

foreach ($line in Get-Content (Join-Path $Backup "SHA256SUMS")) {
    if ($line -notmatch '^([0-9a-fA-F]{64}) [ *](.+)$') { throw "SHA256SUMS invalido: $line" }
    $expected, $name = $Matches[1].ToLower(), $Matches[2]
    $actual = (Get-FileHash (Join-Path $Backup ($name -replace '^\./', '')) -Algorithm SHA256).Hash.ToLower()
    if ($actual -cne $expected) { throw "checksum do backup nao confere: $name" }
}

# Restauracao estrita: o pg_restore nao pode ter erro algum. A extensao timescaledb ja existe no banco de destino
# (template1/CREATE EXTENSION), entao suas entradas saem da lista; qualquer outro erro aborta.
function Import-Dump([string]$Target, [string]$As, [string]$Database) {
    Invoke-Docker cp $dump "${Target}:/tmp/restore.dump" | Out-Null
    Invoke-Docker exec $Target psql -X -U $As -d $Database -v ON_ERROR_STOP=1 -q -c "CREATE EXTENSION IF NOT EXISTS timescaledb" -c "SELECT timescaledb_pre_restore()" | Out-Null
    Invoke-Docker exec $Target sh -c "pg_restore --list /tmp/restore.dump | grep -v ' EXTENSION ' > /tmp/restore.list" | Out-Null
    $restore = Invoke-DockerRaw exec $Target pg_restore -U $As -d $Database --no-owner --no-acl --exit-on-error -L /tmp/restore.list /tmp/restore.dump
    Invoke-DockerRaw exec $Target rm -f /tmp/restore.dump /tmp/restore.list | Out-Null
    if ($restore.Code -ne 0) {
        $restore.Err | Select-Object -Last 5 | ForEach-Object { [Console]::Error.WriteLine("pg_restore: $_") }
        throw "pg_restore reportou erro"
    }
    Invoke-Docker exec $Target psql -X -U $As -d $Database -v ON_ERROR_STOP=1 -q -c "SELECT timescaledb_post_restore()" | Out-Null
}

function Test-Restored([string]$Target, [string]$As, [string]$Database) {
    $restored = (Invoke-Docker exec $Target psql -X -U $As -d $Database -tA -c "SELECT version_num FROM alembic_version") -join ""
    $expectedVersion = (Get-Content (Join-Path $Backup "alembic_version.txt") -Raw).Trim()
    Write-Output "alembic current: restaurado=$restored backup=$expectedVersion"
    if ($restored -ne $expectedVersion) { throw "FALHA: versao do alembic diverge" }
    $restoredCounts = (Invoke-Docker exec $Target psql -X -U $As -d $Database -tA -c $countsSql) -join "`n"
    $expectedCounts = (Get-Content (Join-Path $Backup "counts.txt") -Raw).Trim() -replace "`r", ""
    if ($restoredCounts -eq $expectedCounts) {
        Write-Output "contagens: $(($expectedCounts -split "`n").Count) tabelas conferem"
        return
    }
    $tables = { param($text) (($text -split "`n") | ForEach-Object { ($_ -split '\|')[0] }) -join "," }
    if ($AllowDrift -and (& $tables $restoredCounts) -eq (& $tables $expectedCounts)) {
        Write-Warning "contagens diferem (-AllowDrift); as tabelas conferem"
        return
    }
    throw "FALHA: contagens divergem do manifesto"
}

if ($Mode -eq "verify") {
    $name = "ci-restore-verify-$PID"
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $password = ([Convert]::ToBase64String($bytes) -replace '[^A-Za-z0-9]', '')
    try {
        Invoke-Docker run -d --name $name --network none -e "POSTGRES_PASSWORD=$password" -e POSTGRES_DB=verify $image | Out-Null
        $ready = $false
        for ($i = 0; $i -lt 60 -and -not $ready; $i++) {
            $log = (Invoke-DockerRaw logs $name)
            if (($log.Out + $log.Err) -join "`n" -match "init process complete") {
                $ready = ((Invoke-DockerRaw exec $name pg_isready -q -U postgres -d verify).Code -eq 0)
            }
            if (-not $ready) { Start-Sleep -Seconds 2 }
        }
        if (-not $ready) { throw "banco descartavel nao subiu" }
        Import-Dump $name "postgres" "verify"
        Test-Restored $name "postgres" "verify"
        Write-Output "restauracao verificada"
    }
    finally { Invoke-DockerRaw rm -f -v $name | Out-Null }
    return
}

if (-not $Container) { throw "informe -Container" }
if (-not ((Test-Path (Join-Path $Backup "alembic_version.txt")) -and (Test-Path (Join-Path $Backup "counts.txt")))) {
    throw "FALHA: alembic_version.txt/counts.txt ausentes no backup"
}
if ($ConfirmDatabase -cne $db) {
    throw "confirmacao ausente ou errada: digite -ConfirmDatabase $db (nome do banco que sera SUBSTITUIDO)"
}
$stamp = [DateTime]::UtcNow.ToString("yyyyMMddHHmmss")
$tmpDb = "${db}_restore_$stamp"
$oldDb = "${db}_old_$stamp"
function Invoke-Admin([string[]]$Sql) {
    $arguments = @("exec", $Container, "psql", "-X", "-U", $user, "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-q")
    foreach ($statement in $Sql) { $arguments += @("-c", $statement) }
    return Invoke-Docker @arguments
}

Invoke-Docker cp $dump "${Container}:/tmp/verify.dump" | Out-Null
$listing = Invoke-DockerRaw exec $Container pg_restore --list /tmp/verify.dump
Invoke-DockerRaw exec $Container rm -f /tmp/verify.dump | Out-Null
if ($listing.Code -ne 0) { throw "FALHA: db.dump nao e um dump valido (pg_restore --list). Nada foi alterado." }

if (-not $IStoppedServices) {
    foreach ($svc in "api", "runner") {
        $ls = Invoke-DockerRaw service ls --filter "name=${stack}_$svc" --format "{{.Name}} {{.Replicas}}"
        $line = if ($ls.Code -eq 0) { $ls.Out | Where-Object { $_ -like "${stack}_$svc *" } | Select-Object -First 1 } else { $null }
        if (-not $line -or ($line -split " ")[1] -ne "0/0") {
            throw "FALHA: ${stack}_$svc nao esta com 0 replicas (docker service scale ${stack}_api=0 ${stack}_runner=0) ou passe -IStoppedServices. Nada foi alterado."
        }
    }
}

$safety = Get-Setting "SAFETY_DIR" (Join-Path ".\backups" "pre-restore-$stamp")
New-Item -ItemType Directory -Force $safety | Out-Null
Restrict-Acl $safety
try {
    Invoke-Docker exec $Container pg_dump -U $user -d $db -Fc -Z 6 --no-owner --no-acl -f /tmp/safety.dump | Out-Null
    Invoke-Docker cp "${Container}:/tmp/safety.dump" (Join-Path $safety "db.dump") | Out-Null
}
catch { throw "FALHA: pg_dump de seguranca falhou. Nada foi alterado. $_" }
finally { Invoke-DockerRaw exec $Container rm -f /tmp/safety.dump | Out-Null }
$hash = (Get-FileHash (Join-Path $safety "db.dump") -Algorithm SHA256).Hash.ToLower()
[IO.File]::WriteAllText((Join-Path $safety "SHA256SUMS"), "$hash  ./db.dump`n", (New-Object System.Text.UTF8Encoding($false)))
Write-Output "dump de seguranca do banco atual: $safety (SHA256 em SHA256SUMS)"

$swapped = $false
try {
    Invoke-Admin @("CREATE DATABASE $tmpDb") | Out-Null
    Import-Dump $Container $user $tmpDb
    Test-Restored $Container $user $tmpDb
    Invoke-Admin @("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('$db', '$tmpDb') AND pid <> pg_backend_pid()") | Out-Null
    Invoke-Admin @("ALTER DATABASE $db RENAME TO $oldDb") | Out-Null
    try { Invoke-Admin @("ALTER DATABASE $tmpDb RENAME TO $db") | Out-Null }
    catch {
        try { Invoke-Admin @("ALTER DATABASE $oldDb RENAME TO $db") | Out-Null }
        catch { throw "FALHA: troca falhou e a reversao tambem: rode ALTER DATABASE $oldDb RENAME TO $db no postgres." }
        throw "FALHA: troca falhou; o banco original foi restaurado ao nome $db."
    }
    $swapped = $true
}
catch {
    [Console]::Error.WriteLine("$_ O banco $db NAO foi alterado (ou foi revertido). Dump de seguranca em $safety.")
    throw
}
finally {
    if (-not $swapped) {
        try { Invoke-Admin @("DROP DATABASE IF EXISTS $tmpDb WITH (FORCE)") | Out-Null } catch { $null = $_ }
    }
}
Write-Output "banco $db restaurado e validado. O anterior foi mantido como $oldDb."
Write-Output "Proximo passo: rode o job migrate (reaplica db_roles.sql e confere permissoes) e suba api/runner."
Write-Output "Para REVERTER (api/runner parados): ALTER DATABASE $db RENAME TO ${db}_failed_$stamp; ALTER DATABASE $oldDb RENAME TO $db"
Write-Output "Depois de confirmar o resultado, descarte o antigo manualmente: DROP DATABASE $oldDb"
