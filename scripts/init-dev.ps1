$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
New-Item -ItemType Directory -Force "secrets" | Out-Null

$secret = "secrets/database_password"
if (-not (Test-Path $secret)) {
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $password = [Convert]::ToBase64String($bytes).Replace("+", "A").Replace("/", "B").Replace("=", "")
    [System.IO.File]::WriteAllText((Join-Path $root $secret), $password)
    Write-Host "Segredo de desenvolvimento gerado em $secret"
}
