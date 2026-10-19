from datetime import UTC, date, datetime


def require_aware(value: datetime, name: str = "now") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def utc_date(value: datetime) -> date:
    return require_aware(value).astimezone(UTC).date()
