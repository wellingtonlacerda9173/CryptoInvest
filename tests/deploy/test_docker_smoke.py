"""Build e smoke das imagens com Docker REAL (pytest -m docker). Tudo efemero: rede, volume e containers proprios
(prefixo `ci-smoke-`), removidos ao fim; nunca toca no Timescale/Redis de dev nem em outros containers."""

import contextlib
import http.client
import shutil
import subprocess
import time
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from engine.infrastructure.persistence.schema_check import expected_head

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker indisponivel"),
]

ROOT = Path(__file__).resolve().parents[2]
TIMESCALE = "timescale/timescaledb:2.30.2-pg17"
BUILDS = [
    ("Dockerfile", "runner"),
    ("Dockerfile", "api"),
    ("Dockerfile", "migrate"),
    ("Dockerfile.web", "web"),
]


def docker(*args: str, check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=timeout)


@pytest.fixture(scope="module", autouse=True)
def images() -> None:
    try:
        docker("info")
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("daemon do docker indisponivel")
    for dockerfile, target in BUILDS:
        docker(
            "build",
            "-q",
            "-f",
            str(ROOT / dockerfile),
            "--target",
            target,
            "-t",
            f"cryptoinvest/{target}:test",
            str(ROOT),
        )


def test_python_images_run_as_non_root_and_ship_no_secrets() -> None:
    for target in ("runner", "api", "migrate"):
        image = f"cryptoinvest/{target}:test"
        assert docker("run", "--rm", "--entrypoint", "id", image, "-u").stdout.strip() == "10001"
        listing = docker(
            "run", "--rm", "--entrypoint", "sh", image, "-c", "ls -A /app; ls -A /run/secrets 2>&1 | head -1"
        ).stdout
        for forbidden in (".env", "secrets", "data", ".git", "tests"):
            assert forbidden not in listing.split(), (target, forbidden)
    assert (
        "usage"
        in docker(
            "run",
            "--rm",
            "--read-only",
            "cryptoinvest/runner:test",
            "python",
            "-m",
            "engine.runner.paper",
            "--help",
        ).stdout
    )
    assert (
        "usage"
        in docker(
            "run", "--rm", "--read-only", "cryptoinvest/api:test", "python", "-m", "api.serve", "--help"
        ).stdout
    )


@contextlib.contextmanager
def web_container() -> Iterator[str]:
    name = f"ci-smoke-web-{uuid.uuid4().hex[:8]}"
    docker(
        "run",
        "-d",
        "--name",
        name,
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--cap-drop",
        "ALL",
        "-e",
        "PANEL_DOMAIN=painel.test",
        "-e",
        "TRUSTED_LB_CIDR=10.0.0.1/32",
        "-p",
        "127.0.0.1::8080",
        "cryptoinvest/web:test",
    )
    try:
        port = docker("port", name, "8080/tcp").stdout.strip().rsplit(":", 1)[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        docker("rm", "-f", "-v", name, check=False)


def fetch(url: str, timeout: int = 3) -> http.client.HTTPResponse:
    request = urllib.request.Request(url, headers={"Host": "painel.test"})  # noqa: S310
    return urllib.request.urlopen(request, timeout=timeout)  # type: ignore[no-any-return]  # noqa: S310


def test_web_serves_spa_with_security_headers_and_no_mock() -> None:
    with web_container() as base:
        for _ in range(30):
            try:
                response = fetch(f"{base}/")
                break
            except OSError:
                time.sleep(1)
        else:
            pytest.fail("web nao subiu")
        csp = response.headers["Content-Security-Policy"]
        assert (
            "sha256-6g9Yr4Z5uQLOKYFWnN3z0UaPj+qslZDOMIpfzLFIiaU=" in csp
            and "sha256-8kvkwELjYxqgmcCymBgnwPIUNqW7QGz4P6+5470rI0g=" in csp
        )
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert fetch(f"{base}/healthz").read() == b"ok\n"
        html = response.read().decode()
        assert "__ciMock" not in html
        with pytest.raises(urllib.error.HTTPError) as err:
            fetch(f"{base}/api/health", timeout=15)
        assert err.value.code == 502


def test_migrate_job_end_to_end_on_throwaway_database() -> None:
    tag = f"ci-smoke-{uuid.uuid4().hex[:8]}"
    network, secrets_volume, database = f"{tag}-net", f"{tag}-sec", f"{tag}-db"
    mount = [
        "--network",
        network,
        "--read-only",
        "--tmpfs",
        "/tmp",
        "-v",
        f"{secrets_volume}:/run/secrets:ro",
    ]
    try:
        docker("network", "create", network)
        docker("volume", "create", secrets_volume)
        docker(
            "run",
            "--rm",
            "--user",
            "root",
            "-v",
            f"{secrets_volume}:/s",
            "--entrypoint",
            "sh",
            "cryptoinvest/migrate:test",
            "-c",
            "cd /s; for n in postgres_password migrator_password app_password api_database_password; do "
            "head -c 33 /dev/urandom | base64 | tr -d '\\n=+/' > $n; done; chmod 0444 *",
        )
        docker(
            "run",
            "-d",
            "--name",
            database,
            "--network",
            network,
            "--network-alias",
            "timescaledb",
            "-v",
            f"{secrets_volume}:/run/secrets:ro",
            "-e",
            "POSTGRES_DB=cryptoinvest",
            "-e",
            "POSTGRES_USER=cryptoinvest",
            "-e",
            "POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password",
            TIMESCALE,
        )
        for _ in range(60):
            if (
                docker(
                    "exec",
                    database,
                    "pg_isready",
                    "-q",
                    "-U",
                    "cryptoinvest",
                    "-d",
                    "cryptoinvest",
                    check=False,
                ).returncode
                == 0
            ):
                break
            time.sleep(2)
        for _ in range(2):  # 1a: superusuario; 2a: papel migrador ja dono (idempotente)
            result = docker("run", "--rm", *mount, "cryptoinvest/migrate:test")
            assert "Permissoes do papel da API conferem" in result.stdout
        owners = docker(
            "exec",
            database,
            "psql",
            "-U",
            "cryptoinvest",
            "-d",
            "cryptoinvest",
            "-tAc",
            "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'",
        ).stdout.split()
        assert owners == ["cryptoinvest_migrator"]
        for role in ("cryptoinvest_app", "cryptoinvest_api"):
            version = docker(
                *("exec", database, "psql", "-U", "cryptoinvest", "-d", "cryptoinvest", "-tAc"),
                f"SET ROLE {role}; SELECT public.schema_version()",
            )
            assert version.stdout.split()[-1] == expected_head(), role
            direct = docker(
                *("exec", database, "psql", "-U", "cryptoinvest", "-d", "cryptoinvest", "-tAc"),
                f"SET ROLE {role}; SELECT * FROM alembic_version",
                check=False,
            )
            assert direct.returncode != 0 and "permission denied" in direct.stderr
    finally:
        docker("rm", "-f", "-v", database, check=False)
        docker("volume", "rm", "-f", secrets_volume, check=False)
        docker("network", "rm", network, check=False)
