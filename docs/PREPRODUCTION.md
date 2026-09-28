# Предпродакшен

Изолированное окружение для проверки регистрации, ключей, платежей и генераций
на production-коде. Это не открытие продаж и не обновление основного production.

## Изоляция и конфигурация

- Compose project: `ooo-preprod`, отдельные PostgreSQL, Redis и support volume.
- `APP_ENV=production`: проверки production-конфигурации остаются включёнными.
- Собственные случайные DB/admin/encryption secrets; не копировать production `.env`.
- Отдельный тестовый Telegram-бот; один polling-процесс.
- `PUBLIC_API_BASE_URL=https://api.xn--e1aikcel5c5a.online/preprod`.
- API слушает только `127.0.0.1:18002`; host Nginx завершает TLS.
- Crypto Pay — только `https://testnet-pay.crypt.bot`, отдельный API-ключ testnet.
- ArgoLink — реальный провайдер: тестовые монеты Crypto Pay не оплачивают его услуги.
  Каждый живой smoke ограничивать явным бюджетом; не повторять submit при неизвестном результате.
- Начальное финансовое обеспечение `0`. Не подделывать wallet snapshots, платежи или ledger
  для обхода проверок. Регистрация и выпуск ключей доступны без оплаченных генераций.
- Резервный курс для этого прогона: `84.52 RUB/USDT` по указанию владельца.
  Это ручная тестовая настройка, не подтверждение текущего рыночного курса USDT.
- Перед запуском Telegram нужны согласованные HTTPS-ссылки на документы тестового окружения.

## Запуск

Нужен Docker Compose с поддержкой `!override` (на сервере проверена версия 2.40.3).
Релизы размещаются в `/opt/neironych-preprod/releases/<sha>`, `.env` хранится уровнем выше,
с правами `0600`. Образ собирается из проверенного commit, имеет такой же immutable tag;
`APP_REVISION` содержит полный SHA. `current` указывает на выбранный release.

```sh
cd /opt/neironych-preprod
docker compose --project-directory "$PWD" --project-name ooo-preprod --env-file .env \
  -f current/docker-compose.prod.yml -f current/docker-compose.preprod.yml config --quiet
docker compose --project-directory "$PWD" --project-name ooo-preprod --env-file .env \
  -f current/docker-compose.prod.yml -f current/docker-compose.preprod.yml up -d --wait postgres redis
docker compose --project-directory "$PWD" --project-name ooo-preprod --env-file .env \
  -f current/docker-compose.prod.yml -f current/docker-compose.preprod.yml run --rm app alembic upgrade head
docker compose --project-directory "$PWD" --project-name ooo-preprod --env-file .env \
  -f current/docker-compose.prod.yml -f current/docker-compose.preprod.yml up -d --wait app worker webhook_worker
# Только после настройки TELEGRAM_BOT_TOKEN, TERMS_URL, PRIVACY_POLICY_URL:
docker compose --project-directory "$PWD" --project-name ooo-preprod --env-file .env \
  -f current/docker-compose.prod.yml -f current/docker-compose.preprod.yml --profile telegram up -d telegram
```

На данном VPS Docker egress закрыт по умолчанию. Разрешать HTTP(S)/DNS только для
bridge/subnet `ooo-preprod_default`; Telegram использует существующий egress tunnel.
Не очищать общие firewall-таблицы и не выключать общий Telegram NAT.

В HTTPS server block добавить `nginx/preprod.locations.conf`, проверить `nginx -t`
и выполнить reload. Основной `location /` не меняется. `/preprod/internal/` закрыт.
Перед изменением сохранить предыдущую конфигурацию Nginx.

## Проверки и полный прогон

1. Readiness через HTTPS сообщает ожидаемый SHA, DB и Redis доступны.
2. `/preprod/docs` и переключатель языка остаются в предпроде; внутренние metrics закрыты.
3. Основной API сохраняет прежний SHA и readiness. Volumes и Compose labels различаются.
4. Миграции доходят до head; `alembic check` не находит новых операций.
5. Реальный пользователь принимает условия в боте, подаёт заявку; админ привязывает
   проверенный персональный upstream-ключ и одобряет её. Не имитировать согласие человека.
6. Выдача/повторная выдача/отзыв ключей, неверный ключ, изоляция клиентов,
   отказ генерации при нулевом балансе и отсутствии обеспечения.
7. Testnet invoice → оплата пользователем → paid waiting credit → ручное подтверждение
   админом → повтор подтверждения без повторного зачисления.
8. Seedance 2.5 с референсом: bounded submit, polling, результат, фактический provider cost,
   списание, повторы idempotency key. Изменение upstream-квоты требует документированного
   API провайдера или отдельного ограниченного ключа; чтение usage не доказывает её изменение.
9. Перезапуск сервисов сохраняет БД/ключи/балансы; восстановление backup проверять
   в отдельной временной БД. Секреты и отчёты с ключами не отправлять в GitHub.

Нет Crypto Pay testnet key/оплаты — финансовый end-to-end остаётся непроверенным.
Не отмечать весь прогон успешным по одним unit-тестам или readiness.

## Остановка и откат

Останавливать только сервисы проекта `ooo-preprod`, без `down -v`.
Перед обновлением сохранять зашифрованный dump БД, support attachments и конфигурацию.
Возврат приложения: предыдущий image/SHA и совместимые с ним миграции.
Не выполнять слепой downgrade схемы. Для удаления маршрута восстановить сохранённый
Nginx-конфиг, выполнить `nginx -t`, затем reload. Основной production не переключать.
