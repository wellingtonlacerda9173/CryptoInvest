"""uma unica raiz em config_versions, FK do pai e novos resultados de comando

- `uq_config_versions_single_root`: indice unico parcial `((true)) WHERE parent_id IS NULL`. O UNIQUE(parent_id)
  da 0007 nao protege a 1a versao (NULL e distinto de NULL): duas gravacoes concorrentes viravam duas raizes.
- `fk_config_versions_parent`: o pai de uma versao tem de existir (cadeia de hashes sem elo solto).
- `ck_control_commands_state`: aceita `partially_applied` e `refused` (o runner passa a dizer o que de fato
  aconteceu; recusa nunca vira 'aplicado').

Precondicao (bancos que viveram a corrida das duas raizes): a migracao ABORTA, sem alterar nada, se houver
mais de uma raiz ou pai inexistente em `config_versions`. Nao ha saneamento automatico de proposito: a tabela
e append-only (trigger) e cada `chain_hash` encadeia o do pai; reencadear uma raiz concorrente sob a outra
exigiria desligar a imutabilidade e reescrever hash e linhagem de versoes cujo `diff` foi calculado contra
outra base, ou seja, falsificar silenciosamente o historico. A mensagem de erro traz o diagnostico e o
procedimento manual (com backup e registro) para o operador decidir qual linhagem e a canonica.

Downgrade: `refused` volta como `failed` e `partially_applied` como `applied` (os estados antigos), antes de
restaurar a restricao anterior.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATES = "state IN ('pending_delivery', 'delivered', 'applied', 'discarded', 'expired', 'failed')"
NEW_STATES = (
    "state IN ('pending_delivery', 'delivered', 'applied', 'partially_applied', 'refused', "
    "'discarded', 'expired', 'failed')"
)


def _refuse_unsafe_history() -> None:
    bind = op.get_bind()
    roots = bind.execute(
        sa.text("SELECT id, created_at, actor FROM config_versions WHERE parent_id IS NULL ORDER BY id")
    ).all()
    orphans = bind.execute(
        sa.text(
            "SELECT v.id, v.parent_id FROM config_versions v "
            "WHERE v.parent_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM config_versions p WHERE p.id = v.parent_id) "
            "ORDER BY v.id"
        )
    ).all()
    if len(roots) <= 1 and not orphans:
        return
    found = "; ".join(f"raiz id={r.id} ({r.created_at:%Y-%m-%d %H:%M} por {r.actor})" for r in roots)
    lost = "; ".join(f"id={o.id} aponta para pai inexistente {o.parent_id}" for o in orphans)
    raise RuntimeError(
        "Migracao 0009 abortada SEM alterar o banco: config_versions tem historico que a regra de raiz unica "
        f"e a FK do pai nao aceitam. Encontrado: {found or 'nenhuma raiz'}{'; ' + lost if lost else ''}. "
        "O historico e append-only e encadeado por hash; esta migracao nao o reescreve. Procedimento: "
        "1) pare a API e o runner e faca backup; 2) decida qual linhagem e a canonica; 3) como administrador "
        "do banco, desligue o trigger config_versions_append_only, ligue cada versao da linhagem descartada "
        "ao seu novo pai (ou remova-a) registrando o que foi feito e o porque em audit_log, e religue o "
        "trigger; 4) rode `alembic upgrade head` de novo."
    )


def upgrade() -> None:
    _refuse_unsafe_history()
    op.execute(
        "CREATE UNIQUE INDEX uq_config_versions_single_root ON config_versions ((true)) WHERE parent_id IS NULL"
    )
    op.create_foreign_key(
        "fk_config_versions_parent", "config_versions", "config_versions", ["parent_id"], ["id"]
    )
    op.drop_constraint("ck_control_commands_state", "control_commands", type_="check")
    op.create_check_constraint("ck_control_commands_state", "control_commands", NEW_STATES)


def downgrade() -> None:
    op.execute("UPDATE control_commands SET state = 'failed' WHERE state = 'refused'")
    op.execute("UPDATE control_commands SET state = 'applied' WHERE state = 'partially_applied'")
    op.drop_constraint("ck_control_commands_state", "control_commands", type_="check")
    op.create_check_constraint("ck_control_commands_state", "control_commands", OLD_STATES)
    op.drop_constraint("fk_config_versions_parent", "config_versions", type_="foreignkey")
    op.execute("DROP INDEX uq_config_versions_single_root")
