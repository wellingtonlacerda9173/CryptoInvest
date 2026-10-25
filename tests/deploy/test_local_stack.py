"""Stack LOCAL com Docker REAL (pytest -m docker -k local): sobe o docker-compose.yml com projeto TEMPORARIO
`ci-local-<hex>` (volumes e redes proprios, porta livre em 127.0.0.1) pelo proprio deploy/local-up.*, verifica painel,
API, login com 2FA e a demonstracao, e derruba com `down -v` removendo SO o que criou. Nunca toca no projeto
`cryptoinvest-local`, no `cryptoinvest-dev` nem em outros contêineres."""

import contextlib
import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from api.security.totp import STEP_SECONDS, current_code
from tests.api_support import GOOD_PASSWORD

pytestmark = [
    pytest.mark.docker,
    pytest.mark.deploy,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker indisponivel"),
]

ROOT = Path(__file__).resolve().parents[2]
USERNAME = "admin.local"
PROFILES = ["--profile", "tools", "--profile", "demo", "--profile", "ai"]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LocalStack:
    def __init__(self, project: str, secrets: Path, port: int, tag: str) -> None:
        self.project, self.port, self.tag = project, port, tag
        self.secret: str | None = None
        self.env = {
            **os.environ,
            "CRYPTOINVEST_PROJECT": project,
            "CRYPTOINVEST_SECRETS_DIR": str(secrets),
            "CRYPTOINVEST_WEB_PORT": str(port),
            "CRYPTOINVEST_TAG": tag,
        }
        self.env.pop("CRYPTOINVEST_DB_NAME", None)

    @property
    def base(self) -> str:
        return f"http://localhost:{self.port}"

    def script(self, *args: str, timeout: int = 1500) -> subprocess.CompletedProcess[str]:
        if os.name == "nt":
            powershell = shutil.which("powershell") or "powershell"
            cmd = [powershell, "-NoProfile", "-File", str(ROOT / "deploy" / "local-up.ps1"), *args]
        else:
            cmd = ["sh", str(ROOT / "deploy" / "local-up.sh"), *args]
        return subprocess.run(
            cmd, capture_output=True, text=True, env=self.env, timeout=timeout, cwd=ROOT, check=False
        )

    def compose(
        self, *args: str, stdin: str | None = None, check: bool = True, timeout: int = 300
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["docker", "compose", "-p", self.project, "-f", str(ROOT / "docker-compose.yml"), *args]
        return subprocess.run(
            cmd, input=stdin, capture_output=True, text=True, env=self.env, timeout=timeout, check=check
        )

    def psql(self, database: str, sql: str) -> str:
        out = self.compose(
            "exec", "-T", "timescaledb", "psql", "-X", "-U", "cryptoinvest", "-d", database, "-tAc", sql
        )
        return out.stdout.strip()

    def resources(self) -> list[str]:
        found: list[str] = []
        for kind in (["ps", "-a"], ["volume", "ls"], ["network", "ls"]):
            fmt = "{{.Names}}" if kind[0] == "ps" else "{{.Name}}"
            out = subprocess.run(
                ["docker", *kind, "--format", fmt], capture_output=True, text=True, check=True
            ).stdout.split()
            found += [name for name in out if name.startswith(self.project)]
        return found


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LocalStack]:
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=60)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip("daemon do docker indisponivel")
    suffix = uuid.uuid4().hex[:6]
    secrets = tmp_path_factory.mktemp("local-secrets") / "secrets"
    stk = LocalStack(f"ci-local-{suffix}", secrets, free_port(), f"ci-local-{suffix}")
    try:
        yield stk
    finally:
        stk.compose(*PROFILES, "down", "-v", "--remove-orphans", check=False, timeout=300)
        for image in ("api", "runner", "migrate", "web"):
            subprocess.run(
                ["docker", "rmi", "-f", f"cryptoinvest/{image}:{stk.tag}"], capture_output=True, check=False
            )


def http(stack: LocalStack, path: str, **kwargs: Any) -> httpx.Response:
    return httpx.get(f"{stack.base}{path}", timeout=15, **kwargs)


def totp_at(secret: str, offset_steps: int = 0) -> str:
    moment = datetime.fromtimestamp(time.time() + offset_steps * STEP_SECONDS, UTC)
    return current_code(secret, moment)


@contextlib.contextmanager
def login_session(stack: LocalStack, secret: str | None, token: str | None) -> Iterator[httpx.Client]:
    client = httpx.Client(base_url=f"{stack.base}/api", timeout=30, headers={"Origin": stack.base})
    first = client.post("/auth/login", json={"username": USERNAME, "password": GOOD_PASSWORD})
    assert first.status_code == 200, first.text
    csrf = first.json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}
    if first.json()["stage"] == "enrollment":
        assert token is not None
        begin = client.post("/auth/2fa/enroll", json={"activation_token": token}, headers=headers)
        assert begin.status_code == 200, begin.text
        stack.secret = begin.json()["secret"]
        done = client.post("/auth/2fa/enroll/confirm", json={"code": totp_at(stack.secret)}, headers=headers)
    else:
        assert secret is not None
        done = client.post("/auth/2fa/verify", json={"code": totp_at(secret)}, headers=headers)
    assert done.status_code == 200, done.text
    client.headers["X-CSRF-Token"] = done.json()["csrf_token"]
    try:
        yield client
    finally:
        client.close()


def test_local_stack_end_to_end(stack: LocalStack) -> None:
    up = stack.script("up")
    assert up.returncode == 0, up.stdout[-3000:] + up.stderr[-3000:]
    assert f"http://localhost:{stack.port}" in up.stdout
    assert "create-admin" in up.stdout

    states = {
        row["Service"]: row
        for row in map(json.loads, stack.compose("ps", "-a", "--format", "json").stdout.splitlines())
    }
    for name in ("timescaledb", "redis", "api", "runner", "web"):
        assert states[name]["Health"] == "healthy", states[name]
    assert states["migrate"]["ExitCode"] == 0
    published = [p for row in states.values() for p in row.get("Publishers") or [] if p.get("PublishedPort")]
    assert [(p["URL"], p["PublishedPort"]) for p in published] == [("127.0.0.1", stack.port)]

    assert http(stack, "/healthz").text == "ok\n"
    assert http(stack, "/api/health").json() == {"status": "ok"}
    assert http(stack, "/api/ready").json()["database"] is True
    page = http(stack, "/")
    assert page.status_code == 200 and '<div id="root"' in page.text
    with pytest.raises(httpx.HTTPError):
        http(stack, "/", headers={"Host": "outro.exemplo"})

    secret_files = {
        p.name for p in (Path(stack.env["CRYPTOINVEST_SECRETS_DIR"])).iterdir() if not p.name.startswith(".")
    }
    assert "api_master_keys" in secret_files and "live_authorization_hmac_key" in secret_files

    created = stack.compose(
        "run", "--rm", "-T", "admin", "create-admin", "--username", USERNAME, "--password-stdin",
        stdin=GOOD_PASSWORD + "\n",
    )  # fmt: skip
    token = next(
        line.split(": ", 1)[1].strip()
        for line in created.stdout.splitlines()
        if line.startswith("TOKEN DE ATIVACAO")
    )

    with login_session(stack, None, token) as client:
        enrolled_step = int(time.time()) // STEP_SECONDS
        me = client.get("/auth/me")
        assert me.status_code == 200 and me.json()["username"] == USERNAME and me.json()["totp_enabled"]
        assert client.get("/trades").json()["items"] == []
        status = client.get("/status")
        assert status.status_code == 200, status.text
    secret = stack.secret

    # demonstracao: banco separado, rotulada, inelegivel para o Live; o diario real nunca recebe dados demo
    while int(time.time()) // STEP_SECONDS == enrolled_step:
        time.sleep(1)
    demo = stack.script("demo")
    assert demo.returncode == 0, demo.stdout[-3000:] + demo.stderr[-3000:]
    assert "DEMO" in demo.stdout or "FICTICIOS" in demo.stdout
    assert stack.psql("cryptoinvest", "SELECT count(*) FROM runs WHERE run_id LIKE 'demo-%'") == "0"
    assert stack.psql("cryptoinvest", "SELECT count(*) FROM trades") == "0"
    assert stack.psql("cryptoinvest_demo", "SELECT count(*) FROM runs WHERE run_id = 'demo-paper'") == "1"
    assert int(stack.psql("cryptoinvest_demo", "SELECT count(*) FROM trades")) > 0
    assert stack.compose("ps", "-q", "--status", "running", "runner").stdout.strip() == ""
    again = stack.script("demo")
    assert again.returncode != 0, "demo repetida deve ser recusada"

    with login_session(stack, secret, None) as client:
        trades = client.get("/trades", params={"limit": 200}).json()
        assert trades["items"], "a demo deveria devolver operacoes"
        assert {t["run_id"] for t in trades["items"]} == {"demo-paper"}
        decisions = client.get("/decisions", params={"run_id": "demo-paper"}).json()
        assert any("DEMO" in str(d) for d in decisions["items"])
        gate = client.get("/live-gate", params={"run_id": "demo-paper"}).json()
        assert gate["eligible_for_live"] is False and gate["approved"] is False
        assert gate["live_available"] is False

    removed = stack.script("demo", "--remove" if os.name != "nt" else "-Remove")
    assert removed.returncode == 0, removed.stdout[-3000:] + removed.stderr[-3000:]
    assert (
        stack.psql("postgres", "SELECT count(*) FROM pg_database WHERE datname = 'cryptoinvest_demo'") == "0"
    )
    assert stack.psql("cryptoinvest", "SELECT count(*) FROM trades") == "0"
    assert http(stack, "/api/ready").json()["database"] is True
    assert stack.compose("ps", "-q", "--status", "running", "runner").stdout.strip() != ""

    down = stack.script("down")
    assert down.returncode == 0, down.stderr[-2000:]
    assert stack.compose("ps", "-q").stdout.strip() == ""
    kept = stack.resources()
    assert any(name.endswith("_timescale-data") for name in kept), "down nao pode apagar dados"

    stack.compose(*PROFILES, "down", "-v", "--remove-orphans", timeout=300)
    assert stack.resources() == []
