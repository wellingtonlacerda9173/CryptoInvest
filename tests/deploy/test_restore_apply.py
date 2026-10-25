"""`restore apply` contra um Timescale EFEMERO (rede `none`, prefixo `ci-restore-`), em .sh e .ps1: nada toca
containers existentes; o container criado e removido ao fim."""

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.deploy.test_static import ROOT

pytestmark = [
    pytest.mark.docker,
    pytest.mark.deploy,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker indisponivel"),
]

IMAGE = "timescale/timescaledb:2.30.2-pg17"
DB = "cryptoinvest"
Run = Callable[..., subprocess.CompletedProcess[str]]


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=300)


def psql(container: str, sql: str, database: str = DB) -> str:
    return docker("exec", container, "psql", "-X", "-U", DB, "-d", database, "-tAc", sql).stdout.strip()


@pytest.fixture(scope="module")
def container() -> Iterator[str]:
    try:
        docker("info")
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("daemon do docker indisponivel")
    name = f"ci-restore-{uuid.uuid4().hex[:8]}"
    docker(
        "run", "-d", "--name", name, "--network", "none",
        "-e", f"POSTGRES_USER={DB}", "-e", "POSTGRES_PASSWORD=ci-throwaway", "-e", f"POSTGRES_DB={DB}",
        IMAGE,
    )  # fmt: skip
    try:
        for _ in range(60):
            logs = docker("logs", name, check=False)
            ready = docker("exec", name, "pg_isready", "-q", "-U", DB, check=False)
            if ready.returncode == 0 and "init process complete" in logs.stdout + logs.stderr:
                break
            time.sleep(2)
        else:
            pytest.fail("timescale efemero nao subiu")
        yield name
    finally:
        docker("rm", "-f", "-v", name, check=False)


def reset_database(container: str) -> None:
    docker(
        "exec",
        container,
        "psql",
        "-X",
        "-U",
        DB,
        "-d",
        "postgres",
        "-qc",
        f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)",
    )
    for stray in psql(
        container,
        "SELECT datname FROM pg_database WHERE datname LIKE '%\\_old\\_%' OR datname LIKE '%\\_restore\\_%'",
        "postgres",
    ).split():
        docker(
            "exec",
            container,
            "psql",
            "-X",
            "-U",
            DB,
            "-d",
            "postgres",
            "-qc",
            f"DROP DATABASE {stray} WITH (FORCE)",
        )
    docker("exec", container, "psql", "-X", "-U", DB, "-d", "postgres", "-qc", f"CREATE DATABASE {DB}")
    psql(
        container,
        "CREATE TABLE alembic_version(version_num text); INSERT INTO alembic_version VALUES ('abc123');"
        "CREATE TABLE precious(x int); INSERT INTO precious VALUES (42), (43)",
    )


def make_backup(container: str, tmp_path: Path) -> Path:
    env = {
        **os.environ,
        "BACKUP_DIR": str(tmp_path / "backups"),
        "TIMESCALE_CONTAINER": container,
        "PAPER_VOLUME": "ci-restore-none",
    }
    subprocess.run(
        ["sh", str(ROOT / "deploy/backup.sh")], env=env, check=True, capture_output=True, timeout=300
    )
    return next((tmp_path / "backups").iterdir())


def reseal(backup: Path) -> None:
    lines = subprocess.run(
        ["sha256sum", "./db.dump", "./alembic_version.txt", "./counts.txt"],
        cwd=backup,
        capture_output=True,
        check=True,
    ).stdout
    (backup / "SHA256SUMS").write_bytes(lines)


def runner(kind: str, tmp_path: Path) -> Run:
    def run(
        backup: Path, container: str, confirm: str | None, *, stopped: bool = True, drift: bool = False
    ) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "SAFETY_DIR": str(tmp_path / "safety"), "STACK_NAME": "ci-restore-none-stack"}
        if kind == "sh":
            cmd = ["sh", str(ROOT / "deploy/restore.sh"), "apply", str(backup), "--container", container]
            if confirm is not None:
                cmd += ["--confirm-database", confirm]
            cmd += ["--i-stopped-services"] * stopped + ["--allow-drift"] * drift
        else:
            cmd = [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "deploy/restore.ps1"),
                "apply",
                str(backup),
                "-Container",
                container,
            ]
            if confirm is not None:
                cmd += ["-ConfirmDatabase", confirm]
            cmd += ["-IStoppedServices"] * stopped + ["-AllowDrift"] * drift
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)

    return run


@pytest.fixture(params=["sh", "ps1"])
def apply(request: pytest.FixtureRequest, tmp_path: Path) -> Run:
    if request.param == "ps1" and shutil.which("powershell") is None:
        pytest.skip("powershell indisponivel")
    return runner(request.param, tmp_path)


def precious(container: str) -> str:
    return psql(container, "SELECT string_agg(x::text, ',' ORDER BY x) FROM precious")


def stray_databases(container: str) -> list[str]:
    return psql(
        container, "SELECT datname FROM pg_database WHERE datname LIKE '%\\_restore\\_%'", "postgres"
    ).split()


def test_invalid_dump_with_coherent_checksums_keeps_original_database(
    container: str, apply: Run, tmp_path: Path
) -> None:
    reset_database(container)
    backup = tmp_path / "bk"
    backup.mkdir()
    (backup / "db.dump").write_text("nao e um dump", encoding="utf-8")
    (backup / "alembic_version.txt").write_text("abc123\n", encoding="utf-8")
    (backup / "counts.txt").write_text("precious|2\n", encoding="utf-8")
    reseal(backup)
    result = apply(backup, container, DB)
    assert result.returncode != 0
    assert "pg_restore --list" in result.stderr
    assert precious(container) == "42,43"
    assert not stray_databases(container)


def test_valid_restore_swaps_database_and_keeps_the_old_one(
    container: str, apply: Run, tmp_path: Path
) -> None:
    reset_database(container)
    backup = make_backup(container, tmp_path)
    psql(container, "DELETE FROM precious; INSERT INTO precious VALUES (7)")
    result = apply(backup, container, DB)
    assert result.returncode == 0, result.stderr + result.stdout
    assert precious(container) == "42,43"
    assert psql(container, "SELECT version_num FROM alembic_version") == "abc123"
    olds = psql(
        container, "SELECT datname FROM pg_database WHERE datname LIKE 'cryptoinvest\\_old\\_%'", "postgres"
    ).split()
    assert len(olds) == 1
    assert psql(container, "SELECT x FROM precious", olds[0]) == "7"
    assert not stray_databases(container)
    safety = tmp_path / "safety"
    assert (safety / "db.dump").stat().st_size > 0 and (safety / "SHA256SUMS").is_file()
    assert olds[0] in result.stdout and "DROP DATABASE" in result.stdout


def test_altered_checksum_is_refused(container: str, apply: Run, tmp_path: Path) -> None:
    reset_database(container)
    backup = make_backup(container, tmp_path)
    with (backup / "db.dump").open("ab") as handle:
        handle.write(b"x")
    psql(container, "DELETE FROM precious WHERE x = 43")
    result = apply(backup, container, DB)
    assert result.returncode != 0
    assert "checksum" in result.stderr.lower() + result.stdout.lower()
    assert precious(container) == "42"


@pytest.mark.parametrize("confirm", [None, "", "outro_banco", "CRYPTOINVEST"])
def test_wrong_or_missing_confirmation_is_refused(
    container: str, apply: Run, tmp_path: Path, confirm: str | None
) -> None:
    reset_database(container)
    backup = make_backup(container, tmp_path)
    psql(container, "DELETE FROM precious WHERE x = 43")
    result = apply(backup, container, confirm)
    assert result.returncode != 0
    assert "confirm" in (result.stderr + result.stdout).lower()
    assert precious(container) == "42"
    assert not (tmp_path / "safety").exists()


def test_refuses_without_stopped_services_before_touching_anything(
    container: str, apply: Run, tmp_path: Path
) -> None:
    reset_database(container)
    backup = make_backup(container, tmp_path)
    psql(container, "DELETE FROM precious WHERE x = 43")
    result = apply(backup, container, DB, stopped=False)
    assert result.returncode != 0
    assert "0 replicas" in result.stderr
    assert precious(container) == "42"
    assert not (tmp_path / "safety").exists()


def test_manifest_divergence_aborts_and_cleans_temporary_database(
    container: str, apply: Run, tmp_path: Path
) -> None:
    reset_database(container)
    backup = make_backup(container, tmp_path)
    (backup / "counts.txt").write_text("precious|999\nalembic_version|1\n", encoding="utf-8")
    reseal(backup)
    psql(container, "DELETE FROM precious WHERE x = 43")
    result = apply(backup, container, DB)
    assert result.returncode != 0
    assert "NAO foi alterado" in result.stderr
    assert precious(container) == "42"
    assert not stray_databases(container)
    assert (tmp_path / "safety" / "db.dump").is_file()


def test_verify_restores_a_backup_in_a_disposable_database_with_sh(container: str, tmp_path: Path) -> None:
    reset_database(container)
    backup = make_backup(container, tmp_path)
    result = subprocess.run(
        ["sh", str(ROOT / "deploy/restore.sh"), "verify", str(backup)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    assert "restauracao verificada" in result.stdout


@pytest.mark.skipif(shutil.which("powershell") is None, reason="powershell indisponivel")
def test_backup_ps1_locks_the_acl_and_its_backup_passes_verify_ps1(container: str, tmp_path: Path) -> None:
    reset_database(container)
    env = {
        **os.environ,
        "BACKUP_DIR": str(tmp_path / "psbackups"),
        "TIMESCALE_CONTAINER": container,
        "PAPER_VOLUME": "ci-restore-none",
    }
    powershell = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    made = subprocess.run(
        [*powershell, str(ROOT / "deploy/backup.ps1")], env=env, capture_output=True, text=True, timeout=300
    )
    assert made.returncode == 0, made.stderr
    backup = next((tmp_path / "psbackups").iterdir())
    acl = subprocess.run(["icacls", str(backup)], capture_output=True, text=True, check=True).stdout
    assert "Everyone" not in acl and "BUILTIN\\Users" not in acl
    verified = subprocess.run(
        [*powershell, str(ROOT / "deploy/restore.ps1"), "verify", str(backup)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert verified.returncode == 0, verified.stderr
    assert "restauracao verificada" in verified.stdout
