from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
from zoneinfo import ZoneInfo
MOSCOW = ZoneInfo("Europe/Moscow")
@dataclass(frozen=True)
class Recurrence:
    value: int
    unit: str
    def __post_init__(self):
        if self.value <= 0 or self.unit not in {"DAYS", "MONTHS", "YEARS"}: raise ValueError("invalid recurrence")
def moscow_to_utc(date_value: str, time_value: str) -> datetime:
    return datetime.fromisoformat(f"{date_value}T{time_value}").replace(tzinfo=MOSCOW).astimezone(timezone.utc)
def add_recurrence(value: datetime, recurrence: Recurrence) -> datetime:
    if value.tzinfo is None: raise ValueError("aware datetime required")
    args={recurrence.unit.lower(): recurrence.value}
    return value + relativedelta(**args)
def remaining_text(end_at: datetime, now: datetime) -> str:
    seconds=max(0, int((end_at-now).total_seconds())); hours, rem=divmod(seconds, 3600); return f"{hours} ч {rem//60} мин"
