<#
Backup: pg_dump comprimido (formato custom) do Timescale + copia do volume data/paper + manifesto de verificacao.
Variaveis de ambiente: BACKUP_DIR (.\backups) RETENTION_DAYS (14) TIMESCALE_CONTAINER STACK_NAME PAPER_VOLUME
DB_NAME DB_USER TOOLS_IMAGE. Nao usa senha: o pg_dump roda DENTRO do container pelo socket local.
O backup contem hashes de senha, segredos TOTP e credenciais da Binance CIFRADAS: a pasta do backup fica com ACL
restrita ao usuario atual (icacls); guarde copias fora do no em local igualmente protegido.
#>
$ErrorActionPreference = "Stop"

function Get-Setting([string]$Name, [string]$Default) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ($value) { return $value } else { return $Default }
}
function Invoke-Docker {
    $output = & docker @args
    if ($LASTEXITCODE -ne 0) { throw "docker $($args -join ' ') falhou" }
    return $output
}

$stack = Get-Setting "STACK_NAME" "cryptoinvest"
$backupDir = Get-Setting "BACKUP_DIR" ".\backups"
$retention = [int](Get-Setting "RETENTION_DAYS" "14")
$db = Get-Setting "DB_NAME" "cryptoinvest"
$user = Get-Setting "DB_USER" "cryptoinvest"
$volume = Get-Setting "PAPER_VOLUME" "${stack}_paper-data"
$image = Get-Setting "TOOLS_IMAGE" "timescale/timescaledb:2.30.2-pg17"
$container = Get-Setting "TIMESCALE_CONTAINER" ""
if (-not $container) {
    $container = (Invoke-Docker ps -q -f "label=com.docker.swarm.service.name=${stack}_timescaledb" | Select-Object -First 1)
}
if (-not $container) { throw "container do timescaledb nao encontrado (defina TIMESCALE_CONTAINER)" }

$countsSql = "SELECT table_name || '|' || (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name), false, true, '')))[1]::text FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name"
$utf8 = New-Object System.Text.UTF8Encoding($false)
$dest = Join-Path $backupDir ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ"))
New-Item -ItemType Directory -Force $dest | Out-Null
$me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls $dest /inheritance:r /grant:r "${me}:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "nao foi possivel restringir a ACL de $dest" }
$ok = $false
try {
    $version = Invoke-Docker exec $container psql -X -U $user -d $db -tA -c "SELECT version_num FROM alembic_version"
    [IO.File]::WriteAllText((Join-Path $dest "alembic_version.txt"), (($version -join "`n") + "`n"), $utf8)
    $counts = Invoke-Docker exec $container psql -X -U $user -d $db -tA -c $countsSql
    [IO.File]::WriteAllText((Join-Path $dest "counts.txt"), (($counts -join "`n") + "`n"), $utf8)
    Invoke-Docker exec $container pg_dump -U $user -d $db -Fc -Z 6 --no-owner --no-acl -f /tmp/backup.dump | Out-Null
    Invoke-Docker cp "${container}:/tmp/backup.dump" (Join-Path $dest "db.dump") | Out-Null
    & docker exec $container rm -f /tmp/backup.dump | Out-Null

    # stderr redirecionado sob ErrorActionPreference=Stop viraria excecao no Windows PowerShell 5.1
    $ErrorActionPreference = "Continue"
    & docker volume inspect $volume 2> $null | Out-Null
    $volumeExists = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = "Stop"
    if ($volumeExists) {
        $tmp = "ci-backup-paper-$PID"
        Invoke-Docker create --name $tmp --network none -v "${volume}:/data:ro" --entrypoint tar $image czf /tmp/paper-data.tar.gz -C /data . | Out-Null
        try {
            Invoke-Docker start -a $tmp | Out-Null
            Invoke-Docker cp "${tmp}:/tmp/paper-data.tar.gz" (Join-Path $dest "paper-data.tar.gz") | Out-Null
        }
        finally {
            $ErrorActionPreference = "Continue"
            & docker rm -f $tmp 2> $null | Out-Null
            $ErrorActionPreference = "Stop"
        }
    }
    else { Write-Warning "volume $volume nao existe; data/paper nao incluido" }

    $lines = Get-ChildItem $dest -File | Where-Object Name -ne "SHA256SUMS" | Sort-Object Name | ForEach-Object {
        "$((Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower())  ./$($_.Name)"
    }
    [IO.File]::WriteAllText((Join-Path $dest "SHA256SUMS"), (($lines -join "`n") + "`n"), $utf8)
    $ok = $true
}
finally {
    if (-not $ok) { Remove-Item -Recurse -Force $dest -ErrorAction SilentlyContinue }
}

$limit = (Get-Date).AddDays(-$retention)
Get-ChildItem $backupDir -Directory | Where-Object { $_.Name -match '^2.*T.*Z$' -and $_.LastWriteTime -lt $limit } |
    Remove-Item -Recurse -Force
Write-Output "backup ok: $dest"
