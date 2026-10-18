from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import AfterValidator, AwareDatetime, BeforeValidator, Field, StringConstraints


def _reject_float(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("float is not allowed for monetary values; use Decimal or str")
    return value


StrictDecimal = Annotated[Decimal, BeforeValidator(_reject_float), Field(allow_inf_nan=False)]
Price = Annotated[StrictDecimal, Field(gt=0)]
Quantity = Annotated[StrictDecimal, Field(gt=0)]
Volume = Annotated[StrictDecimal, Field(ge=0)]
Fee = Annotated[StrictDecimal, Field(ge=0)]

Symbol = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{2,12}/[A-Z0-9]{2,12}$")]


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]


class Timeframe(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def seconds(self) -> int:
        return _SECONDS[self]


_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.M30: 1800,
    Timeframe.H1: 3600,
    Timeframe.H4: 14400,
    Timeframe.D1: 86400,
}
