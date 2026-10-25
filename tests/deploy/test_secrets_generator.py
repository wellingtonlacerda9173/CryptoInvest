"""Gerador de secrets (deploy/secrets.sh e .ps1) em modo dry-run (--out): nada de docker, so arquivos temporarios."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from pydantic import SecretStr

from api.config import ApiSettings
from api.security.crypto import KeyRing

pytestmark = pytest.mark.deploy

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
EXPECTED = {
    "postgres_password",
    "migrator_password",
    "app_password",
    "api_database_password",
    "redis_password",
    "api_session_secret",
    "api_recovery_hmac_key",
    "command_hmac_key",
    "api_master_keys",
}


def run_sh(out: Path) -> subprocess.CompletedProcess[str]:
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("sh indisponivel")
    return subprocess.run(
        [shell, str(DEPLOY / "secrets.sh"), "--out", str(out)], capture_output=True, text=True, check=True
    )


def run_ps1(out: Path) -> subprocess.CompletedProcess[str]:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None or os.name != "nt":
        pytest.skip("PowerShell do Windows indisponivel")
    return subprocess.run(
        [powershell, "-NoProfile", "-File", str(DEPLOY / "secrets.ps1"), "-Out", str(out)],
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.mark.parametrize("runner", [run_sh, run_ps1], ids=["sh", "ps1"])
def test_generates_strong_unique_secrets_without_printing_values(tmp_path: Path, runner) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "secrets"
    result = runner(out)
    files = {p.name: p.read_text(encoding="utf-8") for p in out.iterdir()}
    assert set(files) == EXPECTED
    for name, value in files.items():
        if name != "api_master_keys":
            assert len(value.strip()) >= 32, name
            assert value.strip().isalnum(), name
        assert value.strip() not in result.stdout + result.stderr
    assert len({v.strip() for v in files.values()}) == len(files)
    if os.name == "posix":
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in out.iterdir())

    ring = KeyRing.parse(files["api_master_keys"])
    assert ring.active_version == 1
    settings = ApiSettings(
        _secrets_dir=out,
        session_secret=SecretStr(files["api_session_secret"]),
        master_keys=SecretStr(files["api_master_keys"]),
        command_hmac_key=SecretStr(files["command_hmac_key"]),
        recovery_hmac_key=SecretStr(files["api_recovery_hmac_key"]),
    )
    assert settings.session_secret.get_secret_value() == files["api_session_secret"].strip()


@pytest.mark.parametrize("runner", [run_sh, run_ps1], ids=["sh", "ps1"])
def test_never_overwrites_existing_secrets(tmp_path: Path, runner) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "secrets"
    runner(out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    again = runner(out)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before
    assert again.stdout.count("existe:") == len(EXPECTED)
