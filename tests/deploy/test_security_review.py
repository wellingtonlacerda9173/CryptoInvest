"""Revisao de seguranca da implantacao: regras que a suite estatica nao cobria (mutacoes sobreviventes) e
evidencias de defeitos (xfail strict `BUG:`)."""

import re
from typing import Any

import pytest

from tests.deploy.test_static import DEPLOY, load, secret_targets

pytestmark = pytest.mark.deploy


@pytest.fixture(scope="module")
def stack() -> dict[str, Any]:
    return load()


def test_all_mounted_secrets_are_owner_only_for_app_services(stack: dict[str, Any]) -> None:
    for name in ("migrate", "api", "runner"):
        for item in stack["services"][name]["secrets"]:
            assert item["mode"] in (0o400, 256), f"{name}:{item['source']} precisa de mode 0400"
            assert str(item["uid"]) == "10001", f"{name}:{item['source']} precisa de uid 10001"


def test_tls_override_secrets_are_owner_only_for_nginx_user() -> None:
    merged = load("stack.tls.yml")
    for item in merged["services"]["web"]["secrets"]:
        assert item["mode"] in (0o400, 256)
        assert str(item["uid"]) == "101"


def test_least_privilege_secret_distribution(stack: dict[str, Any]) -> None:
    services = stack["services"]
    api = set(secret_targets(services["api"]).values())
    runner = set(secret_targets(services["runner"]).values())
    assert not api & {"postgres_password", "migrator_password"}
    assert runner == {"app_password", "command_hmac_key", "redis_password"}
    assert "secrets" not in services["web"]
    assert set(secret_targets(services["redis"]).values()) == {"redis_password"}
    assert set(secret_targets(services["timescaledb"]).values()) == {"postgres_password"}


def test_no_docker_socket_privileged_or_host_namespaces(stack: dict[str, Any]) -> None:
    for path in DEPLOY.glob("stack*.yml"):
        text = path.read_text(encoding="utf-8")
        assert "docker.sock" not in text
        assert not re.search(r"^\s*(privileged|pid|ipc|userns_mode):", text, re.MULTILINE)
        assert not re.search(r"network_mode|cap_add|devices:", text)
    for name, service in stack["services"].items():
        for volume in service.get("volumes", []):
            source = volume.split(":")[0] if isinstance(volume, str) else volume.get("source", "")
            assert not str(source).startswith(("/", ".")), f"{name}: bind mount do host {source}"


def test_only_the_proxy_publishes_ports_and_always_in_host_mode() -> None:
    for combo in (["stack.http.yml"], ["stack.tls.yml"]):
        merged = load(*combo)
        for name, service in merged["services"].items():
            if name != "web":
                assert "ports" not in service, name
        for port in merged["services"]["web"]["ports"]:
            assert port["mode"] == "host", "o ingress do Swarm faz SNAT e esconde o IP do cliente"
    assert {p["published"] for p in load("stack.tls.yml")["services"]["web"]["ports"]} == {80, 443}


def test_nginx_never_reveals_version_and_tls_is_modern() -> None:
    nginx = (DEPLOY / "web/nginx.conf").read_text(encoding="utf-8")
    tls = (DEPLOY / "web/server-tls.conf").read_text(encoding="utf-8")
    assert "server_tokens off;" in nginx
    assert re.search(r"client_max_body_size\s+\d+k;", nginx)
    assert re.search(r"ssl_protocols\s+TLSv1\.2 TLSv1\.3;", tls)
    assert "Strict-Transport-Security" in tls
    assert "ssl_session_tickets off;" in tls


def test_backup_scripts_create_files_owner_only() -> None:
    for script in ("backup.sh", "migrate.sh", "secrets.sh"):
        assert "umask 077" in (DEPLOY / script).read_text(encoding="utf-8"), script


def test_restore_apply_requires_explicit_confirmation_of_database_name() -> None:
    text = (DEPLOY / "restore.sh").read_text(encoding="utf-8")
    assert '[ "$CONFIRM" = "$DB_NAME" ]' in text
    assert text.index('[ "$CONFIRM" = "$DB_NAME" ]') < text.index("DROP DATABASE")


def test_auth_and_api_rate_limits_are_server_wide_and_keyed_by_normalized_uri() -> None:
    locations = (DEPLOY / "web/snippets/locations.conf").read_text(encoding="utf-8")
    head = locations.split("\nlocation ")[0]
    nginx = (DEPLOY / "web/nginx.conf").read_text(encoding="utf-8")
    assert "limit_req zone=auth" in head
    assert "limit_req zone=api" in head
    assert "return 400" in head
    assert "map $uri $auth_normalized" in nginx and "map $request_uri $auth_literal" in nginx
    assert "limit_req_zone $auth_limit_key" in nginx and "limit_req_zone $api_limit_key" in nginx


def test_stateful_services_drop_capabilities_and_run_non_root(stack: dict[str, Any]) -> None:
    for name in ("timescaledb", "redis"):
        assert stack["services"][name].get("cap_drop") == ["ALL"], name
        assert stack["services"][name]["read_only"] is True, name
    assert str(stack["services"]["timescaledb"]["user"]) == "70:70"
    targets = {v["target"] for v in stack["services"]["timescaledb"]["volumes"] if isinstance(v, dict)}
    assert {"/tmp", "/var/run/postgresql"} <= targets


def test_trusted_lb_cidr_is_required_by_the_http_override_only() -> None:
    http = (DEPLOY / "stack.http.yml").read_text(encoding="utf-8")
    tls = (DEPLOY / "stack.tls.yml").read_text(encoding="utf-8")
    assert "${TRUSTED_LB_CIDR:?" in http
    assert "TRUSTED_LB_CIDR" not in tls


def test_restore_apply_validates_dump_and_swaps_by_rename_instead_of_dropping_the_database() -> None:
    for name in ("restore.sh", "restore.ps1"):
        text = (DEPLOY / name).read_text(encoding="utf-8")
        assert not [line for line in text.splitlines() if "pg_restore" in line and "|| true" in line]
        assert "pg_restore --list" in text and "--exit-on-error" in text
        assert "RENAME TO" in text and "DROP DATABASE IF EXISTS $tmp" in text
        assert "DROP DATABASE IF EXISTS $DB_NAME" not in text and "DROP DATABASE IF EXISTS $db" not in text
