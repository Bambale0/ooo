from pathlib import Path


def test_worker_resilience_workflow_is_non_production_and_uses_loopback_stub():
    workflow = Path(".github/workflows/worker-resilience.yml").read_text(encoding="utf-8")

    assert "APP_ENV: test" in workflow
    assert "ARGOLINK_BASE_URL: http://127.0.0.1:18080" in workflow
    assert "LOAD_WORKER_TEST_ACK: I_UNDERSTAND_LOCAL_STUB_ONLY" in workflow
    assert "api.нейроныч.online" not in workflow
    assert "workflow_dispatch:" in workflow


def test_worker_resilience_workflow_covers_retry_after_scenario():
    workflow = Path(".github/workflows/worker-resilience.yml").read_text(encoding="utf-8")

    assert "baseline" in workflow
    assert "retry-after" in workflow
    assert "LOAD_STUB_SUBMIT_429_EVERY" in workflow
    assert "LOAD_STUB_POLL_429_EVERY" in workflow
    assert "python -m ops.load.worker_scenario" in workflow
