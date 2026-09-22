import pytest

from app.infrastructure.config import get_settings
from ops.load.seed import _assert_safe_environment, _load_api_keys


def test_load_test_api_keys_are_required(monkeypatch):
    monkeypatch.delenv("LOAD_TEST_API_KEYS", raising=False)
    with pytest.raises(RuntimeError, match="LOAD_TEST_API_KEYS"):
        _load_api_keys()


def test_load_test_api_keys_are_deduplicated(monkeypatch):
    monkeypatch.setenv(
        "LOAD_TEST_API_KEYS",
        "nrn_load_test_alpha_123456789,nrn_load_test_alpha_123456789,nrn_load_test_beta_123456789",
    )
    assert _load_api_keys() == [
        "nrn_load_test_alpha_123456789",
        "nrn_load_test_beta_123456789",
    ]


def test_load_fixture_refuses_production():
    settings = get_settings()
    original = settings.app_env
    settings.app_env = "production"
    try:
        with pytest.raises(RuntimeError, match="production"):
            _assert_safe_environment()
    finally:
        settings.app_env = original
