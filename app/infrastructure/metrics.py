from collections.abc import Mapping
from datetime import datetime
from time import perf_counter

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

HTTP_REQUESTS_TOTAL = Counter(
    "neironych_http_requests_total",
    "HTTP requests processed by the API.",
    ["method", "route", "status_code"],
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "neironych_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0, 5.0),
)
PROVIDER_REQUESTS_TOTAL = Counter(
    "neironych_provider_requests_total",
    "Provider operations by bounded outcome.",
    ["provider", "operation", "outcome"],
)
PROVIDER_REQUEST_DURATION_SECONDS = Histogram(
    "neironych_provider_request_duration_seconds",
    "Provider operation latency in seconds.",
    ["provider", "operation"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
)
WEBHOOK_DELIVERIES_TOTAL = Counter(
    "neironych_webhook_deliveries_total",
    "Webhook delivery attempts by bounded outcome.",
    ["outcome"],
)
WEBHOOK_DELIVERY_DURATION_SECONDS = Histogram(
    "neironych_webhook_delivery_duration_seconds",
    "Webhook delivery attempt latency in seconds.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
)
GENERATION_QUEUE_DEPTH = Gauge(
    "neironych_generation_queue_depth",
    "Current generation count by queue-relevant status.",
    ["status"],
)
GENERATION_QUEUE_OLDEST_AGE_SECONDS = Gauge(
    "neironych_generation_queue_oldest_age_seconds",
    "Age in seconds of the oldest generation by queue-relevant status.",
    ["status"],
)
DB_POOL_CHECKED_OUT = Gauge(
    "neironych_db_pool_checked_out",
    "Database connections currently checked out.",
)
DB_POOL_SIZE = Gauge(
    "neironych_db_pool_size",
    "Configured SQLAlchemy pool size when supported.",
)
DB_POOL_OVERFLOW = Gauge(
    "neironych_db_pool_overflow",
    "Current SQLAlchemy pool overflow when supported.",
)

_QUEUE_STATUSES = ("queued", "sent_to_provider", "processing", "timeout")


def observe_http_request(*, method: str, route: str, status_code: int, duration_seconds: float) -> None:
    HTTP_REQUESTS_TOTAL.labels(method=method, route=route, status_code=str(status_code)).inc()
    HTTP_REQUEST_DURATION_SECONDS.labels(method=method, route=route).observe(duration_seconds)


def observe_provider_request(
    *,
    provider: str,
    operation: str,
    outcome: str,
    duration_seconds: float,
) -> None:
    PROVIDER_REQUESTS_TOTAL.labels(provider=provider, operation=operation, outcome=outcome).inc()
    PROVIDER_REQUEST_DURATION_SECONDS.labels(provider=provider, operation=operation).observe(duration_seconds)


def observe_webhook_delivery(*, outcome: str, duration_seconds: float) -> None:
    WEBHOOK_DELIVERIES_TOTAL.labels(outcome=outcome).inc()
    WEBHOOK_DELIVERY_DURATION_SECONDS.observe(duration_seconds)


def update_generation_queue_metrics(
    rows: Mapping[str, tuple[int, datetime | None]],
    *,
    now: datetime,
) -> None:
    for status in _QUEUE_STATUSES:
        depth, oldest = rows.get(status, (0, None))
        GENERATION_QUEUE_DEPTH.labels(status=status).set(depth)
        if oldest is None:
            GENERATION_QUEUE_OLDEST_AGE_SECONDS.labels(status=status).set(0)
            continue
        oldest_aware = oldest if oldest.tzinfo is not None else oldest.replace(tzinfo=now.tzinfo)
        GENERATION_QUEUE_OLDEST_AGE_SECONDS.labels(status=status).set(
            max(0.0, (now - oldest_aware).total_seconds())
        )


def refresh_db_pool_metrics(pool: object) -> None:
    checkedout = getattr(pool, "checkedout", None)
    size = getattr(pool, "size", None)
    overflow = getattr(pool, "overflow", None)
    if callable(checkedout):
        DB_POOL_CHECKED_OUT.set(checkedout())
    if callable(size):
        DB_POOL_SIZE.set(size())
    if callable(overflow):
        DB_POOL_OVERFLOW.set(max(0, overflow()))


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


def monotonic_seconds() -> float:
    return perf_counter()
