'use strict';

(() => {
  const $ = (sel, el = document) => el.querySelector(sel);
  const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));

  const store = {
    get(key, fallback) {
      try {
        const raw = localStorage.getItem(key);
        return raw === null ? fallback : JSON.parse(raw);
      } catch (e) {
        return fallback;
      }
    },
    set(key, value) {
      try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* private mode */ }
    },
  };

  const I18N = {
    zh: {
      skip: '跳到论文列表',
      total: (shown, all) => (shown === all ? `共 ${all} 篇` : `显示 ${shown} / ${all} 篇`),
      showRest: (n) => `显示其余 ${n} 篇`, views: (n) => `${n} 次阅读`,
      sortHot: '按热度', sortOriginal: '原始顺序', sortHf: '按 HF 点赞',
      popular: (n) => `热门 · 近 ${n} 天`, popularNote: '按 HF 与 alphaXiv 点赞（alphaXiv 每 10 次访问折合 1 赞）排序', heatTip: (hf, ax, v) => `HF 点赞 ${hf} · alphaXiv 点赞 ${ax} · alphaXiv 访问 ${v}`,
      prevDay: '前一天', nextDay: '后一天', theme: '切换深色 / 浅色',
      authors: '作者', subject: '分类', submitted: '提交', comment: '备注',
      loading: '正在加载…', loadFail: '数据加载失败：',
      empty: '没有符合条件的论文。',
      openNew: '新标签打开 ↗', close: '收起', copied: '已复制到剪贴板',
      copyFail: '复制失败', relDone: (n) => `已按 ${n} 个标签筛选相关论文`, relNone: '这篇没有标签可用于相关推荐',
      starred: '星标', search: '搜索', settings: '设置', top: '顶部',
      include: '包含', exclude: '排除', apply: '搜索', clear: '清空', filter: '只显示匹配',
      noStars: '还没有星标论文。点标题右边的 ☆ 收藏。', exportMd: '复制 Markdown', exportDl: '下载 .md',
      star: '星标', unstar: '取消星标', moreAuthors: (n) => `+${n} 人`,
      orgs: '机构', preview: '预览图',
      showThumbs: '显示预览图',
      disclaimer: '论文元数据来自 arXiv，点赞数来自 Hugging Face Daily Papers。解读由 AI 生成，可能有误，请以原文为准；论文版权归原作者所有。',
    },
    en: {
      skip: 'Skip to papers',
      total: (shown, all) => (shown === all ? `${all} papers` : `${shown} / ${all} papers`),
      showRest: (n) => `Show the other ${n}`, views: (n) => `${n} views`,
      sortHot: 'By popularity', sortOriginal: 'arXiv order', sortHf: 'By HF upvotes',
      popular: (n) => `Popular · last ${n} days`, popularNote: 'Ranked by HF + alphaXiv upvotes (10 alphaXiv visits count as one)', heatTip: (hf, ax, v) => `HF upvotes ${hf} · alphaXiv upvotes ${ax} · alphaXiv visits ${v}`,
      prevDay: 'Previous day', nextDay: 'Next day', theme: 'Toggle dark / light',
      authors: 'Authors', subject: 'Subject', submitted: 'Submitted', comment: 'Comment',
      loading: 'Loading…', loadFail: 'Failed to load data: ',
      empty: 'No papers match the current filters.',
      openNew: 'Open in new tab ↗', close: 'Close', copied: 'Copied to clipboard',
      copyFail: 'Copy failed', relDone: (n) => `Filtered by ${n} tag(s)`, relNone: 'No tags to find related papers',
      starred: 'Starred', search: 'Search', settings: 'Settings', top: 'Top',
      include: 'Include', exclude: 'Exclude', apply: 'Search', clear: 'Clear', filter: 'Filter',
      noStars: 'No starred papers yet. Click ☆ next to a title.', exportMd: 'Copy Markdown', exportDl: 'Download .md',
      star: 'Star', unstar: 'Unstar', moreAuthors: (n) => `+${n} more`,
      orgs: 'Organizations', preview: 'Preview',
      showThumbs: 'Show preview images',
      disclaimer: 'Paper metadata from arXiv; upvotes from Hugging Face Daily Papers. Readings are AI-generated and may contain errors; please check the original paper. Copyright belongs to the authors.',
    },
  };

  const STAR_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z"/></svg>';
  const AUTHOR_LIMIT = 6; // names shown inline next to the organizations
  const ORG_LIMIT = 4;
  const DAY_LIMIT = 50; // a day shows its top 50; the rest behind a button

  const params = new URLSearchParams(location.search);
  const state = {
    index: null,
    day: null,
    dayCache: new Map(),
    date: params.get('date'),
    cat: params.get('cat'),
    lang: store.get('dp-lang', (navigator.language || '').toLowerCase().startsWith('zh') ? 'zh' : 'en'),
    sort: ['hot', 'original', 'hf'].includes(store.get('dp-sort')) ? store.get('dp-sort') : 'hot',
    showThumbs: store.get('dp-thumbs', true),
    search: { inc: '', exc: '', logic: 'OR', filter: true },
    stars: store.get('dp-stars', {}),
    visited: new Set(store.get('dp-visited', [])),
    openPdf: new Set(),
    expandedAuthors: new Set(),
    scrolledToHash: false,
  };

  const t = (key, ...args) => {
    const value = (I18N[state.lang] || I18N.zh)[key];
    return typeof value === 'function' ? value(...args) : (value ?? key);
  };

  const escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
  const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

  // ---------- search ----------
  const keywords = (text) => text.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);

  function searchTerms() {
    return { inc: keywords(state.search.inc), exc: keywords(state.search.exc) };
  }

  function matches(paper, terms) {
    const hay = `${paper.title} ${paper.abstract} ${paper.authors.join(' ')} ${(paper.orgs || []).map((o) => (typeof o === 'string' ? o : o.name)).join(' ')} ${(paper.tags || []).join(' ')} ${summaryText(paper)}`.toLowerCase();
    if (terms.exc.some((k) => hay.includes(k.toLowerCase()))) return false;
    if (!terms.inc.length) return true;
    const hit = (k) => hay.includes(k.toLowerCase());
    return state.search.logic === 'AND' ? terms.inc.every(hit) : terms.inc.some(hit);
  }

  // Search filters the list; matches are not marked in the text (that made the list noisy).
  const hl = (text) => escapeHtml(text);

  // ---------- helpers ----------
  function summaryText(paper) {
    const s = paper.summary;
    if (!s) return '';
    return s[state.lang] || s.zh || s.en || '';
  }

  function catName(id) {
    const names = state.index?.category_names?.[id];
    if (!names) return id;
    return `${state.lang === 'zh' ? names.zh : names.en} (${id})`;
  }

  function fmtTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
  }


  let toastTimer = null;
  function toast(msg) {
    const el = $('#toast');
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 1800);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      if (!ok) throw e;
    }
  }

  function renderMath(el) {
    if (typeof window.renderMathInElement !== 'function') return;
    try {
      window.renderMathInElement(el, {
        delimiters: [
          { left: '$$', right: '$$', display: true },
          { left: '$', right: '$', display: false },
          { left: '\\(', right: '\\)', display: false },
        ],
        throwOnError: false,
        ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code', 'option'],
      });
    } catch (e) { /* malformed TeX in an abstract should not break the page */ }
  }

  function markVisited(id) {
    if (state.visited.has(id)) return;
    state.visited.add(id);
    store.set('dp-visited', Array.from(state.visited).slice(-3000));
    document.getElementById(id)?.classList.add('visited');
  }

  // ---------- static texts ----------
  function applyI18n() {
    document.documentElement.lang = state.lang === 'zh' ? 'zh-CN' : 'en';
    $$('[data-i18n]').forEach((el) => { el.textContent = t(el.dataset.i18n); });
    $$('[data-i18n-title]').forEach((el) => { el.title = t(el.dataset.i18nTitle); });
    $('#lang-btn').textContent = state.lang === 'zh' ? 'EN' : '中文';
  }

  // ---------- data ----------
  async function getJson(url) {
    const resp = await fetch(url, { cache: 'no-cache' });
    if (!resp.ok) throw new Error(`${resp.status} ${url}`);
    return resp.json();
  }

  // Popular views live in the date select as "hot7" / "hot30".
  function popularPeriod(value) {
    const m = /^hot(\d+)$/.exec(value || '');
    return m && state.index?.popular?.periods?.[m[1]] ? m[1] : null;
  }

  function popularPeriods() {
    const periods = state.index?.popular?.periods || {};
    // Hide a longer period while it covers no more days than a shorter one (same list).
    let lastDays = 0;
    return Object.keys(periods).map(Number).sort((x, y) => x - y).filter((n) => {
      const p = periods[n];
      if (!p.papers.length || p.days <= lastDays) return false;
      lastDays = p.days;
      return true;
    });
  }

  async function loadIndex() {
    state.index = await getJson('data/index.json');
    state.index.popular = await getJson('data/popular.json').catch(() => null);
    const days = state.index.days.map((d) => d.date);
    if (!days.includes(state.date) && !popularPeriods().includes(Number(popularPeriod(state.date)))) state.date = days[0];
    const cats = state.index.categories.map((c) => c.id);
    if (!cats.includes(state.cat)) state.cat = state.index.default_category || cats[0];
  }

  async function loadDay() {
    $('#status').textContent = t('loading');
    $('#papers').innerHTML = '';
    try {
      const period = popularPeriod(state.date);
      if (period) {
        const popular = state.index.popular;
        state.day = { date: state.date, popular: true, papers: popular.periods[period].papers };
      } else {
        if (!state.dayCache.has(state.date)) {
          state.dayCache.set(state.date, await getJson(`data/days/${state.date}.json`));
        }
        state.day = state.dayCache.get(state.date);
      }
      $('#status').textContent = '';
    } catch (e) {
      state.day = null;
      $('#status').textContent = t('loadFail') + e.message;
      return;
    }
    state.openPdf.clear();
    state.showAll = false;
    render();
  }

  function syncUrl() {
    const q = new URLSearchParams({ date: state.date, cat: state.cat });
    history.replaceState(null, '', `?${q}${location.hash}`);
  }

  // ---------- header / toolbar ----------
  function renderChrome() {
    const nav = $('#cat-nav');
    nav.innerHTML = state.index.categories.map((c) => {
      const name = state.lang === 'zh' ? (c.name_zh || c.name) : c.name;
      const cur = c.id === state.cat ? ' aria-current="page"' : '';
      return `<a href="?date=${encodeURIComponent(state.date)}&cat=${encodeURIComponent(c.id)}" data-cat="${escapeHtml(c.id)}"${cur}>${escapeHtml(c.id)} · ${escapeHtml(name)}</a>`;
    }).join('');

    const cat = state.index.categories.find((c) => c.id === state.cat);
    $('#cat-title').textContent = cat ? (state.lang === 'zh' ? (cat.name_zh || cat.name) : cat.name) : state.cat;

    const sel = $('#date-select');
    sel.innerHTML = popularPeriods()
      .map((n) => `<option value="hot${n}">${escapeHtml(t('popular', n))}</option>`)
      .join('')
      + state.index.days
        .map((d) => `<option value="${escapeHtml(d.date)}">${escapeHtml(d.date)} (${d.count})</option>`)
        .join('');
    sel.value = state.date;
    const days = state.index.days.map((d) => d.date);
    const i = days.indexOf(state.date);
    $('#prev-day').disabled = i < 0 || i >= days.length - 1;
    $('#next-day').disabled = i <= 0;

    const isPopular = Boolean(state.day?.popular);
    $('#sort-select').hidden = isPopular;
    $('#sort-select').value = state.sort;
    $('#popular-note').hidden = !isPopular;
  }

  // ---------- paper list ----------
  function visiblePapers() {
    const all = (state.day?.papers || []).filter((p) => (p.categories || [p.primary]).includes(state.cat));
    const terms = searchTerms();
    let list = all.map((p, i) => ({ p, order: i }));
    if (state.search.filter) list = list.filter(({ p }) => matches(p, terms));
    const heat = (p) => p.heat ?? p.hf_upvotes ?? 0;
    const by = state.day?.popular ? (a, b) => a.order - b.order : {
      hot: (a, b) => heat(b.p) - heat(a.p) || (b.p.ax_visits ?? 0) - (a.p.ax_visits ?? 0) || a.order - b.order,
      hf: (a, b) => (b.p.hf_upvotes ?? -1) - (a.p.hf_upvotes ?? -1) || a.order - b.order,
      original: (a, b) => a.order - b.order,
    }[state.sort] || ((a, b) => a.order - b.order);
    list.sort(by);
    const searching = terms.inc.length > 0 || terms.exc.length > 0;
    const rest = state.day?.popular || searching || state.showAll ? 0 : Math.max(0, list.length - DAY_LIMIT);
    if (rest) list = list.slice(0, DAY_LIMIT);
    return { list: list.map((x) => x.p), total: all.length, rest };
  }

  function authorsHtml(paper) {
    const names = paper.authors || [];
    const full = state.expandedAuthors.has(paper.id) || names.length <= AUTHOR_LIMIT + 1;
    const shown = full ? names : names.slice(0, AUTHOR_LIMIT);
    let html = shown.map((name) => `<span class="author">${escapeHtml(name)}</span>`).join('');
    if (!full) {
      html += `<button type="button" class="author more" data-act="authors">${escapeHtml(t('moreAuthors', names.length - shown.length))}</button>`;
    }
    return html;
  }

  function orgChip(org) {
    const o = typeof org === 'string' ? { name: org } : org;
    const initial = (o.name || '?').replace(/^(The|University of|Universit\S* de)\s+/i, '').charAt(0).toUpperCase();
    const icon = o.logo
      ? `<img class="org-logo" src="${escapeHtml(o.logo)}" alt="" width="16" height="16" loading="lazy" decoding="async">`
      : `<span class="org-logo mono" aria-hidden="true">${escapeHtml(initial)}</span>`;
    return `<span class="org" title="${escapeHtml(o.name)}">${icon}<span class="org-name">${escapeHtml(o.name)}</span></span>`;
  }

  function cardHtml(paper, n) {
    const id = escapeHtml(paper.id);
    const starred = Boolean(state.stars[paper.id]);
    const summary = summaryText(paper);
    const orgs = paper.orgs || [];
    const orgHtml = orgs.slice(0, ORG_LIMIT).map(orgChip).join('')
      + (orgs.length > ORG_LIMIT ? `<span class="org more">+${orgs.length - ORG_LIMIT}</span>` : '');
    const date = escapeHtml((paper.published || '').slice(0, 10));
    const tags = (paper.tags || [])
      .map((tag) => `<button type="button" class="tag" data-tag="${escapeHtml(tag)}">${escapeHtml(tag)}</button>`)
      .join('<span class="dot">·</span>');
    const tip = escapeHtml(t('heatTip', paper.hf_upvotes || 0, paper.ax_votes || 0, paper.ax_visits || 0));
    const hf = (paper.hf_upvotes
      ? `<a class="hf" href="https://huggingface.co/papers/${id}" target="_blank" rel="noopener" title="${tip}">HF ▲ ${paper.hf_upvotes}</a>`
      : '')
      + (paper.ax_votes || paper.ax_visits
        ? `<a class="hf" href="https://www.alphaxiv.org/abs/${id}" target="_blank" rel="noopener" title="${tip}">alphaXiv${paper.ax_votes ? ` ▲ ${paper.ax_votes}` : ''}${paper.ax_visits ? ` · ${escapeHtml(t('views', paper.ax_visits))}` : ''}</a>`
        : '');
    const code = paper.code_url
      ? `<a class="act" href="${escapeHtml(paper.code_url)}" target="_blank" rel="noopener">[Code]</a>`
      : '';
    const thumb = state.showThumbs && paper.thumb
      ? `<button type="button" class="thumb" data-act="thumb" title="${escapeHtml(t('preview'))}"><img src="${escapeHtml(paper.thumb)}" width="${paper.thumb_w || ''}" height="${paper.thumb_h || ''}" loading="lazy" decoding="async" alt="${escapeHtml(t('preview'))}: ${escapeHtml(paper.title)}"></button>`
      : '';
    const subjects = (paper.categories || [paper.primary]).map((c) => catName(c)).join('; ');
    return `
<li class="paper${state.visited.has(paper.id) ? ' visited' : ''}${thumb ? ' has-thumb' : ''}" id="${id}" data-id="${id}">
  <a class="paper-index" href="https://arxiv.org/abs/${id}" target="_blank" rel="noopener" title="arXiv:${id}">#${n}</a>
  <div class="paper-main">
    <h2 class="paper-title">${escapeHtml(paper.title)}</h2>
    <div class="paper-sub">${orgHtml}${orgHtml ? '<span class="sub-sep" aria-hidden="true"></span>' : ''}<span class="authors">${authorsHtml(paper)}</span></div>
    ${summary ? `<p class="summary">${escapeHtml(summary)}</p>` : ''}
    <div class="paper-foot">
      <div class="paper-tags">
        <span class="date" title="${escapeHtml(subjects)}">${date}</span>
        ${tags}${hf}
      </div>
      <div class="paper-actions">
        <button type="button" class="act" data-act="pdf" aria-expanded="${state.openPdf.has(paper.id)}">[PDF]</button>
        <button type="button" class="act" data-act="copy">[Copy]</button>
        <button type="button" class="act" data-act="rel">[REL]</button>
        ${code}
        <button type="button" class="star" data-act="star" aria-pressed="${starred}" title="${escapeHtml(t(starred ? 'unstar' : 'star'))}">${STAR_SVG}</button>
      </div>
    </div>
  </div>
  ${thumb}
  <div class="paper-panels"><div class="slot-pdf"></div></div>
</li>`;
  }

  function render() {
    if (!state.day) return;
    renderChrome();
    const { list, total, rest } = visiblePapers();
    $('#total').textContent = t('total', list.length, total);
    const container = $('#papers');
    container.innerHTML = list.map((p, i) => cardHtml(p, i + 1)).join('')
      + (rest ? `<li class="show-rest"><button type="button" class="btn" data-act="show-rest">${escapeHtml(t('showRest', rest))}</button></li>` : '');
    $('#status').textContent = list.length ? '' : t('empty');
    renderMath(container);
    state.openPdf.forEach((id) => openPdf(id, false));
    renderStars();
    renderSearchBadge();
    syncUrl();
    if (!state.scrolledToHash && location.hash.length > 1) {
      state.scrolledToHash = true;
      scrollToPaper(decodeURIComponent(location.hash.slice(1)));
    }
  }

  function scrollToPaper(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.scrollIntoView({ block: 'start' });
    el.classList.remove('flash');
    void el.offsetWidth;
    el.classList.add('flash');
  }

  function findPaper(id) {
    return state.day?.papers.find((p) => p.id === id);
  }

  // ---------- PDF ----------
  function openPdf(id, toggle = true) {
    const card = document.getElementById(id);
    if (!card) return;
    const slot = $('.slot-pdf', card);
    const btn = $('[data-act="pdf"]', card);
    if (toggle && state.openPdf.has(id)) {
      state.openPdf.delete(id);
      slot.innerHTML = '';
      btn.setAttribute('aria-expanded', 'false');
      return;
    }
    state.openPdf.add(id);
    btn.setAttribute('aria-expanded', 'true');
    const url = `https://arxiv.org/pdf/${encodeURIComponent(id)}`;
    slot.innerHTML = `
<div class="panel pdf-panel">
  <div class="panel-head"><span>arXiv:${escapeHtml(id)}</span>
    <span class="head-actions"><a href="${url}" target="_blank" rel="noopener">${escapeHtml(t('openNew'))}</a>
    <button type="button" data-act="pdf">${escapeHtml(t('close'))}</button></span></div>
  <iframe src="${url}" title="PDF ${escapeHtml(id)}" loading="lazy"></iframe>
</div>`;
    markVisited(id);
  }

  // ---------- stars ----------
  function toggleStar(id) {
    const paper = findPaper(id);
    if (state.stars[id]) {
      delete state.stars[id];
    } else if (paper) {
      state.stars[id] = { title: paper.title, date: state.date, cat: state.cat, t: Date.now() };
    }
    store.set('dp-stars', state.stars);
    const btn = $('[data-act="star"]', document.getElementById(id) || document.createElement('div'));
    if (btn) {
      const on = Boolean(state.stars[id]);
      btn.setAttribute('aria-pressed', String(on));
      btn.title = t(on ? 'unstar' : 'star');
    }
    renderStars();
  }

  function starEntries() {
    return Object.entries(state.stars).sort((a, b) => (b[1].t || 0) - (a[1].t || 0));
  }

  function renderStars() {
    const entries = starEntries();
    $('#star-badge').textContent = entries.length ? String(entries.length) : '';
    $('#star-count').textContent = entries.length ? String(entries.length) : '';
    const list = $('#star-list');
    if (!entries.length) {
      list.innerHTML = `<li class="muted">${escapeHtml(t('noStars'))}</li>`;
      return;
    }
    list.innerHTML = entries.map(([id, s]) => {
      const href = `?date=${encodeURIComponent(s.date)}&cat=${encodeURIComponent(s.cat)}#${encodeURIComponent(id)}`;
      return `<li><span class="s-date">${escapeHtml(s.date)}</span><a href="${href}" data-jump="${escapeHtml(id)}" data-date="${escapeHtml(s.date)}" data-cat="${escapeHtml(s.cat)}">${escapeHtml(s.title)}</a><button type="button" data-unstar="${escapeHtml(id)}" title="${escapeHtml(t('unstar'))}">×</button></li>`;
    }).join('');
    renderMath(list);
  }

  function starsMarkdown() {
    return starEntries()
      .map(([id, s]) => `- [${s.title}](https://arxiv.org/abs/${id}) (${s.date})`)
      .join('\n');
  }

  // ---------- search panel ----------
  function renderSearchBadge() {
    const n = searchTerms().inc.length + searchTerms().exc.length;
    $('#search-badge').textContent = n ? String(n) : '';
  }

  function loadSearchForm() {
    $('#kw-include').value = state.search.inc;
    $('#kw-exclude').value = state.search.exc;
    $('#logic-btn').textContent = state.search.logic;
    $('#search-filter').checked = state.search.filter;
  }

  function applySearch() {
    state.search.inc = $('#kw-include').value;
    state.search.exc = $('#kw-exclude').value;
    state.search.filter = $('#search-filter').checked;
    render();
  }

  // ---------- preview lightbox ----------
  function openLightbox(paper) {
    if (!paper?.thumb) return;
    const box = $('#lightbox');
    box.innerHTML = `
<figure>
  <img src="${escapeHtml(paper.thumb)}" alt="${escapeHtml(t('preview'))}">
  <figcaption><span>${escapeHtml(paper.title)}</span> <a href="https://arxiv.org/abs/${escapeHtml(paper.id)}" target="_blank" rel="noopener">arXiv ↗</a></figcaption>
</figure>`;
    renderMath(box);
    box.hidden = false;
    document.body.classList.add('no-scroll');
  }

  function closeLightbox() {
    const box = $('#lightbox');
    if (box.hidden) return;
    box.hidden = true;
    box.innerHTML = '';
    document.body.classList.remove('no-scroll');
  }

  // ---------- events ----------
  function bind() {
    $('#papers').addEventListener('click', async (ev) => {
      const tagBtn = ev.target.closest('[data-tag]');
      const actBtn = ev.target.closest('[data-act]');
      if (ev.target.closest('[data-act="show-rest"]')) {
        state.showAll = true;
        render();
        return;
      }
      const card = ev.target.closest('.paper');
      if (!card) return;
      const id = card.dataset.id;
      if (tagBtn) {
        state.search.inc = tagBtn.dataset.tag;
        state.search.filter = true;
        loadSearchForm();
        render();
        return;
      }
      if (!actBtn) return;
      const paper = findPaper(id);
      switch (actBtn.dataset.act) {
        case 'pdf': openPdf(id); break;
        case 'star': toggleStar(id); break;
        case 'thumb': openLightbox(paper); break;
        case 'authors':
          state.expandedAuthors.add(id);
          $('.authors', card).innerHTML = authorsHtml(paper);
          break;
        case 'copy':
          try {
            await copyText(`${paper.title}\n${paper.authors.join(', ')}\nhttps://arxiv.org/abs/${id}`);
            toast(t('copied'));
          } catch (e) {
            toast(t('copyFail'));
          }
          break;
        case 'rel': {
          const tags = paper.tags || [];
          if (!tags.length) { toast(t('relNone')); break; }
          state.search.inc = tags.join('\n');
          state.search.logic = 'OR';
          state.search.filter = true;
            loadSearchForm();
          render();
          toast(t('relDone', tags.length));
          break;
        }
        default: break;
      }
    });

    $('#cat-nav').addEventListener('click', (ev) => {
      const a = ev.target.closest('[data-cat]');
      if (!a) return;
      ev.preventDefault();
      state.cat = a.dataset.cat;
      render();
    });

    $('#date-select').addEventListener('change', (ev) => { state.date = ev.target.value; loadDay(); });
    const step = (delta) => {
      const days = state.index.days.map((d) => d.date);
      const i = days.indexOf(state.date) + delta;
      if (i >= 0 && i < days.length) { state.date = days[i]; loadDay(); }
    };
    $('#prev-day').addEventListener('click', () => step(1));
    $('#next-day').addEventListener('click', () => step(-1));
    $('#sort-select').addEventListener('change', (ev) => { state.sort = ev.target.value; store.set('dp-sort', state.sort); render(); });

    $('#lang-btn').addEventListener('click', () => {
      state.lang = state.lang === 'zh' ? 'en' : 'zh';
      store.set('dp-lang', state.lang);
      applyI18n();
      render();
    });
    $('#theme-btn').addEventListener('click', () => {
      const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      store.set('dp-theme', next);
    });

    // bottom bar
    $$('.bar-btn[data-panel]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const id = btn.dataset.panel;
        const panel = document.getElementById(id);
        const willOpen = panel.hidden;
        $$('.bar-panel').forEach((p) => { p.hidden = true; });
        $$('.bar-btn[data-panel]').forEach((b) => b.setAttribute('aria-expanded', 'false'));
        if (willOpen) {
          panel.hidden = false;
          btn.setAttribute('aria-expanded', 'true');
          if (id === 'panel-search') $('#kw-include').focus();
        }
      });
    });
    const closePanels = () => {
      $$('.bar-panel').forEach((p) => { p.hidden = true; });
      $$('.bar-btn[data-panel]').forEach((b) => b.setAttribute('aria-expanded', 'false'));
    };
    document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') { closePanels(); closeLightbox(); } });
    document.addEventListener('click', (ev) => { if (!ev.target.closest('#app-bar')) closePanels(); });
    $('#top-btn').addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));

    $('#logic-btn').addEventListener('click', () => {
      state.search.logic = state.search.logic === 'OR' ? 'AND' : 'OR';
      $('#logic-btn').textContent = state.search.logic;
    });
    $('#search-apply').addEventListener('click', applySearch);
    $('#search-clear').addEventListener('click', () => {
      $('#kw-include').value = '';
      $('#kw-exclude').value = '';
      applySearch();
    });
    [$('#kw-include'), $('#kw-exclude')].forEach((ta) => ta.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && (ev.metaKey || ev.ctrlKey)) applySearch();
    }));

    $('#star-list').addEventListener('click', (ev) => {
      const un = ev.target.closest('[data-unstar]');
      if (un) { toggleStar(un.dataset.unstar); return; }
      const a = ev.target.closest('[data-jump]');
      if (a && a.dataset.date === state.date && a.dataset.cat === state.cat && document.getElementById(a.dataset.jump)) {
        ev.preventDefault();
        closePanels();
        scrollToPaper(a.dataset.jump);
      }
    });
    $('#export-md').addEventListener('click', async () => {
      try { await copyText(starsMarkdown()); toast(t('copied')); } catch (e) { toast(t('copyFail')); }
    });
    $('#export-dl').addEventListener('click', () => {
      const blob = new Blob([`${starsMarkdown()}\n`], { type: 'text/markdown' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `daily-paper-starred-${new Date().toISOString().slice(0, 10)}.md`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    });

    const opt = (sel, key, storeKey) => {
      const el = $(sel);
      el.checked = state[key];
      el.addEventListener('change', () => { state[key] = el.checked; store.set(storeKey, state[key]); render(); });
    };
    opt('#opt-thumbs', 'showThumbs', 'dp-thumbs');

    $('#lightbox').addEventListener('click', (ev) => {
      if (!ev.target.closest('a')) closeLightbox();
    });
  }

  async function init() {
    applyI18n();
    loadSearchForm();
    bind();
    try {
      await loadIndex();
    } catch (e) {
      $('#status').textContent = t('loadFail') + e.message;
      return;
    }
    await loadDay();
  }

  init();
})();
