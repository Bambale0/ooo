/* Optional enhancements: the complete reference and price list work without JS. */
(() => {
  const ru = document.documentElement.lang === 'ru';
  const t = (r, e) => ru ? r : e;
  const toast = document.querySelector('.toast');
  let noticeTimer;
  function notify(message) {
    clearTimeout(noticeTimer);
    toast.textContent = message;
    noticeTimer = setTimeout(() => { toast.textContent = ''; }, 6000);
  }
  document.querySelectorAll('[data-language]').forEach(link => {
    link.addEventListener('click', () => { link.hash = location.hash; });
  });
  document.querySelectorAll('.document pre').forEach(pre => {
    const code = pre.querySelector('code');
    if (!code) return;
    const frame = document.createElement('div');
    frame.className = 'code-frame';
    const toolbar = document.createElement('div');
    toolbar.className = 'code-toolbar';
    const label = document.createElement('span');
    label.textContent = t('ПРИМЕР', 'EXAMPLE');
    const button = document.createElement('button');
    button.className = 'copy-button';
    button.type = 'button';
    button.textContent = t('Копировать', 'Copy');
    button.setAttribute('aria-label', t('Копировать пример кода', 'Copy code example'));
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable');
        await navigator.clipboard.writeText(code.textContent);
        notify(t('Скопировано в буфер обмена', 'Copied to clipboard'));
      } catch {
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(code);
        selection.removeAllRanges();
        selection.addRange(range);
        notify(t('Код выделен. Нажмите Ctrl+C / ⌘C или выберите «Копировать».', 'Code selected. Press Ctrl+C / ⌘C or choose Copy.'));
      } finally {
        button.disabled = false;
      }
    });
    toolbar.append(label, button);
    pre.before(frame);
    frame.append(toolbar, pre);
  });
  document.querySelectorAll('.table-scroll, .document pre').forEach((region, index) => {
    region.tabIndex = 0;
    region.setAttribute('role', 'group');
    const label = region.matches('pre') ? t('Пример кода', 'Code example') : t('Таблица данных', 'Data table');
    region.setAttribute('aria-label', `${label} ${index + 1}`);
  });
  const links = [...document.querySelectorAll('.contents a')];
  function markCurrent(id) {
    links.forEach(link => {
      if (link.hash === '#' + id) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
  }
  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) if (entry.isIntersecting) markCurrent(entry.target.id);
    }, {rootMargin: '-20% 0px -60% 0px', threshold: 0});
    links.forEach(link => {
      const section = document.getElementById(link.hash.slice(1));
      if (section) observer.observe(section);
    });
  }
  const openHash = () => {
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch { return; }
    const target = document.getElementById(id);
    if (!target) return;
    let parent = target.parentElement;
    let opened = false;
    while (parent) {
      if (parent.tagName === 'DETAILS' && !parent.open) { parent.open = true; opened = true; }
      parent = parent.parentElement;
    }
    if (opened) target.scrollIntoView();
    markCurrent(id);
  };
  window.addEventListener('hashchange', openHash);
  openHash();
  const search = document.querySelector('#price-search');
  if (search) {
    document.querySelector('.price-tools').hidden = false;
    const rows = [...document.querySelectorAll('#price-table tbody tr')];
    const count = document.querySelector('#price-count');
    const empty = document.querySelector('#no-results');
    const clear = document.querySelector('#clear-search');
    const normalize = value => value.toLocaleLowerCase('ru').replace(/ё/g, 'е').trim();
    const indexed = rows.map(row => [row, normalize(row.textContent)]);
    function filter() {
      const query = normalize(search.value);
      let shown = 0;
      indexed.forEach(([row, text]) => {
        row.hidden = !text.includes(query);
        if (!row.hidden) shown++;
      });
      count.textContent = `Показано конфигураций: ${shown} из ${rows.length}`;
      empty.hidden = shown > 0;
      document.querySelector('#price-table').hidden = shown === 0;
      clear.disabled = !search.value;
    }
    search.addEventListener('input', filter);
    clear.addEventListener('click', () => { search.value = ''; filter(); search.focus(); });
    filter();
  }
})();
