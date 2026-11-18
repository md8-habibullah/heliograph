// The virtual grid and the thumbnail scheduler.
//
// Why this exists: a library can have millions of files. The window must never create one DOM
// node, one image request or one decoded bitmap for anything that is not (nearly) on screen.
//   * The server keeps the sorted/grouped list of ids as a snapshot ("view"); this file asks for
//     pages of 120 items only when the rows they belong to come close to the viewport.
//   * Only the rows in (and one screen above/below) the viewport exist as DOM nodes - a few
//     hundred - and they are recycled while scrolling, so memory and CPU do not grow with the library.
//   * Thumbnails are requested by ThumbLoader: at most 4 at a time, nearest-to-the-centre first,
//     re-prioritised whenever the window settles, cancelled when scrolled away, and kept in a
//     bounded cache of object URLs. During a fast fling nothing is requested at all.

import { h, icon, api, post, fmt, LRU, S, KIND_ICON } from './core.js';

const PAGE = 120;
const GAP = 8;
const PAD = 20;
const HEADER_H = 44;
const GROUP_GAP = 10;
const MAX_PAGES = 60;

// ================================================================ thumbnails
/** Cache key of one thumbnail: the id AND the file's tag (see fileUrl in core.js). Ids are reused for other files. */
const tkey = (id, k) => id + ':' + (k || '');

export class ThumbLoader {
  constructor({ parallel = 4, cacheMax = 700 } = {}) {
    this.parallel = parallel;
    this.cache = new LRU(cacheMax, (_key, url) => URL.revokeObjectURL(url));
    this.failed = new Set();
    this.inflight = new Map();      // key -> {ctrl, extra}
    this.wanted = [];               // [{id, k}] highest priority first
    this.extra = [];                // low priority (folder covers)
    this.waiters = new Map();       // key -> [resolve]
    this.onLoad = () => {};
    this.onFail = () => {};
  }

  url(id, k) { return this.cache.get(tkey(id, k)) || null; }
  failedFor(id, k) { return this.failed.has(tkey(id, k)); }
  /** True when there is nothing left to do for this thumbnail (it is loaded, or loading it failed). */
  settled(id, k) { const key = tkey(id, k); return this.cache.has(key) || this.failed.has(key); }

  setWanted(list) {
    this.wanted = list.filter((x) => !this.settled(x.id, x.k));
    const keep = new Set(this.wanted.map((x) => tkey(x.id, x.k)));
    for (const [key, f] of this.inflight) if (!f.extra && !keep.has(key)) f.ctrl.abort();
    this.pump();
  }

  /** Low-priority load for something that is not in the grid (a folder cover). Resolves to a URL or null. */
  request(id, k) {
    const key = tkey(id, k);
    const u = this.cache.get(key);
    if (u) return Promise.resolve(u);
    if (this.failed.has(key)) return Promise.resolve(null);
    return new Promise((resolve) => {
      const w = this.waiters.get(key) || [];
      w.push(resolve);
      this.waiters.set(key, w);
      if (!this.inflight.has(key) && !this.extra.some((x) => tkey(x.id, x.k) === key)) this.extra.push({ id, k, extra: true });
      this.pump();
    });
  }

  pump() {
    const busy = (x) => this.cache.has(tkey(x.id, x.k)) || this.inflight.has(tkey(x.id, x.k));
    while (this.inflight.size < this.parallel) {
      let next = this.wanted.shift() || this.extra.shift();
      while (next && busy(next)) next = this.wanted.shift() || this.extra.shift();
      if (!next) break;
      this.run(next);
    }
  }

  async run({ id, k, extra }) {
    const key = tkey(id, k);
    const ctrl = new AbortController();
    this.inflight.set(key, { ctrl, extra: !!extra });
    let url = null;
    try {
      const r = await fetch(`/thumb/${id}?k=${encodeURIComponent(k || '')}`, { signal: ctrl.signal });
      if (r.ok) {
        url = URL.createObjectURL(await r.blob());
        this.cache.set(key, url);
        this.onLoad(id, url, k);
      } else {
        this.failed.add(key);
        this.onFail(id, k);
      }
    } catch (e) {
      if (e.name !== 'AbortError') { this.failed.add(key); this.onFail(id, k); }
    } finally {
      this.inflight.delete(key);
      const w = this.waiters.get(key);
      if (w) { this.waiters.delete(key); for (const f of w) f(url); }
      this.pump();
    }
  }

  reset() {
    for (const f of this.inflight.values()) f.ctrl.abort();
    this.wanted = [];
    this.extra = [];
  }
}

// ================================================================ the grid
export class Grid {
  constructor({ scroll, content, stick, loader, renderHeader, onOpen, onSelection, onFav, onCount }) {
    Object.assign(this, { scroll, content, stick, loader, renderHeader, onOpen, onSelection, onFav, onCount });
    this.tileMin = 190;
    this.view = null;
    this.groups = [];
    this.pages = new Map();
    this.pending = new Map();
    this.els = new Map();          // index -> element
    this.elById = new Map();       // item id -> element
    this.pool = [];
    this.selected = new Set();
    this.anchor = -1;
    this.cursor = -1;
    this.lastY = 0;
    this.lastT = 0;
    this.lastWant = 0;
    this.fast = false;
    this.stickIdx = -2;
    this.detailsAsked = new Set();

    scroll.addEventListener('scroll', () => this.onScroll(), { passive: true });
    new ResizeObserver(() => this.relayout()).observe(scroll);
    content.addEventListener('click', (e) => this.onClick(e));
    content.addEventListener('dblclick', (e) => e.preventDefault());
    // A result only belongs on a tile that still shows the very same file (its id may have been given to another one).
    loader.onLoad = (id, url, k) => {
      const el = this.elById.get(id);
      if (el && el._k === (k || undefined)) this.setThumb(el, url);
    };
    loader.onFail = (id, k) => { const el = this.elById.get(id); if (el && el._k === (k || undefined)) el.classList.add('nothumb'); };
  }

  // ---------------------------------------------------------------- view + layout
  setView(view, { keepScroll = false } = {}) {
    this.loader.reset();
    this.view = view;
    this.pages.clear();
    this.pending.forEach((c) => c.abort());
    this.pending.clear();
    this.detailsAsked.clear();
    this.selected.clear();
    this.content.classList.remove('selecting');
    this.anchor = -1;
    this.cursor = -1;
    let start = 0;
    this.groups = view.groups.length
      ? view.groups.map((g) => { const o = { key: g.key, count: g.count, start }; start += g.count; return o; })
      : [{ key: '', count: view.total, start: 0, plain: true }];
    this.total = view.total;
    this.clearTiles();
    this.layout();
    if (!keepScroll) this.scroll.scrollTop = 0;
    this.stickIdx = -2;
    this.render();
    this.settle();
    this.onSelection(this.selected);
    this.onCount && this.onCount(view.total);
  }

  clearTiles() {
    for (const el of this.els.values()) this.recycle(el);
    this.els.clear();
    this.elById.clear();
    this.content.replaceChildren();
    this.pool.length = 0;
  }

  layout() {
    const W = Math.max(100, this.scroll.clientWidth - PAD * 2);
    this.cols = Math.max(1, Math.floor((W + GAP) / (this.tileMin + GAP)));
    this.cell = Math.floor((W - (this.cols - 1) * GAP) / this.cols);
    this.rowH = this.cell + GAP;
    const n = this.groups.length;
    this.gTop = new Float64Array(n);
    this.gHdr = new Float64Array(n);
    let y = 6;
    for (let i = 0; i < n; i++) {
      const g = this.groups[i];
      const hdr = g.plain ? 0 : HEADER_H;
      this.gTop[i] = y;
      this.gHdr[i] = hdr;
      y += hdr + Math.ceil(g.count / this.cols) * this.rowH + (g.plain ? 0 : GROUP_GAP);
    }
    this.totalH = y + 24;
    this.content.style.height = this.totalH + 'px';
    this.content.style.setProperty('--cell', this.cell + 'px');
  }

  /** Keep the same picture at the top when the window or tile size changes. */
  relayout(newTileMin) {
    if (!this.view) return;
    if (newTileMin) this.tileMin = newTileMin;
    const top = this.topIndex();
    const delta = this.scroll.scrollTop - this.indexY(top);      // how far into (or above) that row the window was
    const oldCols = this.cols, oldCell = this.cell;
    this.layout();
    if (this.cols === oldCols && this.cell === oldCell && !newTileMin) return;
    for (const el of this.els.values()) { el._x = el._y = -1; }
    this.scroll.scrollTop = Math.max(0, this.indexY(top) + delta);
    this.render();
    this.settle();
  }

  groupAt(y) {                      // last group whose top is <= y
    let lo = 0, hi = this.groups.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (this.gTop[mid] <= y) lo = mid; else hi = mid - 1;
    }
    return lo;
  }

  topIndex() {
    if (!this.groups.length) return 0;
    const y = this.scroll.scrollTop;
    const i = this.groupAt(y);
    const g = this.groups[i];
    const row = Math.max(0, Math.floor((y - this.gTop[i] - this.gHdr[i]) / this.rowH));
    return Math.min(this.total - 1, g.start + row * this.cols);
  }

  indexY(idx) {
    let lo = 0, hi = this.groups.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (this.groups[mid].start <= idx) lo = mid; else hi = mid - 1;
    }
    const g = this.groups[lo];
    return this.gTop[lo] + this.gHdr[lo] + Math.floor((idx - g.start) / this.cols) * this.rowH;
  }

  scrollToIndex(idx, align = 'nearest') {
    if (idx < 0) return;
    const y = this.indexY(idx);
    const vh = this.scroll.clientHeight, st = this.scroll.scrollTop;
    if (align === 'start') this.scroll.scrollTop = Math.max(0, y - (this.groups[this.groupAt(y)].plain ? 0 : 0));
    else if (y < st + 4) this.scroll.scrollTop = Math.max(0, y - HEADER_H - 8);
    else if (y + this.cell > st + vh - 4) this.scroll.scrollTop = y + this.cell - vh + 16;
  }

  // ---------------------------------------------------------------- scrolling
  onScroll() {
    if (!this.raf) {
      this.raf = requestAnimationFrame(() => { this.raf = 0; this.render(); this.updateStick(); });
    }
    const now = performance.now();
    const y = this.scroll.scrollTop;
    const dt = now - this.lastT;
    const speed = dt > 0 ? (Math.abs(y - this.lastY) / dt) * 1000 : 0;
    this.lastY = y;
    this.lastT = now;
    this.fast = speed > this.scroll.clientHeight * 3.2;           // a fling: do not ask for anything yet
    clearTimeout(this.settleT);
    this.settleT = setTimeout(() => this.settle(), 150);
    if (!this.fast && now - this.lastWant > 220) { this.lastWant = now; this.settle(); }
  }

  updateStick() {
    if (!this.groups.length || this.groups[0].plain) { this.stick.hidden = true; return; }
    const st = this.scroll.scrollTop;
    const i = this.groupAt(st);
    if (st <= this.gTop[i] + 6) { this.stick.hidden = true; this.stickIdx = -2; return; }
    if (i !== this.stickIdx) {
      this.stickIdx = i;
      this.renderHeader(this.stick, this.groups[i]);
    }
    this.stick.hidden = false;
  }

  // ---------------------------------------------------------------- rendering of the visible window
  render() {
    if (!this.view) return;
    const vh = this.scroll.clientHeight, st = this.scroll.scrollTop;
    const from = Math.max(0, st - vh), to = st + vh * 2;
    const seen = new Set();
    const n = this.groups.length;
    const needPages = new Set();
    for (let i = this.groupAt(from); i < n && this.gTop[i] < to; i++) {
      const g = this.groups[i];
      const top = this.gTop[i], hdr = this.gHdr[i];
      if (hdr && top + hdr > from) this.placeHeader(i, g, top, seen);
      const rows = Math.ceil(g.count / this.cols);
      const r0 = Math.max(0, Math.floor((from - top - hdr) / this.rowH));
      const r1 = Math.min(rows - 1, Math.floor((to - top - hdr) / this.rowH));
      for (let r = r0; r <= r1; r++) {
        const y = top + hdr + r * this.rowH;
        for (let c = 0; c < this.cols; c++) {
          const idx = g.start + r * this.cols + c;
          if (idx >= g.start + g.count) break;
          this.placeTile(idx, PAD + c * (this.cell + GAP), y, seen);
          const p = (idx / PAGE) | 0;
          if (!this.pages.has(p)) needPages.add(p);
        }
      }
    }
    for (const [key, el] of this.els) {
      if (!seen.has(key)) { this.els.delete(key); this.recycle(el); }
    }
    if (!this.fast) for (const p of needPages) this.fetchPage(p);
  }

  placeHeader(i, g, top, seen) {
    const key = 'h' + i;
    seen.add(key);
    let el = this.els.get(key);
    if (!el) {
      el = h('div', { class: 'ghead' });
      el.style.transform = `translate3d(${PAD}px, ${top}px, 0)`;
      el._x = PAD; el._y = top;
      this.renderHeader(el, g);
      this.content.append(el);
      this.els.set(key, el);
      el._isHead = true;
    } else if (el._y !== top) {
      el.style.transform = `translate3d(${PAD}px, ${top}px, 0)`;
      el._y = top;
    }
  }

  placeTile(idx, x, y, seen) {
    seen.add(idx);
    let el = this.els.get(idx);
    if (!el) {
      el = this.pool.pop() || this.makeTile();
      this.els.set(idx, el);
      if (!el.isConnected) this.content.append(el);
      el._isHead = false;
      this.bind(el, idx);
    } else if (el._bindPending && this.itemAt(idx)) {
      this.bind(el, idx);
    }
    if (el._x !== x || el._y !== y) {
      el.style.transform = `translate3d(${x}px, ${y}px, 0)`;
      el._x = x; el._y = y;
    }
  }

  makeTile() {
    const el = h('div', { class: 'tile', role: 'button', tabindex: '-1' },
      h('div', { class: 'ph' }, icon('image'), h('span', { class: 'ext' })),
      h('img', { alt: '', draggable: 'false', decoding: 'async' }),
      h('span', { class: 'glyph' }, icon('play', 'fill')),
      h('div', { class: 'cap' }),
      h('span', { class: 'pill' }),
      h('button', { class: 'star', type: 'button', 'aria-label': 'Favourite', tabindex: '-1' }, icon('star')),
      h('span', { class: 'check' }, icon('check')));
    el._img = el.querySelector('img');
    el._ph = el.querySelector('.ph');
    el._cap = el.querySelector('.cap');
    el._pill = el.querySelector('.pill');
    el._x = el._y = -1;
    return el;
  }

  recycle(el) {
    if (el._id != null && this.elById.get(el._id) === el) this.elById.delete(el._id);
    el._id = null;
    if (el._img) el._img.removeAttribute('src');          // cancels a pending decode, frees the bitmap
    el.remove();
    el._x = el._y = -1;
    if (!el._isHead && this.pool.length < 400) this.pool.push(el);
  }

  // ---------------------------------------------------------------- items
  itemAt(idx) {
    const page = this.pages.get((idx / PAGE) | 0);
    return page ? page[idx % PAGE] : undefined;
  }

  bind(el, idx) {
    if (el._id != null && this.elById.get(el._id) === el) this.elById.delete(el._id);
    const it = this.itemAt(idx);
    el.dataset.idx = idx;
    if (!it || it.gone) {
      el._id = null;
      el._bindPending = !it;
      el.className = 'tile skel';
      el._img.removeAttribute('src');
      return;
    }
    el._bindPending = false;
    el._id = it.id;
    el._k = it.k;
    this.elById.set(it.id, el);
    el.dataset.id = it.id;
    el.className = `tile ${it.kind}` + (it.fav ? ' fav' : '') + (this.selected.has(it.id) ? ' sel' : '') +
      (idx === this.cursor ? ' cur' : '') + (this.loader.failedFor(it.id, it.k) ? ' nothumb' : '');
    el._ph.firstChild.firstChild.setAttribute('href', '#i-' + KIND_ICON[it.kind]);
    el._ph.lastChild.textContent = it.ext;
    el._cap.textContent = '';
    if (it.kind === 'audio') {
      el._cap.append(h('b', { text: it.title || it.name }), h('span', { text: it.artist || it.album || '' }));
    } else {
      el._cap.textContent = it.name;
    }
    el._pill.textContent = it.kind === 'video' ? (it.dur != null ? fmt.dur(it.dur) : it.ext.toUpperCase())
      : it.kind === 'audio' ? (it.dur != null ? fmt.dur(it.dur) : '') : (it.ext === 'gif' ? 'GIF' : '');
    el._pill.hidden = !el._pill.textContent;
    const u = this.loader.url(it.id, it.k);
    if (u) this.setThumb(el, u); else { el._img.removeAttribute('src'); el.classList.remove('loaded'); }
  }

  setThumb(el, url) {
    el._img.src = url;
    el.classList.add('loaded');
  }

  fetchPage(p, force = false) {
    if ((this.pages.has(p) && !force) || this.pending.has(p) || p * PAGE >= this.total) return Promise.resolve();
    const ctrl = new AbortController();
    this.pending.set(p, ctrl);
    const vid = this.view.view;
    return api('/api/items', { view: vid, offset: p * PAGE, limit: PAGE }, { signal: ctrl.signal }).then((r) => {
      if (!this.view || this.view.view !== vid) return;
      this.pages.set(p, r.items);
      this.trimPages();
      for (let i = 0; i < r.items.length; i++) {
        const idx = p * PAGE + i;
        const el = this.els.get(idx);
        if (el) this.bind(el, idx);
      }
      this.noteDetails(p, r.items);
      this.settleSoon();                       // the tiles just got their items: now decide which thumbnails to fetch
    }).catch((e) => {
      if (e.name === 'AbortError') return;
      if (e.status === 410) this.onExpired && this.onExpired();
    }).finally(() => this.pending.delete(p));
  }

  trimPages() {
    if (this.pages.size <= MAX_PAGES) return;
    const mid = (this.topIndex() / PAGE) | 0;
    const far = [...this.pages.keys()].sort((a, b) => Math.abs(b - mid) - Math.abs(a - mid));
    while (this.pages.size > MAX_PAGES && far.length) this.pages.delete(far.shift());
  }

  async ensureItem(idx) {
    if (idx < 0 || idx >= this.total) return null;
    let it = this.itemAt(idx);
    if (!it) { await this.fetchPage((idx / PAGE) | 0); it = this.itemAt(idx); }
    return it || null;
  }

  /** Ask the service to read details (size in pixels, duration) of items missing them, then refresh. */
  noteDetails(p, items) {
    const lo = this.scroll.scrollTop - this.scroll.clientHeight, hi = this.scroll.scrollTop + this.scroll.clientHeight * 2;
    const ids = [];
    items.forEach((it, i) => {
      if (it.gone || this.detailsAsked.has(it.id)) return;
      const missing = it.kind === 'image' ? it.w == null : it.dur == null;
      if (!missing) return;
      const y = this.indexY(p * PAGE + i);
      if (y >= lo && y <= hi) { ids.push(it.id); this.detailsAsked.add(it.id); }
    });
    if (!ids.length) return;
    post('/api/want', { ids: ids.slice(0, 120) }).catch(() => {});
    clearTimeout(this.refreshT);
    this.refreshT = setTimeout(() => {
      const mid = (this.topIndex() / PAGE) | 0;
      for (const q of [mid, mid + 1]) if (this.pages.has(q)) this.fetchPage(q, true);
    }, 1800);
  }

  settleSoon() {
    clearTimeout(this.soonT);
    this.soonT = setTimeout(() => { if (!this.fast) this.settle(); }, 40);
  }

  /** The window settled (or is scrolling slowly): decide which thumbnails to load. */
  settle() {
    if (!this.view) return;
    this.fast = false;
    this.render();
    const vh = this.scroll.clientHeight, st = this.scroll.scrollTop;
    const mid = st + vh / 2;
    const want = [];
    for (const [idx, el] of this.els) {
      if (el._isHead || el._id == null || this.loader.settled(el._id, el._k)) continue;
      const y = el._y;
      const inView = y + this.cell > st - 60 && y < st + vh + 60;
      const dist = Math.abs(y + this.cell / 2 - mid) + (inView ? 0 : 100000);
      const it = this.itemAt(idx);
      if (it) want.push({ id: it.id, k: it.k, dist });
    }
    want.sort((a, b) => a.dist - b.dist);
    this.loader.setWanted(want.slice(0, 90));
    for (const p of new Set([...this.els.keys()].filter((k) => typeof k === 'number').map((k) => (k / PAGE) | 0))) {
      if (this.pages.has(p)) this.noteDetails(p, this.pages.get(p));
    }
  }

  // ---------------------------------------------------------------- interaction
  onClick(e) {
    const el = e.target.closest('.tile');
    if (!el || el._id == null) return;
    const idx = Number(el.dataset.idx);
    const it = this.itemAt(idx);
    if (!it) return;
    if (e.target.closest('.star')) {
      e.stopPropagation();
      this.onFav(it);
      return;
    }
    this.cursor = idx;
    if (e.shiftKey && this.anchor >= 0) { this.selectRange(this.anchor, idx); return; }
    if (e.ctrlKey || e.metaKey || this.selected.size > 0 || e.target.closest('.check')) {
      this.toggle(it.id, idx);
      return;
    }
    this.onOpen(idx);
  }

  toggle(id, idx) {
    if (this.selected.has(id)) this.selected.delete(id); else this.selected.add(id);
    this.anchor = idx;
    this.paintSelection();
  }

  async selectRange(a, b) {
    const [lo, hi] = a < b ? [a, b] : [b, a];
    try {
      const r = await api('/api/ids', { view: this.view.view, from: lo, to: hi + 1 });
      r.ids.forEach((id) => this.selected.add(id));
    } catch { /* view expired */ }
    this.paintSelection();
  }

  async selectAll() {
    if (this.total > 20000) { this.onTooMany && this.onTooMany(); return; }
    try {
      const r = await api('/api/ids', { view: this.view.view, from: 0, to: this.total });
      this.selected = new Set(r.ids);
    } catch { return; }
    this.paintSelection();
  }

  clearSelection() { this.selected.clear(); this.anchor = -1; this.paintSelection(); }

  paintSelection() {
    for (const el of this.els.values()) {
      if (el._id != null) el.classList.toggle('sel', this.selected.has(el._id));
    }
    this.content.classList.toggle('selecting', this.selected.size > 0);
    this.onSelection(this.selected);
  }

  setCursor(idx, { open = false } = {}) {
    idx = Math.max(0, Math.min(this.total - 1, idx));
    const old = this.els.get(this.cursor);
    if (old) old.classList.remove('cur');
    this.cursor = idx;
    this.scrollToIndex(idx);
    this.render();
    const el = this.els.get(idx);
    if (el) el.classList.add('cur');
    this.ensureItem(idx);
    if (open) this.onOpen(idx);
  }

  moveCursor(dx, dy) {
    const cur = this.cursor < 0 ? this.topIndex() : this.cursor;
    this.setCursor(cur + dx + dy * this.cols);
  }

  /** Items of the given ids that are currently loaded (the rest are unknown to the page). */
  knownItems(ids) {
    const want = new Set(ids), out = [];
    for (const page of this.pages.values()) for (const it of page) if (want.has(it.id)) out.push(it);
    return out;
  }

  updateItem(id, patch) {
    for (const page of this.pages.values()) {
      const it = page.find((x) => x.id === id);
      if (it) Object.assign(it, patch);
    }
    const el = this.elById.get(id);
    if (el) {
      if ('fav' in patch) el.classList.toggle('fav', !!patch.fav);
    }
  }
}
