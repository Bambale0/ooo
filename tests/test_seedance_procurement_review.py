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
    assert entry["procurement"]["video_input_tiers"] == {
        "480p": Decimal(".0523"),
        "720p": Decimal(".117"),
        "1080p": Decimal(".289"),
    }
    assert entry["procurement"]["extend_tiers"] == {
        "480p": Decimal(".0615"),
        "720p": Decimal(".1379"),
        "1080p": Decimal(".3398"),
    }
    assert entry["procurement"]["draft_tiers"] == {
        "preview_480p": Decimal(".1028"),
        "full_1080p": Decimal(".5682"),
    }
    assert entry["checked_at"] == "2026-10-28"
    assert entry["source"] == "https://argolink.io/en/models/seedance-2.5"


def test_seedance_20_procurement_is_not_changed_by_25_review():
    assert {tier["label"]: tier["price"] for tier in MODELS["seedance-2.0"]["procurement"]["tiers"]} == {
        "480p": Decimal(".053"),
        "720p": Decimal(".11"),
        "1080p": Decimal(".28"),
        "4k": Decimal(".58"),
    }
