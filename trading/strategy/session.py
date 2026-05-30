from __future__ import annotations

from datetime import datetime, time


SESSION_BUCKET_OPEN_0_10 = "OPEN_0_10"
SESSION_BUCKET_OPEN_10_90 = "OPEN_10_90"
SESSION_BUCKET_MIDDAY = "MIDDAY"
SESSION_BUCKET_LATE = "LATE"
SESSION_BUCKET_UNKNOWN = "UNKNOWN"


def session_bucket_at(value: datetime | str | None) -> str:
    if value is None:
        return SESSION_BUCKET_UNKNOWN
    if isinstance(value, str):
        if not value:
            return SESSION_BUCKET_UNKNOWN
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return SESSION_BUCKET_UNKNOWN
    current = value.time()
    if time(9, 0) <= current < time(9, 10):
        return SESSION_BUCKET_OPEN_0_10
    if time(9, 10) <= current < time(10, 30):
        return SESSION_BUCKET_OPEN_10_90
    if time(10, 30) <= current < time(13, 30):
        return SESSION_BUCKET_MIDDAY
    if current >= time(13, 30):
        return SESSION_BUCKET_LATE
    return SESSION_BUCKET_UNKNOWN
