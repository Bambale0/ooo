"""Local ArgoLink-compatible stub for worker resilience/load tests.

Run only on loopback in an isolated test environment:

    uvicorn ops.load.provider_stub:app --host 127.0.0.1 --port 18080

Behavior is controlled through environment variables so scenarios remain
reproducible without editing application code.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Response, status
from fastapi.responses import JSONResponse

app = FastAPI(title="Neironych local provider load stub")


@dataclass
class StubState:
    submit_calls: int = 0
    poll_calls: int = 0
    content_calls: int = 0
    rate_limited_submits: int = 0
    rate_limited_polls: int = 0
    server_error_submits: int = 0
    server_error_polls: int = 0
    jobs: dict[str, int] = field(default_factory=dict)


_state = StubState()
_lock = asyncio.Lock()


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = int(raw)
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return value


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = float(raw)
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return value


def _valid_key(authorization: str | None) -> bool:
    if not authorization or not authorization.startswith("Bearer "):
        return False
    token = authorization.removeprefix("Bearer ").strip()
    prefix = os.getenv("LOAD_STUB_KEY_PREFIX", "local-load-partner-")
    return bool(prefix) and token.startswith(prefix)


def _require_auth(authorization: str | None) -> None:
    if not _valid_key(authorization):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


async def _delay() -> None:
    delay_ms = _env_float("LOAD_STUB_LATENCY_MS", 0.0)
    if delay_ms:
        await asyncio.sleep(delay_ms / 1000.0)


def _every(counter: int, env_name: str) -> bool:
    every = _env_int(env_name, 0)
    return every > 0 and counter % every == 0


def _retry_after_headers() -> dict[str, str]:
    seconds = _env_float("LOAD_STUB_RETRY_AFTER_SECONDS", 1.0, minimum=0.001)
    return {"Retry-After": f"{seconds:g}"}


@app.get("/v1/models")
async def models() -> dict[str, object]:
    return {
        "object": "list",
        "data": [
            {
                "id": "seedance-2.5",
                "object": "model",
            }
        ],
    }


@app.get("/v1/usage")
async def usage(authorization: str | None = Header(default=None)) -> dict[str, object]:
    _require_auth(authorization)
    return {
        "isValid": True,
        "mode": "balance",
        "status": "active",
        "quota": 1_000_000,
        "remaining": 1_000_000,
        "unit": "USDT",
    }


@app.post("/v1/videos/generations")
async def submit_generation(
    payload: dict[str, object],
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _require_auth(authorization)
    await _delay()

    async with _lock:
        _state.submit_calls += 1
        call = _state.submit_calls
        if _every(call, "LOAD_STUB_SUBMIT_429_EVERY"):
            _state.rate_limited_submits += 1
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"error": {"message": "synthetic rate limit"}},
                headers=_retry_after_headers(),
            )
        if _every(call, "LOAD_STUB_SUBMIT_500_EVERY"):
            _state.server_error_submits += 1
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"error": {"message": "synthetic submit failure"}},
            )

        request_id = f"load_{uuid4().hex}"
        _state.jobs[request_id] = 0

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "request_id": request_id,
            "status": "pending",
            "model": str(payload.get("model") or "seedance-2.5"),
        },
    )


@app.get("/v1/videos/{request_id}/content")
async def video_content(
    request_id: str,
    authorization: str | None = Header(default=None),
) -> Response:
    _require_auth(authorization)
    await _delay()
    async with _lock:
        if request_id not in _state.jobs:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
        _state.content_calls += 1
    return Response(
        content=b"local-load-stub-video",
        media_type="video/mp4",
        headers={"Accept-Ranges": "bytes"},
    )


@app.get("/v1/videos/{request_id}")
async def poll_generation(
    request_id: str,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _require_auth(authorization)
    await _delay()

    async with _lock:
        if request_id not in _state.jobs:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")

        _state.poll_calls += 1
        call = _state.poll_calls
        if _every(call, "LOAD_STUB_POLL_429_EVERY"):
            _state.rate_limited_polls += 1
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"error": {"message": "synthetic poll rate limit"}},
                headers=_retry_after_headers(),
            )
        if _every(call, "LOAD_STUB_POLL_500_EVERY"):
            _state.server_error_polls += 1
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"error": {"message": "synthetic poll failure"}},
            )

        poll_count = _state.jobs[request_id] + 1
        _state.jobs[request_id] = poll_count

    processing_polls = _env_int("LOAD_STUB_PROCESSING_POLLS", 2)
    if poll_count <= processing_polls:
        return JSONResponse(
            content={
                "request_id": request_id,
                "status": "pending",
                "model": "seedance-2.5",
            }
        )

    return JSONResponse(
        content={
            "request_id": request_id,
            "status": "done",
            "model": "seedance-2.5",
            "usage": {
                "billed_seconds": 5,
                "output_seconds": 5,
                "reference_video_seconds": 0,
            },
        }
    )


@app.get("/__load__/stats")
async def stats() -> dict[str, object]:
    async with _lock:
        return {
            "submit_calls": _state.submit_calls,
            "poll_calls": _state.poll_calls,
            "content_calls": _state.content_calls,
            "rate_limited_submits": _state.rate_limited_submits,
            "rate_limited_polls": _state.rate_limited_polls,
            "server_error_submits": _state.server_error_submits,
            "server_error_polls": _state.server_error_polls,
            "jobs": len(_state.jobs),
        }


@app.post("/__load__/reset")
async def reset(x_load_test_ack: str | None = Header(default=None)) -> dict[str, bool]:
    if x_load_test_ack != "I_UNDERSTAND":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="load_test_ack_required")
    async with _lock:
        _state.submit_calls = 0
        _state.poll_calls = 0
        _state.content_calls = 0
        _state.rate_limited_submits = 0
        _state.rate_limited_polls = 0
        _state.server_error_submits = 0
        _state.server_error_polls = 0
        _state.jobs.clear()
    return {"reset": True}
