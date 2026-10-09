from datetime import UTC, datetime
from decimal import Decimal

from ops.reports import admin_model_stats as report

START = datetime(2026, 10, 9, 6, tzinfo=UTC)
END = datetime(2026, 10, 9, 18, tzinfo=UTC)


def sample(**updates):
    row = {
        'id': 'generation-1', 'model_slug': 'seedance-2.5', 'modality': 'video',
        'status': 'completed', 'provider': 'argolink',
        'provider_usage': {'output_seconds': 10, 'reference_video_seconds': 0, 'billed_seconds': 10},
        'revenue_rub': Decimal('238.00'), 'cost_usd': Decimal('1.96'),
        'fx': Decimal('84.50'), 'uncertain_cost': False, 'historical_rate': False,
    }
    return {**row, **updates}


def test_money_is_added():
    text = report.render_report(START, END, [sample()])
    assert '238,00 ₽' in text
    assert '165,62 ₽' in text
    assert '72,38 ₽' in text
    assert 'до налогов' in text.lower()


def test_edit_uses_billed_cost_but_only_output_for_usage_statistics():
    text = report.render_report(START, END, [sample(
        revenue_rub=Decimal('381.24'), cost_usd=Decimal('3.92'),
        provider_usage={'output_seconds': 10, 'reference_video_seconds': 10, 'billed_seconds': 20},
    )])
    assert '50,00 ₽' in text
    assert '· 10 сек' in text
    assert '· 20 сек' not in text


def test_retries_do_not_double_count_and_failed_cost_is_deducted():
    rows = [sample(), sample(), sample(
        id='failed-1', status='failed', cost_usd=Decimal('0.5'), revenue_rub=Decimal('1000'),
    )]
    totals = report.financials(rows)
    assert totals['revenue'] == Decimal('238')
    assert totals['cost'] == Decimal('165.62')
    assert totals['loss'] == Decimal('42.25')
    assert totals['earnings'] == Decimal('30.13')


def test_unknown_cost_is_not_treated_as_zero():
    rows = [sample(cost_usd=None), sample(id='unknown-submit', status='reconciliation_required',
                                         revenue_rub=Decimal('1428'), uncertain_cost=True)]
    totals = report.financials(rows)
    assert totals['revenue'] == Decimal('238')
    assert totals['earnings'] == 0
    assert totals['unpriced_revenue'] == Decimal('238')
    assert totals['incomplete'] == 1
    assert totals['uncertain'] == 1
    assert 'предварительный' in report.render_report(START, END, rows)


def test_free_generation_still_has_our_cost_and_negative_margin():
    totals = report.financials([sample(revenue_rub=Decimal(0))])
    assert totals['earnings'] == Decimal('-165.62')
    assert '-165,62 ₽' in report.render_report(START, END, [sample(revenue_rub=Decimal(0))])


def test_confirmed_zero_cost_is_not_missing_cost():
    totals = report.financials([sample(cost_usd=Decimal(0))])
    assert totals['earnings'] == Decimal('238')
    assert totals['incomplete'] == 0


def test_reserves_for_in_flight_jobs_are_not_income():
    for status in ('queued', 'submitting', 'processing', 'sent_to_provider', 'reconciliation_required'):
        assert report.financials([sample(status=status)])['earnings'] == 0
        assert report.financials([sample(status=status)])['revenue'] == 0


def test_saved_fx_and_procurement_are_not_repriced():
    rows = [sample(cost_usd=Decimal('1.7'), fx=Decimal('83.296218'), historical_rate=True)]
    totals = report.financials(rows)
    assert totals['cost'] == Decimal('141.60')
    assert totals['earnings'] == Decimal('96.40')
    assert totals['historical'] == 1
    assert 'без пересчёта' in report.render_report(START, END, rows)


def test_missing_duration_does_not_use_ordered_or_total_billed_seconds():
    row = sample(provider_usage={'billed_seconds': 32})
    text = report.render_report(START, END, [row])
    assert 'длительность неизвестна: 1' in text
    assert '· 32 сек' not in text
    assert report.actual_video_seconds('infai', {'billed_seconds': 12}) == Decimal(12)
    assert report.actual_video_seconds('argolink', {'output_seconds': 2.4}) == Decimal('2.4')


def test_invalid_numbers_fail_closed_and_rounding_is_per_task():
    for value in (None, True, False, float('nan'), 1.96, 'NaN', 'Infinity', '-1', 'oops'):
        assert report.number(value) is None
    assert report.money(Decimal('1.005')) == '1,01 ₽'
    assert report.money(Decimal('10000.00')) == '10 000,00 ₽'


def test_empty_period_shows_zero_earnings():
    text = report.render_report(START, END, [])
    assert 'Расчётный заработок: 0,00 ₽' in text
    assert 'завершённых генераций нет' in text


def test_window_boundary_and_message_chunking():
    before = datetime(2026, 10, 9, 5, 59, 59, tzinfo=UTC)
    at = datetime(2026, 10, 9, 6, tzinfo=UTC)
    assert report.closed_window(before)[1] == datetime(2026, 10, 8, 18, tzinfo=UTC)
    assert report.closed_window(at)[1] == at
    assert report.closed_window(END)[1] == END
    parts = report.chunks(('line\n' * 1000) + ('x' * 4000))
    assert len(parts) >= 3
    assert all(0 < len(part) <= 3500 for part in parts)


async def test_database_completion_boundaries_ledger_and_cost_aggregation(db_session):
    from datetime import timedelta

    from app.accounts.models import Partner
    from app.billing.models import LedgerEntry
    from app.catalog.models import Model, PartnerPrice
    from app.generations.models import Generation
    from app.providers.models import ProviderAttempt, ProviderOutcome

    p = Partner(telegram_id='99801', company_name='Report test', project_name='Report test')
    m = Model(slug='report-model', name='Report model', modality='video', status='production')
    db_session.add_all([p, m])
    await db_session.flush()
    db_session.add(PartnerPrice(model_id=m.id, mode='default', resolution='720p',
                                price_rub=Decimal('23.80'), provider_cost_usdt=Decimal('.196'),
                                billing_unit='second'))

    async def generation(finished, *, early_error=False):
        g = Generation(
            partner_id=p.id, model_id=m.id, model_slug=m.slug, mode='videos/generations',
            resolution='720p', duration_seconds=10, status='completed', prompt='Test',
            idempotency_key='report-test:' + finished.isoformat(),
            created_at=START-timedelta(days=1), partner_price_rub=Decimal('238'),
            provider_cost_usdt_snapshot=Decimal('1.96'), rub_per_usdt_snapshot=Decimal('84.50'),
            provider_cost_reserve_rub=Decimal('165.62'), actual_charge_rub=Decimal('238'),
            actual_provider_cost_usdt=Decimal('1.96'),
            request_payload={'rates': {'seconds': {'retail': '23.80', 'cost': '.196'}}},
        )
        db_session.add(g)
        await db_session.flush()
        for operation, amount, created in [
            ('generation_reserve', Decimal('-238'), g.created_at),
            ('generation_usage_adjustment', Decimal('0'), finished),
        ]:
            db_session.add(LedgerEntry(partner_id=p.id, generation_id=g.id, operation_type=operation,
                                       amount_rub=amount, balance_after_rub=Decimal('1000'),
                                       idempotency_key=operation + ':' + g.id, created_at=created))
        db_session.add_all([
            ProviderOutcome(generation_id=g.id, provider='argolink',
                            outcome='error' if early_error else 'success',
                            created_at=g.created_at if early_error else finished),
            ProviderAttempt(generation_id=g.id, provider='argolink', status='completed',
                            provider_task_id='upstream-' + g.id,
                            usage_snapshot={'output_seconds': 10, 'billed_seconds': 10},
                            provider_cost_usdt=Decimal('1.96'), cost_status='settled'),
        ])
        return g

    included = await generation(START, early_error=True)
    await generation(END)
    db_session.add(LedgerEntry(partner_id=p.id, operation_type='payment_credit', amount_rub=Decimal('10000'),
                               balance_after_rub=Decimal('11000'), idempotency_key='report-test-payment',
                               created_at=START))
    await db_session.commit()
    rows = await report.load_rows(db_session, START, END)
    assert [row['id'] for row in rows] == [included.id]
    totals = report.financials(rows)
    assert totals['revenue'] == Decimal('238')
    assert totals['cost'] == Decimal('165.62')
    assert totals['earnings'] == Decimal('72.38')
    assert totals['historical'] == 0


async def test_database_notifications_are_admin_only_and_idempotent(db_session):
    import pytest
    from sqlalchemy import select

    from app.telegram.models import BotNotification

    with pytest.raises(RuntimeError):
        await report.enqueue_report(db_session, None, 'report', 'report-test')
    assert await report.enqueue_report(db_session, '999', 'report', 'report-test')
    await db_session.commit()
    assert not await report.enqueue_report(db_session, '999', 'report again', 'report-test')
    await db_session.commit()
    notifications = list(await db_session.scalars(select(BotNotification)))
    assert len(notifications) == 1
    assert notifications[0].telegram_id == '999'
    assert notifications[0].text == 'report'
