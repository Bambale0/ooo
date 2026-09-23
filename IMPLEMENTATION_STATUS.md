# Состояние реализации — 23.09.2026

Текущая доказательная база: [production review](docs/PRODUCTION_REVIEW.md).
Она заменяет ранние записи о семи тестах, hash-only provider keys и заглушках кабинета.
Production launch отдельно регулируется [чек-листом](PRODUCTION_LAUNCH_CHECKLIST.md).

| Область | Проверенный результат |
| --- | --- |
| Контракты | 39 моделей в reviewed catalog; text JSON/SSE, images JSON/multipart, video/reference/edit, upload tickets |
| Биллинг | Decimal, immutable price/FX snapshots, реальные usage units, idempotent settlement и reconciliation |
| Казначейство | Wallet freshness, working capital, current reserves, safe-to-withdraw, audit withdrawals/corrections |
| FX / alerts | Automatic → manual fallback → last automatic; persistent deficit/economic/low-margin incidents, threshold hierarchy/history |
| Кабинет | Заявка/consent, approval/reject, ключи/webhook, счета/credit, история/search, transfer/disable/delete, support/files |
| Безопасность | Tenant isolation, encrypted credentials, protected video proxy, SSRF/DNS pinning, log redaction |
| Queue | Durable intent, no ambiguous paid replay, original credential pin, late settlement, cancellation before submit |
| Trial / legal | Два lifetime запуска на Telegram ID, 0 ₽ при реальном учёте затрат; versioned consent и persistent notices |
| Provider recovery | Durable circuit, три бесплатные проверки, затем три реальные задачи по одной; без платных synthetic probes |
| Admin UI | Guided confirmations: цены/пороги, refund/withdrawal/corrections, сверки, модельные gates/capabilities и encrypted credential provisioning |
| Recovery | Encrypted logical backup + support files, physical backup + WAL/PITR; оба restore пути проверены |
| Runtime | App + два worker после restart; Redis outage даёт readiness 503 |
| Проверки | 223 tests PASS, включая PostgreSQL concurrency, Ruff, dependency audit, SAST, secret scan; Alembic head 0020 без drift |

Live: **30/39** моделей дали результат хотя бы через один протокол. Все 6 видео-моделей
проверены с предоставленным референсом. Это не PASS всех комбинаций параметров.
Точные неуспехи, квота и расходы — в [live-отчёте](docs/LIVE_VERIFICATION_2026-09-23.md).

Остаются внешние launch gates: исправление нерабочих upstream-конфигураций, проверка
каждого продаваемого режима/цены, production Crypto Pay, deployment/TLS/юридические
документы, независимый РФ backup storage и off-site recovery на реальном объёме.

Закрыты ранее перечисленные локальные пробелы: lifetime trial, provider circuit /
staged recovery, пороги маржи global → model → configuration с append-only history,
уведомления об изменении документов и административные формы основных операций.
Описание точного поведения: [cabinet/recovery](docs/CABINET_AND_RECOVERY.md).
Полный PASS всех режимов upstream, production SLO и внешних launch gates
в изолированном окружении не выдан.
