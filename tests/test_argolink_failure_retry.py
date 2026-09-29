import httpx
import pytest

from app.providers.argolink import ArgoLinkAdapter
from app.providers.base import ProviderPollResult


async def poll_failure(overrides: dict[str, object]) -> ProviderPollResult:
    payload = {
        "status": "failed",
        "error": {"code": "internal_error", "message": "Temporary upstream failure", "retryable": True},
        **overrides,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/videos/failed-task"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(base_url="https://argolink.io", transport=httpx.MockTransport(handler)) as client:
        return await ArgoLinkAdapter(api_key="test-key", client=client).poll_generation("failed-task")


@pytest.mark.parametrize("overrides", [{}, {"usage": None}, {"usage": {}}], ids=["missing", "null", "empty"])
async def test_explicit_retryable_internal_failure_without_usage_allows_retry(overrides):
    result = await poll_failure(overrides)

    assert result.status == "failed"
    assert result.retryable_failure is True
    assert result.error_code == "internal_error"
    assert result.raw_error == "Temporary upstream failure"
    assert result.result_url is None


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"status": "expired"}, id="expired"),
        pytest.param({"status": "error"}, id="error-status"),
        pytest.param({"status": "FAILED"}, id="nonexact-status"),
        pytest.param({"status": "processing"}, id="still-processing"),
        pytest.param({"status": "done"}, id="completed"),
        pytest.param({"error": {"code": "internal_error", "retryable": False}}, id="false"),
        pytest.param({"error": {"code": "internal_error", "retryable": "true"}}, id="string-true"),
        pytest.param({"error": {"code": "internal_error", "retryable": 1}}, id="integer-one"),
        pytest.param({"error": {"code": "internal_error", "retryable": 0}}, id="integer-zero"),
        pytest.param({"error": {"code": "internal_error", "retryable": None}}, id="null-retryable"),
        pytest.param({"error": {"code": "internal_error"}}, id="missing-retryable"),
        pytest.param({"error": {"code": "content_policy_violation", "retryable": True}}, id="moderation"),
        pytest.param({"error": {"code": "INTERNAL_ERROR", "retryable": True}}, id="nonexact-code"),
        pytest.param({"error": {"retryable": True}}, id="missing-code"),
        pytest.param({"error": "internal_error"}, id="unstructured-error"),
        pytest.param({"error": None}, id="null-error"),
        pytest.param({"error": []}, id="list-error"),
        pytest.param({"usage": {"billed_seconds": 5}}, id="billed-usage"),
        pytest.param({"usage": {"billed_seconds": 0}}, id="nonempty-zero-usage"),
        pytest.param({"usage": []}, id="malformed-usage"),
        pytest.param({"video": {"url": "https://cdn.example.test/result.mp4"}}, id="video-result"),
        pytest.param({"video": {"duration": 5}}, id="video-metadata"),
    ],
)
async def test_unconfirmed_failure_or_existing_usage_or_result_does_not_allow_retry(overrides):
    result = await poll_failure(overrides)

    assert result.retryable_failure is False


def test_poll_result_does_not_allow_retry_without_explicit_provider_confirmation():
    assert ProviderPollResult(status="failed", raw_error="internal_error").retryable_failure is False
