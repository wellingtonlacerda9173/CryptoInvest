"""QA FINAL: 0009 + 0010 sobre dados existentes (up/down/up) e alembic check."""

import uuid

import pytest
from alembic import command
from sqlalchemy import URL

from tests.integration.api.test_migration_0009 import COMMAND, VERSION, _config, _run

pytestmark = pytest.mark.integration


def test_0010_adds_lock_epoch_default_zero_to_existing_users_and_survives_down_up(migration_url: URL) -> None:
    config = _config(migration_url)
    command.upgrade(config, "0009")
    uid = uuid.uuid4()
    _run(
        migration_url,
        f"INSERT INTO users (id, username, password_hash, role, failed_attempts) VALUES ('{uid}', 'ana', 'x', 'admin', 3)",
    )
    command.upgrade(config, "head")
    command.check(config)
    assert _run(migration_url, "SELECT lock_epoch, failed_attempts FROM users") == [(0, 3)]
    _run(migration_url, "UPDATE users SET lock_epoch = 7")
    command.downgrade(config, "0009")
    cols = {
        r[0]
        for r in _run(
            migration_url, "SELECT column_name FROM information_schema.columns WHERE table_name='users'"
        )
    }
    assert "lock_epoch" not in cols
    assert _run(migration_url, "SELECT failed_attempts FROM users") == [(3,)]
    command.upgrade(config, "head")
    command.check(config)
    assert _run(migration_url, "SELECT lock_epoch FROM users") == [(0,)]


def test_0009_with_data_down_maps_new_states_and_up_again_keeps_the_chain(migration_url: URL) -> None:
    config = _config(migration_url)
    command.upgrade(config, "head")
    _run(migration_url, VERSION.format(parent="NULL"))
    _run(migration_url, VERSION.format(parent="(SELECT min(id) FROM config_versions)"))
    for i, state in enumerate(["applied", "partially_applied", "refused", "failed", "expired"]):
        _run(migration_url, COMMAND.format(id=f"cmd{i:08d}", state=state))
    command.downgrade(config, "0008")
    states = sorted(r[0] for r in _run(migration_url, "SELECT state FROM control_commands"))
    assert states == ["applied", "applied", "expired", "failed", "failed"]
    assert _run(migration_url, "SELECT count(*) FROM config_versions") == [(2,)]
    command.upgrade(config, "head")
    command.check(config)
    assert _run(migration_url, "SELECT count(*) FROM config_versions WHERE parent_id IS NULL") == [(1,)]
