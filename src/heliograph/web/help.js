// The help and About window: every keyboard shortcut, a few tips, and what Heliograph is.
// One data table drives the shortcuts, so the text is easy to keep in step with the key handlers in main.js and player.js.
// Nothing here uses innerHTML. Links are plain anchors that open in the default browser only when the user clicks them.

import { $, h, clear, toast, S } from './core.js';

/** [keys, what it does, separator?]. Keys are alternatives ("or"), or a back/forward pair when the third item is "/".
 *  A "+" inside one key means "together". */
const SECTIONS = [
  { id: 'library', title: 'Library', note: 'The grid of pictures, videos and music', rows: [
    [['← → ↑ ↓'], 'Move around the grid'],
    [['PageUp', 'PageDown'], 'Move one screen up / down', '/'],
    [['Home', 'End'], 'First / last item', '/'],
    [['Enter'], 'Open the item in the viewer'],
    [['Space', 'X'], 'Select or unselect the item'],
    [['Ctrl+A'], 'Select everything in this view'],
    [['Esc'], 'Clear the selection'],
    [['/'], 'Search'],
    [['Ctrl+O'], 'Open a folder'],
    [['Ctrl+Shift+O'], 'Open a single file'],
    [['?'], 'Show this help'],
  ] },
  { id: 'image', title: 'Pictures', note: 'In the viewer', rows: [
    [['←', '→'], 'Previous / next picture (PageUp and PageDown do the same)', '/'],
    [['Home', 'End'], 'First / last in this view', '/'],
    [['Z'], 'Zoom in to the original, again to fit (clicking the picture does the same)'],
    [['I'], 'Details: size, camera, folder'],
    [['S'], 'Favourite'],
    [['Shift+C'], 'Convert (format, size, quality)'],
    [['F'], 'Full screen'],
    [['Esc'], 'Close the viewer'],
  ] },
  { id: 'video', title: 'Video', note: 'The same keys as YouTube', rows: [
    [['Space', 'K'], 'Play / pause'],
    [['←', '→'], 'Back / forward 5 seconds', '/'],
    [['J', 'L'], 'Back / forward 10 seconds', '/'],
    [['↑', '↓'], 'Volume up / down', '/'],
    [['M'], 'Mute'],
    [['0 … 9'], 'Jump to 0% … 90% of the video'],
    [['Home', 'End'], 'Start / end', '/'],
    [[',', '.'], 'One frame back / forward (pauses; only for files that play directly)', '/'],
    [['Shift+,', 'Shift+.'], 'Slower / faster, from 0.25× up to 3.5×', '/'],
    [['C'], 'Subtitles on / off'],
    [['R'], 'Repeat this video'],
    [['F'], 'Full screen (double-clicking the video does the same)'],
    [['PageUp', 'PageDown'], 'Previous / next video', '/'],
  ] },
  { id: 'audio', title: 'Music and audio', note: 'Plays on, like a music app', rows: [
    [['Space', 'K'], 'Play / pause'],
    [['Shift+N', 'Shift+P'], 'Next / previous track (Shift+P goes back to the start of a track that has played for more than 3 seconds)', '/'],
    [['←', '→'], 'Back / forward 5 seconds', '/'],
    [['J', 'L'], 'Back / forward 10 seconds', '/'],
    [['↑', '↓'], 'Volume up / down', '/'],
    [['M'], 'Mute'],
    [['0 … 9'], 'Jump to 0% … 90% of the track'],
    [['Shift+,', 'Shift+.'], 'Slower / faster (the pitch stays the same)', '/'],
    [['R'], 'Repeat this track'],
    [['PageUp', 'PageDown'], 'Previous / next track', '/'],
  ] },
  { id: 'anywhere', title: 'Anywhere in the viewer', note: '', rows: [
    [['?'], 'This help'],
    [['I'], 'Details'],
    [['S'], 'Favourite'],
    [['Shift+C'], 'Convert'],
    [['F'], 'Full screen'],
    [['Esc'], 'Close'],
  ] },
];

const TIPS = [
  'Open a folder with Open folder (Ctrl+O) to browse it just for now, or choose Add to library to keep it in the sidebar.',
  'Open a single picture or video from your file manager and the files next to it are one arrow key away.',
  'Only what is on screen is loaded. A fast scroll loads nothing until you stop, so a folder of tens of thousands of files stays light.',
  'Videos the browser cannot play (MOV, MKV, HEVC, 10-bit and more) are repackaged or converted on the fly. The Details panel (I) says which.',
  'In the player, the speed, repeat and autoplay buttons remember your choice separately for video and for music.',
  'Media keys on your keyboard and your desktop’s media controls can play, pause and skip when your desktop supports them.',
  'Select several files (Space, Ctrl+click or Shift+click) to convert them all at once. Originals are never changed.',
];

function keycap(text) {
  const parts = text.split('+');
  const out = [];
  parts.forEach((p, i) => {
    if (i) out.push(h('span', { class: 'plus', text: '+' }));
    out.push(h('kbd', { text: p }));
  });
  return out;
}

function keysCell(keys, sep) {
  const cell = h('span', { class: 'keys' });
  keys.forEach((k, i) => {
    if (i) cell.append(h('span', { class: 'or', text: sep === '/' ? '/' : 'or' }));
    cell.append(...keycap(k));
  });
  return cell;
}

function link(url, label) {
  // target=_blank with noopener keeps this window as it is; the page never opens a link by itself.
  return h('a', { href: url, target: '_blank', rel: 'noopener noreferrer', text: label || url.replace(/^https?:\/\//, '') });
}

export function createHelp() {
  const root = $('#help'), body = $('#h-body'), tabs = $('#h-tabs');
  let tab = 'keys', focusKind = null, returnTo = null;

  function section(sec, on) {
    return h('section', { class: 'hsec' + (on ? ' on' : ''), 'data-sec': sec.id },
      h('h3', null, sec.title, sec.note ? h('span', { text: sec.note }) : null),
      h('div', { class: 'hrows' }, sec.rows.map(([keys, what, sep]) => h('div', { class: 'hrow' }, keysCell(keys, sep), h('span', { class: 'what', text: what })))));
  }

  function paintKeys() {
    // The section for what is open right now comes first and is marked.
    const order = [...SECTIONS];
    const i = focusKind ? order.findIndex((s) => s.id === focusKind) : -1;
    if (i > 0) order.unshift(order.splice(i, 1)[0]);
    body.append(...order.map((s) => section(s, s.id === focusKind)));
    body.append(h('section', { class: 'hsec wide' }, h('h3', null, 'Good to know'), h('ul', { class: 'tips' }, TIPS.map((t) => h('li', { text: t })))));
  }

  function paintAbout() {
    const a = (S.state && S.state.about) || {};
    const f = a.formats || {};
    const tools = (S.state && S.state.tools) || {};
    body.append(
      h('section', { class: 'about' },
        h('div', { class: 'about-head' },
          h('img', { src: '/static/icon.svg', alt: '', width: '56', height: '56' }),
          h('div', null,
            h('h3', { text: 'Heliograph' }),
            h('p', { class: 'ver', text: 'Version ' + ((S.state && S.state.version) || '') + ' · ' + (a.license || 'MIT') + ' licence · free and open source' }))),
        h('p', { text: 'A fast, private media library and player for Linux. Point it at a folder with thousands of pictures, videos and songs: it opens at once, loads only what is on your screen, and keeps out of the PC’s way.' }),
        h('h4', { text: 'What it handles' }),
        h('ul', null,
          h('li', null, h('b', { text: 'Pictures: ' }), (f.image || 0) + ' file extensions. JPEG, PNG, GIF, WebP, AVIF, TIFF, BMP and SVG, plus HEIC and camera RAW when the optional converters are installed.'),
          h('li', null, h('b', { text: 'Video: ' }), (f.video || 0) + ' file extensions. MP4, MOV, MKV, AVI, WebM, WMV, MPEG and more, played directly when possible and repackaged or converted on the fly when not.'),
          h('li', null, h('b', { text: 'Audio: ' }), (f.audio || 0) + ' file extensions. MP3, FLAC, WAV, AAC, M4A, OGG, Opus and more, with a music-player view.'),
          h('li', null, h('b', { text: 'Convert: ' }), 'pictures, video and audio to other formats with FFmpeg, one job at a time, originals untouched.'),
          h('li', null, h('b', { text: 'Local and private: ' }), 'everything stays on this PC in a local SQLite database. No account, no telemetry.')),
        h('p', { class: 'small', text: 'Anything else FFmpeg or ImageMagick can read is a best-effort bonus. What works for the rarer formats depends on the tools installed on this PC (FFmpeg: '
          + (tools.ffmpeg ? 'found' : 'not found') + ', Pillow: ' + (tools.pillow ? 'found' : 'not found') + ').' }),
        h('h4', { text: 'Made by' }),
        h('p', null, h('b', { text: a.author || 'MD. Habibullah Sharif' }), ' · ', link(a.site || 'https://habibullah.dev')),
        h('h4', { text: 'Source code, issues and updates' }),
        h('p', null, link(a.repo || 'https://github.com/md8-habibullah/heliograph'),
          ' ', h('button', { class: 'btn tiny', type: 'button', text: 'Copy link', onclick: () => copy(a.repo || '') })),
        h('p', { class: 'small', text: 'Links open in your default browser, and only when you click them. Heliograph itself never connects to the internet.' })));
  }

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); toast('Link copied'); }
    catch { toast('Select the link and copy it with Ctrl+C', 'err'); }
  }

  function paint() {
    clear(body);
    body.scrollTop = 0;
    for (const b of tabs.querySelectorAll('button')) {
      const on = b.dataset.tab === tab;
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
    }
    if (tab === 'keys') paintKeys(); else paintAbout();
  }

  /** open('keys', kind) shows the shortcuts with the section for `kind` first; open('about') shows About. */
  function open(which = 'keys', kind = null) {
    tab = which;
    focusKind = kind;
    returnTo = document.activeElement;
    paint();
    root.hidden = false;
    tabs.querySelector('[aria-selected="true"]').focus();       // not Close: Space or Enter must not dismiss it by accident
  }
  function close() {
    if (root.hidden) return;
    root.hidden = true;
    if (returnTo && returnTo.isConnected && returnTo.focus) { try { returnTo.focus({ preventScroll: true }); } catch { /* gone */ } }
    returnTo = null;
  }

  tabs.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-tab]');
    if (b) { tab = b.dataset.tab; paint(); }
  });
  $('#h-close').addEventListener('click', close);
  root.addEventListener('mousedown', (e) => { if (e.target === root) close(); });
  // Capture phase: while this window is open it owns the keyboard, so the viewer behind it does not react.
  document.addEventListener('keydown', (e) => {
    if (root.hidden) return;
    if (e.key === 'Escape' || e.key === '?') { e.preventDefault(); e.stopPropagation(); close(); return; }
    if (e.key === 'Tab') {                                     // keep focus inside the window
      const f = [...root.querySelectorAll('button, a[href]')].filter((x) => x.tabIndex >= 0 && !x.disabled);
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      return;
    }
    if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && e.target.closest && e.target.closest('#h-tabs')) {
      e.preventDefault();
      tab = tab === 'keys' ? 'about' : 'keys';
      paint();
      tabs.querySelector(`button[data-tab="${tab}"]`).focus();
    }
    e.stopPropagation();                                       // arrows and letters must not move the grid or seek the video behind
  }, true);

  return { open, close, isOpen: () => !root.hidden };
}
