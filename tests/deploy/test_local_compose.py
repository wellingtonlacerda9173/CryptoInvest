"""Estaticos do stack LOCAL (docker-compose.yml, deploy/local-up.*): sem Docker e sem rede."""

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.deploy

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
LOCAL_SECRETS = {
    "postgres_password",
    "migrator_password",
    "app_password",
    "api_database_password",
    "redis_password",
    "api_session_secret",
    "api_recovery_hmac_key",
    "command_hmac_key",
    "api_master_keys",
    "live_authorization_hmac_key",
}
OPTIONAL_SECRETS = {"anthropic_api_key"}
PROJECT = "cryptoinvest-local"


def load(name: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return load("docker-compose.yml")


@pytest.fixture(scope="module")
def services(compose: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return compose["services"]  # type: ignore[no-any-return]


def test_local_project_and_resources_are_namespaced(compose: dict[str, Any]) -> None:
    assert compose["name"] == PROJECT
    assert load("docker-compose.dev.yml")["name"] != PROJECT
    for kind in ("volumes", "networks"):
        for key, value in compose[kind].items():
            assert "name" not in (value or {}), f"{kind}.{key}: nome fixo desabilita o prefixo do projeto"
            assert "external" not in (value or {}), f"{kind}.{key}"
    assert all("container_name" not in s for s in compose["services"].values())


def test_local_does_not_collide_with_dev_stack(compose: dict[str, Any]) -> None:
    dev = load("docker-compose.dev.yml")

    def effective(doc: dict[str, Any]) -> dict[str, set[str]]:
        project = doc["name"]
        return {
            "containers": {f"{project}-{s}-1" for s in doc["services"]},
            "volumes": {f"{project}_{v}" for v in doc.get("volumes", {})},
            "networks": {f"{project}_{n}" for n in doc.get("networks", {})} | {f"{project}_default"},
        }

    mine, theirs = effective(compose), effective(dev)
    for kind in mine:
        assert not mine[kind] & theirs[kind], kind

    def host_ports(doc: dict[str, Any]) -> set[str]:
        found: set[str] = set()
        for service in doc["services"].values():
            for port in service.get("ports", []):
                found.add(
                    str(port).rsplit(":", 2)[-2] if str(port).count(":") >= 2 else str(port).split(":")[0]
                )
        return found

    assert not host_ports(compose) & host_ports(dev)
    assert not host_ports(compose) & {"5432", "6379", "8080"}


def test_local_publishes_only_the_panel_on_loopback(services: dict[str, dict[str, Any]]) -> None:
    published = {name: s["ports"] for name, s in services.items() if "ports" in s}
    assert list(published) == ["web"]
    (port,) = published["web"]
    assert isinstance(port, str)
    assert re.fullmatch(r"127\.0\.0\.1:\$\{CRYPTOINVEST_WEB_PORT:-8088\}:8080", port)
    for service in services.values():
        assert "expose" not in service or "0.0.0.0" not in str(service["expose"])  # noqa: S104
        assert service.get("network_mode") not in {"host"}


def test_local_images_are_pinned_and_services_hardened(services: dict[str, dict[str, Any]]) -> None:
    for name, service in services.items():
        image = service["image"]
        assert ":" in image.rsplit("/", 1)[-1], name
        assert ":latest" not in image, name
        if "timescale" in image or "redis" in image:
            assert "@sha256:" in image, name
        user = str(service.get("user", ""))
        assert user and user.split(":")[0] not in {"0", "root"}, f"{name}: precisa de usuario nao root"
        assert service.get("read_only") is True, name
        assert service.get("cap_drop") == ["ALL"], name
        assert "cap_add" not in service and not service.get("privileged"), name
        assert "no-new-privileges:true" in service.get("security_opt", []), name


def test_local_has_no_cleartext_secrets(services: dict[str, dict[str, Any]]) -> None:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for name, service in services.items():
        for key, value in (service.get("environment") or {}).items():
            if re.search(r"PASSWORD|SECRET|KEY|TOKEN", key) and not key.endswith("_FILE"):
                pytest.fail(f"{name}.{key}: segredo em variavel de ambiente")
            assert (
                "@" not in str(value)
                or "://" not in str(value)
                or ":" not in str(value).split("@")[0].split("//")[-1]
            ), (
                name,
                key,
            )
    assert not re.search(
        r"(?i)(password|secret)\s*[:=]\s*['\"]?(?!/run/secrets)[A-Za-z0-9+/]{16,}", text.replace("_FILE", "")
    )
    # nada de ${VAR} para senha: segredo so por arquivo
    assert not re.search(r"\$\{[A-Z_]*(PASSWORD|KEY|TOKEN)[A-Z_]*", text)


def test_local_secrets_are_referenced_exist_and_are_files(
    compose: dict[str, Any], services: dict[str, dict[str, Any]]
) -> None:
    top = compose["secrets"]
    assert set(top) == LOCAL_SECRETS | OPTIONAL_SECRETS
    for name, spec in top.items():
        assert spec["file"] == f"${{CRYPTOINVEST_SECRETS_DIR:-./deploy/local-secrets}}/{name}", name
        assert set(spec) == {"file"}
    used: set[str] = set()
    for service in services.values():
        for entry in service.get("secrets", []):
            source = entry["source"] if isinstance(entry, dict) else entry
            assert source in top, source
            used.add(source)
    assert used == set(top)
    assert services["web"].get("secrets") is None


def test_local_dependencies_wait_for_health_and_migration(services: dict[str, dict[str, Any]]) -> None:
    for name in ("api", "runner"):
        deps = services[name]["depends_on"]
        assert deps["migrate"]["condition"] == "service_completed_successfully"
        assert deps["timescaledb"]["condition"] == "service_healthy"
        assert deps["redis"]["condition"] == "service_healthy"
        assert services[name]["restart"] == "unless-stopped"
        assert "healthcheck" in services[name]
    assert services["migrate"]["restart"] == "no"
    assert services["web"]["depends_on"]["api"]["condition"] == "service_healthy"
    for name in ("timescaledb", "redis", "web"):
        assert "healthcheck" in services[name]
    assert services["runner"]["stop_grace_period"] == "120s"
    assert "deploy" not in services["runner"]


def test_local_runs_in_clearly_local_mode_with_minimal_relaxation(
    services: dict[str, dict[str, Any]],
) -> None:
    api = services["api"]["environment"]
    assert api["API_ENVIRONMENT"] == "dev"
    assert api["API_COOKIE_SECURE"] == "false"
    assert api["API_PANEL_ORIGINS"].startswith('["http://localhost:')
    assert "https" not in api["API_PANEL_ORIGINS"]
    assert "API_TRUST_FORWARDED_FOR" not in api
    relaxed = {k for k in api if k.startswith("API_") and k not in {
        "API_ENVIRONMENT", "API_COOKIE_SECURE", "API_PANEL_ORIGINS", "API_DATABASE_USER", "API_RUNNER_DATA_DIR",
        "API_BACKTEST_CACHE_DIR", "API_BACKTEST_MAX_MEMORY_MB"}}  # fmt: skip
    assert not relaxed
    assert not any("LIVE" in k or "SKIP_SCHEMA" in k for k in api)
    web = services["web"]["environment"]
    assert web["PANEL_DOMAIN"] == "localhost"
    assert web["TRUSTED_LB_CIDR"] == "127.0.0.1/32"
    assert "/0" not in web["TRUSTED_LB_CIDR"]
    # o stack de producao segue exigindo o CIDR do balanceador
    assert ":?" in (DEPLOY / "stack.http.yml").read_text(encoding="utf-8")


def test_local_runner_is_paper_with_closed_calendar_by_default(services: dict[str, dict[str, Any]]) -> None:
    command = services["runner"]["command"]
    assert command[:4] == ["python", "-m", "engine.runner.paper", "start"]
    assert "--allow-open-calendar" not in command
    assert not {"--live", "--selector"} & set(command)
    env = services["runner"]["environment"]
    assert not any(k.startswith("BINANCE") for k in env), "sem chaves da Binance: so dados publicos"
    override = load("deploy/local-open-calendar.yml")["services"]["runner"]["command"]
    assert "--allow-open-calendar" in override
    run_id = override[override.index("--run-id") + 1]
    assert run_id == "paper-local-open"
    assert "paper-local-open" not in str(command)
    header = (DEPLOY / "local-open-calendar.yml").read_text(encoding="utf-8")
    assert "INELEGIVEL" in header and "TESTE" in header


def test_local_tools_profiles_share_image_secrets_and_network(services: dict[str, dict[str, Any]]) -> None:
    api_image = services["api"]["image"]
    entrypoints = {
        "backtest": ["python", "-m", "engine.runner.backtest"],
        "validate": ["python", "-m", "engine.runner.validate"],
        "lessons": ["python", "-m", "engine.runner.lessons"],
        "admin": ["python", "-m", "api.cli"],
    }
    for name, entrypoint in entrypoints.items():
        tool = services[name]
        assert tool["profiles"] == ["tools"], name
        assert tool["entrypoint"] == entrypoint
        assert tool["image"] == api_image
        assert "ports" not in tool and tool["restart"] == "no"
        assert {s["source"] for s in tool["secrets"]} == {s["source"] for s in services["api"]["secrets"]}
        targets = " ".join(str(v) for v in tool["volumes"])
        assert "reports:" in targets and "cache:" in targets
        assert "backend" in tool["networks"]
    for name in ("ai-api", "demo-migrate", "demo-seed"):
        assert services[name]["profiles"] in (["ai"], ["demo"]), name
    for name, service in services.items():
        if name not in {"backtest", "validate", "lessons", "admin", "ai-api", "demo-migrate", "demo-seed"}:
            assert "profiles" not in service, name


def test_local_demo_is_isolated_in_a_separate_database(services: dict[str, dict[str, Any]]) -> None:
    assert services["demo-migrate"]["environment"]["DATABASE_NAME"] == "cryptoinvest_demo"
    assert services["demo-seed"]["environment"]["DATABASE_NAME"] == "cryptoinvest_demo"
    assert services["api"]["environment"]["DATABASE_NAME"] == "${CRYPTOINVEST_DB_NAME:-cryptoinvest}"
    assert services["runner"]["environment"]["DATABASE_NAME"] == "${CRYPTOINVEST_DB_NAME:-cryptoinvest}"
    assert services["migrate"]["environment"]["DATABASE_NAME"] == "cryptoinvest"


def test_local_gitignore_covers_generated_secrets() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "deploy/local-secrets/" in ignored and "backups/" in ignored


# --------------------------------------------------------------------------- scripts


def run_secrets_sh(out: Path) -> subprocess.CompletedProcess[str]:
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("sh indisponivel")
    env = {**os.environ, "CRYPTOINVEST_SECRETS_DIR": str(out)}
    return subprocess.run(
        [shell, str(DEPLOY / "local-up.sh"), "secrets"], capture_output=True, text=True, check=True, env=env
    )


def run_secrets_ps1(out: Path) -> subprocess.CompletedProcess[str]:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None or os.name != "nt":
        pytest.skip("PowerShell do Windows indisponivel")
    env = {**os.environ, "CRYPTOINVEST_SECRETS_DIR": str(out)}
    return subprocess.run(
        [powershell, "-NoProfile", "-File", str(DEPLOY / "local-up.ps1"), "secrets"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )


@pytest.mark.parametrize("runner", [run_secrets_sh, run_secrets_ps1], ids=["sh", "ps1"])
def test_local_up_generates_secrets_once_and_never_overwrites(tmp_path: Path, runner) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "local-secrets"
    first = runner(out)
    files = {p.name: p.read_text(encoding="utf-8") for p in out.iterdir() if not p.name.startswith(".")}
    assert set(files) == LOCAL_SECRETS
    assert not {n for n in files if "binance" in n or "anthropic" in n}
    for name, value in files.items():
        assert value.strip(), name
        if name != "api_master_keys":
            assert len(value.strip()) >= 32, name
    assert len(set(files.values())) == len(files)
    for value in files.values():
        assert value.strip() not in first.stdout + first.stderr
    assert re.fullmatch(r"1:[A-Za-z0-9+/]{43}=\n?", files["api_master_keys"])

    (out / "redis_password").write_text("minha-senha-existente-com-mais-de-32-caracteres", encoding="utf-8")
    runner(out)
    runner(out)
    after = {p.name: p.read_text(encoding="utf-8") for p in out.iterdir() if not p.name.startswith(".")}
    assert after["redis_password"] == "minha-senha-existente-com-mais-de-32-caracteres"
    assert {k: v for k, v in after.items() if k != "redis_password"} == {
        k: v for k, v in files.items() if k != "redis_password"
    }

    if os.name == "nt":
        acl = subprocess.run(["icacls", str(out)], capture_output=True, text=True, check=True).stdout
        assert "Everyone" not in acl and "BUILTIN\\Users" not in acl and "Authenticated Users" not in acl
    else:
        assert stat.S_IMODE(out.stat().st_mode) == 0o700
        for path in out.iterdir():
            assert stat.S_IMODE(path.stat().st_mode) & 0o022 == 0, path.name


def test_local_up_scripts_expose_the_same_subcommands() -> None:
    sh = (DEPLOY / "local-up.sh").read_text(encoding="utf-8")
    ps1 = (DEPLOY / "local-up.ps1").read_text(encoding="utf-8")
    for command in (
        "up",
        "down",
        "reset",
        "logs",
        "status",
        "create-admin",
        "backup",
        "demo",
        "tool",
        "secrets",
    ):
        assert re.search(rf'^\s+"?{command}"?\)? ', sh, re.M) or f"{command})" in sh, command
        assert f'"{command}"' in ps1, command
    for text in (sh, ps1):
        assert "up -d --build --wait" in text
        assert "--password-stdin" in text
        assert "DROP DATABASE" in text and "cryptoinvest_demo" in text
        assert "0.0.0.0" not in text  # noqa: S104
    assert "docker compose -p" in ps1 and '-p "$PROJECT"' in sh
    # a senha nunca vai em argv: so por stdin
    assert "--password " not in sh and "--password " not in ps1
    shell = shutil.which("sh")
    if shell:
        subprocess.run([shell, "-n", str(DEPLOY / "local-up.sh")], check=True)


def test_local_up_reset_requires_typed_confirmation_and_only_touches_the_project() -> None:
    sh = (DEPLOY / "local-up.sh").read_text(encoding="utf-8")
    body = sh[sh.index("cmd_reset()") : sh.index("cmd_create_admin()")]
    assert '[ "$answer" = "$PROJECT" ] || die' in body
    assert body.index("die") < body.index("down -v")
    assert "docker volume rm" not in sh and "docker system prune" not in sh and "docker rm " not in sh
    ps1 = (DEPLOY / "local-up.ps1").read_text(encoding="utf-8")
    body = ps1[ps1.index('"reset" {') : ps1.index('"secrets" {')]
    assert "$answer -ne $Project" in body and body.index("Fail") < body.index("down -v")
    assert "docker volume rm" not in ps1 and "system prune" not in ps1
