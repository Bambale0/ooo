"""Read-only quota/catalog check by default. Paid smoke requires --execute.

Secrets are loaded from a private JSON file, never from command-line values.
Generated media and private upstream IDs are not written into the public report.
"""

import argparse
import asyncio
import base64
import json
from decimal import Decimal
from pathlib import Path

import httpx

from app.contracts.registry import CATALOG, MODELS, validate_request


def money_json(text):
    return json.loads(text, parse_float=Decimal)


def quota_summary(data):
    quota = data.get("quota") or {}
    return {
        "status": data.get("status"),
        "mode": data.get("mode"),
        "limit_usd": str(quota.get("limit")),
        "used_usd": str(quota.get("used")),
        "remaining_usd": str(quota.get("remaining")),
        "total_actual_usd": str(data.get("usage", {}).get("total", {}).get("actual_cost")),
    }


def catalog_matches(data, models):
    """A targeted smoke reviews only selected entries, including exact prices."""
    if not models:
        return data.get("revision") == CATALOG["revision"]
    live = {entry["id"]: entry for entry in data["items"]}
    for slug in models:
        saved, current = MODELS[slug], live.get(slug)
        if current is None or any(saved[field] != current[field] for field in ("category", "endpoint")):
            return False
        effective = {key: value for key, value in current["pricing"]["effective"].items() if value is not None}
        if effective != saved["procurement"]:
            return False
    return True


def validate_budget(budget, usage):
    if budget is None or not budget.is_finite() or budget <= 0:
        raise SystemExit("--execute requires a finite positive --budget-usd.")
    remaining = (usage.get("quota") or {}).get("remaining")
    if remaining is None:
        if usage.get("mode") != "unrestricted":
            raise SystemExit("Cannot verify the current key quota.")
    elif not Decimal(str(remaining)).is_finite() or budget > Decimal(str(remaining)):
        raise SystemExit("The requested test budget exceeds the current key quota.")


def video_smoke_request(slug, media_url, mode):
    tier = min(MODELS[slug]["procurement"]["tiers"], key=lambda t: Decimal(str(t["price"])))
    duration = 4 if slug.startswith("seedance") or slug == "minimax-h3" else 2 if slug == "wan-3" else 1
    body = {
        "model": slug,
        "prompt": "Animate this reference with a gentle camera move.",
        "duration": duration,
        "resolution": tier["label"],
    }
    if mode == "reference":
        body["reference_images"] = [{"url": media_url}]
    else:
        body["image" if slug.startswith("grok") else "start_image"] = {"url": media_url}
    return validate_request("videos/generations", body), Decimal(str(tier["price"])) * duration


async def run(args):
    if args.model and set(args.model) - MODELS.keys():
        raise SystemExit("Unknown --model; use the reviewed catalog")
    secret = json.loads(await asyncio.to_thread(Path(args.secret_file).read_text))["argolink_key"]
    report_path = Path(args.report)
    if await asyncio.to_thread(report_path.exists):
        raise SystemExit("Use a new report path; existing runs must not be replayed accidentally.")
    report = {"catalog_revision": CATALOG["revision"], "paid_execution": args.execute, "results": []}

    def save():
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    async with httpx.AsyncClient(
        base_url="https://argolink.io", headers={"Authorization": "Bearer " + secret}, timeout=650, trust_env=False
    ) as client:
        before = await client.get("/v1/usage")
        before.raise_for_status()
        usage_before = money_json(before.text)
        report["quota_before"] = quota_summary(usage_before)
        save()
        catalog = await client.get("/api/catalog/v1/models?page_size=100")
        catalog.raise_for_status()
        live_catalog = money_json(catalog.text)
        report["live_catalog_revision"] = live_catalog.get("revision")
        report["reviewed_models"] = args.model or list(MODELS)
        save()
        if not catalog_matches(live_catalog, args.model):
            report["catalog_drift"] = True
            save()
            raise SystemExit("Catalog changed; review contracts before paid testing.")
        if not args.execute:
            return
        if not args.reference:
            raise SystemExit("--execute requires --reference JPEG/PNG.")
        validate_budget(args.budget_usd, usage_before)
        if args.protocol and (not args.model or len(args.model) != 1 or MODELS[args.model[0]]["category"] != "chat"):
            raise SystemExit("--protocol requires exactly one text --model")
        photo = await asyncio.to_thread(Path(args.reference).read_bytes)
        content_type = "image/png" if photo.startswith(b"\x89PNG") else "image/jpeg"
        ticket = await client.post(
            "/v1/media/uploads",
            json={"model": "seedance-2.5", "type": "image", "content_type": content_type, "size_bytes": len(photo)},
        )
        ticket.raise_for_status()
        ticket = ticket.json()
        async with httpx.AsyncClient(timeout=60, trust_env=False) as storage:
            uploaded = await storage.put(ticket["upload_url"], content=photo, headers={"Content-Type": content_type})
            uploaded.raise_for_status()
        held = Decimal(0)
        pending = []
        for slug, entry in MODELS.items():
            if args.model and slug not in args.model:
                continue
            p = entry["procurement"]
            endpoint = args.protocol or entry["endpoint"]
            if entry["category"] == "chat":
                # Includes a margin for hidden reasoning and protocol wrappers.
                estimate = max(
                    Decimal(str(p[k])) for k in ("input_per_million", "cache_write_per_million") if k in p
                ) * Decimal(".008192") + Decimal(str(p["output_per_million"])) * Decimal(".000512")
                body = {"model": slug, "stream": False}
                if endpoint.endswith("responses"):
                    body.update(input="Reply OK.", max_output_tokens=128)
                else:
                    body.update(messages=[{"role": "user", "content": "Reply OK."}], max_tokens=128)
            elif entry["category"] == "image":
                from app.contracts.registry import OBSERVATIONS

                observed = OBSERVATIONS["manual_procurement_review"].get(slug, {})
                estimate = max(
                    [Decimal(str(t["price"])) for t in p["tiers"]]
                    + [Decimal(observed.get("observed_default_edit_usd", "0"))]
                )
                endpoint = "/v1/images/edits"
                body = {
                    "model": slug,
                    "prompt": "Keep the composition. Make one small background color change.",
                    "n": 1,
                    "response_format": "b64_json",
                    "images": [{"image_url": "data:" + content_type + ";base64," + base64.b64encode(photo).decode()}],
                }
            else:
                body, estimate = video_smoke_request(slug, ticket["media_url"], args.video_mode)
            if held + estimate > args.budget_usd:
                report["results"].append({"model": slug, "result": "budget_skipped"})
                save()
                continue
            held += estimate
            report["reserved_estimate_usd"] = str(held)
            save()
            row = {"model": slug, "protocol": endpoint}
            if entry["category"] == "video":
                row.update(mode=args.video_mode, resolution=body["resolution"], duration=body["duration"])
            try:
                response = await client.post(endpoint, json=body)
                row.update(http_status=response.status_code, content_type=response.headers.get("content-type"))
                if "json" not in response.headers.get("content-type", ""):
                    row["result"] = "non_json_response"
                elif not response.is_success:
                    row["result"] = (
                        "rejected" if response.status_code < 500 and response.status_code != 408 else "outcome_unknown"
                    )
                else:
                    data = response.json()
                    if entry["category"] == "video" and data.get("request_id"):
                        pending.append((row, data["request_id"]))
                        row["result"] = "accepted"
                    elif entry["category"] == "image":
                        row.update(
                            result="completed" if data.get("data") else "empty_result",
                            image_count=len(data.get("data", [])),
                        )
                    else:
                        usage = data.get("usage") or {}
                        row["token_usage"] = {k: v for k, v in usage.items() if "cost" not in k}
                        row["result"] = (
                            "completed"
                            if any(
                                usage.get(k, 0) > 0
                                for k in ("input_tokens", "prompt_tokens", "output_tokens", "completion_tokens")
                            )
                            else "unverified_empty_usage"
                        )
            except (httpx.HTTPError, ValueError, KeyError) as error:
                row.update(result="outcome_unknown", exception=type(error).__name__)
            report["results"].append(row)
            save()
        for _ in range(90):
            if not pending:
                break
            for row, task in pending[:]:
                response = await client.get("/v1/videos/" + task)
                if not response.is_success:
                    continue
                data = response.json()
                if data.get("status") in {"done", "failed", "expired"}:
                    row["result"] = data["status"]
                    row["billable_seconds"] = (data.get("usage") or {}).get("billed_seconds") or (
                        data.get("video") or {}
                    ).get("duration")
                    if data["status"] == "done":
                        async with client.stream(
                            "GET", "/v1/videos/" + task + "/content", headers={"Range": "bytes=0-1023"}
                        ) as media:
                            row["content_http_status"] = media.status_code
                            row["content_type"] = media.headers.get("content-type")
                            async for chunk in media.aiter_bytes():
                                row["content_received"] = bool(chunk)
                                break
                            if (
                                media.status_code not in {200, 206}
                                or "video/" not in (row["content_type"] or "")
                                or not row.get("content_received")
                            ):
                                row["result"] = "content_unverified"
                    pending.remove((row, task))
                    save()
            if pending:
                await asyncio.sleep(10)
        for row, _ in pending:
            row["result"] = "poll_timeout"
        after = await client.get("/v1/usage")
        after.raise_for_status()
        report["quota_after"] = quota_summary(money_json(after.text))
        save()
    if any(row["result"] not in {"completed", "done"} for row in report["results"]):
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secret-file", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--reference")
    parser.add_argument("--model", action="append")
    parser.add_argument("--video-mode", choices=["first_frame", "reference"], default="first_frame")
    parser.add_argument("--protocol", choices=["/v1/responses", "/v1/chat/completions", "/v1/messages"])
    parser.add_argument("--budget-usd", type=Decimal)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
