// Dialogs: the folder / file picker, the Convert dialog and the conversions panel.

import { $, h, icon, clear, api, post, fmt, prefs, toast, tilde, baseName, S, home } from './core.js';

// ================================================================ picker (open folder / open file / add to library / choose output)
export function createPicker({ onFolder, onLibrary, onFile }) {
  const modal = $('#modal'), title = $('#m-title'), quick = $('#m-quick'), form = $('#m-form'), input = $('#m-path');
  const list = $('#m-list'), hint = $('#m-hint'), bOpen = $('#m-open'), bAdd = $('#m-add'), bCancel = $('#m-cancel'), bUp = $('#m-up');
  let mode = 'folder', cwd = '', parent = null, picked = null, pickCb = null, last = null, seq = 0;

  const TITLES = { folder: 'Open a folder', library: 'Add a folder to the library', file: 'Open a file', dest: 'Choose the output folder' };

  function layout() {
    title.textContent = TITLES[mode];
    bOpen.hidden = mode === 'library';
    bAdd.hidden = mode !== 'folder' && mode !== 'library';
    bOpen.textContent = mode === 'dest' ? 'Use this folder' : 'Open';
    bAdd.className = mode === 'library' ? 'btn primary' : 'btn';
    bOpen.disabled = mode === 'file';
  }

  async function go(path) {
    const my = ++seq;
    let r;
    try { r = await api('/api/fs', { path, mode: mode === 'file' ? 'file' : '' }); }
    catch (e) { toast(e.message, 'err'); return; }
    if (my !== seq) return;
    last = r;
    cwd = r.path; parent = r.parent; picked = null;
    home.path = r.home;
    input.value = cwd;
    bUp.disabled = !parent;
    if (mode === 'file') bOpen.disabled = true;
    clear(quick);
    quick.append(h('button', { type: 'button', onclick: () => go(r.home) }, icon('monitor', 'sm'), 'Home'));
    for (const q of r.quick) quick.append(h('button', { type: 'button', onclick: () => go(q.path) }, icon(q.name.endsWith('(drive)') ? 'drive' : 'folder', 'sm'), q.name));
    clear(list);
    for (const d of r.dirs) list.append(h('button', { type: 'button', role: 'option', onclick: () => go(d.path) }, icon('folder', 'sm'), d.name));
    for (const f of r.files) {
      const b = h('button', { type: 'button', role: 'option', onclick: () => select(f, b), ondblclick: () => finishFile(f) }, icon(f.kind, 'sm'), f.name);
      list.append(b);
    }
    if (!r.dirs.length && !r.files.length) list.append(h('div', { class: 'none', text: mode === 'file' ? 'No folders or media files here.' : 'No sub-folders here.' }));
    hint.textContent = mode === 'file' ? `${r.dirs.length} folders · ${r.files.length} media files` : `${r.dirs.length} sub-folders · ${tilde(cwd)}`;
    list.scrollTop = 0;
  }

  function select(f, btn) {
    picked = f;
    for (const b of list.querySelectorAll('button.on')) b.classList.remove('on');
    btn.classList.add('on');
    bOpen.disabled = false;
    hint.textContent = f.name;
  }

  function finishFile(f) { close(); onFile(f.path); }

  function open(m, cb) {
    mode = m;
    pickCb = cb || null;
    layout();
    modal.hidden = false;
    go(prefs.get('pickerPath', '') || '');
    input.focus();
  }

  function close() { modal.hidden = true; seq++; }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const p = input.value.trim();
    if (!p) return;
    if (mode === 'file') {                                  // a typed path may be a file
      try {
        const r = await api('/api/fs', { path: p.replace(/\/[^/]*$/, '') || '/', mode: 'file' });
        const hit = r.files.find((f) => f.path === p);
        if (hit) { finishFile(hit); return; }
      } catch { /* fall through to folder navigation */ }
    }
    go(p);
  });
  bUp.addEventListener('click', () => parent && go(parent));
  bCancel.addEventListener('click', close);
  modal.addEventListener('mousedown', (e) => { if (e.target === modal) close(); });
  bOpen.addEventListener('click', () => {
    if (mode === 'file') { if (picked) finishFile(picked); return; }
    prefs.set('pickerPath', cwd);
    close();
    if (mode === 'dest') pickCb && pickCb(cwd); else onFolder(cwd);
  });
  bAdd.addEventListener('click', () => { prefs.set('pickerPath', cwd); close(); onLibrary(cwd); });
  document.addEventListener('keydown', (e) => { if (!modal.hidden && e.key === 'Escape') { e.stopPropagation(); close(); } }, true);
  return { open, close, isOpen: () => !modal.hidden };
}

// ================================================================ Convert dialog
const QUALITY = { high: 'Best quality', balanced: 'Balanced', small: 'Smallest file' };
const IMG_SIZES = [[0, 'Original size'], [7680, '7680 px (8K)'], [3840, '3840 px (4K)'], [2560, '2560 px'], [1920, '1920 px (Full HD)'], [1280, '1280 px'], [800, '800 px']];
const VID_SIZES = [[0, 'Original size'], [2160, '2160p (4K)'], [1440, '1440p'], [1080, '1080p (Full HD)'], [720, '720p'], [480, '480p'], [360, '360p']];
const DEFAULT_PRESET = { image: 'img-jpg', video: 'vid-mp4-h264', audio: 'aud-mp3' };

let presetsCache = null;
export async function loadPresets() {
  if (!presetsCache) presetsCache = await api('/api/convert/presets');
  return presetsCache;
}

export function createConverter({ onQueued }) {
  const dlg = $('#convert'), body = $('#c-body'), sub = $('#c-sub'), hint = $('#c-hint'), go = $('#c-go');
  let ids = [], kinds = [];

  function describe(items, n) {
    const names = { image: 'picture', video: 'video', audio: 'audio file' };
    if (items.length === n && n === 1) return items[0].name;
    const by = {};
    for (const it of items) by[it.kind] = (by[it.kind] || 0) + 1;
    const known = Object.entries(by).map(([k, c]) => `${fmt.num(c)} ${names[k]}${c === 1 ? '' : 's'}`);
    const unknown = n - items.length;
    if (unknown > 0) known.push(`${fmt.num(unknown)} more`);
    return known.join(' · ') + ' selected';
  }

  async function open(idList, items) {
    ids = idList;
    kinds = [...new Set(items.map((i) => i.kind))];
    let P;
    try { P = await loadPresets(); } catch (e) { toast(e.message, 'err'); return; }
    if (!P.tools.ffmpeg) { toast('FFmpeg is not installed. Install it to convert files.', 'err'); return; }
    sub.textContent = describe(items, ids.length) + '. Originals are never changed or overwritten.';
    clear(body);

    const shown = kinds.length && items.length === ids.length ? kinds : ['image', 'video', 'audio'];
    const sel = h('select', { id: 'c-preset', 'aria-label': 'Convert to' });
    const byKind = { image: [], video: [], audio: [] }, seen = new Set();
    for (const k of shown) for (const p of P.presets[k]) if (!seen.has(p.id)) { seen.add(p.id); byKind[p.kind].push(p); }
    const label = { image: 'Pictures', video: 'Video', audio: shown.includes('video') ? 'Audio only (extract the sound)' : 'Audio' };
    for (const k of [...new Set([...shown, 'image', 'video', 'audio'])]) {
      if (!byKind[k].length) continue;
      const g = h('optgroup', { label: label[k] });
      for (const p of byKind[k]) g.append(h('option', { value: p.id, text: p.label }));
      sel.append(g);
    }
    const remembered = prefs.get('preset', '');
    const first = DEFAULT_PRESET[shown[0]];
    const want = [...sel.options].some((o) => o.value === remembered) ? remembered : first;
    sel.value = [...sel.options].some((o) => o.value === want) ? want : sel.options[0].value;

    const desc = h('div', { class: 'desc' });
    const quality = h('select', { id: 'c-quality' }, Object.entries(QUALITY).map(([v, l]) => h('option', { value: v, text: l })));
    quality.value = prefs.get('quality', 'balanced');
    const size = h('select', { id: 'c-size' });
    const sizeLabel = h('span');
    const gpu = h('input', { type: 'checkbox', id: 'c-gpu' });
    gpu.checked = prefs.get('gpu', true);
    const strip = h('input', { type: 'checkbox', id: 'c-strip' });
    strip.checked = prefs.get('strip', false);
    const out = h('input', { type: 'text', id: 'c-out', placeholder: 'Next to the original file', readonly: true });
    const outBtn = h('button', { type: 'button', class: 'btn', text: 'Choose…' });
    const outClr = h('button', { type: 'button', class: 'btn', text: 'Reset' });
    outBtn.addEventListener('click', () => dialogs.picker.open('dest', (p) => { out.value = p; }));
    outClr.addEventListener('click', () => { out.value = ''; });

    const rQuality = h('label', null, h('span', { text: 'Quality' }), quality);
    const rSize = h('label', null, sizeLabel, size);
    const rGpu = h('label', { class: 'check' }, gpu, h('span', { text: P.gpu ? 'Use the graphics card when possible (faster, lower CPU use)' : 'Use the graphics card (none found: the CPU will be used)' }));
    const rStrip = h('label', { class: 'check' }, strip, h('span', { text: 'Remove metadata (location, camera, tags)' }));
    const rOut = h('label', null, h('span', { text: 'Save to' }), h('span', { style: 'display:flex;gap:6px' }, out, outBtn, outClr));
    body.append(h('label', null, h('span', { text: 'Convert to' }), sel), desc, rQuality, rSize, rGpu, rStrip, rOut);

    const byId = {};
    for (const k of ['image', 'video', 'audio']) for (const p of P.presets[k]) byId[p.id] = p;
    function refresh() {
      const p = byId[sel.value];
      desc.textContent = p.desc;
      const isImg = p.kind === 'image', isVid = p.kind === 'video';
      const copy = sel.value.endsWith('-copy');
      rSize.hidden = !(isImg || isVid) || copy;
      rQuality.hidden = copy || sel.value === 'vid-gif' || sel.value === 'img-bmp' || sel.value === 'img-tiff';
      rGpu.hidden = !(sel.value === 'vid-mp4-h264' || sel.value === 'vid-mp4-hevc') || !P.gpu;   // only these have a GPU encoder
      rStrip.hidden = p.kind === 'audio' ? false : copy;
      sizeLabel.textContent = isImg ? 'Longest side' : 'Maximum height';
      const opts = isImg ? IMG_SIZES : VID_SIZES;
      const keep = size.value;
      clear(size).append(...opts.map(([v, l]) => h('option', { value: String(v), text: l })));
      size.value = opts.some(([v]) => String(v) === keep) ? keep : '0';
    }
    sel.addEventListener('change', refresh);
    refresh();
    hint.textContent = '';
    go.disabled = false;
    go.onclick = async () => {
      const p = byId[sel.value];
      const spec = { ids, preset: sel.value, quality: quality.value, gpu: gpu.checked ? 'auto' : 'off', strip: strip.checked };
      if (!rSize.hidden) spec[p.kind === 'image' ? 'max_size' : 'max_height'] = Number(size.value);
      if (out.value) spec.out_dir = out.value;
      prefs.set('preset', sel.value); prefs.set('quality', quality.value); prefs.set('gpu', gpu.checked); prefs.set('strip', strip.checked);
      go.disabled = true;
      try {
        const r = await post('/api/convert', spec);
        close();
        toast(`Queued ${fmt.num(r.jobs.length)} conversion${r.jobs.length === 1 ? '' : 's'}` + (r.skipped ? ` (${fmt.num(r.skipped)} skipped: not suited to ${p.label})` : ''));
        onQueued();
      } catch (e) { hint.textContent = e.message; go.disabled = false; }
    };
    dlg.hidden = false;
    sel.focus();
  }

  function close() { dlg.hidden = true; }
  $('#c-cancel').addEventListener('click', close);
  dlg.addEventListener('mousedown', (e) => { if (e.target === dlg) close(); });
  document.addEventListener('keydown', (e) => { if (!dlg.hidden && e.key === 'Escape') { e.stopPropagation(); close(); } }, true);
  const dialogs = { open, close, picker: null, isOpen: () => !dlg.hidden };
  return dialogs;
}

// ================================================================ conversions panel
export function createJobs({ onShow }) {
  const panel = $('#jobs'), listEl = $('#jobs-list'), badge = $('#jobs-badge');
  let timer = 0, labels = null, busy = false;

  async function ensureLabels() {
    if (labels) return;
    try {
      const P = await loadPresets();
      labels = {};
      for (const k of ['image', 'video', 'audio']) for (const p of P.presets[k]) labels[p.id] = p.label;
    } catch { labels = {}; }
  }

  function row(j) {
    const name = baseName(j.src);
    const pct = Math.round((j.progress || 0) * 100);
    const st = { queued: 'Waiting', running: pct + '%', done: 'Done', failed: 'Failed', cancelled: 'Cancelled' }[j.status] || j.status;
    const meta = j.status === 'done' ? `${baseName(j.dst || '')} · ${fmt.bytes(j.bytes_in)} → ${fmt.bytes(j.bytes_out)}`
      : j.status === 'failed' ? (j.message || 'Failed') : (labels && labels[j.preset]) || j.preset;
    const top = h('div', { class: 'top' }, h('span', { class: 'nm', title: j.src, text: name }),
      h('span', { class: 'st ' + (j.status === 'failed' ? 'fail' : j.status === 'done' ? 'ok' : ''), text: st }));
    if (j.status === 'queued' || j.status === 'running') {
      top.append(h('button', { class: 'iconbtn', type: 'button', title: 'Cancel', 'aria-label': 'Cancel', onclick: async () => { await post('/api/jobs/cancel', { id: j.id }); refresh(); } }, icon('x', 'sm')));
    } else if (j.status === 'done' && j.dst) {
      top.append(h('button', { class: 'iconbtn', type: 'button', title: 'Show the result', 'aria-label': 'Show the result', onclick: () => onShow(j.dst) }, icon('folder', 'sm')));
    }
    const el = h('div', { class: 'job' }, top, h('div', { class: 'meta', text: meta }));
    if (j.status === 'running' || j.status === 'queued') el.append(h('div', { class: 'progress' }, h('i', { style: `width:${pct}%` })));
    return el;
  }

  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      await ensureLabels();
      const { jobs } = await api('/api/jobs');
      const active = jobs.filter((j) => j.status === 'running' || j.status === 'queued').length;
      badge.hidden = !active;
      badge.textContent = String(active);
      if (!panel.hidden) {
        clear(listEl);
        if (!jobs.length) listEl.append(h('div', { class: 'none', text: 'No conversions yet. Select pictures or videos and choose Convert.' }));
        for (const j of jobs) listEl.append(row(j));
      }
      schedule(active > 0);
    } catch { schedule(false); }
    busy = false;
  }

  function schedule(active) {
    clearTimeout(timer);
    if (active) timer = setTimeout(refresh, panel.hidden ? 2500 : 900);
  }

  function show() { panel.hidden = false; refresh(); }
  function hide() { panel.hidden = true; }
  $('#btn-jobs').addEventListener('click', () => (panel.hidden ? show() : hide()));
  $('#jobs-close').addEventListener('click', hide);
  $('#jobs-clear').addEventListener('click', async () => { await post('/api/jobs/clear'); refresh(); });
  return { refresh, show, hide, isOpen: () => !panel.hidden };
}
