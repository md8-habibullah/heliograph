// The viewer (pictures, video, audio) and the player behind it.
//
// Playback has three modes, chosen by the service from what is inside the file:
//   direct     the browser plays the original file (HTTP range requests, instant seeking)
//   remux      the service repackages it into fragmented MP4 without re-encoding (cheap)
//   transcode  the service converts it to H.264/AAC, using the GPU when there is one
// Remuxed and converted streams cannot be seeked by the browser, so seeking restarts the stream at the
// requested time ("start" offset). If a mode fails to play the player quietly steps down to the next one.

import { h, icon, clear, api, post, fmt, prefs, toast, S, tilde, fileUrl } from './core.js';

const REMUXABLE = new Set(['h264', 'vp9', 'av1']);
const FULL_DECODE_MAX_PIXELS = 12e6;          // bigger pictures are shown from a 2400 px preview, the original only on zoom
const SPEEDS = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3, 3.5];
const MEDIA_ACTIONS = ['play', 'pause', 'stop', 'seekbackward', 'seekforward', 'seekto', 'previoustrack', 'nexttrack'];

function trackLabel(a, i) {
  const bits = [a.lang && a.lang !== 'und' ? a.lang.toUpperCase() : '', a.title || '', (a.codec || '').toUpperCase(),
    a.channels ? (a.channels === 2 ? 'stereo' : a.channels === 6 ? '5.1' : a.channels + ' ch') : ''].filter(Boolean);
  return `${i + 1}. ${bits.join(' · ')}`;
}
function subLabel(s, i) {
  const bits = [s.lang && s.lang !== 'und' ? s.lang.toUpperCase() : '', s.title || s.file || ''].filter(Boolean);
  return `${i + 1}. ${bits.join(' · ') || (s.codec || 'subtitles')}`;
}

// ================================================================ the player
export class Player {
  constructor({ item, host, onFailed, onStep }) {
    this.item = item;
    this.info = item.info || {};
    this.mode = (item.play && item.play.mode) || 'direct';
    this.why = (item.play && item.play.why) || '';
    this.isAudio = item.kind === 'audio';
    this.dur = this.info.dur || item.dur || 0;
    this.t0 = 0;
    this.audioIdx = null;
    this.sub = null;
    const kind = this.kind = this.isAudio ? 'audio' : 'video';
    const saved = prefs.get('speed.' + kind, 1);
    this.speed = SPEEDS.includes(saved) ? saved : 1;          // the chosen speed carries over to the next file
    this.auto = !!prefs.get('autonext.' + kind, this.isAudio);   // music goes on to the next track by itself, video asks first
    this.repeat = false;
    this.sessionOn = false;
    this.onFailed = onFailed;
    this.onStep = onStep;
    this.dead = false;
    this.tried = new Set([this.mode]);
    this.dragging = false;
    this.posSeq = 0;

    const el = this.el = document.createElement(this.isAudio ? 'audio' : 'video');
    el.preload = 'auto';
    el.playsInline = true;
    el.preservesPitch = true;                  // faster or slower without a chipmunk voice
    el.defaultPlaybackRate = this.speed;
    el.volume = prefs.get('volume', 0.9);
    el.muted = prefs.get('muted', false);

    this.root = h('div', { class: 'player' + (this.isAudio ? ' audio' : '') });
    this.osdEl = h('div', { class: 'osd', role: 'status', 'aria-live': 'polite' });
    this.spinner = h('div', { class: 'vload', hidden: true }, h('div', { class: 'spin' }));
    this.build();
    if (this.isAudio) this.root.append(this.audioCard());
    else this.root.append(el, this.spinner);
    this.root.append(this.bar, this.osdEl);
    if (this.isAudio) this.root.append(el);
    host.append(this.root);

    el.addEventListener('timeupdate', () => this.paint());
    el.addEventListener('progress', () => this.paint());
    el.addEventListener('durationchange', () => { if (!this.dur && isFinite(el.duration)) this.dur = el.duration; this.paint(); });
    el.addEventListener('play', () => this.paintButtons());
    el.addEventListener('pause', () => this.paintButtons());
    el.addEventListener('ended', () => this.ended());
    el.addEventListener('waiting', () => this.busy(true));
    el.addEventListener('seeking', () => this.busy(true));
    el.addEventListener('stalled', () => this.busy(true));
    for (const ev of ['playing', 'canplay', 'seeked', 'loadeddata']) el.addEventListener(ev, () => this.busy(false));
    el.addEventListener('volumechange', () => { prefs.set('volume', el.volume); prefs.set('muted', el.muted); this.paintButtons(); });
    el.addEventListener('error', () => this.failed());
    if (!this.isAudio) {
      el.addEventListener('click', () => this.toggle());
      this.root.addEventListener('mousemove', () => this.wake());
      this.root.addEventListener('mouseleave', () => this.sleepSoon());
    }
    this.load(0, true);
    this.paintButtons();
    this.mediaSession();
    this.wake();
  }

  // ---------------------------------------------------------------- source handling
  url(start) {
    const id = this.item.id;
    if (this.mode === 'direct') return fileUrl('media', this.item);
    const q = new URLSearchParams({ mode: this.mode, start: start.toFixed(2) });
    if (this.audioIdx != null) q.set('audio', this.audioIdx);
    return `/stream/${id}?${q}`;
  }

  time() { return this.t0 + (this.el.currentTime || 0); }

  load(start, play) {
    if (this.dead) return;
    this.t0 = this.mode === 'direct' ? 0 : start;
    this.busy(true);
    this.el.src = this.url(start);
    this.el.playbackRate = this.speed;
    if (this.mode === 'direct' && start > 0.5) {
      const go = () => { this.el.currentTime = start; };
      this.el.addEventListener('loadedmetadata', go, { once: true });
    }
    if (play) { const p = this.el.play(); if (p && p.catch) p.catch(() => this.busy(false)); }
    this.applySub();
    this.paint();
  }

  /** The current mode cannot play this file: step down to the next one, from where we were. */
  failed() {
    if (this.dead || !this.el.getAttribute('src')) return;
    const t = this.time();
    const wasPlaying = !this.el.paused || this.el.readyState < 2;
    const vcodec = this.info.video && this.info.video.codec;
    let next = null;
    if (this.mode === 'direct') next = (this.isAudio || REMUXABLE.has(vcodec)) && !this.tried.has('remux') ? 'remux' : 'transcode';
    else if (this.mode === 'remux') next = 'transcode';
    if (!next || this.tried.has(next)) { this.busy(false); this.onFailed && this.onFailed(); return; }
    this.tried.add(next);
    this.mode = next;
    this.why = next === 'remux' ? 'Repackaged for playback' : 'Converted for playback';
    this.refreshWhy();
    this.reposition(t, wasPlaying);
  }

  /** Start the stream at time t. A repackaged (not re-encoded) stream can only begin on a key frame, so ask
   *  the service where that is: the clock then stays right, and if the key frame is far back the stream is
   *  converted live instead, which is frame-accurate. */
  async reposition(t, play) {
    const my = ++this.posSeq;
    if (this.mode === 'remux' && t > 0.5) {
      this.busy(true);
      let k = t;
      try { k = (await api('/api/keyframe', { id: this.item.id, t: t.toFixed(3) })).start; } catch { /* use t as is */ }
      if (this.dead || my !== this.posSeq) return;
      if (t - k > 5) {
        this.mode = 'transcode';
        this.tried.add('transcode');
        this.why = 'Converted live for an exact position';
        this.refreshWhy();
      } else {
        t = Math.ceil(k * 100) / 100;                         // rounded up so FFmpeg lands on that very key frame
      }
    }
    this.load(t, play);
  }

  seek(t) {
    t = Math.max(0, this.dur ? Math.min(t, this.dur - 0.2) : t);
    if (this.mode === 'direct') { this.el.currentTime = t; return; }
    this.reposition(t, true);
  }

  setAudio(idx) {
    const t = this.time();
    this.audioIdx = idx;
    if (this.mode === 'direct') { this.mode = 'remux'; this.tried.add('remux'); this.why = 'Repackaged for the chosen audio track'; this.refreshWhy(); }
    this.reposition(t, !this.el.paused);
  }

  setSub(s) {                 // s: null | subtitle description from /api/item
    this.sub = s;
    this.applySub();
  }

  applySub() {
    for (const t of this.el.querySelectorAll('track')) t.remove();
    if (!this.sub || this.isAudio) return;
    const q = this.sub.file ? `f=${encodeURIComponent(this.sub.file)}` : `i=${this.sub.index}`;
    const tr = h('track', { kind: 'subtitles', label: 'Subtitles', default: true, src: `/sub/${this.item.id}?${q}&start=${this.t0.toFixed(2)}` });
    this.el.append(tr);
    tr.addEventListener('load', () => { tr.track.mode = 'showing'; });
    tr.addEventListener('error', () => toast('Could not read these subtitles', 'err'));
    tr.track.mode = 'showing';
  }

  setSpeed(r) {
    this.speed = r;
    this.el.defaultPlaybackRate = r;
    this.el.playbackRate = r;
    prefs.set('speed.' + this.kind, r);
    this.paintButtons();
  }
  nudgeSpeed(dir) {
    let i = SPEEDS.indexOf(this.speed);
    if (i < 0) i = SPEEDS.findIndex((r) => r >= this.speed);
    i = Math.max(0, Math.min(SPEEDS.length - 1, (i < 0 ? SPEEDS.length - 1 : i) + dir));
    this.setSpeed(SPEEDS[i]);
    this.osd('Speed ' + SPEEDS[i] + '×');
  }
  total() { return this.dur || (isFinite(this.el.duration) ? this.el.duration : 0); }

  /** A short read-out in the corner of the picture, like the one YouTube shows for a key press. */
  osd(text) {
    this.osdEl.textContent = text;
    this.osdEl.classList.add('show');
    clearTimeout(this.osdT);
    this.osdT = setTimeout(() => this.osdEl.classList.remove('show'), 1000);
  }
  setRepeat(on) { this.repeat = on; this.paintButtons(); this.osd('Repeat ' + (on ? 'on' : 'off')); }
  setAuto(on) {
    this.auto = on;
    prefs.set('autonext.' + this.kind, on);
    this.paintButtons();
    this.osd('Play next ' + (on ? 'automatically' : 'manually'));
  }
  /** One frame back or forward (video that plays directly; a converted stream cannot step exactly). */
  frame(dir) {
    if (this.isAudio) return;
    if (this.mode !== 'direct') { this.osd('Frame step needs a file that plays directly'); return; }
    this.el.pause();
    const fps = (this.info.video && this.info.video.fps) || 30;
    this.el.currentTime = Math.max(0, this.el.currentTime + dir / fps);
    this.osd(dir > 0 ? 'Next frame' : 'Previous frame');
  }
  toggleSubs() {
    const subs = (this.info.subs || []).filter((s) => s.text);
    if (!subs.length) { this.osd('No subtitles'); return; }
    if (this.sub) { this.lastSub = this.sub; this.setSub(null); this.osd('Subtitles off'); }
    else { this.setSub(this.lastSub || subs[0]); this.osd('Subtitles on'); }
  }
  restart() {
    this.seek(0);
    if (this.mode === 'direct') { const p = this.el.play(); if (p && p.catch) p.catch(() => {}); }
  }
  ended() {
    if (this.repeat) { this.restart(); return; }
    if (this.auto) this.nextTrack(true);
  }
  /** The next file of the same kind (audio goes to the next song, not to the next picture). */
  async nextTrack(auto) {
    if (!this.onStep) return;
    const moved = await this.onStep(1);
    if (!moved && !this.dead) this.osd(auto ? 'End of the list' : 'No next ' + (this.isAudio ? 'track' : 'video'));
  }
  /** Like a music player: more than 3 s in, "previous" starts the song again; otherwise it goes back one. */
  async prevTrack() {
    if (this.isAudio && this.time() > 3) { this.seek(0); this.osd('From the start'); return; }
    if (!this.onStep) return;
    const moved = await this.onStep(-1);
    if (!moved && !this.dead) this.osd('No previous ' + (this.isAudio ? 'track' : 'video'));
  }

  /** Media keys, desktop media widgets and headphone buttons: tell the system what is playing and let it drive the player. */
  mediaSession() {
    const ms = navigator.mediaSession;
    if (!ms || typeof MediaMetadata === 'undefined') return;
    const it = this.item;
    try {
      ms.metadata = new MediaMetadata({
        title: it.title || it.name || '', artist: it.artist || '', album: it.album || '',
        artwork: [{ src: fileUrl('thumb', it), sizes: '400x400', type: 'image/jpeg' }],
      });
    } catch { /* metadata not supported */ }
    const on = (action, fn) => { try { ms.setActionHandler(action, fn); } catch { /* this action is not offered here */ } };
    const play = () => { const p = this.el.play(); if (p && p.catch) p.catch(() => {}); };
    on('play', play);
    on('pause', () => this.el.pause());
    on('stop', () => this.el.pause());
    on('seekbackward', (d) => this.seek(this.time() - ((d && d.seekOffset) || 10)));
    on('seekforward', (d) => this.seek(this.time() + ((d && d.seekOffset) || 10)));
    on('seekto', (d) => { if (d && d.seekTime != null) this.seek(d.seekTime); });
    on('previoustrack', () => this.prevTrack());
    on('nexttrack', () => this.nextTrack());
    this.sessionOn = true;
    this.sessionState();
  }
  sessionState() {
    if (!this.sessionOn) return;
    try { navigator.mediaSession.playbackState = this.el.paused ? 'paused' : 'playing'; } catch { /* ignore */ }
  }
  toggle() { if (this.el.paused) { const p = this.el.play(); if (p && p.catch) p.catch(() => {}); } else this.el.pause(); }
  busy(on) { this.spinner.hidden = !on; }

  destroy() {
    this.dead = true;
    clearTimeout(this.idleT);
    clearTimeout(this.osdT);
    if (this.sessionOn) {
      const ms = navigator.mediaSession;
      for (const a of MEDIA_ACTIONS) { try { ms.setActionHandler(a, null); } catch { /* ignore */ } }
      try { ms.metadata = null; ms.playbackState = 'none'; } catch { /* ignore */ }
      this.sessionOn = false;
    }
    try { this.el.pause(); } catch { /* ignore */ }
    this.el.removeAttribute('src');
    for (const t of this.el.querySelectorAll('track')) t.remove();
    this.el.load();                       // drops the connection, so the service stops FFmpeg at once
    this.root.remove();
  }

  // ---------------------------------------------------------------- UI
  audioCard() {
    const cover = h('div', { class: 'cover' }, icon('audio'));
    const img = h('img', { alt: '', src: fileUrl('thumb', this.item) });
    img.addEventListener('load', () => { cover.replaceChildren(img); });
    img.addEventListener('error', () => img.remove());
    const it = this.item;
    return h('div', { class: 'audio-card' }, cover, h('h3', { text: it.title || it.name }),
      h('p', { text: [it.artist, it.album].filter(Boolean).join(' · ') }));
  }

  build() {
    const mk = (name, title, fn) => h('button', { class: 'iconbtn', type: 'button', title, 'aria-label': title, onclick: fn }, icon(name));
    this.bPlay = mk('play', 'Play / pause  ( Space or K )', () => this.toggle());
    this.bMute = mk('volume', 'Mute  ( M )', () => { this.el.muted = !this.el.muted; });
    this.vol = h('input', { type: 'range', min: '0', max: '1', step: '0.02', value: String(this.el.volume), 'aria-label': 'Volume' });
    this.vol.addEventListener('input', () => { this.el.volume = Number(this.vol.value); this.el.muted = false; });
    this.time_ = h('span', { class: 'time', text: '0:00' });
    this.whyEl = h('span', { class: 'why', hidden: true });

    const audio = this.info.audio || [];
    const subs = (this.info.subs || []).filter((s) => s.text);
    this.bAudio = audio.length > 1 ? mk('tracks', 'Audio track', (e) => this.menu(e, 'audio')) : null;
    this.bSubs = subs.length && !this.isAudio ? mk('subs', 'Subtitles  ( C )', (e) => this.menu(e, 'subs')) : null;
    this.bSpeed = mk('gauge', 'Speed  ( < slower   > faster )', (e) => this.menu(e, 'speed'));
    this.rateEl = h('span', { class: 'time', hidden: true });
    this.bRepeat = mk('repeat', 'Repeat this file  ( R )', () => this.setRepeat(!this.repeat));
    this.bAuto = mk('next', 'Play the next file automatically', () => this.setAuto(!this.auto));

    this.seekEl = h('div', { class: 'seek', role: 'slider', 'aria-label': 'Seek', tabindex: '0' },
      h('div', { class: 'track' }, h('div', { class: 'buf' }), h('div', { class: 'pos' }), h('div', { class: 'knob' })),
      h('div', { class: 'tip' }));
    this.bindSeek();
    this.bar = h('div', { class: 'pbar' }, this.seekEl,
      h('div', { class: 'prow' }, this.bPlay, this.time_, this.bMute, this.vol, h('span', { class: 'grow' }), this.whyEl,
        this.bAudio, this.bSubs, this.rateEl, this.bSpeed, this.bRepeat, this.bAuto));
    this.refreshWhy();
  }

  refreshWhy() {
    if (!this.whyEl) return;
    const note = this.mode === 'direct' ? '' : this.mode === 'remux' ? 'Repackaged' : 'Converted live';
    this.whyEl.textContent = note;
    this.whyEl.title = this.why;
    this.whyEl.hidden = !note;
  }

  bindSeek() {
    const track = this.seekEl.querySelector('.track'), tip = this.seekEl.querySelector('.tip');
    const frac = (e) => { const r = track.getBoundingClientRect(); return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)); };
    const total = () => this.dur || (isFinite(this.el.duration) ? this.el.duration : 0);
    let target = 0;
    this.seekEl.addEventListener('pointerdown', (e) => {
      if (!total()) return;
      this.dragging = true;
      this.seekEl.classList.add('drag');
      this.seekEl.setPointerCapture(e.pointerId);
      target = frac(e) * total();
      this.paint(target);
    });
    this.seekEl.addEventListener('pointermove', (e) => {
      const f = frac(e);
      tip.textContent = fmt.dur(f * total());
      tip.style.left = (f * 100) + '%';
      if (this.dragging) { target = f * total(); this.paint(target); }
    });
    const end = (e) => {
      if (!this.dragging) return;
      this.dragging = false;
      this.seekEl.classList.remove('drag');
      target = frac(e) * total();
      this.seek(target);
    };
    this.seekEl.addEventListener('pointerup', end);
    this.seekEl.addEventListener('pointercancel', () => { this.dragging = false; this.seekEl.classList.remove('drag'); });
    this.seekEl.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.stopPropagation(); this.seek(this.time() + (e.key === 'ArrowRight' ? 5 : -5)); }
    });
  }

  paint(override) {
    const total = this.dur || (isFinite(this.el.duration) ? this.el.duration : 0);
    const cur = override != null ? override : this.time();
    this.time_.textContent = total ? `${fmt.dur(cur)} / ${fmt.dur(total)}` : fmt.dur(cur);
    const f = total ? Math.min(1, cur / total) : 0;
    const pos = this.seekEl.querySelector('.pos'), knob = this.seekEl.querySelector('.knob'), buf = this.seekEl.querySelector('.buf');
    pos.style.width = (f * 100) + '%';
    knob.style.left = (f * 100) + '%';
    let end = 0;
    const b = this.el.buffered;
    for (let i = 0; i < b.length; i++) if (b.start(i) <= this.el.currentTime + 0.5) end = Math.max(end, b.end(i));
    buf.style.width = total ? Math.min(100, ((this.t0 + end) / total) * 100) + '%' : '0';
    this.seekEl.setAttribute('aria-valuenow', Math.round(cur));
    if (this.sessionOn && override == null && total) {
      const now = performance.now();
      if (now - (this.posT || 0) > 1000) {                    // at most once a second
        this.posT = now;
        try { navigator.mediaSession.setPositionState({ duration: total, position: Math.max(0, Math.min(cur, total)), playbackRate: this.speed }); } catch { /* ignore */ }
      }
    }
  }

  paintButtons() {
    const ic = (b, name) => { b.replaceChildren(icon(name)); };
    ic(this.bPlay, this.el.paused ? 'play' : 'pause');
    ic(this.bMute, this.el.muted || this.el.volume === 0 ? 'mute' : 'volume');
    this.vol.value = this.el.muted ? '0' : String(this.el.volume);
    this.root.classList.toggle('paused', this.el.paused);
    this.rateEl.textContent = this.speed + '×';
    this.rateEl.hidden = this.speed === 1;
    this.bRepeat.setAttribute('aria-pressed', String(this.repeat));
    this.bAuto.setAttribute('aria-pressed', String(this.auto));
    this.sessionState();
  }

  menu(ev, which) {
    ev.stopPropagation();
    const old = this.root.querySelector('.menu');
    const again = old && old.dataset.which === which;
    if (old) old.remove();
    if (again) return;
    const m = h('div', { class: 'menu', 'data-which': which });
    const row = (label, on, fn) => m.append(h('button', { type: 'button', class: on ? 'on' : '', onclick: () => { m.remove(); fn(); } }, icon('check'), label));
    if (which === 'audio') {
      m.append(h('h4', { text: 'Audio track' }));
      (this.info.audio || []).forEach((a, i) => row(trackLabel(a, i), (this.audioIdx ?? (this.info.audio.find((x) => x.default) || this.info.audio[0]).index) === a.index, () => this.setAudio(a.index)));
    } else if (which === 'subs') {
      m.append(h('h4', { text: 'Subtitles' }));
      row('Off', !this.sub, () => this.setSub(null));
      (this.info.subs || []).filter((s) => s.text).forEach((s, i) => row(subLabel(s, i), this.sub === s, () => this.setSub(s)));
    } else {
      m.append(h('h4', { text: 'Speed' }));
      SPEEDS.forEach((r) => row(r === 1 ? 'Normal' : r + '×', this.speed === r, () => this.setSpeed(r)));
      if (this.mode === 'transcode') m.append(h('p', { class: 'hint', text: 'This file is converted while it plays, so speeds above 2× may stutter.' }));
    }
    this.root.append(m);
    const close = (e) => { if (!m.contains(e.target)) { m.remove(); document.removeEventListener('pointerdown', close, true); } };
    setTimeout(() => document.addEventListener('pointerdown', close, true), 0);
  }

  // controls fade away while a video plays
  wake() {
    this.root.classList.remove('idle');
    clearTimeout(this.idleT);
    this.sleepSoon();
  }
  sleepSoon() {
    clearTimeout(this.idleT);
    if (this.isAudio) return;
    this.idleT = setTimeout(() => {
      if (!this.el.paused && !this.root.querySelector('.menu') && !this.dragging) this.root.classList.add('idle');
    }, 2600);
  }

  /** YouTube's keys for the player, plus the few a music player adds. Returns true when the key was used. */
  key(e) {
    const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;      // Caps Lock must not matter
    const jump = (dt) => { this.seek(this.time() + dt); this.osd((dt > 0 ? '+' : '−') + Math.abs(dt) + ' s'); };
    const vol = (dv) => {
      this.el.muted = false;
      this.el.volume = Math.max(0, Math.min(1, Math.round((this.el.volume + dv) * 100) / 100));
      this.osd('Volume ' + Math.round(this.el.volume * 100) + '%');
    };
    if (e.code === 'Comma' || e.code === 'Period') {                  // , .  = frame by frame     < >  = slower / faster
      if (e.shiftKey) this.nudgeSpeed(e.code === 'Period' ? 1 : -1);
      else this.frame(e.code === 'Period' ? 1 : -1);
    } else if (/^[0-9]$/.test(k) && !e.shiftKey) {                    // 0-9 = jump to 0%-90%
      const t = this.total();
      if (!t) return false;
      this.seek(t * Number(k) / 10);
      this.osd(Number(k) * 10 + '%');
    } else {
      switch (k) {
        case ' ': case 'k': this.toggle(); break;
        case 'ArrowRight': jump(5); break;
        case 'ArrowLeft': jump(-5); break;
        case 'l': jump(10); break;
        case 'j': jump(-10); break;
        case 'ArrowUp': vol(0.05); break;
        case 'ArrowDown': vol(-0.05); break;
        case 'm': this.el.muted = !this.el.muted; this.osd(this.el.muted ? 'Muted' : 'Volume ' + Math.round(this.el.volume * 100) + '%'); break;
        case 'Home': this.seek(0); this.osd('Start'); break;
        case 'End': if (this.total()) this.seek(this.total()); break;
        case 'n': if (!e.shiftKey) return false; this.nextTrack(); break;      // Shift+N next, Shift+P previous
        case 'p': if (!e.shiftKey) return false; this.prevTrack(); break;
        case 'r': this.setRepeat(!this.repeat); break;
        case 'c': if (this.isAudio || e.shiftKey) return false; this.toggleSubs(); break;   // Shift+C stays "convert"
        default: return false;
      }
    }
    this.wake();
    return true;
  }
}

// ================================================================ the viewer
export function createViewer({ grid, loader, onFav, onConvert, onFolder, onHelp }) {
  const root = document.getElementById('viewer');
  const stage = document.getElementById('stage');
  const info = document.getElementById('vinfo');
  const bName = document.getElementById('v-name');
  const bCount = document.getElementById('v-count');
  const bPrev = document.getElementById('v-prev');
  const bNext = document.getElementById('v-next');
  const bFav = document.getElementById('v-fav');
  const bInfo = document.getElementById('v-info');
  const NATIVE = new Set(S.state ? S.state.native_image : []);
  let idx = -1, item = null, token = 0, player = null, infoOpen = prefs.get('info', false), zoomFn = null;
  const prefersPreview = (it) => !NATIVE.has(it.ext) || (it.w && it.h ? it.w * it.h > FULL_DECODE_MAX_PIXELS : it.size > 8 * 1048576);
  const V = { isOpen: () => !root.hidden, get index() { return idx; } };

  function endPlayer() { if (player) { player.destroy(); player = null; } }
  function say(parts) { clear(stage); stage.classList.remove('zoomed'); stage.append(h('div', { class: 'fallback' }, ...parts)); }

  function actionsFor(it, full) {
    const tools = (S.state && S.state.tools) || {};
    const btn = (label, ic, fn) => h('button', { class: 'btn', type: 'button', onclick: fn }, icon(ic, 'sm'), label);
    const out = [btn('Open with default app', 'external', () => post('/api/open_external', { id: it.id, action: 'default' }).catch((e) => toast(e.message, 'err')))];
    if (it.kind !== 'image' && tools.mpv) out.push(btn('Play with mpv', 'play', () => post('/api/open_external', { id: it.id, action: 'mpv' }).catch((e) => toast(e.message, 'err'))));
    out.push(btn('Show in file manager', 'folder', () => post('/api/open_external', { id: it.id, action: 'folder' }).catch((e) => toast(e.message, 'err'))));
    if (full && full.path) out.push(btn('Copy path', 'copy', () => navigator.clipboard.writeText(full.path).then(() => toast('Path copied'), () => toast('Could not copy', 'err'))));
    return out;
  }

  function cannotShow(it, why) {
    const parts = [];
    const u = loader.url(it.id, it.k);
    if (u) parts.push(h('img', { src: u, alt: '' }));
    parts.push(h('div', { text: why }));
    parts.push(h('div', { class: 'acts' }, actionsFor(it)));
    say(parts);
  }

  // ---------------------------------------------------------------- pictures
  async function showImage(it, my) {
    const img = h('img', { class: 'main', alt: it.name, draggable: 'false' });
    const soft = loader.url(it.id, it.k);
    if (soft) { img.src = soft; img.classList.add('soft'); }
    const spin = h('div', { class: 'vload' }, h('div', { class: 'spin' }));
    clear(stage); stage.classList.remove('zoomed');
    stage.append(img, spin);
    const native = NATIVE.has(it.ext);
    const order = prefersPreview(it) ? [fileUrl('preview', it), ...(native ? [fileUrl('media', it)] : [])] : [fileUrl('media', it), fileUrl('preview', it)];
    for (const src of order) {
      const probe = new Image();
      probe.decoding = 'async';
      probe.src = src;
      try { await probe.decode(); } catch { continue; }
      if (my !== token) return;
      img.src = src;
      img.classList.remove('soft');
      spin.remove();
      let original = src.startsWith('/media/');
      zoomFn = async () => {
        if (stage.classList.contains('zoomed')) { stage.classList.remove('zoomed'); return; }
        if (!original && native) {                          // zooming in: now the full-size original is worth decoding
          const big = new Image();
          big.src = fileUrl('media', it);
          try { await big.decode(); } catch { /* keep the preview */ }
          if (my !== token) return;
          if (big.naturalWidth) { img.src = big.src; original = true; }
        }
        stage.classList.add('zoomed');
      };
      img.addEventListener('click', () => zoomFn());
      preloadNeighbours();
      return;
    }
    if (my !== token) return;
    cannotShow(it, 'This picture cannot be shown here. Your system may have a program that opens it.');
  }

  function preloadNeighbours() {
    for (const d of [1, -1]) {
      const n = grid.itemAt(idx + d);
      if (n && n.kind === 'image' && !n.gone) {
        const p = new Image();
        p.decoding = 'async';
        p.src = prefersPreview(n) ? fileUrl('preview', n) : fileUrl('media', n);
      }
    }
  }

  // ---------------------------------------------------------------- video / audio
  async function showMedia(it, my) {
    clear(stage); stage.classList.remove('zoomed');
    const spin = h('div', { class: 'vload' }, h('div', { class: 'spin' }));
    stage.append(spin);
    let full;
    try { full = await api(`/api/item/${it.id}`); } catch (e) { if (my === token) cannotShow(it, e.message); return; }
    if (my !== token) return;
    renderInfo(full);
    if (!full.exists) { cannotShow(it, 'This file is no longer on disk.'); return; }
    if (full.play && full.play.mode === 'unsupported') {
      cannotShow(it, 'This file cannot be played here (' + full.play.why + ').');
      return;
    }
    spin.remove();
    player = new Player({
      item: full, host: stage,
      onFailed: () => { endPlayer(); cannotShow(it, 'This file could not be played, even after converting it.'); },
      onStep: (d) => stepKind(d, it.kind),
    });
  }

  // ---------------------------------------------------------------- details panel
  async function renderInfo(full) {
    if (!infoOpen) return;
    if (!full) { try { full = await api(`/api/item/${item.id}`); } catch { return; } }
    if (!item || full.id !== item.id) return;
    clear(info);
    const dl = (rows) => {
      const d = h('dl');
      for (const [k, v] of rows) if (v != null && v !== '') d.append(h('dt', { text: k }), h('dd', null, v));
      return d;
    };
    const folder = h('button', { type: 'button', text: tilde(full.dir), onclick: () => { close(); onFolder(full.dir); } });
    info.append(h('h3', { text: 'File' }),
      dl([['Name', full.name], ['Folder', folder], ['Type', `${full.ext.toUpperCase()} · ${full.kind}`], ['Size', fmt.bytes(full.size)],
        ['Modified', fmt.datetime(full.mtime)], ['Date', full.ts ? fmt.datetime(full.ts) : ''],
        ['Dimensions', full.w ? `${full.w} × ${full.h}` : ''], ['Duration', full.dur != null ? fmt.dur(full.dur) : ''],
        ['Title', full.title], ['Artist', full.artist], ['Album', full.album]]));
    const i = full.info;
    if (i) {
      const v = i.video;
      info.append(h('h3', { text: 'Streams' }), dl([
        ['Container', i.container_long || i.container],
        ['Video', v ? [v.codec, v.profile, v.pix_fmt].filter(Boolean).join(' · ') : ''],
        ['Frame', v ? `${v.w} × ${v.h}${v.fps ? ' · ' + v.fps + ' fps' : ''}` : ''],
        ['Bit rate', i.bit_rate ? Math.round(i.bit_rate / 1000).toLocaleString() + ' kb/s' : ''],
        ...(i.audio || []).map((a, n) => [n === 0 ? 'Audio' : '', trackLabel(a, n)]),
        ['Subtitles', (i.subs || []).length ? String(i.subs.length) : '']]));
    }
    if (full.play) info.append(h('h3', { text: 'Playback' }), dl([['Mode', { direct: 'Plays as is', remux: 'Repackaged (not re-encoded)', transcode: 'Converted live' }[full.play.mode] || full.play.mode],
      ...(full.play.mode === 'direct' ? [] : [['Why', full.play.why]])]));
    info.append(h('div', { class: 'acts' }, actionsFor(item, full)));
  }

  // ---------------------------------------------------------------- navigation
  async function show(i) {
    const my = ++token;
    endPlayer();
    zoomFn = null;
    idx = Math.max(0, Math.min(grid.total - 1, i));
    grid.setCursor(idx);
    bCount.textContent = `${(idx + 1).toLocaleString()} of ${grid.total.toLocaleString()}`;
    bPrev.disabled = idx <= 0;
    bNext.disabled = idx >= grid.total - 1;
    const it = await grid.ensureItem(idx);
    if (my !== token) return;
    if (!it) { say([h('div', { text: 'Nothing to show here.' })]); return; }
    item = it;
    bName.textContent = it.name || '';
    paintFav();
    clear(info);
    if (it.gone) { say([h('div', { text: 'This file is no longer in the library.' })]); return; }
    if (it.kind === 'image') { showImage(it, my); renderInfo(); }
    else showMedia(it, my);
  }

  /** Next or previous file of one kind (a song after a song, a video after a video), skipping pictures and the like.
   *  Resolves to false when there is none, or when the viewer moved on in the meantime. */
  async function stepKind(d, kind) {
    const my = token;
    for (let i = idx + d, n = 0; i >= 0 && i < grid.total && n < 3000; i += d, n++) {
      const it = await grid.ensureItem(i);
      if (my !== token) return false;
      if (it && !it.gone && it.kind === kind) { show(i); return true; }
    }
    return false;
  }

  function paintFav() { bFav.classList.toggle('on', !!(item && item.fav)); }
  function step(d) { if (idx + d >= 0 && idx + d < grid.total) show(idx + d); }

  function open(i) {
    root.hidden = false;
    document.body.classList.add('viewing');
    bInfo.setAttribute('aria-pressed', String(infoOpen));
    info.hidden = !infoOpen;
    show(i);
  }

  function close() {
    if (root.hidden) return;
    token++;
    endPlayer();
    clear(stage);
    root.hidden = true;
    document.body.classList.remove('viewing');
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    const keep = idx;
    idx = -1; item = null;
    grid.setCursor(keep);
  }

  bPrev.addEventListener('click', () => step(-1));
  bNext.addEventListener('click', () => step(1));
  document.getElementById('v-close').addEventListener('click', close);
  bFav.addEventListener('click', () => { if (item) onFav(item).then(paintFav); });
  document.getElementById('v-convert').addEventListener('click', () => { if (item) onConvert([item.id], [item]); });
  document.getElementById('v-full').addEventListener('click', toggleFull);
  document.getElementById('v-help').addEventListener('click', () => onHelp && onHelp(item ? item.kind : null));
  bInfo.addEventListener('click', toggleInfo);
  stage.addEventListener('dblclick', (e) => { if (e.target.closest('video')) toggleFull(); });

  function toggleFull() {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else root.requestFullscreen().catch(() => {});
  }
  function toggleInfo() {
    infoOpen = !infoOpen;
    prefs.set('info', infoOpen);
    bInfo.setAttribute('aria-pressed', String(infoOpen));
    info.hidden = !infoOpen;
    if (infoOpen) renderInfo();
  }

  /** Returns true when the key was used. */
  V.key = (e) => {
    if (root.hidden) return false;
    if (e.ctrlKey || e.metaKey || e.altKey) return false;
    if (e.target && e.target.closest && e.target.closest('input[type=range]') && (e.key.startsWith('Arrow'))) return false;
    if (e.key === 'Escape') { if (!document.fullscreenElement) close(); return true; }
    const isVideo = item && item.kind !== 'image';
    if (player && isVideo && player.key(e)) return true;
    const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    switch (k) {
      case 'ArrowRight': case 'PageDown': step(1); return true;
      case 'ArrowLeft': case 'PageUp': step(-1); return true;
      case 'Home': show(0); return true;
      case 'End': show(grid.total - 1); return true;
      case 'i': toggleInfo(); return true;
      case 'f': toggleFull(); return true;
      case 's': if (item) onFav(item).then(paintFav); return true;
      case 'c': if (!e.shiftKey) return false; if (item) onConvert([item.id], [item]); return true;      // Shift+C: C is captions in the player
      case 'z': if (zoomFn) zoomFn(); return true;
      case '?': if (onHelp) onHelp(item ? item.kind : null); return true;
      default: return false;
    }
  };
  V.open = open;
  V.close = close;
  V.step = step;
  V.repaintFav = paintFav;
  return V;
}
