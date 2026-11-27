"""Playground markup. All interactive output uses DOM text, never HTML injection."""
from html import escape


def playground_view(csrf: str) -> str:
    return f'''
<meta name="playground-csrf" content="{escape(csrf, quote=True)}">
<link rel="stylesheet" href="/ui/playground.css?v=1">
<script src="/ui/playground.js?v=1" defer></script>
<div class="page-heading"><div><p class="eyebrow">РАЗРАБОТЧИКАМ</p><h1>Песочница API</h1>
<p class="page-description">Реальные запросы с текущего баланса. Стоимость списывается по фактическому usage API.</p>
</div><a class="button button-secondary" href="/cabinet?section=logs">История и расходы →</a></div>
<p id="pg-status" role="status" aria-live="polite">Загружаем доступные модели…</p>
<div class="pg-grid" id="pg-app" hidden>
<section class="panel pg-panel"><h2>Запрос</h2>
<form id="pg-form">
<label>Модель<select id="pg-model" required></select></label>
<label>API-ключ (только название)<select id="pg-key" required></select></label>
<label>Метод API<select id="pg-protocol" required></select></label>
<label>Задание<textarea id="pg-prompt" rows="4" required placeholder="Опишите задачу"></textarea></label>
<div id="pg-fields"></div>
<label>Дополнительные параметры JSON<textarea id="pg-extra" rows="5" spellcheck="false">{{}}</textarea></label>
<p class="hint">JSON дополняет параметры и позволяет передать референсы, tools и другие поля контракта.
Проверка выполняется действующим API. Аудио появится при наличии поддерживаемого контракта.</p>
<p id="pg-estimate" aria-live="polite">Сначала рассчитайте стоимость.</p>
<div class="pg-actions">
<button type="button" id="pg-quote" class="button button-secondary">Рассчитать стоимость</button>
<button type="submit" id="pg-run" class="button button-primary" disabled>Отправить платный запрос</button>
<button type="button" id="pg-new" class="button button-secondary">Новый запрос</button></div>
<p class="hint">Повтор отправки использует тот же idempotency key. Для новой генерации нажмите «Новый запрос».
После закрытия страницы статус и расходы доступны в «Запросах».</p>
</form></section>
<section class="panel pg-panel"><h2>Результат</h2><p id="pg-result-status" role="status">Запрос ещё не отправлен.</p>
<div id="pg-preview"></div>
<h3>JSON-запрос</h3><pre id="pg-request" tabindex="0"></pre>
<h3>JSON-ответ</h3><pre id="pg-response" tabindex="0"></pre>
<h3>curl</h3><p class="hint">Укажите свой ключ в переменной NEIRONYCH_API_KEY на своей машине.</p>
<pre id="pg-curl" tabindex="0"></pre></section></div>
'''
