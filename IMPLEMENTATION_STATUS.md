# Состояние реализации — 23.09.2026

Текущая доказательная база: [production review](docs/PRODUCTION_REVIEW.md).
Она заменяет ранние записи о семи тестах, hash-only provider keys и заглушках кабинета.
Production launch отдельно регулируется [чек-листом](PRODUCTION_LAUNCH_CHECKLIST.md).

| Область | Проверенный результат |
| --- | --- |
| Контракты | 39 моделей в reviewed catalog; text JSON/SSE, images JSON/multipart, video/reference/edit, upload tickets |
| Биллинг | Decimal, immutable price/FX snapshots, реальные usage units, idempotent settlement и reconciliation |
| Казначейство | Wallet freshness, working capital, current reserves, safe-to-withdraw, audit withdrawals/corrections |
| FX / alerts | Automatic → manual fallback → last automatic; persistent deficit/economic incidents |
| Кабинет | Заявка/consent, approval/reject, ключи/webhook, счета/credit, история/search, transfer/disable/delete, support/files |
| Безопасность | Tenant isolation, encrypted credentials, protected video proxy, SSRF/DNS pinning, log redaction |
| Queue | Durable intent, no ambiguous paid replay, original credential pin, late settlement, cancellation before submit |
| Recovery | Encrypted logical backup + support files, physical backup + WAL/PITR; оба restore пути проверены |
| Runtime | App + два worker после restart; Redis outage даёт readiness 503 |
| Проверки | 209 tests PASS с PostgreSQL, Ruff, dependency audit, SAST, secret scan; Alembic head 0019 без drift |

Live: **30/39** моделей дали результат хотя бы через один протокол. Все 6 видео-моделей
проверены с предоставленным референсом. Это не PASS всех комбинаций параметров.
Точные неуспехи, квота и расходы — в [live-отчёте](docs/LIVE_VERIFICATION_2026-09-23.md).

Остаются внешние launch gates: исправление нерабочих upstream-конфигураций, проверка
каждого продаваемого режима/цены, production Crypto Pay, deployment/TLS/юридические
документы, независимый РФ backup storage и off-site recovery на реальном объёме.

Расширенные продуктовые требования, не входящие в подтверждённую выше реализацию:
бесплатные пробные генерации с lifetime eligibility, полноценный provider circuit
breaker/staged recovery, настраиваемые low-margin thresholds с тремя уровнями,
уведомления о смене условий и полный административный UI для всех API-операций.
Их нельзя считать выполненными по зелёному CI. Полный PASS всей продуктовой
спецификации или всех launch gates в этой проверке не выдан.
