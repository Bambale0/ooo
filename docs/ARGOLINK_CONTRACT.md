# Контракт ArgoLink: проверено 2026-09-23

Источник: [официальная документация ArgoLink](https://argolink.io/en/docs).
Проверка публичного API с заведомо неверным ключом дала 401. Платные вызовы не выполнялись.

## Матрица upstream video

| Model ID | Duration | Resolution | Image references |
|---|---|---|---|
| seedance-2.5 | 4–30 s | 480p, 720p, 1080p | 30 |
| seedance-2.0 | 4–15 s | 720p, 1080p, 4k | 9 |
| seedance-2.0-mini | 4–15 s | 720p | 9 |
| seedance-2.0-fast | 4–15 s | 720p | 9 |
| grok-imagine-video-1.5 | 1–15 s | 480p, 720p, 1080p | 7; references max 720p |

Seedance ratios: 16:9, 9:16, 1:1, 4:3, 3:4, 21:9.
Grok ratios: 16:9, 9:16, 1:1, 4:3, 3:4, 3:2, 2:3.
Seedance first/last frames determine aspect ratio from the input.

POST `/v1/videos/generations` returns `request_id`. GET `/v1/videos/{request_id}`
returns pending/done/failed/expired; GET `/v1/videos/{request_id}/content` streams the output.
All use Bearer authentication under the submitting account. `/v1/models` is public.
Seedance accepts HTTPS media URLs. Local inputs use `/v1/media/uploads` then upload bytes.
Video inputs incur additional billable seconds; image/audio references do not.

## Реализованный публичный API

Наш API — `/api/v1/generations`, не прозрачная копия `/v1` ArgoLink.
Соответствия: `model_slug -> model`, `duration_seconds -> duration`,
`reference_images -> reference_images`, `start_image -> start_image` для Seedance
и `start_image -> image` для Grok, `end_image -> end_image` для Seedance.
`mode` выбирает проверку полей и цену внутри шлюза; metadata не заменяет media fields.

`duration_seconds` по умолчанию теперь 5. Разрешён только явно поддержанный
model/resolution/mode. `text_to_video` не принимает изображения;
`image_to_video`/`first_frame` требуют `start_image`, `first_last_frame` — оба кадра.
`reference` принимает reference_images. `default` выводит режим из полей.
`resolution=default` не принимается: цена должна соответствовать явной конфигурации.

Неизвестные поля отвергаются с 422. В частности, `reference_videos`,
`reference_audios`, edit, file_id, data URI и multipart пока не реализованы.
Нельзя обещать весь контракт Seedance. Это сохраняет требование product brief
«full capability support»: выпуск Seedance пока заблокирован, оно не отменено.
Image/LLM-модели нельзя включить через enable endpoint этого видеоадаптера.

Контрактные тесты проверяют JSON и ошибки с MockTransport, а не успешный рендер
на стороне провайдера. Enable gates нельзя отмечать successful_smoke на основании этих тестов.

## Ошибки и повторная отправка

* 429 / ConnectTimeout / PoolTimeout / ConnectError допускают ограниченный retry.
* Read/write timeout, 408, 5xx и невалидный submit response не повторяются автоматически:
  провайдер мог принять платную задачу.
* До сетевого вызова фиксируется ProviderAttempt со статусом `submitting`.
  Crash оставляет диагностируемое состояние; перезапуск не отправляет задачу повторно.
* Попытка сохраняет credential_id. Poll/content используют исходный ключ после его
  локальной замены; фактически отозванный у провайдера ключ по-прежнему требует разбора.
* Внешние content redirects отклоняются. Если upstream начнёт выдавать CDN redirects,
  нужна проверяемая политика egress/DNS pinning перед их включением.
* HTTP outage или одинаковый 404 на валидный и контрольный неверный ключ не подтверждают ключ.

Исторические ProviderAttempt без credential_id используют текущий ключ для обратной
совместимости. Перед ротацией при обновлении существующей установки завершите такие задачи.
