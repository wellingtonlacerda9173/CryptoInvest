from typing import cast

from api.persistence.models import SessionRow, UserRow
from api.schemas import Role, SessionOut, UserOut


def user_out(user: UserRow) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        role=cast("Role", user.role),
        totp_enabled=user.totp_enabled,
        disabled=user.disabled,
        created_at=user.created_at,
        locked_until=user.locked_until,
    )


def session_out(row: SessionRow, *, current: bool) -> SessionOut:
    return SessionOut(
        id=row.id,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        expires_at=row.expires_at,
        ip=row.ip,
        user_agent=row.user_agent,
        current=current,
    )
