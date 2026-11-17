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
  // Public docs: search only the navigation, never hide deep-link targets.
  const docsNavSearch = document.querySelector('#docs-nav-search');
  if (docsNavSearch) {
    docsNavSearch.hidden = false;
    const items = [...document.querySelectorAll('.docs-aside .contents a')];
    docsNavSearch.addEventListener('input', () => {
      const query = docsNavSearch.value.toLocaleLowerCase('ru').replace(/ё/g, 'е').trim();
      items.forEach(link => {
        link.hidden = !link.textContent.toLocaleLowerCase('ru').replace(/ё/g, 'е').includes(query);
      });
    });
  }
  document.querySelectorAll('[data-copy-target]').forEach(button => {
    button.addEventListener('click', async () => {
      const source = document.getElementById(button.dataset.copyTarget);
      if (!source) return;
      try {
        if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable');
        await navigator.clipboard.writeText(source.textContent);
        notify(t('Скопировано в буфер обмена', 'Copied to clipboard'));
      } catch {
        const range = document.createRange();
        range.selectNodeContents(source);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        notify(t('Текст выделен — скопируйте вручную', 'Text selected — copy manually'));
      }
    });
  });
  const search = document.querySelector('#price-search');
  if (search) {
    const toolbar = document.querySelector('.price-tools');
    const categories = document.querySelector('.catalog-filters');
    toolbar.hidden = false;
    categories.hidden = false;
    const tbody = document.querySelector('#price-table tbody');
    const rows = [...tbody.querySelectorAll('tr')];
    const count = document.querySelector('#price-count');
    const empty = document.querySelector('#no-results');
    const clear = document.querySelector('#clear-search');
    const sorting = document.querySelector('#price-sort');
    const table = document.querySelector('#price-table');
    const filterButtons = [...categories.querySelectorAll('[data-kind]')];
    let activeKind = 'all';
    const normalize = value => value.toLocaleLowerCase('ru').replace(/ё/g, 'е').trim();
    const indexed = rows.map((row, i) => ({
      row, i, text: normalize(row.textContent), name: normalize(row.dataset.name || ''),
      kind: row.dataset.kind, price: Number(row.dataset.price)
    }));
    function update() {
      const query = normalize(search.value);
      const sorted = [...indexed].sort((a, b) => {
        if (sorting.value === 'price-asc') return a.price - b.price || a.i - b.i;
        if (sorting.value === 'price-desc') return b.price - a.price || a.i - b.i;
        return a.name.localeCompare(b.name, 'ru') || a.i - b.i;
      });
      let shown = 0;
      sorted.forEach(item => {
        const visible = (activeKind === 'all' || item.kind === activeKind) && item.text.includes(query);
        item.row.hidden = !visible;
        if (visible) shown++;
        tbody.appendChild(item.row);
      });
      count.textContent = t(
        'Показано конфигураций: ' + shown + ' из ' + rows.length,
        'Configurations shown: ' + shown + ' of ' + rows.length
      );
      empty.hidden = shown > 0;
      table.hidden = shown === 0;
      clear.disabled = !search.value && activeKind === 'all' && sorting.value === 'name';
    }
    filterButtons.forEach(button => {
      button.addEventListener('click', () => {
        activeKind = button.dataset.kind;
        filterButtons.forEach(item => {
          const selected = item === button;
          item.classList.toggle('active', selected);
          item.setAttribute('aria-pressed', String(selected));
        });
        update();
      });
    });
    search.addEventListener('input', update);
    sorting.addEventListener('change', update);
    clear.addEventListener('click', () => {
      search.value = '';
      sorting.value = 'name';
      filterButtons.find(button => button.dataset.kind === 'all')?.click();
      search.focus();
    });
    update();
  }
})();
