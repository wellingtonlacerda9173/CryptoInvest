"""Validacao estatica da implantacao (sem rede e sem Docker): stack, Dockerfiles, .dockerignore, nginx e scripts."""

import base64
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from api.cli import COMMAND_KEY_FILE, MASTER_KEYS_FILE, RECOVERY_KEY_FILE, SESSION_SECRET_FILE
from engine.infrastructure.config.redis_auth import PASSWORD_FILE as REDIS_PASSWORD_FILE

pytestmark = pytest.mark.deploy

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
OVERRIDES = ["stack.http.yml", "stack.tls.yml", "stack.ai.yml"]
LONG_RUNNING = ["timescaledb", "redis", "api", "runner", "web"]


def load(*names: str) -> dict[str, Any]:
    merged: dict[str, Any] = {"services": {}, "secrets": {}, "networks": {}}
    for name in ("stack.yml", *names):
        doc = yaml.safe_load((DEPLOY / name).read_text(encoding="utf-8"))
        for section in ("services", "secrets", "networks"):
            for key, value in (doc.get(section) or {}).items():
                if section == "services" and key in merged["services"]:
                    base = merged["services"][key]
                    for field, extra in value.items():
                        base[field] = base.get(field, []) + extra if isinstance(extra, list) else extra
                else:
                    merged[section][key] = value
    return merged


@pytest.fixture(scope="module")
def stack() -> dict[str, Any]:
    return load()


def secret_targets(service: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in service.get("secrets", []):
        out[item["target"]] = item["source"]
    return out


@pytest.mark.parametrize(
    "combo", [[], ["stack.http.yml"], ["stack.tls.yml"], ["stack.http.yml", "stack.ai.yml"]]
)
def test_every_referenced_secret_is_declared_and_external(combo: list[str]) -> None:
    merged = load(*combo)
    for name, service in merged["services"].items():
        for item in service.get("secrets", []):
            assert item["source"] in merged["secrets"], f"{name}: secret {item['source']} nao declarado"
    for name, definition in merged["secrets"].items():
        assert definition.get("external") is True, f"secret {name} precisa ser externo"
        assert "file" not in definition


def test_no_plaintext_secret_in_stack_files() -> None:
    sensitive = re.compile(r"PASSWORD|SECRET|KEY|TOKEN", re.IGNORECASE)
    for path in DEPLOY.glob("stack*.yml"):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, service in (doc.get("services") or {}).items():
            for var, value in (service.get("environment") or {}).items():
                if sensitive.search(var):
                    assert var.endswith("_FILE"), f"{path.name}:{name}:{var} parece segredo em texto"
                assert not re.fullmatch(r"[A-Za-z0-9+/_=-]{32,}", str(value)), f"{name}:{var} parece segredo"
        text = path.read_text(encoding="utf-8")
        assert "BEGIN PRIVATE KEY" not in text


def test_secret_file_names_match_what_the_code_reads(stack: dict[str, Any]) -> None:
    runner = secret_targets(stack["services"]["runner"])
    api = secret_targets(stack["services"]["api"])
    assert {"database_password", COMMAND_KEY_FILE, REDIS_PASSWORD_FILE} <= runner.keys()
    assert {
        "database_password",
        "api_database_password",
        SESSION_SECRET_FILE,
        MASTER_KEYS_FILE,
        RECOVERY_KEY_FILE,
        COMMAND_KEY_FILE,
        REDIS_PASSWORD_FILE,
    } <= api.keys()
    assert runner[COMMAND_KEY_FILE] == api[COMMAND_KEY_FILE]
    assert runner["database_password"] == api["database_password"] == "app_password"
    assert (
        not {"api_master_keys", "api_session_secret", "postgres_password", "migrator_password"}
        & runner.keys()
    )
    assert "binance_api_key" not in runner and "binance_api_secret" not in runner


def test_migrate_job_gets_only_database_secrets(stack: dict[str, Any]) -> None:
    migrate = stack["services"]["migrate"]
    assert migrate["deploy"]["mode"] == "replicated-job"
    assert set(secret_targets(migrate)) == {
        "postgres_password",
        "migrator_password",
        "app_password",
        "api_database_password",
    }


def test_networks_isolate_database_and_redis(stack: dict[str, Any]) -> None:
    services, networks = stack["services"], stack["networks"]
    assert networks["backend"]["internal"] is True
    assert networks["edge"]["internal"] is True
    assert "internal" not in networks["egress"] and "internal" not in networks["frontend"]
    assert services["timescaledb"]["networks"] == ["backend"]
    assert services["redis"]["networks"] == ["backend"]
    assert services["migrate"]["networks"] == ["backend"]
    assert set(services["web"]["networks"]) == {"frontend", "edge"}
    members = {n: {s for s, d in services.items() if n in d["networks"]} for n in networks}
    assert members["egress"] == {"api", "runner"}
    assert members["frontend"] == {"web"}
    assert members["edge"] == {"web", "api"}
    for name in ("timescaledb", "redis", "api", "runner", "migrate"):
        assert "ports" not in services[name], f"{name} nao pode publicar porta"


def test_trusted_proxies_cover_only_the_edge_network(stack: dict[str, Any]) -> None:
    env = stack["services"]["api"]["environment"]
    assert env["API_TRUSTED_PROXIES"] == f'["{stack["networks"]["edge"]["ipam"]["config"][0]["subnet"]}"]'
    assert env["API_TRUST_FORWARDED_FOR"] == "true"
    assert env["API_COOKIE_SECURE"] == "true"
    assert env["API_ENVIRONMENT"] == "prod"


@pytest.mark.parametrize("name", ["runner", "api", "timescaledb"])
def test_singletons_are_stop_first_with_rollback(stack: dict[str, Any], name: str) -> None:
    deploy = stack["services"][name]["deploy"]
    assert deploy["replicas"] == 1
    assert deploy["update_config"]["order"] == "stop-first"
    assert deploy["update_config"]["parallelism"] == 1
    assert deploy["update_config"]["failure_action"] == "rollback"
    assert deploy["rollback_config"]["order"] == "stop-first"


def test_runner_orderly_shutdown_and_single_run(stack: dict[str, Any]) -> None:
    runner = stack["services"]["runner"]
    assert int(runner["stop_grace_period"].rstrip("s")) >= 60
    assert runner["stop_signal"] == "SIGTERM"
    assert runner["deploy"]["placement"]["max_replicas_per_node"] == 1
    command = runner["command"]
    assert "--run-id" in command and "--data-dir" in command
    assert any("paper-data" in v for v in runner["volumes"] if isinstance(v, str))


@pytest.mark.parametrize("name", LONG_RUNNING)
def test_services_have_healthcheck_restart_and_limits(stack: dict[str, Any], name: str) -> None:
    service = stack["services"][name]
    assert service["healthcheck"]["test"]
    assert service["deploy"]["restart_policy"]["condition"] == "on-failure"
    assert service["deploy"]["resources"]["limits"]["memory"]
    assert service["deploy"]["update_config"]["failure_action"] == "rollback"


def test_hardening_of_application_services(stack: dict[str, Any]) -> None:
    for name in ("api", "runner", "web", "migrate", "redis", "timescaledb"):
        service = stack["services"][name]
        assert service["read_only"] is True, name
        assert str(service["user"]).split(":")[0] not in {"0", "root"}
        assert any(isinstance(v, dict) and v["target"] == "/tmp" for v in service["volumes"]), name
    for name in ("api", "runner", "web", "migrate", "redis", "timescaledb"):
        assert stack["services"][name]["cap_drop"] == ["ALL"]


def test_images_are_pinned_and_never_latest() -> None:
    for path in DEPLOY.glob("stack*.yml"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if match := re.match(r"\s+image:\s*(\S+)", line):
                image = match.group(1)
                assert "latest" not in image, f"{path.name}: {image}"
                tail = image.rsplit("/", 1)[-1]
                assert ":" in tail, f"{path.name}: {image} sem tag"
    for name in ("timescaledb", "redis"):
        assert "@sha256:" in load()["services"][name]["image"]


def stages(path: Path) -> dict[str, dict[str, Any]]:
    """Estagios do Dockerfile com USER e HEALTHCHECK efetivos (herdados do FROM interno)."""
    result: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for raw in re.sub(r"\\\r?\n", " ", path.read_text(encoding="utf-8")).splitlines():
        line = raw.strip()
        if match := re.match(r"FROM\s+(\S+)(?:\s+AS\s+(\S+))?", line, re.IGNORECASE):
            parent = result.get(match.group(1))
            current = {
                "from": match.group(1),
                "user": parent["user"] if parent else None,
                "healthcheck": parent["healthcheck"] if parent else False,
                "lines": [],
            }
            result[match.group(2) or f"stage{len(result)}"] = current
        elif current is not None and line and not line.startswith("#"):
            current["lines"].append(line)
            if line.upper().startswith("USER "):
                current["user"] = line.split(None, 1)[1]
            if line.upper().startswith("HEALTHCHECK "):
                current["healthcheck"] = True
    return result


@pytest.mark.parametrize(
    ("dockerfile", "targets"),
    [
        ("Dockerfile", {"runner": True, "api": True, "migrate": False}),
        ("Dockerfile.web", {"web": True, "web-tls": True}),
    ],
)
def test_dockerfiles_run_as_non_root_with_healthcheck(dockerfile: str, targets: dict[str, bool]) -> None:
    parsed = stages(ROOT / dockerfile)
    for target, needs_healthcheck in targets.items():
        stage = parsed[target]
        assert stage["user"] and stage["user"].split(":")[0] not in {"root", "0"}, f"{target}: USER final"
        if needs_healthcheck:
            assert stage["healthcheck"], f"{target}: sem HEALTHCHECK"
    for name, stage in parsed.items():
        if stage["from"] not in parsed:
            assert "latest" not in stage["from"] and (":" in stage["from"]), f"{name}: base nao fixa"
            assert "@sha256:" in stage["from"] or re.search(r":\d+\.\d+", stage["from"]), name


def test_dockerfiles_have_no_remote_add_or_secrets() -> None:
    for name in ("Dockerfile", "Dockerfile.web"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*ADD\s+https?://", text, re.MULTILINE | re.IGNORECASE)
        assert not re.search(r"^\s*(COPY|ADD)\s+.*(\.env|secrets)", text, re.MULTILINE | re.IGNORECASE)
        assert not re.search(
            r"^\s*(ENV|ARG)\s+\S*(PASSWORD|SECRET|TOKEN|API_KEY)", text, re.MULTILINE | re.IGNORECASE
        )
        assert "curl | sh" not in text and "| bash" not in text


def test_dockerignore_excludes_secrets_data_and_caches() -> None:
    entries = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    required = {".env", ".env.*", "secrets/", "data/", "reports/", ".git", "node_modules", ".hypothesis"}
    assert required <= entries, required - entries


def test_runner_healthcheck_reads_status_freshness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util
    import os
    import time

    spec = importlib.util.spec_from_file_location("ci_healthcheck", DEPLOY / "healthcheck.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    assert module.main(["healthcheck.py", "runner"]) == 1
    (tmp_path / "latest").write_text("paper-main", encoding="utf-8")
    run = tmp_path / "paper-main"
    run.mkdir()
    status = run / "status.json"
    status.write_text("{}", encoding="utf-8")
    assert module.main(["healthcheck.py", "runner"]) == 0
    old = time.time() - 600
    os.utime(status, (old, old))
    assert module.main(["healthcheck.py", "runner"]) == 1
    assert module.main(["healthcheck.py", "outro"]) == 2


def _script_hashes(html: str) -> list[str]:
    found = re.findall(r"<(?:script|style)(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</(?:script|style)>", html)
    return ["sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() for body in found]


def test_csp_hashes_match_docs_and_index_html() -> None:
    headers = (DEPLOY / "web/snippets/headers.conf").read_text(encoding="utf-8")
    docs = (ROOT / "docs/spa-csp-e-mock.md").read_text(encoding="utf-8")
    doc_hashes = set(re.findall(r"'(sha256-[^']+)'", docs))
    conf_hashes = set(re.findall(r"'(sha256-[^']+)'", headers))
    assert len(conf_hashes) == 2
    assert conf_hashes <= doc_hashes
    script_hash = _script_hashes((ROOT / "web/index.html").read_text(encoding="utf-8"))
    assert any(h in conf_hashes for h in script_hash), "hash do script anti-flash nao bate com web/index.html"
    assert "script-src 'self' 'sha256-" in headers and "'unsafe-inline'; img" in headers
    script_src = re.search(r"script-src ([^;]*);", headers)
    assert script_src and "unsafe-inline" not in script_src.group(1)
    for header in ("X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy", "Permissions-Policy"):
        assert header in headers


def test_nginx_proxy_forwards_origin_and_upgrades_websocket() -> None:
    locations = (DEPLOY / "web/snippets/locations.conf").read_text(encoding="utf-8")
    proxy = (DEPLOY / "web/snippets/proxy.conf").read_text(encoding="utf-8")
    assert "location = /api/ws" in locations and "$connection_upgrade" in locations
    assert "proxy_set_header Origin $http_origin;" in proxy
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in proxy
    assert "$proxy_add_x_forwarded_for" not in proxy


@pytest.mark.parametrize("script", ["migrate.sh", "secrets.sh", "backup.sh", "restore.sh"])
def test_shell_scripts_are_syntactically_valid_and_lf(script: str) -> None:
    path = DEPLOY / script
    assert b"\r" not in path.read_bytes()
    if shell := shutil.which("sh"):
        subprocess.run([shell, "-n", str(path)], check=True)


def test_migrate_script_keeps_passwords_out_of_argv() -> None:
    text = (DEPLOY / "migrate.sh").read_text(encoding="utf-8")
    assert "PGPASSFILE" in text and "PGPASSWORD" not in text
    assert not re.search(r"-v\s+\"?\w*password", text, re.IGNORECASE)
    assert "set -x" not in text
    assert "check-grants" in text and "alembic upgrade head" in text and "db_roles.sql" in text


def _run_entrypoint(cidr: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PANEL_DOMAIN": "painel.exemplo.com", "TRUSTED_LB_CIDR": cidr, "WEB_MODE": "http"}
    return subprocess.run(
        [shutil.which("sh") or "sh", str(DEPLOY / "web" / "entrypoint.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.skipif(shutil.which("sh") is None, reason="sh indisponivel")
@pytest.mark.parametrize(
    "cidr", ["0.0.0.0/0", "::/0", "10.0.0.1/32,0.0.0.0/0", "10.0.0.0/00", "10.0.0.5 ::/000"]
)
def test_entrypoint_refuses_a_trusted_lb_cidr_that_matches_everyone(cidr: str) -> None:
    result = _run_entrypoint(cidr)

    assert result.returncode != 0
    assert "/0" in result.stderr and "QUALQUER origem" in result.stderr


@pytest.mark.skipif(shutil.which("sh") is None, reason="sh indisponivel")
def test_entrypoint_does_not_flag_a_specific_lb_cidr_as_open() -> None:
    result = _run_entrypoint("10.0.0.1/32")

    assert "QUALQUER origem" not in result.stderr
