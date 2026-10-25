<#
CryptoInvest LOCAL (docker-compose.yml, projeto `cryptoinvest-local`). NAO e producao: ver docs/local.md.
  .\deploy\local-up.ps1 up [-OpenCalendar]    gera segredos (1a vez), constroi e sobe tudo; mostra a URL do painel
  .\deploy\local-up.ps1 down                  para tudo e PRESERVA os dados
  .\deploy\local-up.ps1 reset                 apaga os volumes do projeto (pede confirmacao digitada)
  .\deploy\local-up.ps1 logs [servico]        acompanha os logs
  .\deploy\local-up.ps1 status                estado dos servicos
  .\deploy\local-up.ps1 create-admin [usuario]  cria o administrador (senha por prompt) e imprime o token do 2FA
  .\deploy\local-up.ps1 backup                pg_dump + volume paper-data em .\backups
  .\deploy\local-up.ps1 demo [-Remove]        carrega (ou remove) dados FICTICIOS de demonstracao
  .\deploy\local-up.ps1 secrets             so gera os segredos (sem Docker)
  .\deploy\local-up.ps1 tool backtest|validate|lessons|admin [args]   ferramentas sob demanda
Variaveis de ambiente: CRYPTOINVEST_WEB_PORT (8088), CRYPTOINVEST_SECRETS_DIR (deploy\local-secrets), CRYPTOINVEST_PROJECT.
#>
param(
    [Parameter(Position = 0)][string]$Command = "",
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest = @(),
    [switch]$OpenCalendar,
    [switch]$Remove
)
$ErrorActionPreference = "Continue"

$Root = Split-Path -Parent $PSScriptRoot
$Project = if ($env:CRYPTOINVEST_PROJECT) { $env:CRYPTOINVEST_PROJECT } else { "cryptoinvest-local" }
$SecretsDir = if ($env:CRYPTOINVEST_SECRETS_DIR) { [IO.Path]::GetFullPath($env:CRYPTOINVEST_SECRETS_DIR) } else { Join-Path $Root "deploy\local-secrets" }
$Port = if ($env:CRYPTOINVEST_WEB_PORT) { $env:CRYPTOINVEST_WEB_PORT } else { "8088" }
$DemoDb = "cryptoinvest_demo"
$DemoState = Join-Path $SecretsDir ".demo-active"
$env:CRYPTOINVEST_SECRETS_DIR = $SecretsDir
$env:CRYPTOINVEST_WEB_PORT = $Port
$ComposeFiles = @("-f", (Join-Path $Root "docker-compose.yml"))

function Fail([string]$Message) { Write-Error $Message; exit 1 }
function Test-DemoActive { return (Test-Path $DemoState) }
if (Test-DemoActive) { $env:CRYPTOINVEST_DB_NAME = $DemoDb }

function Assert-Docker {
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { Fail "O Docker nao esta em execucao (abra o Docker Desktop)." }
}
function Invoke-Compose {
    & docker compose -p $Project --project-directory $Root @ComposeFiles @args
}
function Invoke-ComposeChecked {
    Invoke-Compose @args
    if ($LASTEXITCODE -ne 0) { throw "docker compose $($args -join ' ') falhou (codigo $LASTEXITCODE)" }
}

function Initialize-Secrets {
    if ((Test-Path (Join-Path $SecretsDir "api_master_keys")) -and (Test-Path (Join-Path $SecretsDir "live_authorization_hmac_key"))) { return }
    Write-Output "Gerando segredos locais em $SecretsDir (so os que faltam; nunca sobrescreve)..."
    New-Item -ItemType Directory -Force $SecretsDir | Out-Null
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls $SecretsDir /inheritance:r /grant:r "${me}:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "nao foi possivel restringir a ACL de $SecretsDir" }
    & (Join-Path $PSScriptRoot "secrets.ps1") -Out $SecretsDir | Out-Null
    $live = Join-Path $SecretsDir "live_authorization_hmac_key"
    if (-not (Test-Path $live)) {
        $bytes = New-Object byte[] 96
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $text = ([Convert]::ToBase64String($bytes) -replace '[^A-Za-z0-9]', '').Substring(0, 64)
        [IO.File]::WriteAllText($live, $text, (New-Object Text.UTF8Encoding($false)))
    }
    Write-Output "Segredos prontos. FACA COPIA de $SecretsDir : sem api_master_keys os 2FA ficam irrecuperaveis."
}

function Invoke-DbPsql([string]$Database, [string]$Sql) {
    $out = Invoke-Compose exec -T timescaledb psql -X -U cryptoinvest -d $Database -v ON_ERROR_STOP=1 -tAc $Sql
    if ($LASTEXITCODE -ne 0) { throw "psql falhou: $Sql" }
    return ($out | Out-String).Trim()
}

function Write-Access {
    Write-Output ""
    Write-Output "Painel: http://localhost:$Port   (abra no navegador; use 'localhost', nao 127.0.0.1)"
    Write-Output "Primeiro acesso:"
    Write-Output "  1) .\deploy\local-up.ps1 create-admin         (informe usuario e senha; anote o TOKEN DE ATIVACAO)"
    Write-Output "  2) no painel, entre com usuario e senha, informe o token e leia o QR code no app autenticador"
    Write-Output "  3) o robo esta em PAPER, com o calendario de eventos fechado: ele recusa entradas ate haver cobertura"
    Write-Output "Para ver as telas preenchidas: .\deploy\local-up.ps1 demo   (dados ficticios, removiveis)"
}

function Read-PlainPassword([string]$Prompt) {
    $secure = Read-Host $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

switch ($Command) {
    "up" {
        Assert-Docker
        Initialize-Secrets
        if ($OpenCalendar) {
            Write-Warning "--OpenCalendar e so para TESTE: o robo opera sem o calendario de eventos e o run 'paper-local-open' fica INELEGIVEL para o Live para sempre."
            $ComposeFiles += @("-f", (Join-Path $Root "deploy\local-open-calendar.yml"))
        }
        $scale = @()
        if (Test-DemoActive) {
            Write-Output "Modo demonstracao ativo: o robo fica parado e o painel le o banco $DemoDb (remova com: demo -Remove)."
            $scale = @("--scale", "runner=0")
        }
        Invoke-Compose up -d --build --wait @scale
        if ($LASTEXITCODE -ne 0) { Fail "Falha ao subir. Veja: .\deploy\local-up.ps1 logs   (porta $Port ocupada? defina CRYPTOINVEST_WEB_PORT)" }
        Write-Access
    }
    "down" {
        Assert-Docker
        Invoke-Compose --profile tools --profile demo --profile ai down --remove-orphans
        Write-Output "Parado. Os dados foram preservados."
    }
    "reset" {
        Assert-Docker
        Write-Output "Isto APAGA todos os volumes do projeto '$Project' (banco, Redis, diario do robo, relatorios, cache)."
        Write-Output "Nada fora do projeto e tocado. Os segredos em $SecretsDir sao mantidos."
        $answer = Read-Host "Digite $Project para confirmar"
        if ($answer -ne $Project) { Fail "Confirmacao diferente; nada foi apagado." }
        Invoke-ComposeChecked --profile tools --profile demo --profile ai down -v --remove-orphans
        Remove-Item -Force $DemoState -ErrorAction SilentlyContinue
        Write-Output "Projeto $Project apagado."
    }
    "secrets" { Initialize-Secrets }
    "logs" { Assert-Docker; Invoke-Compose logs -f --tail 100 @Rest }
    "status" {
        Assert-Docker
        Invoke-Compose --profile tools --profile demo ps -a
        Write-Output "Painel: http://localhost:$Port"
    }
    "create-admin" {
        Assert-Docker
        $user = if ($Rest.Count -gt 0) { $Rest[0] } else { Read-Host "Usuario" }
        $pass = Read-PlainPassword "Senha"
        if ($pass -ne (Read-PlainPassword "Repita a senha")) { Fail "As senhas nao conferem." }
        if (Test-DemoActive) { Write-Warning "Modo demonstracao ativo; este usuario valera so no banco demo." }
        $OutputEncoding = New-Object Text.UTF8Encoding($false)
        $pass | & docker compose -p $Project --project-directory $Root @ComposeFiles run --rm -T admin create-admin --username $user --password-stdin
        if ($LASTEXITCODE -ne 0) { Fail "create-admin falhou." }
    }
    "backup" {
        Assert-Docker
        $container = (Invoke-Compose ps -q timescaledb | Out-String).Trim()
        if (-not $container) { Fail "O banco nao esta em execucao (rode up)." }
        $env:TIMESCALE_CONTAINER = $container
        $env:PAPER_VOLUME = "${Project}_paper-data"
        $env:STACK_NAME = $Project
        if (-not $env:BACKUP_DIR) { $env:BACKUP_DIR = Join-Path $Root "backups" }
        & (Join-Path $PSScriptRoot "backup.ps1")
    }
    "demo" {
        Assert-Docker
        if ($Remove) {
            Remove-Item -Force $DemoState -ErrorAction SilentlyContinue
            Remove-Item Env:CRYPTOINVEST_DB_NAME -ErrorAction SilentlyContinue
            Invoke-ComposeChecked up -d --wait
            Invoke-DbPsql postgres "DROP DATABASE IF EXISTS $DemoDb WITH (FORCE)" | Out-Null
            Write-Output "Demonstracao removida (banco $DemoDb descartado); API e robo voltaram ao diario real."
            break
        }
        if (Test-DemoActive) { Fail "A demonstracao ja esta ativa (remova com: demo -Remove)." }
        if (-not (Invoke-Compose ps -q --status running timescaledb | Out-String).Trim()) { Fail "Rode 'up' antes." }
        if ((Invoke-DbPsql cryptoinvest "SELECT count(*) FROM users") -eq "0") {
            Fail "Crie o administrador antes (create-admin): o usuario e copiado para o banco demo."
        }
        Write-Output "Criando o banco separado $DemoDb (o diario real nunca e tocado)..."
        Invoke-DbPsql postgres "DROP DATABASE IF EXISTS $DemoDb WITH (FORCE)" | Out-Null
        Invoke-DbPsql postgres "CREATE DATABASE $DemoDb OWNER cryptoinvest" | Out-Null
        Invoke-ComposeChecked --profile demo run --rm demo-migrate | Out-Null
        $copy = "pg_dump -U cryptoinvest -d cryptoinvest --data-only -t users -t recovery_codes | psql -X -q -U cryptoinvest -d $DemoDb -v ON_ERROR_STOP=1"
        Invoke-ComposeChecked exec -T timescaledb sh -c $copy | Out-Null
        Invoke-ComposeChecked --profile demo run --rm demo-seed
        New-Item -ItemType File -Force $DemoState | Out-Null
        $env:CRYPTOINVEST_DB_NAME = $DemoDb
        Invoke-ComposeChecked stop runner
        Invoke-ComposeChecked up -d --wait --scale runner=0
        Write-Output ""
        Write-Output "Demonstracao ATIVA em http://localhost:$Port : dados FICTICIOS (run demo-paper, motivos com 'DEMO'), robo parado."
        Write-Output "Entre com o seu usuario de sempre (sessoes antigas foram encerradas). Remova com: .\deploy\local-up.ps1 demo -Remove"
    }
    "tool" {
        Assert-Docker
        if ($Rest.Count -eq 0 -or $Rest[0] -notin "backtest", "validate", "lessons", "admin") { Fail "Uso: tool backtest|validate|lessons|admin [args]" }
        $svc = $Rest[0]
        $more = if ($Rest.Count -gt 1) { $Rest[1..($Rest.Count - 1)] } else { @() }
        Invoke-Compose run --rm $svc @more
    }
    default { Get-Help $PSCommandPath | Out-String | Write-Output; exit 2 }
}
