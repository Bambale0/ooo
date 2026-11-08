from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/kling-4-pricing.yml")


def test_kling_4_pricing_workflow_is_manual_and_production_scoped():
    text = WORKFLOW.read_text(encoding="utf-8")
    config = yaml.safe_load(text)

    assert "workflow_dispatch:" in text
    job = config["jobs"]["apply"]
    assert job["environment"] == "production"
    assert job["concurrency"]["group"] == "production-deploy"
    assert config["permissions"] == {"contents": "read"}
    assert "release_sha must be the exact current main revision" in text
    assert "live production revision does not match release_sha" in text


def test_kling_4_pricing_workflow_only_registers_hidden_prices():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "python -m app.catalog.kling_4 --apply" in text
    assert 'r["status"]=="draft"' in text
    assert 'r["successful_smoke_claimed"] is False' in text
    assert 'len(r["prices"])==4' in text
    assert "/enable" not in text
    assert "smoke --apply" not in text
