"""QA 2a rodada: migracao 0009 sobre dados JA existentes (bancos que viveram com a corrida das duas raizes)."""

import pytest
from alembic import command
from sqlalchemy import URL

from tests.integration.api.test_migration_0009 import VERSION, _config, _run

pytestmark = pytest.mark.integration


def test_upgrade_0009_over_a_database_that_already_has_two_roots_aborts_clearly_and_changes_nothing(
    migration_url: URL,
) -> None:
    config = _config(migration_url)
    command.upgrade(config, "0008")
    _run(migration_url, VERSION.format(parent="NULL"))
    _run(migration_url, VERSION.format(parent="NULL"))

    with pytest.raises(RuntimeError, match=r"abortada SEM alterar.*raiz id=1.*raiz id=2.*audit_log"):
        command.upgrade(config, "0009")

    assert _run(migration_url, "SELECT version_num FROM alembic_version") == [("0008",)]
    assert _run(migration_url, "SELECT count(*) FROM config_versions WHERE parent_id IS NULL") == [(2,)]
    indexes = _run(migration_url, "SELECT indexname FROM pg_indexes WHERE tablename = 'config_versions'")
    assert ("uq_config_versions_single_root",) not in indexes


def test_upgrade_0009_succeeds_after_the_operator_resolves_the_second_root(migration_url: URL) -> None:
    config = _config(migration_url)
    command.upgrade(config, "0008")
    _run(migration_url, VERSION.format(parent="NULL"))
    _run(migration_url, VERSION.format(parent="NULL"))
    _run(migration_url, "ALTER TABLE config_versions DISABLE TRIGGER config_versions_append_only")
    _run(migration_url, "UPDATE config_versions SET parent_id = 1 WHERE id = 2")
    _run(migration_url, "ALTER TABLE config_versions ENABLE TRIGGER config_versions_append_only")

    command.upgrade(config, "0009")

    assert _run(migration_url, "SELECT count(*) FROM config_versions WHERE parent_id IS NULL") == [(1,)]


def test_upgrade_0009_aborts_on_an_orphan_parent_before_touching_anything(migration_url: URL) -> None:
    config = _config(migration_url)
    command.upgrade(config, "0008")
    _run(migration_url, VERSION.format(parent="NULL"))
    _run(migration_url, VERSION.format(parent="999"))

    with pytest.raises(RuntimeError, match="pai inexistente 999"):
        command.upgrade(config, "0009")

    assert _run(migration_url, "SELECT version_num FROM alembic_version") == [("0008",)]


def test_upgrade_0009_over_a_clean_linear_chain_keeps_every_row_and_downgrades_back(
    migration_url: URL,
) -> None:
    config = _config(migration_url)
    command.upgrade(config, "0008")
    _run(migration_url, VERSION.format(parent="NULL"))
    _run(migration_url, VERSION.format(parent="1"))
    _run(migration_url, VERSION.format(parent="2"))

    command.upgrade(config, "0009")
    assert _run(migration_url, "SELECT count(*) FROM config_versions") == [(3,)]
    command.downgrade(config, "0008")
    assert _run(migration_url, "SELECT count(*) FROM config_versions") == [(3,)]
