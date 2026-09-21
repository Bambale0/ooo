import pytest

from app.providers.rate_limit import AsyncRateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_spaces_requests_without_busy_waiting():
    now = [100.0]
    sleeps: list[float] = []

    def clock() -> float:
        return now[0]

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    limiter = AsyncRateLimiter(2.0, clock=clock, sleeper=sleeper)

    await limiter.acquire()
    await limiter.acquire()
    await limiter.acquire()

    assert sleeps == [0.5, 0.5]


@pytest.mark.asyncio
async def test_zero_rate_disables_pacing():
    sleeps: list[float] = []

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    limiter = AsyncRateLimiter(0.0, sleeper=sleeper)

    await limiter.acquire()
    await limiter.acquire()

    assert sleeps == []
