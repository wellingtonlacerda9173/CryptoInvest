<#
Gera secrets aleatorios fortes e os cria no Swarm (docker secret create, valor por stdin: nunca em argv/log).
  .\deploy\secrets.ps1                 cria os que faltarem (nunca sobrescreve nem imprime valores)
  .\deploy\secrets.ps1 -Out DIR        dry-run: grava arquivos em DIR em vez de chamar o docker
#>
param(
    [string]$Prefix = $(if ($env:SECRET_PREFIX) { $env:SECRET_PREFIX } else { "cryptoinvest" }),
    [string]$Out = ""
)
$ErrorActionPreference = "Stop"

function New-RandomBytes([int]$Count) {
    $bytes = New-Object byte[] $Count
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return $bytes
}

function New-Token([int]$Length) {
    $text = [Convert]::ToBase64String((New-RandomBytes 96)) -replace '[^A-Za-z0-9]', ''
    return $text.Substring(0, $Length)
}

function New-SecretValue([string]$Name) {
    if ($Name -eq "api_master_keys") { return "1:" + [Convert]::ToBase64String((New-RandomBytes 32)) + "`n" }
    if ($Name -like "*_password") { return New-Token 40 }
    return New-Token 64
}

function Test-DockerSecret([string]$Name) {
    docker secret inspect $Name *> $null
    return ($LASTEXITCODE -eq 0)
}

function New-DockerSecret([string]$Name, [string]$Value) {
    $info = New-Object System.Diagnostics.ProcessStartInfo("docker", "secret create $Name -")
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.UseShellExecute = $false
    $process = [System.Diagnostics.Process]::Start($info)
    $process.StandardInput.Write($Value)
    $process.StandardInput.Close()
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "docker secret create falhou para $Name" }
}

$names = "postgres_password", "migrator_password", "app_password", "api_database_password", "redis_password",
    "api_session_secret", "api_recovery_hmac_key", "command_hmac_key", "api_master_keys"

if ($Out) { New-Item -ItemType Directory -Force $Out | Out-Null }
foreach ($name in $names) {
    if ($Out) {
        $path = Join-Path $Out $name
        if (Test-Path $path) { Write-Output "existe: $name (mantido)"; continue }
        [System.IO.File]::WriteAllText($path, (New-SecretValue $name), (New-Object System.Text.UTF8Encoding($false)))
    }
    else {
        if (Test-DockerSecret "${Prefix}_$name") { Write-Output "existe: ${Prefix}_$name (mantido)"; continue }
        New-DockerSecret "${Prefix}_$name" (New-SecretValue $name)
    }
    Write-Output "criado: $name"
}
