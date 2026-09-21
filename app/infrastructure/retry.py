from datetime import UTC, datetime, timedelta
from random import uniform


def utc_now() -> datetime:
    return datetime.now(UTC)


def compute_backoff_delay_seconds(
    retry_count: int,
    *,
    base_seconds: float = 5.0,
    max_seconds: float = 300.0,
    jitter_ratio: float = 0.2,
) -> float:
    exponential = min(base_seconds * (2**max(retry_count, 0)), max_seconds)
    jitter = exponential * jitter_ratio
    return max(0.0, exponential + uniform(-jitter, jitter))


def next_retry_at(
    retry_count: int,
    *,
    base_seconds: float = 5.0,
    max_seconds: float = 300.0,
) -> datetime:
    return utc_now() + timedelta(
        seconds=compute_backoff_delay_seconds(
            retry_count,
            base_seconds=base_seconds,
            max_seconds=max_seconds,
        )
    )


def retry_after_at(retry_after_seconds: float) -> datetime:
    return utc_now() + timedelta(seconds=max(0.0, retry_after_seconds))


def next_poll_at(
    poll_count: int,
    *,
    initial_seconds: float = 5.0,
    base_seconds: float = 5.0,
    max_seconds: float = 30.0,
) -> datetime:
    if poll_count <= 0:
        delay_seconds = initial_seconds
    else:
        delay_seconds = min(base_seconds * (2 ** max(poll_count - 1, 0)), max_seconds)
    return utc_now() + timedelta(seconds=delay_seconds)


def is_due(value: object | None, *, now: datetime | None = None) -> bool:
    if value is None:
        return True
    if not isinstance(value, datetime):
        return False
    deadline = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return deadline <= (now or utc_now())


def is_older_than(value: object | None, seconds: float, *, now: datetime | None = None) -> bool:
    if value is None or not isinstance(value, datetime):
        return False
    started_at = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return started_at + timedelta(seconds=seconds) <= (now or utc_now())
