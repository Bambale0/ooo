from decimal import Decimal

from app.contracts.registry import MODELS


def test_seedance_25_reviewed_procurement_matches_provider_tiers():
    entry = MODELS["seedance-2.5"]
    assert entry["procurement"]["unit"] == "second"
    assert entry["procurement"]["generation_per_unit"] == Decimal(".0874")
    assert {tier["label"]: tier["price"] for tier in entry["procurement"]["tiers"]} == {
        "480p": Decimal(".0874"),
        "720p": Decimal(".196"),
        "1080p": Decimal(".483"),
    }
    assert entry["checked_at"] == "2026-10-09"
    assert entry["source"] == "https://argolink.io/en/models/seedance-2.5"


def test_seedance_20_procurement_is_not_changed_by_25_review():
    assert {tier["label"]: tier["price"] for tier in MODELS["seedance-2.0"]["procurement"]["tiers"]} == {
        "480p": Decimal(".053"),
        "720p": Decimal(".11"),
        "1080p": Decimal(".28"),
        "4k": Decimal(".58"),
    }
