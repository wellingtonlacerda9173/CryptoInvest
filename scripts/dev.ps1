param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("setup", "up", "down", "migrate", "lint", "typecheck", "test", "test-integration", "check")]
    [string]$Task
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Invoke-Step([scriptblock]$Block) {
    & $Block
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

switch ($Task) {
    "setup"            { Invoke-Step { uv sync }; Invoke-Step { & "$PSScriptRoot/init-dev.ps1" } }
    "up"               { Invoke-Step { & "$PSScriptRoot/init-dev.ps1" }; Invoke-Step { docker compose -f docker-compose.dev.yml up -d --wait } }
    "down"             { Invoke-Step { docker compose -f docker-compose.dev.yml down } }
    "migrate"          { Invoke-Step { uv run alembic upgrade head } }
    "lint"             { Invoke-Step { uv run ruff check . } }
    "typecheck"        { Invoke-Step { uv run mypy --strict engine } }
    "test"             { Invoke-Step { uv run pytest } }
    "test-integration" { Invoke-Step { uv run pytest -m integration } }
    "check"            { Invoke-Step { uv run ruff check . }; Invoke-Step { uv run mypy --strict engine }; Invoke-Step { uv run pytest } }
}
