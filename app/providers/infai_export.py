"""Export InfAI procurement by group alongside existing Neironych retail prices."""

import argparse
import asyncio
import csv
import json
from decimal import Decimal
from pathlib import Path

from fastapi.encoders import jsonable_encoder


def write_report(report: dict, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    directory.chmod(0o700)
    data = jsonable_encoder(report, custom_encoder={Decimal: str})
    (directory / "catalog.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = [
        "model",
        "listed_for_account",
        "group",
        "ratio",
        "ratio_source",
        "quota_type",
        "base_input_usd_per_million",
        "base_output_usd_per_million",
        "provider_base_price_usd",
        "retail_variants_rub",
        "request_specific_quote_required",
    ]
    with (directory / "prices-by-group.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for model in data["models"]:
            for quote in model["group_quotes"] or [{}]:
                rates = quote.get("base_text_rates_usd_per_million") or {}
                row = {
                    "model": model["id"],
                    "listed_for_account": model["listed_for_account"],
                    "group": quote.get("group"),
                    "ratio": quote.get("ratio"),
                    "ratio_source": quote.get("ratio_source"),
                    "quota_type": (model["pricing"] or {}).get("quota_type"),
                    "base_input_usd_per_million": rates.get("input"),
                    "base_output_usd_per_million": rates.get("output"),
                    "provider_base_price_usd": quote.get("provider_base_price_usd"),
                    "retail_variants_rub": json.dumps(model.get("retail_variants", []), ensure_ascii=False),
                    "request_specific_quote_required": True,
                }
                # CSV is intended for spreadsheet viewing; provider labels are untrusted.
                writer.writerow(
                    {
                        key: "'" + value
                        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
                        else value
                        for key, value in row.items()
                    }
                )
    for path in directory.iterdir():
        path.chmod(0o600)


async def run(directory: Path) -> None:
    from app.infrastructure.database import SessionLocal, engine
    from app.providers.http_client import close_provider_http_clients
    from app.providers.infai import catalog_with_retail

    try:
        async with SessionLocal() as db:
            report = await catalog_with_retail(db)
        write_report(report, directory)
        print(json.dumps({"counts": report["counts"], "retail_prices_changed": False}))
    finally:
        await close_provider_http_clients()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", required=True, type=Path, help="New private directory; never overwrites reports"
    )
    args = parser.parse_args()
    from app.providers.infai import InfaiError

    try:
        asyncio.run(run(args.output_dir))
    except InfaiError as error:
        raise SystemExit(error.code) from None


if __name__ == "__main__":
    main()
