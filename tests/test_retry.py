from datetime import UTC, datetime, timedelta

from app.infrastructure.retry import next_retry_at, parse_retry_after_seconds


def test_parse_retry_after_seconds_supports_delta_seconds():
    assert parse_retry_after_seconds("45") == 45.0
    assert parse_retry_after_seconds("0.5") == 0.5


def test_parse_retry_after_seconds_supports_http_date():
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    assert parse_retry_after_seconds(
        "Mon, 21 Sep 2026 12:01:30 GMT",
        now=now,
    ) == 90.0


def test_parse_retry_after_seconds_rejects_invalid_value():
    assert parse_retry_after_seconds("not-a-date") is None
    assert parse_retry_after_seconds(None) is None


def test_next_retry_at_prefers_provider_retry_after_hint():
    before = datetime.now(UTC)
    retry_at = next_retry_at(
        1,
        base_seconds=1.0,
        max_seconds=2.0,
        retry_after_seconds=60.0,
    )
    after = datetime.now(UTC)

    assert before + timedelta(seconds=59.9) <= retry_at
    assert retry_at <= after + timedelta(seconds=60.1)
