"""Proxy web com nginx REAL efemero (imagens cryptoinvest/web*:test, rede e containers `ci-nginx-*`, tudo removido ao fim):
limite de /api/auth contra variantes de URI, real_ip configuravel, Host fixo e cache de /assets."""

import collections
import contextlib
import http.client
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.docker,
    pytest.mark.deploy,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker indisponivel"),
]

ROOT = Path(__file__).resolve().parents[2]
DOMAIN = "painel.test"
UPSTREAM_IMAGE = "python:3.12.13-slim-bookworm"
AUTH_VARIANTS = [
    "/api//auth/login",
    "/api/auth%2flogin",
    "/api/auth%2Flogin",
    "/api/%61uth/login",
    "/api/./auth/login",
    "/api/x/../auth/login",
    "//api/auth/login",
]


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=600)


@pytest.fixture(scope="module", autouse=True)
def images() -> None:
    try:
        docker("info")
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("daemon do docker indisponivel")
    for target in ("web", "web-tls"):
        docker(
            "build",
            "-q",
            "-f",
            str(ROOT / "Dockerfile.web"),
            "--target",
            target,
            "-t",
            f"cryptoinvest/{target}:test",
            str(ROOT),
        )


@dataclass
class Net:
    name: str
    subnet: str

    def ip(self, last: int) -> str:
        return self.subnet.rsplit(".", 1)[0] + f".{last}"


@contextlib.contextmanager
def network() -> Iterator[Net]:
    name = f"ci-nginx-net-{uuid.uuid4().hex[:6]}"
    for third in range(200, 240):
        subnet = f"10.{third}.{uuid.uuid4().int % 200 + 20}.0/24"
        created = docker("network", "create", "--subnet", subnet, name, check=False)
        if created.returncode == 0:
            break
    else:
        pytest.skip("sem sub-rede livre para o teste")
    try:
        yield Net(name, subnet)
    finally:
        docker("network", "rm", name, check=False)


@contextlib.contextmanager
def upstream(net: Net) -> Iterator[None]:
    name = f"ci-nginx-api-{uuid.uuid4().hex[:6]}"
    docker(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        net.name,
        "--network-alias",
        "api",
        UPSTREAM_IMAGE,
        "python",
        "-m",
        "http.server",
        "8000",
    )
    try:
        yield
    finally:
        docker("rm", "-f", "-v", name, check=False)


@contextlib.contextmanager
def web(
    net: Net, target: str = "web", env: dict[str, str] | None = None, ip: str | None = None
) -> Iterator[tuple[str, int]]:
    name = f"ci-nginx-web-{uuid.uuid4().hex[:6]}"
    envs = {"PANEL_DOMAIN": DOMAIN, **(env or {})}
    args = [
        "run",
        "-d",
        "--name",
        name,
        "--network",
        net.name,
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--cap-drop",
        "ALL",
        "-p",
        "127.0.0.1::8080",
    ]
    if ip:
        args += ["--ip", ip]
    for key, value in envs.items():
        args += ["-e", f"{key}={value}"]
    docker(*args, f"cryptoinvest/{target}:test")
    try:
        for _ in range(30):
            if (
                docker(
                    "exec",
                    name,
                    "wget",
                    "-q",
                    "-O",
                    "/dev/null",
                    "http://127.0.0.1:8080/healthz",
                    check=False,
                ).returncode
                == 0
            ):
                break
            time.sleep(1)
        else:
            pytest.fail(docker("logs", name, check=False).stderr)
        yield name, int(docker("port", name, "8080/tcp").stdout.strip().rsplit(":", 1)[1])
    finally:
        docker("rm", "-f", "-v", name, check=False)


def burst(port: int, path: str, count: int = 120, host: str = DOMAIN) -> collections.Counter[int]:
    codes: collections.Counter[int] = collections.Counter()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    for _ in range(count):
        try:
            conn.request("GET", path, headers={"Host": host})
            response = conn.getresponse()
            response.read()
            codes[response.status] += 1
        except (http.client.HTTPException, OSError):
            codes[0] += 1
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    return codes


def get(port: int, path: str, host: str = DOMAIN) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    conn.request("GET", path, headers={"Host": host})
    response = conn.getresponse()
    response.read()
    return response


def test_auth_rate_limit_holds_for_canonical_and_every_uri_variant() -> None:
    with network() as net, upstream(net), web(net, env={"TRUSTED_LB_CIDR": net.subnet}) as (_, port):
        canonical = burst(port, "/api/auth/login")
        assert canonical[429] >= 60 and canonical[200] + canonical[404] + canonical[501] >= 20, canonical
        for variant in AUTH_VARIANTS:
            codes = burst(port, variant)
            assert set(codes) <= {400, 429}, (variant, codes)
            assert codes[400] + codes[429] == 120, (variant, codes)
        assert get(port, "/api/auth/login").status in {400, 404, 429, 501}


def test_generic_api_zone_limits_every_api_path_and_does_not_touch_the_panel() -> None:
    with network() as net, upstream(net), web(net, env={"TRUSTED_LB_CIDR": net.subnet}) as (_, port):
        codes = burst(port, "/api/health", 400)
        assert codes[429] >= 200, codes
        time.sleep(2)
        assert burst(port, "/", 100)[429] == 0
        assert burst(port, "/healthz", 100)[429] == 0


def test_real_ip_is_configurable_and_xff_only_counts_from_a_trusted_proxy() -> None:
    with network() as net:
        trusted, untrusted = net.ip(10), net.ip(11)
        with web(net, env={"TRUSTED_LB_CIDR": f"{trusted}/32"}) as (name, _):
            for client, expected in ((trusted, "9.9.9.9"), (untrusted, untrusted)):
                docker(
                    "run", "--rm", "--network", net.name, "--ip", client, "--entrypoint", "wget", "cryptoinvest/web:test",
                    "-q", "-O", "/dev/null", "--header", f"Host: {DOMAIN}", "--header", "X-Forwarded-For: 9.9.9.9", f"http://{name}:8080/",
                )  # fmt: skip
                logs = docker("logs", name, check=False)
                last = [line for line in (logs.stdout + logs.stderr).splitlines() if '"GET / ' in line][-1]
                assert last.startswith(expected + " "), (client, last)


def test_http_image_refuses_to_start_without_trusted_lb_cidr_and_tls_image_does_not_need_it() -> None:
    with network() as net:
        missing = docker(
            "run",
            "--rm",
            "--network",
            net.name,
            "-e",
            f"PANEL_DOMAIN={DOMAIN}",
            "cryptoinvest/web:test",
            check=False,
        )
        assert missing.returncode != 0 and "TRUSTED_LB_CIDR" in missing.stderr
        bad = docker(
            "run",
            "--rm",
            "--network",
            net.name,
            "-e",
            f"PANEL_DOMAIN={DOMAIN}",
            "-e",
            "TRUSTED_LB_CIDR=nao-e-cidr",
            "cryptoinvest/web:test",
            check=False,
        )
        assert bad.returncode != 0 and "TRUSTED_LB_CIDR invalido" in bad.stderr
        no_domain = docker(
            "run",
            "--rm",
            "--network",
            net.name,
            "-e",
            "TRUSTED_LB_CIDR=10.0.0.1/32",
            "cryptoinvest/web:test",
            check=False,
        )
        assert no_domain.returncode != 0 and "PANEL_DOMAIN" in no_domain.stderr
        # o alvo TLS exige os secrets; sem eles falha pelo nginx -t, nunca por TRUSTED_LB_CIDR
        tls = docker(
            "run",
            "--rm",
            "--network",
            net.name,
            "-e",
            f"PANEL_DOMAIN={DOMAIN}",
            "cryptoinvest/web-tls:test",
            check=False,
        )
        assert "TRUSTED_LB_CIDR" not in tls.stderr


def test_unknown_host_is_dropped_and_canonical_host_is_served() -> None:
    with network() as net, web(net, env={"TRUSTED_LB_CIDR": net.subnet}) as (_, port):
        assert get(port, "/", DOMAIN).status == 200
        assert get(port, "/healthz", "127.0.0.1").status == 200
        with pytest.raises((http.client.HTTPException, OSError)):
            get(port, "/", "evil.example.com")
        csp = get(port, "/", DOMAIN).headers["Content-Security-Policy"]
        assert f"wss://{DOMAIN}" in csp and "$" not in csp


def test_assets_are_immutable_only_when_found() -> None:
    with network() as net, web(net, env={"TRUSTED_LB_CIDR": net.subnet}) as (name, port):
        missing = get(port, "/assets/nao-existe.js")
        assert missing.status == 404 and "immutable" not in (missing.headers.get("Cache-Control") or "")
        asset = docker("exec", name, "sh", "-c", "ls /usr/share/nginx/html/assets | head -n 1").stdout.strip()
        found = get(port, f"/assets/{asset}")
        assert found.status == 200 and "immutable" in found.headers["Cache-Control"]
        assert get(port, "/").headers["Cache-Control"] == "no-cache"
