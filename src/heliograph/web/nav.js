// The sidebar: library, smart views, timeline, folder tree (loaded lazily, level by level), opened folders
// and the status block with the "pause background work" switch.

import { $, h, icon, clear, api, post, fmt, tilde, baseName, S, toast } from './core.js';

const expanded = new Set(JSON.parse(sessionStorage.getItem('hg.expanded') || '[]'));
const children = new Map();           // dir -> {folders, total}
let years = [];
const yearOpen = new Set();

function saveExpanded() { try { sessionStorage.setItem('hg.expanded', JSON.stringify([...expanded])); } catch { /* ignore */ } }

function item({ ic, label, n, active, offline, onclick, title, actions, depth = 0, twisty }) {
  const row = h('div', { class: 'nav-item' + (active ? ' active' : '') + (offline ? ' offline' : '') + (depth >= 0 ? ' tree-row' : ''),
    style: `--depth:${depth}`, role: 'button', tabindex: '0', title: title || '', onclick });
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter') onclick(e); });
  if (twisty) row.append(twisty);
  if (ic) row.append(icon(ic, 'sm'));
  row.append(h('span', { class: 'label', text: label }));
  if (actions && actions.length) row.append(h('span', { class: 'row-actions' }, actions));
  if (n != null) row.append(h('span', { class: 'n', text: fmt.num(n) }));
  return row;
}

function actionBtn(ic, title, fn) {
  return h('button', { class: 'iconbtn', type: 'button', title, 'aria-label': title,
    onclick: (e) => { e.stopPropagation(); fn(); } }, icon(ic, 'sm'));
}

export function createNav({ go, openPicker, refreshState, afterRootChange }) {
  const sc = () => S.scope;
  const isLib = (k) => !sc().dir && !sc().smart && !sc().year && (sc().kind || '') === k;

  async function loadChildren(dir) {
    if (children.has(dir)) return;
    try { children.set(dir, await api('/api/folders', { dir, covers: '0', limit: 400 })); } catch { children.set(dir, { folders: [], total: 0 }); }
  }

  function renderLibrary() {
    const t = S.state.totals;
    const el = clear($('#nav-library'));
    el.append(
      item({ ic: 'all', label: 'All media', n: t.all, active: isLib('') && !sc().q || (isLib('') && sc().q), onclick: () => go({}, true), depth: -1 }),
      item({ ic: 'image', label: 'Pictures', n: t.image, active: isLib('image'), onclick: () => go({ kind: 'image' }, true), depth: -1 }),
      item({ ic: 'video', label: 'Videos', n: t.video, active: isLib('video'), onclick: () => go({ kind: 'video' }, true), depth: -1 }),
      item({ ic: 'audio', label: 'Audio', n: t.audio, active: isLib('audio'), onclick: () => go({ kind: 'audio' }, true), depth: -1 }));
  }

  function renderSmart() {
    const t = S.state.totals;
    clear($('#nav-smart')).append(
      item({ ic: 'star', label: 'Favourites', n: t.fav, active: sc().smart === 'fav', onclick: () => go({ smart: 'fav' }, true), depth: -1 }),
      item({ ic: 'monitor', label: 'Screenshots', n: t.shots, active: sc().smart === 'shots', onclick: () => go({ smart: 'shots' }, true), depth: -1 }),
      item({ ic: 'clock', label: 'Last 30 days', n: t.recent, active: sc().smart === 'recent', onclick: () => go({ smart: 'recent' }, true), depth: -1 }));
  }

  function renderTime() {
    const el = clear($('#nav-time'));
    if (!years.length) return;
    el.append(h('div', { class: 'sec-head' }, h('span', { text: 'Timeline' })));
    for (const y of years) {
      const open = yearOpen.has(y.year) || sc().year === y.year;
      const tw = h('button', { class: 'twisty' + (open ? ' open' : ''), type: 'button', 'aria-label': 'Show months',
        onclick: (e) => { e.stopPropagation(); if (yearOpen.has(y.year)) yearOpen.delete(y.year); else yearOpen.add(y.year); renderTime(); } }, icon('chev'));
      el.append(item({ ic: 'calendar', label: y.year, n: y.count, active: sc().year === y.year && !sc().month,
        onclick: () => go({ year: y.year }, true), depth: 0, twisty: tw }));
      if (open) {
        const box = h('div', { class: 'sub-months' });
        for (const m of y.months) {
          box.append(item({ label: fmt.monthName(m.month), n: m.count, depth: 1,
            active: sc().year === y.year && sc().month === m.month, onclick: () => go({ year: y.year, month: m.month }, true) }));
        }
        el.append(box);
      }
    }
  }

  function treeRows(dir, depth, out) {
    const c = children.get(dir);
    if (!c) return;
    for (const f of c.folders) {
      const open = expanded.has(f.path);
      const tw = h('button', { class: 'twisty' + (open ? ' open' : '') + ((f.i + f.v + f.a) ? '' : ' none'), type: 'button', 'aria-label': 'Expand',
        onclick: (e) => { e.stopPropagation(); toggle(f.path); } }, icon('chev'));
      out.push(item({ ic: 'folder', label: f.name, n: f.i + f.v + f.a, active: sc().dir === f.path && !sc().kind && !sc().smart,
        onclick: () => go({ dir: f.path }, true), depth, twisty: tw, title: tilde(f.path) }));
      if (open) treeRows(f.path, depth + 1, out);
    }
    if (c.total > c.folders.length) out.push(h('div', { class: 'nav-more', text: `+ ${fmt.num(c.total - c.folders.length)} more folders (use search)` }));
  }

  async function toggle(dir) {
    if (expanded.has(dir)) expanded.delete(dir);
    else { expanded.add(dir); await loadChildren(dir); }
    saveExpanded();
    renderFolders();
    renderOpened();
  }

  function rootRow(r, withActions) {
    const open = expanded.has(r.path);
    const tw = h('button', { class: 'twisty' + (open ? ' open' : ''), type: 'button', 'aria-label': 'Expand',
      onclick: (e) => { e.stopPropagation(); toggle(r.path); } }, icon('chev'));
    const acts = withActions === 'library'
      ? [actionBtn('refresh', 'Rescan this folder', async () => { await post('/api/rescan', { id: r.id }); toast('Scanning ' + baseName(r.path)); }),
         actionBtn('x', 'Remove from the library (files stay on disk)', async () => {
           await post('/api/roots/remove', { id: r.id });
           if (S.scope.dir && S.scope.dir.startsWith(r.path)) go({}, true);
           await afterRootChange();
         })]
      : [actionBtn('plus', 'Add to the library', async () => { await post('/api/roots', { path: r.path }); toast('Added to the library'); await afterRootChange(); }),
         actionBtn('x', 'Close', async () => {
           await post('/api/roots/remove', { id: r.id });
           if (S.scope.dir && S.scope.dir.startsWith(r.path)) go({}, true);
           await afterRootChange();
         })];
    return item({ ic: withActions === 'library' ? 'folder' : 'folder-open', label: baseName(r.path), n: r.count, offline: !r.online,
      active: sc().dir === r.path && !sc().kind && !sc().smart, onclick: () => go({ dir: r.path }, true), depth: 0, twisty: tw,
      title: tilde(r.path) + (r.online ? '' : ' (not available)'), actions: acts });
  }

  function renderFolders() {
    const el = clear($('#nav-folders'));
    el.append(h('div', { class: 'sec-head' }, h('span', { text: 'Folders' }),
      h('button', { class: 'iconbtn', type: 'button', title: 'Add a folder to the library', 'aria-label': 'Add a folder', onclick: () => openPicker('library') }, icon('plus', 'sm'))));
    const roots = S.state.roots.filter((r) => r.persist);
    if (!roots.length) el.append(h('div', { class: 'nav-empty', text: 'No folders yet. Use “Add a folder” or open one.' }));
    for (const r of roots) {
      el.append(rootRow(r, 'library'));
      if (expanded.has(r.path)) { const out = []; treeRows(r.path, 1, out); el.append(...out); }
    }
  }

  function renderOpened() {
    const roots = S.state.roots.filter((r) => !r.persist);
    const el = clear($('#nav-opened'));
    el.hidden = roots.length === 0;
    if (!roots.length) return;
    el.append(h('div', { class: 'sec-head' }, h('span', { text: 'Opened (not in the library)' })));
    for (const r of roots) {
      el.append(rootRow(r, 'opened'));
      if (expanded.has(r.path)) { const out = []; treeRows(r.path, 1, out); el.append(...out); }
    }
  }

  async function renderAll({ reloadYears = true } = {}) {
    if (!S.state) return;
    renderLibrary();
    renderSmart();
    await Promise.all(S.state.roots.filter((r) => expanded.has(r.path)).map((r) => loadChildren(r.path)));
    renderFolders();
    renderOpened();
    if (reloadYears) {
      try { years = (await api('/api/years')).years; } catch { years = []; }
    }
    renderTime();
  }

  function invalidateChildren() { children.clear(); }

  function renderActive() { renderLibrary(); renderSmart(); renderTime(); renderFolders(); renderOpened(); }

  return { renderAll, renderActive, invalidateChildren, loadChildren };
}

// ---------------------------------------------------------------- status block
export function renderStatus(hb, onTogglePause) {
  const el = clear($('#status'));
  if (!hb || !S.state) return;
  const sc = hb.scan, en = hb.enrich;
  const files = S.state.totals.all;
  const busyScan = sc.state === 'scanning';
  el.append(h('div', { class: 'line' }, h('span', { class: 'dot' + (busyScan ? ' busy' : '') }),
    h('span', { text: busyScan ? `Scanning… ${fmt.num(sc.files)} files` : `${fmt.num(files)} files in the library` })));
  const done = en.done, rem = en.remaining;
  if (rem > 0 || en.state === 'running') {
    const total = done + rem;
    const frac = total ? done / total : 0;
    const label = en.state === 'paused' ? 'Details paused'
      : en.state === 'waiting' ? 'Details on hold' : 'Reading details';
    el.append(h('div', { class: 'line' }, h('span', { class: 'dot' + (en.state === 'running' ? ' busy' : ' hold') }),
      h('span', { text: `${label}: ${fmt.num(done)} of ${fmt.num(total)}` })));
    el.append(h('div', { class: 'progress' }, h('i', { style: `width:${(frac * 100).toFixed(1)}%` })));
    if (en.reason) el.append(h('div', { class: 'hint', text: en.reason }));
    else el.append(h('div', { class: 'hint', text: 'Files on screen are read first; the rest in small bursts.' }));
  }
  const paused = S.state.settings.bg_index === 'paused';
  el.append(h('div', { class: 'line' },
    h('button', { class: 'linkbtn', type: 'button', onclick: onTogglePause, title: 'Stops reading file details in the background. Thumbnails of what you look at still load.' },
      icon(paused ? 'play' : 'pause', 'sm'), paused ? 'Resume background work' : 'Pause background work'),
    hb.gov === 'busy' ? h('span', { class: 'hint', text: 'taking it easy' }) : null));
}
