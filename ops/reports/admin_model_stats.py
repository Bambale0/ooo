"""Admin-only usage and earnings report, used by the existing twice-daily timer.

Earnings are contribution margin on completed jobs, less known unsuccessful-job
costs. Deposits and unclosed retail holds are not revenue. Use saved actual cost
and accepted FX, never today's price/FX to rewrite historical profitability.
Unknown charges and historical procurement differences are disclosed, not zeroed.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
CENT = Decimal("0.01")
ZERO = Decimal(0)
FINAL_FAILURES = {"failed", "timeout", "cancelled"}


def closed_window(now_utc: datetime) -> tuple[datetime, datetime]:
    local = now_utc.astimezone(MSK)
    candidates = [
        datetime.combine(local.date(), time(9), MSK),
        datetime.combine(local.date(), time(21), MSK),
        datetime.combine(local.date() - timedelta(days=1), time(21), MSK),
    ]
    end = max(item for item in candidates if item <= local)
    return (end - timedelta(hours=12)).astimezone(UTC), end.astimezone(UTC)


def number(value: object, *, positive: bool = False) -> Decimal | None:
    if value is None or isinstance(value, (bool, float)):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        return None
    return result


def actual_video_seconds(provider: str, usage: object) -> Decimal | None:
    if not isinstance(usage, dict):
        return None
    # A provider may report a decimal JSON duration. It is not a money value.
    output = usage.get("output_seconds")
    seconds = number(str(output) if isinstance(output, float) else output, positive=True)
    if seconds is not None:
        return seconds
    if provider == "infai":
        billed = usage.get("billed_seconds")
        return number(str(billed) if isinstance(billed, float) else billed, positive=True)
    return None


def fmt_seconds(value: Decimal) -> str:
    return format(value.normalize(), "f")


def money(value: Decimal) -> str:
    return f"{value.quantize(CENT, rounding=ROUND_HALF_UP):,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def financials(rows: list[dict]) -> dict:
    result = {
        "revenue": ZERO, "cost": ZERO, "margin": ZERO, "loss": ZERO,
        "unpriced_revenue": ZERO, "incomplete": 0, "uncertain": 0, "historical": 0,
        "by_model": defaultdict(lambda: {"margin": ZERO, "incomplete": 0}),
    }
    seen = set()
    for row in rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        status = row["status"]
        cost_usd, fx = number(row.get("cost_usd")), number(row.get("fx"), positive=True)
        cost = (cost_usd * fx).quantize(CENT, rounding=ROUND_HALF_UP) if cost_usd is not None and fx else None
        if row.get("uncertain_cost"):
            result["uncertain"] += 1
        if status == "completed":
            revenue = number(row.get("revenue_rub"))
            model = result["by_model"][row["model_slug"]]
            if revenue is not None:
                result["revenue"] += revenue
            if cost is not None:
                result["cost"] += cost
            if revenue is None or cost is None:
                result["incomplete"] += 1
                model["incomplete"] += 1
                result["unpriced_revenue"] += revenue or ZERO
                # Do not call an unpriced completed job's entire revenue profit.
                continue
            delta = revenue - cost
            result["margin"] += delta
            model["margin"] += delta
            result["historical"] += bool(row.get("historical_rate"))
        elif status in FINAL_FAILURES and cost is not None:
            result["loss"] += cost
    result["earnings"] = result["margin"] - result["loss"]
    return result


def render_report(start: datetime, end: datetime, rows: list[dict]) -> str:
    counts: dict[str, int] = defaultdict(int)
    seconds: dict[str, Decimal] = defaultdict(Decimal)
    unknown: dict[str, int] = defaultdict(int)
    videos: set[str] = set()
    seen = set()
    for row in rows:
        if row["status"] != "completed" or row["id"] in seen:
            continue
        seen.add(row["id"])
        slug = row["model_slug"]
        counts[slug] += 1
        if row["modality"] == "video":
            videos.add(slug)
            duration = actual_video_seconds(row.get("provider", ""), row.get("provider_usage"))
            if duration is None:
                unknown[slug] += 1
            else:
                seconds[slug] += duration
    totals = financials(rows)
    lines = [
        "📊 Нейроныч · генерации и заработок",
        f"Период: {start.astimezone(MSK):%d.%m %H:%M}–{end.astimezone(MSK):%d.%m %H:%M} МСК",
        "",
        f"💰 Расчётный заработок: {money(totals['earnings'])}",
        f"Списано за завершённые генерации: {money(totals['revenue'])}",
        f"Их себестоимость по учёту: {money(totals['cost'])}",
        f"Известные расходы без результата: {money(totals['loss'])}",
        "",
        "По моделям: завершено · секунды готовых видео · прибыль",
    ]
    if not counts:
        lines.append("За период завершённых генераций нет.")
    for slug in sorted(counts):
        line = f"{slug} → {counts[slug]}"
        if slug in videos:
            line += f" · {fmt_seconds(seconds[slug])} сек"
            if unknown[slug]:
                line += f" · длительность неизвестна: {unknown[slug]}"
        item = totals["by_model"][slug]
        line += f" · {money(item['margin'])}"
        if item["incomplete"]:
            line += f" · без расчёта: {item['incomplete']}"
        lines.append(line)
    lines.extend(["", f"Всего завершено: {sum(counts.values())}"])
    if totals["incomplete"]:
        lines.append(
            f"⚠️ Неполный финансовый расчёт: {totals['incomplete']} задач. "
            f"Их выручка {money(totals['unpriced_revenue'])} не считается прибылью."
        )
    if totals["uncertain"]:
        lines.append(
            f"⚠️ Затраты по {totals['uncertain']} задачам периода требуют сверки; "
            "неизвестные расходы не включены. Итог предварительный."
        )
    if totals["historical"]:
        lines.append(
            f"⚠️ У {totals['historical']} задач историческая закупочная ставка отличается "
            "от текущей. Прибыль по учёту, без пересчёта старых списаний."
        )
    lines.extend([
        "",
        "Это прибыль на генерациях до налогов, серверов и комиссий, не сумма для вывода.",
        "Пополнения и незакрытые резервы не считаются доходом. Курс — из снимка заявки.",
    ])
    return "\n".join(lines)


async def load_rows(db, start: datetime, end: datetime) -> list[dict]:
    from sqlalchemy import case, func, select

    from app.billing.models import LedgerEntry
    from app.catalog.models import Model, PartnerPrice
    from app.generations.models import Generation
    from app.providers.models import ProviderAttempt, ProviderOutcome

    # Settlement is durable and also finds late successes whose first circuit
    # outcome was an error. A submit date is never a completed-job date.
    settled = (
        select(LedgerEntry.generation_id, LedgerEntry.partner_id, func.min(LedgerEntry.created_at).label("at"))
        .where(LedgerEntry.operation_type.in_(["generation_usage_adjustment", "generation_charge"]))
        .group_by(LedgerEntry.generation_id, LedgerEntry.partner_id).subquery()
    )
    success = (
        select(ProviderOutcome.generation_id, func.min(ProviderOutcome.created_at).label("at"))
        .where(ProviderOutcome.outcome == "success").group_by(ProviderOutcome.generation_id).subquery()
    )
    terminal = (
        select(ProviderOutcome.generation_id, func.max(ProviderOutcome.created_at).label("at"))
        .group_by(ProviderOutcome.generation_id).subquery()
    )
    finished_at = case(
        (Generation.status == "completed", func.coalesce(settled.c.at, success.c.at)),
        else_=func.coalesce(terminal.c.at, Generation.created_at),
    )
    selected = (await db.execute(
        select(Generation, Model.modality)
        .join(Model, Model.id == Generation.model_id)
        .outerjoin(
            settled, (settled.c.generation_id == Generation.id) & (settled.c.partner_id == Generation.partner_id)
        )
        .outerjoin(success, success.c.generation_id == Generation.id)
        .outerjoin(terminal, terminal.c.generation_id == Generation.id)
        .where(finished_at >= start, finished_at < end)
        .order_by(Generation.model_slug, Generation.id)
    )).all()
    if not selected:
        return []
    ids = [g.id for g, _ in selected]
    attempts = defaultdict(list)
    for a in await db.scalars(select(ProviderAttempt).where(ProviderAttempt.generation_id.in_(ids)).order_by(
        ProviderAttempt.created_at.desc(), ProviderAttempt.id.desc()
    )):
        attempts[a.generation_id].append(a)
    ledger = defaultdict(list)
    for entry in await db.scalars(select(LedgerEntry).where(
        LedgerEntry.generation_id.in_(ids), LedgerEntry.created_at < end,
        LedgerEntry.operation_type.startswith("generation_", autoescape=True),
    )):
        ledger[(entry.generation_id, entry.partner_id)].append(entry)
    tariffs = {
        (p.model_id, p.mode, p.resolution): p.provider_cost_usdt
        for p in await db.scalars(
            select(PartnerPrice).where(PartnerPrice.model_id.in_({g.model_id for g, _ in selected}))
        )
    }
    rows = []
    for g, modality in selected:
        aa = attempts[g.id]
        completed = next((a for a in aa if a.status == "completed"), None)
        entries = ledger[(g.id, g.partner_id)]
        revenue = -sum((e.amount_rub for e in entries), ZERO) if entries else None
        cost = g.actual_provider_cost_usdt
        if cost is None and g.status in FINAL_FAILURES:
            known = [a.provider_cost_usdt for a in aa if a.provider_cost_usdt is not None]
            if known:
                cost = sum(known, ZERO)
        payload = g.request_payload or {}
        resolved = {a.provider_task_id for a in aa if a.cost_status in {"settled", "confirmed_free"}}
        prior_unknown = any(
            item.get("cost_status") == "unknown" and item.get("provider_task_id") not in resolved
            for item in payload.get("provider_failed_tasks", []) if isinstance(item, dict)
        )
        uncertain = (
            g.status == "reconciliation_required"
            or any(a.cost_status == "unknown" for a in aa)
            or prior_unknown
            or bool(g.provider_cost_hold_usdt and g.provider_cost_hold_usdt > 0)
            or (g.status in FINAL_FAILURES and cost is None and any(a.provider_task_id for a in aa))
        )
        policy = payload.get("pricing_policy") or {}
        accepted = number(((payload.get("rates") or {}).get("seconds") or {}).get("cost"))
        current = number(tariffs.get((g.model_id, policy.get("mode", "default"), g.resolution)))
        historical = bool(
            completed and completed.provider == "argolink" and accepted is not None
            and current is not None and accepted != current
        )
        rows.append({
            "id": g.id, "model_slug": g.model_slug, "modality": modality, "status": g.status,
            "provider": completed.provider if completed else "",
            "provider_usage": completed.usage_snapshot if completed else None,
            "revenue_rub": revenue, "cost_usd": cost, "fx": g.rub_per_usdt_snapshot,
            "uncertain_cost": uncertain, "historical_rate": historical,
        })
    return rows


async def build_report(start: datetime, end: datetime) -> str:
    from sqlalchemy import text

    from app.infrastructure.database import SessionLocal

    async with SessionLocal() as db, db.begin():
        if db.bind.dialect.name == "postgresql":
            await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        rows = await load_rows(db, start, end)
    return render_report(start, end, rows)


def chunks(text_value: str, limit: int = 3500) -> list[str]:
    # Leave space for Telegram UTF-16 accounting and preserve complete lines.
    result, current = [], ""
    for line in text_value.splitlines():
        while len(line) > limit:
            if current:
                result.append(current)
                current = ""
            result.append(line[:limit])
            line = line[limit:]
        candidate = current + ("\n" if current else "") + line
        if len(candidate) > limit:
            result.append(current)
            current = line
        else:
            current = candidate
    if current:
        result.append(current)
    return result


async def enqueue_report(db, admin_id: str | None, text_value: str, key: str) -> bool:
    from sqlalchemy import select, text

    from app.telegram.models import BotNotification
    from app.telegram.service import notify

    if not admin_id or not str(admin_id).isdigit():
        raise RuntimeError("admin_telegram_id is not configured; report not delivered")
    if db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})
    if await db.scalar(select(BotNotification.id).where(BotNotification.dedupe_key == key)):
        return False
    for i, part in enumerate(chunks(text_value)):
        await notify(db, str(admin_id), part, key if i == 0 else f"{key}:part:{i + 1}")
    await db.flush()
    return True


async def main() -> None:
    from app.infrastructure.config import get_settings
    from app.infrastructure.database import SessionLocal

    parser = argparse.ArgumentParser()
    parser.add_argument("--rolling", action="store_true", help="Previous 12 hours, not closed 09/21 MSK window.")
    parser.add_argument("--dry-run", action="store_true", help="Print only; do not enqueue a notification.")
    args = parser.parse_args()
    now = datetime.now(UTC)
    start, end = (now - timedelta(hours=12), now) if args.rolling else closed_window(now)
    report = await build_report(start, end)
    if args.dry_run:
        print(report)
        return
    key = f"admin-model-stats:{start.isoformat()}:{end.isoformat()}"
    async with SessionLocal() as db, db.begin():
        sent = await enqueue_report(db, get_settings().admin_telegram_id, report, key)
    print(f"{key} {'enqueued' if sent else 'already-enqueued'}")


if __name__ == "__main__":
    asyncio.run(main())
