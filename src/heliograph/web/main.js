// Heliograph UI controller: routing, toolbar, selection, heartbeat, keyboard.

import { $, $$, h, icon, clear, api, post, fmt, prefs, toast, debounce, tilde, baseName, home, S } from './core.js';
import { ThumbLoader, Grid } from './grid.js';
import { createNav, renderStatus } from './nav.js';
import { createViewer } from './player.js';
import { createPicker, createConverter, createJobs } from './dialogs.js';
import { createHelp } from './help.js';

const SORTS = [['date:desc', 'Newest first'], ['date:asc', 'Oldest first'], ['name:asc', 'Name A–Z'], ['name:desc', 'Name Z–A'],
  ['size:desc', 'Largest first'], ['size:asc', 'Smallest first'], ['type:asc', 'By type']];
const GROUPS = [['folder', 'Group by folder'], ['date', 'Group by month'], ['type', 'Group by type'], ['none', 'No grouping']];
const KEYS = ['dir', 'kind', 'smart', 'year', 'month', 'sub', 'q'];
const KIND_NAME = { image: 'Pictures', video: 'Videos', audio: 'Audio' };
const SMART_NAME = { fav: 'Favourites', shots: 'Screenshots', recent: 'Last 30 days' };

let nav, viewer, picker, converter, jobs, grid, loader, help;
let loadSeq = 0, lastHb = null, hbFails = 0, seenNav = 0, seenSv = null, lastStatsRefresh = 0, lastAutoRefresh = 0;

// ================================================================ routing (the hash is the scope, so reload and Back work)
function parseHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  const scope = {};
  for (const k of KEYS) if (p.get(k)) scope[k] = p.get(k);
  return { scope, open: p.get('open') };
}
function serialize(scope) {
  const p = new URLSearchParams();
  for (const k of KEYS) if (scope[k]) p.set(k, scope[k]);
  return p.toString();
}
/** go(scope, true) navigates (adds a history entry); go(scope, false) replaces the current one. */
function go(scope, push = true) {
  const s = serialize(scope);
  if (s === location.hash.slice(1)) { applyHash(); return; }
  if (push) location.hash = s;
  else { history.replaceState(null, '', '#' + s); applyHash(); }
}
async function applyHash() {
  const { scope, open } = parseHash();
  S.scope = scope;
  if (open) history.replaceState(null, '', '#' + serialize(scope));
  syncToolbar();
  nav && nav.renderActive();
  await loadView();
  if (open) openById(Number(open));
}

// ================================================================ loading a view
function viewParams() {
  const s = S.scope, p = S.prefs;
  return { ...s, group: p.group, sort: p.sort, order: p.order };
}

/** Shows a slim progress line (and a note) if building a view takes noticeable time, as it can for a library of millions. */
function busyIndicator(my) {
  const timer = setTimeout(() => {
    if (my !== loadSeq) return;
    $('#loadbar').hidden = false;
    $('#scrollwrap').classList.add('loading');
    const n = S.state && S.state.totals ? S.state.totals.all : 0;
    if (n > 50000) $('#sub').textContent = `Sorting ${fmt.num(n)} files…`;
  }, 350);
  return () => { clearTimeout(timer); if (my === loadSeq) { $('#loadbar').hidden = true; $('#scrollwrap').classList.remove('loading'); } };
}

async function loadView({ keepScroll = false } = {}) {
  const my = ++loadSeq;
  const done = busyIndicator(my);
  let view;
  try { view = await api('/api/view', viewParams()); }
  catch (e) { done(); if (my === loadSeq) toast(e.message, 'err'); return; }
  done();
  if (my !== loadSeq) return;
  S.view = view;
  $('#banner').hidden = true;
  const empty = view.total === 0;
  $('#content').hidden = empty;
  grid.setView(view, { keepScroll });
  renderEmpty(empty);
  renderTitle();
  loadFolderStrip(my);
}

function renderTitle() {
  const s = S.scope;
  const t = $('#title');
  clear(t);
  let place;
  if (s.dir) {
    let parts = crumbParts(s.dir);
    if (parts.length > 6) { t.append(h('span', { class: 'sep', text: '…' })); parts = parts.slice(parts.length - 5); }
    parts.forEach((c, i) => {
      if (i || t.firstChild) t.append(h('span', { class: 'sep', text: '›' }));
      const last = i === parts.length - 1;
      t.append(h('button', { class: 'crumb' + (last ? ' last' : ''), type: 'button', title: tilde(c.path), text: c.label, onclick: last ? null : () => go({ dir: c.path }, true) }));
    });
    place = baseName(s.dir);
  } else {
    place = s.smart ? SMART_NAME[s.smart] : s.month ? fmt.month(`${s.year}-${s.month}`) : s.year ? s.year : s.kind ? KIND_NAME[s.kind] : 'All media';
    t.append(h('span', { class: 'crumb last', text: place }));
  }
  const bits = [];
  if (S.view) bits.push(`${fmt.num(S.view.total)} item${S.view.total === 1 ? '' : 's'}`);
  if (s.dir && s.kind) bits.push(KIND_NAME[s.kind].toLowerCase() + ' only');
  if (s.q) bits.push(`matching “${s.q}”`);
  if (s.dir && s.sub === '0') bits.push('this folder only');
  $('#sub').textContent = bits.join(' · ');
  document.title = `${place} — Heliograph`;
}

function crumbParts(path) {
  const hp = home.path;
  const out = [];
  let acc, rest;
  if (hp && (path === hp || path.startsWith(hp + '/'))) { out.push({ label: '~', path: hp }); acc = hp; rest = path.slice(hp.length).split('/').filter(Boolean); }
  else { out.push({ label: '/', path: '/' }); acc = ''; rest = path.split('/').filter(Boolean); }
  for (const seg of rest) { acc += '/' + seg; out.push({ label: seg, path: acc }); }
  return out;
}

function renderHeader(el, g) {
  clear(el);
  const mode = S.prefs.group;
  if (mode === 'folder') {
    let parts = crumbParts(g.key);
    const cut = parts.length > 5 ? parts.length - 4 : 0;
    const path = h('div', { class: 'path' });
    if (cut) path.append(h('span', { class: 'sep', text: '… ›' }));
    parts = parts.slice(cut);
    parts.forEach((c, i) => {
      if (i) path.append(h('span', { class: 'sep', text: '›' }));
      path.append(h('button', { type: 'button', class: i === parts.length - 1 ? 'last' : '', title: tilde(c.path), text: c.label, onclick: () => go({ dir: c.path }, true) }));
    });
    el.append(path);
  } else if (mode === 'date') {
    el.append(h('span', { class: 'label', text: /^\d{4}-\d{2}$/.test(g.key) ? fmt.month(g.key) : 'Unknown date' }));
  } else if (mode === 'type') {
    el.append(h('span', { class: 'label', text: KIND_NAME[g.key] || g.key }));
  }
  el.append(h('span', { class: 'count', text: fmt.num(g.count) }));
}

// ---------------------------------------------------------------- the folder strip (sub-folders with cover pictures)
const coverObserver = new IntersectionObserver((entries) => {
  for (const en of entries) {
    if (!en.isIntersecting) continue;
    coverObserver.unobserve(en.target);
    const el = en.target;
    for (const img of el.querySelectorAll('img[data-id]')) {
      loader.request(Number(img.dataset.id), img.dataset.k).then((u) => { if (u && img.isConnected) img.src = u; });
    }
  }
}, { root: $('#folders'), rootMargin: '200px' });

async function loadFolderStrip(my) {
  const strip = $('#folders');
  coverObserver.disconnect();
  clear(strip);
  strip.hidden = true;
  const s = S.scope;
  if (!s.dir || s.q || s.sub === '0') return;
  let r;
  try { r = await api('/api/folders', { dir: s.dir, kind: s.kind, limit: 40 }); } catch { return; }
  if (my !== loadSeq || !r.folders.length) return;
  for (const f of r.folders) {
    const covers = (f.covers || []).slice(0, 4);
    const m = h('div', { class: 'mosaic ' + (covers.length === 1 ? 'one' : covers.length === 2 ? 'two' : '') });
    if (!covers.length) m.append(h('div', { class: 'ph' }, icon('folder')));
    for (const c of covers) m.append(h('img', { alt: '', 'data-id': c.id, 'data-k': c.k, draggable: 'false' }));
    const tile = h('button', { class: 'ftile', type: 'button', title: tilde(f.path), onclick: () => go({ dir: f.path }, true) }, m,
      h('div', { class: 'fname', text: f.name }), h('div', { class: 'fcount', text: `${fmt.num(f.i + f.v + f.a)} items` }));
    strip.append(tile);
    coverObserver.observe(tile);
  }
  strip.hidden = false;
}

// ---------------------------------------------------------------- empty states
async function renderEmpty(empty) {
  const box = $('#empty');
  box.hidden = !empty;
  clear(box);
  if (!empty) return;
  const s = S.scope;
  const scanning = lastHb && lastHb.scan.state === 'scanning';
  if (!S.state.roots.length) {
    box.append(h('h2', { text: 'Welcome to Heliograph' }),
      h('p', { text: 'Pick a folder and everything inside it — pictures, videos and audio, however deeply nested — appears here, organised by folder and date. Nothing is copied or changed.' }));
    const quick = h('div', { class: 'quick' });
    box.append(quick);
    quick.append(h('button', { type: 'button', onclick: () => picker.open('folder') }, icon('folder-open', 'sm'), 'Open a folder…'),
      h('button', { type: 'button', onclick: () => picker.open('file') }, icon('file', 'sm'), 'Open a file…'));
    try {
      const r = await api('/api/fs', { path: S.state.home });
      const row = h('div', { class: 'quick' });
      for (const q of r.quick) row.append(h('button', { type: 'button', onclick: () => addToLibrary(q.path) }, icon('plus', 'sm'), `Add ${q.name}`));
      box.append(h('p', { style: 'margin-top:18px', text: 'Or add a common folder to your library:' }), row);
    } catch { /* no suggestions */ }
  } else if (scanning) {
    box.append(h('h2', { text: 'Looking for media…' }), h('p', { text: 'Files appear here as they are found.' }));
  } else if (s.q) {
    box.append(h('h2', { text: 'No results' }), h('p', { text: `Nothing matches “${s.q}”. Search looks at file names, folders, titles and artists.` }));
  } else if (s.smart === 'fav') {
    box.append(h('h2', { text: 'No favourites yet' }), h('p', { text: 'Press the star on a picture or video, or S while viewing it.' }));
  } else {
    box.append(h('h2', { text: 'Nothing here' }), h('p', { text: 'There are no pictures, videos or audio files in this view.' }));
  }
}

// ================================================================ toolbar
function syncToolbar() {
  const s = S.scope;
  for (const b of $$('#kinds button')) b.setAttribute('aria-pressed', String((b.dataset.kind || '') === (s.kind || '')));
  const subs = $('#subs');
  subs.hidden = !s.dir;
  subs.setAttribute('aria-pressed', String(s.sub !== '0'));
  if ($('#q') !== document.activeElement) $('#q').value = s.q || '';
}

function initToolbar() {
  const p = S.prefs;
  Object.assign(p, prefs.get('view', {}));
  const group = $('#group'), sort = $('#sort');
  GROUPS.forEach(([v, l]) => group.append(h('option', { value: v, text: l })));
  SORTS.forEach(([v, l]) => sort.append(h('option', { value: v, text: l })));
  group.value = p.group;
  sort.value = `${p.sort}:${p.order}`;
  $('#tile').value = p.tile;
  document.body.classList.toggle('fit', !!p.fit);
  $('#fit').setAttribute('aria-pressed', String(!!p.fit));
  const save = () => prefs.set('view', { group: p.group, sort: p.sort, order: p.order, tile: p.tile, fit: p.fit });

  for (const b of $$('#kinds button')) b.addEventListener('click', () => go({ ...S.scope, kind: b.dataset.kind || '' }, false));
  $('#subs').addEventListener('click', () => go({ ...S.scope, sub: S.scope.sub === '0' ? '' : '0' }, false));
  group.addEventListener('change', () => { p.group = group.value; save(); loadView(); });
  sort.addEventListener('change', () => { [p.sort, p.order] = sort.value.split(':'); save(); loadView(); });
  const resize = debounce(() => grid.relayout(Number($('#tile').value)), 40);
  $('#tile').addEventListener('input', () => { p.tile = Number($('#tile').value); save(); resize(); });
  $('#fit').addEventListener('click', () => { p.fit = !p.fit; save(); document.body.classList.toggle('fit', p.fit); $('#fit').setAttribute('aria-pressed', String(p.fit)); });
  const search = debounce(() => go({ ...S.scope, q: $('#q').value.trim() }, false), 260);
  $('#q').addEventListener('input', search);
  $('#q').addEventListener('keydown', (e) => { if (e.key === 'Escape') { $('#q').value = ''; search.cancel(); go({ ...S.scope, q: '' }, false); $('#q').blur(); } });
  $('#btn-folder').addEventListener('click', () => picker.open('folder'));
  $('#btn-file').addEventListener('click', () => picker.open('file'));
  $('#banner-go').addEventListener('click', () => refreshEverything());
}

// ================================================================ state, library changes
async function refreshState({ reloadNav = true } = {}) {
  S.state = await api('/api/state');
  home.path = S.state.home;
  if (reloadNav) { nav.invalidateChildren(); await nav.renderAll(); }
}

async function refreshEverything() {
  await refreshState();
  await loadView({ keepScroll: true });
}

async function afterRootChange() { await refreshEverything(); }

async function addToLibrary(path) {
  try {
    await post('/api/roots', { path });
    toast(`Added ${baseName(path)} to your library`);
    await refreshState();
    go({ dir: path }, true);
  } catch (e) { toast(e.message, 'err'); }
}

async function openFolderOnce(path) {
  try {
    const r = await post('/api/open', { path });
    await refreshState();
    go({ dir: r.dir }, true);
  } catch (e) { toast(e.message, 'err'); }
}

async function openFileExternal(path) {
  try {
    const r = await post('/api/open', { path });
    await refreshState();
    go({ dir: r.dir }, true);
    setTimeout(() => openById(r.id), 0);
  } catch (e) { toast(e.message, 'err'); }
}

async function openById(id) {
  if (!id) return;
  for (let tries = 0; tries < 8; tries++) {
    if (S.view) {
      try {
        const r = await api('/api/locate', { view: S.view.view, id });
        if (r.index >= 0) { viewer.open(r.index); return; }
      } catch { /* view replaced meanwhile */ }
    }
    await new Promise((res) => setTimeout(res, 400));
  }
  toast('That file is not in this view yet', 'err');
}

// ================================================================ favourites and selection
async function toggleFav(item) {
  const value = !item.fav;
  try { await post('/api/fav', { id: item.id, value }); } catch (e) { toast(e.message, 'err'); return; }
  item.fav = value;
  grid.updateItem(item.id, { fav: value });
  if (S.state) { S.state.totals.fav += value ? 1 : -1; nav.renderActive(); }
}

function onSelection(set) {
  const bar = $('#selbar');
  bar.hidden = set.size === 0;
  $('#sel-n').textContent = `${fmt.num(set.size)} selected`;
}

function initSelection() {
  $('#sel-clear').addEventListener('click', () => grid.clearSelection());
  $('#sel-all').addEventListener('click', () => grid.selectAll());
  $('#sel-convert').addEventListener('click', () => {
    const ids = [...grid.selected];
    converter.open(ids, grid.knownItems(ids));
  });
  $('#sel-fav').addEventListener('click', async () => {
    const ids = [...grid.selected];
    if (ids.length > 400) { toast('Select at most 400 files to favourite at once', 'err'); return; }
    const known = grid.knownItems(ids);
    const value = !(known.length && known.every((i) => i.fav));
    for (const id of ids) {
      try { await post('/api/fav', { id, value }); } catch { /* skip */ }
      grid.updateItem(id, { fav: value });
    }
    await refreshState({ reloadNav: false });
    nav.renderActive();
    toast(value ? 'Added to favourites' : 'Removed from favourites');
  });
}

// ================================================================ heartbeat: status, library changes, requests from the command line
async function beat() {
  try {
    const hb = await api('/api/hb');
    lastHb = hb;
    if (hbFails >= 3) { $('#down').hidden = true; }
    hbFails = 0;
    renderStatus(hb, togglePause);
    if (hb.nav && hb.nav.n !== seenNav) {
      seenNav = hb.nav.n;
      if (hb.nav.dir) {
        if (viewer.isOpen()) viewer.close();          // a new folder or file asked for from the command line replaces what is shown
        await refreshState(); go({ dir: hb.nav.dir }, true); if (hb.nav.file) setTimeout(() => openById(hb.nav.file), 0);
      }
    }
    if (S.view && hb.v !== S.view.version) onLibraryChanged(hb);
    if (hb.sv !== seenSv && Date.now() - lastStatsRefresh > 5000) { seenSv = hb.sv; lastStatsRefresh = Date.now(); statsChanged(); }
    if (hb.jobs.running || hb.jobs.queued) jobs.refresh();
  } catch (e) {
    hbFails++;
    if (hbFails >= 3) serviceDown();
  }
  setTimeout(beat, document.hidden ? 6000 : 1500);
}

async function statsChanged() {
  try {
    S.state = await api('/api/state');
    home.path = S.state.home;
    await nav.renderAll();
    if (lastHb) renderStatus(lastHb, togglePause);
  } catch { /* next beat */ }
}

function onLibraryChanged(hb) {
  const small = grid.total < 200;
  if (small && Date.now() - lastAutoRefresh > 2000) {          // an empty or tiny view fills up on its own while a scan runs
    lastAutoRefresh = Date.now();
    refreshEverything();
    return;
  }
  if (!viewer.isOpen()) $('#banner').hidden = false;
}

function serviceDown() {
  $('#down').hidden = false;
  const poll = async () => {
    try { const r = await fetch('/api/ping', { cache: 'no-store' }); if (r.ok) { location.reload(); return; } } catch { /* still down */ }
    setTimeout(poll, 2000);
  };
  if (!serviceDown.polling) { serviceDown.polling = true; poll(); }
}

async function togglePause() {
  const next = S.state.settings.bg_index === 'paused' ? 'auto' : 'paused';
  try { S.state.settings = await post('/api/settings', { bg_index: next }); } catch (e) { toast(e.message, 'err'); return; }
  if (lastHb) renderStatus(lastHb, togglePause);
}

// ================================================================ keyboard
function initKeys() {
  document.addEventListener('click', (e) => {                       // keep keyboard control with the page, not a clicked button
    const b = e.target.closest('#viewer .iconbtn, #viewer .vnav');
    if (b) b.blur();
  });
  document.addEventListener('keydown', (e) => {
    if (picker.isOpen() || converter.isOpen() || help.isOpen()) return;
    if (viewer.isOpen()) { if (viewer.key(e)) { e.preventDefault(); e.stopPropagation(); } return; }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'o') { e.preventDefault(); picker.open(e.shiftKey ? 'file' : 'folder'); return; }
    const t = e.target;
    if (t.matches && t.matches('input, select, textarea')) { if (e.key === 'Escape') t.blur(); return; }
    if ((e.key === ' ' || e.key === 'Enter') && t.closest && t.closest('button, [role=button]')) return;
    if (e.ctrlKey || e.metaKey || e.altKey) { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a' && S.view) { e.preventDefault(); grid.selectAll(); } return; }
    if (!S.view) return;
    const rows = Math.max(1, Math.floor($('#scroll').clientHeight / grid.rowH) - 1);
    switch (e.key) {
      case '/': e.preventDefault(); $('#q').focus(); $('#q').select(); break;
      case '?': e.preventDefault(); help.open('keys'); break;
      case 'ArrowRight': e.preventDefault(); grid.moveCursor(1, 0); break;
      case 'ArrowLeft': e.preventDefault(); grid.moveCursor(-1, 0); break;
      case 'ArrowDown': e.preventDefault(); grid.moveCursor(0, 1); break;
      case 'ArrowUp': e.preventDefault(); grid.moveCursor(0, -1); break;
      case 'PageDown': e.preventDefault(); grid.moveCursor(0, rows); break;
      case 'PageUp': e.preventDefault(); grid.moveCursor(0, -rows); break;
      case 'Home': e.preventDefault(); grid.setCursor(0); break;
      case 'End': e.preventDefault(); grid.setCursor(grid.total - 1); break;
      case 'Enter': if (grid.total) { e.preventDefault(); viewer.open(grid.cursor >= 0 ? grid.cursor : grid.topIndex()); } break;
      case 'x': case ' ': {
        if (grid.cursor >= 0) {
          e.preventDefault();
          grid.ensureItem(grid.cursor).then((it) => it && !it.gone && grid.toggle(it.id, grid.cursor));
        }
        break;
      }
      case 'Escape': if (grid.selected.size) grid.clearSelection(); break;
      default: break;
    }
  });
}

// ================================================================ start
async function start() {
  try { await refreshState({ reloadNav: false }); }
  catch (e) { serviceDown(); return; }
  initToolbarEarly();
  loader = new ThumbLoader();
  grid = new Grid({
    scroll: $('#scroll'), content: $('#content'), stick: $('#stick'), loader, renderHeader,
    onOpen: (idx) => viewer.open(idx),
    onSelection, onFav: toggleFav,
  });
  grid.onExpired = () => loadView({ keepScroll: true });
  grid.onTooMany = () => toast('Select at most 20,000 files at once', 'err');
  grid.tileMin = S.prefs.tile;
  nav = createNav({ go, openPicker: (m) => picker.open(m), refreshState, afterRootChange });
  picker = createPicker({
    onFolder: openFolderOnce,
    onLibrary: addToLibrary,
    onFile: openFileExternal,
  });
  help = createHelp();
  converter = createConverter({ onQueued: () => { grid.clearSelection(); jobs.show(); } });
  converter.picker = picker;
  jobs = createJobs({
    onShow: async (dst) => {
      const dir = dst.replace(/\/[^/]*$/, '') || '/';
      try { await post('/api/open', { path: dir }); await refreshState(); go({ dir }, true); } catch (e) { toast(e.message, 'err'); }
    },
  });
  viewer = createViewer({
    grid, loader, onFav: toggleFav,
    onConvert: (ids, items) => converter.open(ids, items),
    onFolder: (dir) => go({ dir }, true),
    onHelp: (kind) => help.open('keys', kind),
  });
  $('#btn-help').addEventListener('click', () => help.open('keys'));
  $('#btn-about').addEventListener('click', () => help.open('about'));
  initToolbar();
  initSelection();
  initKeys();
  window.addEventListener('hashchange', applyHash);
  window.addEventListener('pagehide', () => { try { grid.loader.reset(); } catch { /* closing */ } });
  await nav.renderAll();
  await applyHash();
  jobs.refresh();
  beat();
}

function initToolbarEarly() { Object.assign(S.prefs, prefs.get('view', {})); }

start();
