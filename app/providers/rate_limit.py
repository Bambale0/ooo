import asyncio
import time
from collections.abc import Awaitable, Callable
from threading import Lock

from app.infrastructure.config import get_settings

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class AsyncRateLimiter:
    """Process-local provider pacing for the single-worker v1 topology."""

    def __init__(
        self,
        rate_per_second: float,
        *,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        self.rate_per_second = max(0.0, rate_per_second)
        self._interval = 0.0 if self.rate_per_second == 0 else 1.0 / self.rate_per_second
        self._clock = clock
        self._sleeper = sleeper
        self._lock = asyncio.Lock()
        self._next_allowed_at = 0.0

    async def acquire(self) -> None:
        if self._interval == 0.0:
            return
        async with self._lock:
            now = self._clock()
            scheduled_at = max(now, self._next_allowed_at)
            wait_seconds = max(0.0, scheduled_at - now)
            self._next_allowed_at = scheduled_at + self._interval
        if wait_seconds > 0:
            await self._sleeper(wait_seconds)


_limiters: dict[tuple[str, str], AsyncRateLimiter] = {}
_limiters_lock = Lock()


def get_provider_rate_limiter(provider: str, operation: str) -> AsyncRateLimiter:
    key = (provider, operation)
    with _limiters_lock:
        existing = _limiters.get(key)
        if existing is not None:
            return existing
        limiter = AsyncRateLimiter(_configured_rate(provider, operation))
        _limiters[key] = limiter
        return limiter


def reset_provider_rate_limiters() -> None:
    with _limiters_lock:
        _limiters.clear()


def _configured_rate(provider: str, operation: str) -> float:
    settings = get_settings()
    if provider in {"argolink", "asale"}:
        if operation == "submit":
            return getattr(settings, f"{provider}_submit_rps")
        if operation == "poll":
            return getattr(settings, f"{provider}_poll_rps")
    raise ValueError(f"Unsupported provider rate limit: {provider}/{operation}")
