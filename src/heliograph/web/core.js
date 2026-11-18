// Shared helpers: DOM builder, API client, formatting, a small LRU, toasts, preferences.
// Nothing here uses innerHTML: every piece of text goes through textContent / setAttribute.

const TOKEN = document.querySelector('meta[name="heliograph-token"]').content;

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/** h('div', {class:'x', onclick: fn, 'data-id': 3}, child, 'text', ...) */
export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  add(el, kids);
  return el;
}
export function add(el, kids) {
  for (const k of kids.flat()) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}
export function clear(el) { el.replaceChildren(); return el; }

const SVGNS = 'http://www.w3.org/2000/svg';
export function icon(name, cls = '') {
  const svg = document.createElementNS(SVGNS, 'svg');
  svg.setAttribute('class', 'ic ' + cls);
  const use = document.createElementNS(SVGNS, 'use');
  use.setAttribute('href', '#i-' + name);
  svg.append(use);
  return svg;
}

// ---------------------------------------------------------------- API
export async function api(path, params, { method = 'GET', body, signal } = {}) {
  let url = path;
  if (params) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== '' && v != null) q.set(k, v);
    const s = q.toString();
    if (s) url += (url.includes('?') ? '&' : '?') + s;
  }
  const r = await fetch(url, {
    method, signal, cache: 'no-store',
    headers: { 'X-Heliograph-Token': TOKEN, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let j = null;
  try { j = await r.json(); } catch { /* empty body */ }
  if (!r.ok) {
    const e = new Error((j && j.error) || `HTTP ${r.status}`);
    e.status = r.status;
    throw e;
  }
  return j;
}
export const post = (path, body) => api(path, null, { method: 'POST', body: body || {} });

// ---------------------------------------------------------------- formatting
export const fmt = {
  num: (n) => Number(n).toLocaleString(),
  bytes(n) {
    if (n == null) return '';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(v >= 100 ? 0 : 1)) + ' ' + u[i];
  },
  dur(sec) {
    if (sec == null || !isFinite(sec)) return '';
    sec = Math.max(0, Math.round(sec));
    const hh = Math.floor(sec / 3600), mm = Math.floor((sec % 3600) / 60), ss = sec % 60;
    return hh ? `${hh}:${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}` : `${mm}:${String(ss).padStart(2, '0')}`;
  },
  date: (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }),
  datetime: (ts) => new Date(ts * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }),
  month(ym) {
    const [y, m] = ym.split('-').map(Number);
    return new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
  },
  monthName: (m) => new Date(2000, Number(m) - 1, 1).toLocaleDateString(undefined, { month: 'long' }),
  ago(ts) {
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return 'just now';
    if (s < 5400) return Math.round(s / 60) + ' min ago';
    if (s < 129600) return Math.round(s / 3600) + ' h ago';
    return Math.round(s / 86400) + ' days ago';
  },
};

export const home = { path: '' };
export function tilde(p) { return home.path && p.startsWith(home.path) ? '~' + p.slice(home.path.length) : p; }
export function baseName(p) { return p.replace(/\/+$/, '').split('/').pop() || '/'; }

export function debounce(fn, ms) {
  let t;
  const d = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  d.cancel = () => clearTimeout(t);
  return d;
}

export class LRU {
  constructor(max, onEvict) { this.max = max; this.m = new Map(); this.onEvict = onEvict; }
  get(k) { if (!this.m.has(k)) return undefined; const v = this.m.get(k); this.m.delete(k); this.m.set(k, v); return v; }
  has(k) { return this.m.has(k); }
  set(k, v) {
    if (this.m.has(k)) this.m.delete(k);
    this.m.set(k, v);
    while (this.m.size > this.max) {
      const [ok, ov] = this.m.entries().next().value;
      this.m.delete(ok);
      if (this.onEvict) this.onEvict(ok, ov);
    }
  }
  delete(k) { this.m.delete(k); }
  clear() { if (this.onEvict) for (const [k, v] of this.m) this.onEvict(k, v); this.m.clear(); }
  get size() { return this.m.size; }
}

// ---------------------------------------------------------------- preferences (per-viewer conveniences)
export const prefs = {
  get(k, d) { try { const v = localStorage.getItem('hg.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('hg.' + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

export function toast(msg, kind = '') {
  const box = $('#toast');
  const el = h('div', { class: kind, text: msg });
  box.append(el);
  setTimeout(() => el.remove(), kind === 'err' ? 6500 : 3500);
}

export const KIND_ICON = { image: 'image', video: 'video', audio: 'audio' };

/** URL of a file's media, thumbnail or preview. The `k` tag says which file the page means: file ids are handed to
 *  other files after a folder is removed, so an id alone must never be used as a cache key or in a cached URL. */
export function fileUrl(what, it) { return `/${what}/${it.id}?k=${it.k || ''}`; }

/** Shared mutable app state. */
export const S = {
  state: null,          // /api/state
  scope: {},            // dir, kind, smart, year, month, sub, q
  view: null,           // current snapshot view {view,total,groups,version}
  prefs: { group: 'folder', sort: 'date', order: 'desc', tile: 190, fit: false },
};
