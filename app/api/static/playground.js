/* Session BFF only: no Bearer token, dynamic HTML or provider URLs. */
(() => {
  'use strict';
  const root = document.getElementById('pg-app');
  if (!root) return;
  const el = id => document.getElementById(`pg-${id}`);
  const csrf = document.querySelector('meta[name="playground-csrf"]').content;
  let models = [], quote = null, sent = null, timer = null;
  // Only opaque operation IDs survive reload; prompts and responses are never stored.
  const storage = {
    get(key) { try { return sessionStorage.getItem(key); } catch (_) { return null; } },
    set(key, value) { try { sessionStorage.setItem(key, value); } catch (_) { /* memory still works */ } },
    remove(key) { try { sessionStorage.removeItem(key); } catch (_) { /* optional */ } }
  };
  let idem = storage.get('playground-operation') || crypto.randomUUID();
  storage.set('playground-operation', idem);
  const status = text => { el('status').textContent = text; };
  const json = value => JSON.stringify(value, null, 2);
  const option = (value, text) => {
    const node = document.createElement('option'); node.value = value; node.textContent = text; return node;
  };
  async function api(path, payload) {
    const response = await fetch(`/cabinet/playground/${path}`, {
      credentials: 'same-origin', cache: 'no-store',
      ...(payload ? {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        body: JSON.stringify(payload)} : {})
    });
    const data = await response.json();
    if (!response.ok) {
      const code = typeof data.detail === 'string' ? data.detail : data.error?.type || `HTTP ${response.status}`;
      throw new Error(code);
    }
    return {data, requestId: response.headers.get('X-Request-Id')};
  }
  function field(name, label, values, value, type = 'text') {
    const wrapper = document.createElement('label'); wrapper.textContent = label;
    const input = document.createElement(values ? 'select' : 'input');
    input.dataset.parameter = name;
    if (values) values.forEach(v => input.append(option(v, v)));
    else input.type = type;
    input.value = value; wrapper.append(input); el('fields').append(wrapper);
  }
  function parameters() {
    const model = models.find(m => m.id === el('model').value);
    el('fields').replaceChildren();
    if (!model) return;
    if (model.category === 'chat') {
      const name = el('protocol').value === 'responses' ? 'max_output_tokens' : 'max_tokens';
      field(name, 'Максимум токенов ответа', null, '1024', 'number');
    } else {
      const image = model.image;
      if (model.resolutions.length) field('resolution', 'Разрешение', model.resolutions,
        image.default_resolution || model.resolutions[0]);
      if (model.category === 'video') {
        field('duration', 'Длительность результата, сек.', null, '5', 'number');
        field('aspect_ratio', 'Соотношение сторон (необязательно)', null, '');
      }
      if (image.aspect_ratios) field('aspect_ratio', 'Соотношение сторон', image.aspect_ratios,
        image.default_aspect_ratio || image.aspect_ratios[0]);
      if (image.frame === 'size') field('size', 'Размер (auto или ширина×высота через x)', null, 'auto');
      if (image.quality) field('quality', 'Качество', image.quality, image.default_quality || image.quality[0]);
      if (image.response_formats) field('response_format', 'Формат ответа', image.response_formats,
        image.response_formats[0]);
    }
    invalidate(); display();
  }
  function chooseModel() {
    const model = models.find(m => m.id === el('model').value);
    el('protocol').replaceChildren();
    model.protocols.forEach(p => el('protocol').append(option(p, `/v1/${p}`)));
    el('protocol').value = model.default_protocol;
    parameters();
  }
  function payload() {
    const protocol = el('protocol').value;
    const prompt = el('prompt').value;
    let body = protocol === 'responses' ? {input: prompt} :
      ['messages', 'chat/completions'].includes(protocol) ? {messages: [{role: 'user', content: prompt}]} : {prompt};
    el('fields').querySelectorAll('[data-parameter]').forEach(input => {
      if (input.value !== '') body[input.dataset.parameter] = input.type === 'number' ? Number(input.value) : input.value;
    });
    const extra = JSON.parse(el('extra').value);
    if (!extra || Array.isArray(extra) || typeof extra !== 'object') throw new Error('Нужен JSON-объект параметров.');
    body = {...body, ...extra, model: el('model').value, stream: false};
    // Some image contracts reject controls outside their allowlist.
    if (!['responses', 'messages', 'chat/completions'].includes(protocol)) delete body.stream;
    return {protocol, api_key_id: el('key').value, body, idempotency_key: idem};
  }
  function display() {
    try {
      const value = sent || payload();
      el('request').textContent = json(value.body);
      // Shell single-quote escaping preserves arbitrary prompt text, including $() and backticks.
      const quoted = "'" + JSON.stringify(value.body).replace(/'/g, "'\\''") + "'";
      el('curl').textContent = `curl '${location.origin}/v1/${value.protocol}' \\\n  -H "Authorization: Bearer $NEIRONYCH_API_KEY" \\\n  -H 'Content-Type: application/json' \\\n  -H 'Idempotency-Key: playground:${idem}' \\\n  --data-raw ${quoted}`;
    } catch (_) { el('request').textContent = 'Проверьте JSON дополнительных параметров.'; }
  }
  function invalidate() {
    quote = null; el('run').disabled = true;
    el('estimate').textContent = 'Рассчитайте стоимость перед отправкой.';
  }
  function freeze(value) {
    el('form').querySelectorAll('input, select, textarea').forEach(node => { node.disabled = value; });
  }
  async function calculate() {
    el('quote').disabled = true; el('run').disabled = true;
    try {
      const value = sent || payload();
      quote = (await api('quote', value)).data;
      el('estimate').textContent = `Оценка: ${quote.estimate_rub} ₽. ${quote.notice} ` +
        'Для текста и видео это предварительный резервный расчёт, а не фиксированная цена.';
      el('run').disabled = false; status('Стоимость рассчитана. Подтвердите платный запрос кнопкой отправки.');
    } catch (error) { quote = null; status(`Расчёт недоступен: ${error.message}`); }
    finally { el('quote').disabled = false; }
  }
  function preview(data) {
    el('preview').replaceChildren();
    const video = data.video?.url;
    const items = video ? [{url: video, video: true}] : (Array.isArray(data.data) ? data.data.slice(0, 4) : []);
    items.forEach(item => {
      let url = null;
      if (item.url) {
        try { const parsed = new URL(item.url, location.origin);
          if (parsed.origin === location.origin && !parsed.username && !parsed.password) url = parsed.href;
        } catch (_) { /* untrusted URL */ }
      } else if (item.b64_json && /^[A-Za-z0-9+/=\r\n]+$/.test(item.b64_json)) {
        url = `data:image/png;base64,${item.b64_json}`;
      }
      if (!url) return;
      const media = document.createElement(item.video ? 'video' : 'img');
      media.src = url; media.setAttribute('referrerpolicy', 'no-referrer');
      if (item.video) { media.controls = true; media.preload = 'metadata'; } else media.alt = 'Результат генерации';
      el('preview').append(media);
      if (!url.startsWith('data:')) {
        const link = document.createElement('a'); link.href = url; link.textContent = 'Открыть результат ↗';
        link.target = '_blank'; link.rel = 'noopener noreferrer'; link.referrerPolicy = 'no-referrer';
        el('preview').append(link);
      }
    });
  }
  async function poll(id) {
    clearTimeout(timer);
    try {
      const {data} = await api(`results/${encodeURIComponent(id)}`);
      el('result-status').textContent = `${data.status} · ${id}` +
        (data.charge_rub !== null ? ` · Списано ${data.charge_rub} ₽` : ' · Списание уточняется');
      el('response').textContent = json(data); preview(data.response || {});
      if (!['completed', 'failed', 'cancelled', 'timeout'].includes(data.status)) {
        timer = setTimeout(() => poll(id), 3000);
      }
    } catch (error) {
      status(`Статус недоступен: ${error.message}. Проверим повторно.`);
      timer = setTimeout(() => poll(id), 10000);
    }
  }
  el('form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!quote || el('run').disabled) return;
    el('run').disabled = true; el('quote').disabled = true; el('new').disabled = true;
    sent = sent || payload(); freeze(true); display(); status('Выполняется платный запрос…');
    try {
      const {data, requestId} = await api('run', {...sent, quote_token: quote.quote_token});
      el('response').textContent = json(data); preview(data);
      const id = requestId || data.request_id || data.id;
      if (id) { storage.set('playground-result', id); await poll(id); }
      status('Запрос принят. Статус и фактические расходы отображаются справа.');
    } catch (error) {
      status(`Ответ не получен: ${error.message}. Рассчитайте стоимость и повторите тот же запрос; ` +
        'idempotency key сохранён. Не создавайте новую генерацию для проверки статуса.');
    } finally { quote = null; el('quote').disabled = false; el('new').disabled = false; }
  });
  el('quote').addEventListener('click', calculate);
  el('new').addEventListener('click', () => {
    sent = null; idem = crypto.randomUUID(); storage.set('playground-operation', idem);
    storage.remove('playground-result'); clearTimeout(timer); freeze(false); invalidate(); display();
    status('Новый запрос подготовлен. Отправка создаст отдельную платную генерацию.');
  });
  el('model').addEventListener('change', chooseModel);
  el('protocol').addEventListener('change', parameters);
  el('form').addEventListener('input', () => { if (!sent) { invalidate(); display(); } });
  (async () => {
    try {
      const {data} = await api('catalog'); models = data.models;
      if (!models.length || !data.keys.length) {
        status(!models.length ? 'Нет доступных моделей.' : 'Создайте активный API-ключ в разделе «API-ключи».'); return;
      }
      models.forEach(m => el('model').append(option(m.id, m.name)));
      data.keys.forEach(k => el('key').append(option(k.id, k.name)));
      chooseModel(); root.hidden = false; status('Выберите модель и задайте параметры.');
      const resultId = storage.get('playground-result');
      if (resultId) { freeze(true); el('quote').disabled = true; await poll(resultId);
        status('Восстановлен статус последнего запроса. Для новой генерации нажмите «Новый запрос».'); }
    } catch (error) { status(`Не удалось загрузить песочницу: ${error.message}. Обновите страницу.`); }
  })();
  window.addEventListener('pagehide', () => clearTimeout(timer));
})();
