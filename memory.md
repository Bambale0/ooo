# Нейроныч — рабочая память

Актуализировано 23.09.2026. Проверяйте [production review](docs/PRODUCTION_REVIEW.md),
[implementation status](IMPLEMENTATION_STATUS.md) и основной product brief.
Секреты, signed URLs, provider job IDs и пользовательские изображения здесь не хранятся.

- Branch/PR workflow обязателен; main связан с production deploy. Подготовка репозитория
  и изолированные проверки не разрешают автоматически мержить или разворачивать production.
- Деньги: Decimal/NUMERIC. Native API резервирует верхнюю оценку, затем записывает actual
  usage и отдельные компенсирующие ledger entries. Старые snapshots не переписываются.
- Product brief §89: историческое покрытие не является самостоятельным запретом генерации.
  Admission использует retail balance, экономику и текущий working capital с реальным wallet cap.
- FX: automatic, затем manual fallback, затем last automatic. Источник и timestamp сохраняются.
- Полный актуальный priced catalog ArgoLink — 39 моделей. Native routes и пределы описаны
  в docs/ARGOLINK_CONTRACT.md; каталог нельзя считать свидетельством работоспособности.
- Live 23.09: 30/39 дали результат, включая все 6 видео-моделей с референсом. GPT Responses
  и часть GPT Chat возвращали 502; Nano edits не подтверждены. Gemini Omni в docs есть,
  в priced catalog отсутствует. Автоматического fallback модели/протокола нет.
- Partner credentials хранятся зашифрованными. Начатая задача привязана к исходному ключу.
  Admin `/upstream-usage` читает `/v1/usage`; публичный `/v1/models` не валидирует ключ.
- Generated files не сохраняются (brief §53). Video отдаётся через authenticated stream;
  image CDN URL может передаваться непосредственно. URL metadata сохраняется, bytes — нет.
- `submitting`/`reconciliation_required` не пересылаются автоматически. Есть admin
  reconciliation с audit; известный video task можно прикрепить, затем отдельно уточнить usage.
- Payment create с неизвестным исходом также не повторяется; есть read-only lookup и
  явная проверяемая invoice reconciliation. Paid replay не сбрасывает credited state.
- Кабинет имеет PostgreSQL dialog/action state; pending actions проверяют actor/role/tenant.
  Telegram outbox допускает повтор уведомления после crash, не повтор финансового действия.
- Support files — единственные новые сохраняемые пользовательские медиа: ограничены 20 МБ
  и входят в encrypted backup. Генерационные референсы не превращаются в support data.
- Проверены logical restore и WAL/PITR. `.backup.env` и recovery identity не коммитятся;
  private recovery identity не должна находиться на primary/backup server.
- Последний локальный полный прогон: 209 tests с PostgreSQL; schema head 0019, no drift;
  app/workers restart и Redis failure проверены. CI PR остаётся источником проверки commit SHA.
- Внешние launch gates и ещё не реализованные расширенные требования перечислены в
  IMPLEMENTATION_STATUS.md. Не выдавайте полный resale PASS по одному unit-test прогону.
