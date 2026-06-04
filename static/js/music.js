// static/js/music.js
// Free music player — YouTube-backed search + streaming, in a MAGI-style
// floating window. Single persistent <audio> element so playback survives the
// window being minimized; Media Session API wires up the OS media keys.

import uiModule from './ui.js';
import { makeWindowDraggable } from './windowDrag.js';
import * as Modals from './modalManager.js';

const MUSIC_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>';

const IC = {
  play: '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>',
  pause: '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>',
  prev: '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M6 6h2v12H6zM20 6v12l-9-6z"/></svg>',
  next: '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M16 6h2v12h-2zM4 6l9 6-9 6z"/></svg>',
  shuffle: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 3h5v5M4 20L21 3M21 16v5h-5M15 15l6 6M4 4l5 5"/></svg>',
  repeat: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 1l4 4-4 4M3 11V9a4 4 0 0 1 4-4h14M7 23l-4-4 4-4M21 13v2a4 4 0 0 1-4 4H3"/></svg>',
  heart: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8l1.1 1L12 21l7.7-7.6 1.1-1a5.5 5.5 0 0 0 0-7.8z"/></svg>',
  heartFull: '<svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor"><path d="M12 21l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.18z"/></svg>',
  add: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
  trash: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>',
  vol: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5L6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/></svg>',
};

const GENRES = ['Lofi beats', 'Pop hits', 'Hip hop', 'Rock classics', 'Jazz', 'EDM', 'R&B', 'Classical', 'Indie', 'Phonk'];

let API_BASE = '';
let _modal = null;
let _audio = null;
let _view = 'search';

// playback state
let _queue = [];
let _qi = -1;
let _current = null;
let _shuffle = false;
let _repeat = 'off'; // off | all | one
let _lib = { liked: [], playlists: [], recent: [] };

function esc(v) { return uiModule.esc(String(v ?? '')); }

function fmt(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

// --- API ---------------------------------------------------------------------

async function api(path, opts) {
  const res = await fetch(`${API_BASE}/api/music${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) throw new Error((await res.text()) || res.statusText);
  return res.json();
}

async function loadLibrary() {
  try { _lib = await api('/library'); } catch (_) { /* keep cached */ }
}

function isLiked(id) { return _lib.liked.some(t => t.id === id); }

// --- audio engine ------------------------------------------------------------

function ensureAudio() {
  if (_audio) return _audio;
  _audio = new Audio();
  _audio.preload = 'auto';
  _audio.addEventListener('timeupdate', onTime);
  _audio.addEventListener('loadedmetadata', onTime);
  _audio.addEventListener('ended', onEnded);
  _audio.addEventListener('play', syncPlayBtn);
  _audio.addEventListener('pause', syncPlayBtn);
  _audio.addEventListener('error', () => {
    if (_current && uiModule.showError) uiModule.showError('Playback failed for this track.');
  });
  return _audio;
}

function playTrack(track, queue, index) {
  if (!track) return;
  ensureAudio();
  _current = track;
  if (Array.isArray(queue)) { _queue = queue; _qi = index; }
  _audio.src = `${API_BASE}/api/music/stream/${encodeURIComponent(track.id)}`;
  _audio.play().catch(() => {});
  renderBar();
  setMediaSession(track);
  api('/recent', { method: 'POST', body: JSON.stringify(track) }).catch(() => {});
}

function playFromList(list, index) { playTrack(list[index], list.slice(), index); }

function togglePlay() {
  if (!_audio || !_current) return;
  if (_audio.paused) _audio.play().catch(() => {}); else _audio.pause();
}

function next(auto) {
  if (!_queue.length) return;
  if (_repeat === 'one' && auto) { _audio.currentTime = 0; _audio.play().catch(() => {}); return; }
  let i;
  if (_shuffle) {
    i = _queue.length === 1 ? _qi : Math.floor(Math.random() * _queue.length);
  } else {
    i = _qi + 1;
  }
  if (i >= _queue.length) {
    if (_repeat === 'all') i = 0;
    else { syncPlayBtn(); return; }
  }
  playTrack(_queue[i], _queue, i);
}

function prev() {
  if (_audio && _audio.currentTime > 3) { _audio.currentTime = 0; return; }
  if (!_queue.length) return;
  let i = _qi - 1;
  if (i < 0) i = _repeat === 'all' ? _queue.length - 1 : 0;
  playTrack(_queue[i], _queue, i);
}

function onTime() {
  const seek = q('#mb-seek'), cur = q('#mb-cur'), tot = q('#mb-total');
  if (!seek || !_audio) return;
  const d = _audio.duration || _current?.duration || 0;
  if (d) { seek.max = Math.floor(d); seek.value = Math.floor(_audio.currentTime); }
  seek.style.setProperty('--fill', (d ? (_audio.currentTime / d) * 100 : 0) + '%');
  if (cur) cur.textContent = fmt(_audio.currentTime);
  if (tot) tot.textContent = fmt(d);
}

function onEnded() { next(true); }

function syncPlayBtn() {
  const playing = !!(_audio && !_audio.paused);
  const b = q('#mb-play');
  if (b) b.innerHTML = playing ? IC.pause : IC.play;
  const bar = q('#music-bar');
  if (bar) bar.classList.toggle('playing', playing);
  if ('mediaSession' in navigator) {
    navigator.mediaSession.playbackState = playing ? 'playing' : 'paused';
  }
}

function setMediaSession(track) {
  if (!('mediaSession' in navigator)) return;
  try {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: track.title || '',
      artist: track.artist || '',
      artwork: track.thumb ? [{ src: track.thumb, sizes: '480x360', type: 'image/jpeg' }] : [],
    });
    navigator.mediaSession.setActionHandler('play', () => _audio.play());
    navigator.mediaSession.setActionHandler('pause', () => _audio.pause());
    navigator.mediaSession.setActionHandler('previoustrack', () => prev());
    navigator.mediaSession.setActionHandler('nexttrack', () => next(false));
    navigator.mediaSession.setActionHandler('seekto', (d) => { if (d.seekTime != null) _audio.currentTime = d.seekTime; });
  } catch (_) { /* not all handlers supported everywhere */ }
}

// --- rendering ---------------------------------------------------------------

function q(sel) { return _modal ? _modal.querySelector(sel) : null; }

function trackRow(t, ctx) {
  const liked = isLiked(t.id);
  return `
    <div class="music-row${_current && _current.id === t.id ? ' playing' : ''}" data-id="${esc(t.id)}">
      <img class="music-row-thumb" src="${esc(t.thumb)}" loading="lazy" alt="">
      <div class="music-row-info">
        <div class="music-row-title">${esc(t.title)}</div>
        <div class="music-row-artist">${esc(t.artist || '')}</div>
      </div>
      <div class="music-row-dur">${t.duration ? fmt(t.duration) : ''}</div>
      <button class="music-row-btn${liked ? ' on' : ''}" data-act="like" title="Like">${liked ? IC.heartFull : IC.heart}</button>
      <button class="music-row-btn" data-act="add" title="Add to playlist">${IC.add}</button>
      ${ctx && ctx.removable ? `<button class="music-row-btn" data-act="remove" title="Remove">${IC.trash}</button>` : ''}
    </div>`;
}

function trackList(tracks, ctx) {
  if (!tracks || !tracks.length) return `<div class="music-empty">${esc(ctx && ctx.empty || 'Nothing here yet.')}</div>`;
  return `<div class="music-list">${tracks.map(t => trackRow(t, ctx)).join('')}</div>`;
}

// wire clicks for a list container: row -> play, buttons -> actions
function wireList(container, tracks, ctx) {
  container.querySelectorAll('.music-row').forEach((row, idx) => {
    const id = row.getAttribute('data-id');
    const t = tracks.find(x => x.id === id) || tracks[idx];
    row.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-act]');
      if (btn) { handleRowAction(btn.getAttribute('data-act'), t, ctx); e.stopPropagation(); return; }
      playFromList(tracks, tracks.indexOf(t));
    });
  });
}

async function handleRowAction(act, track, ctx) {
  if (act === 'like') {
    if (isLiked(track.id)) { await api(`/like/${track.id}`, { method: 'DELETE' }).catch(() => {}); _lib.liked = _lib.liked.filter(x => x.id !== track.id); }
    else { await api('/like', { method: 'POST', body: JSON.stringify(track) }).catch(() => {}); _lib.liked.unshift(track); }
    refreshView();
  } else if (act === 'add') {
    openPlaylistMenu(track);
  } else if (act === 'remove' && ctx && ctx.onRemove) {
    await ctx.onRemove(track);
    refreshView();
  }
}

function setView(v) { _view = v; refreshView(); _modal.querySelectorAll('.music-nav-btn').forEach(b => b.classList.toggle('active', b.dataset.view === v)); }

function refreshView() {
  const main = q('#music-main');
  if (!main) return;
  if (_view === 'search') renderSearch(main);
  else if (_view === 'home') renderHome(main);
  else renderLibrary(main);
}

function renderSearch(main) {
  if (!main.querySelector('.music-search')) {
    main.innerHTML = `
      <div class="music-search">
        <input id="music-q" placeholder="Search songs, artists, albums…" autocomplete="off">
        <button id="music-search-btn" class="music-btn music-btn-primary">Search</button>
      </div>
      <div class="music-results" id="music-results"><div class="music-empty">Search YouTube's catalog — anything from Drake to lo-fi.</div></div>`;
    const input = main.querySelector('#music-q');
    const go = () => doSearch(input.value);
    main.querySelector('#music-search-btn').addEventListener('click', go);
    input.addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
    setTimeout(() => input.focus(), 50);
  }
}

let _lastResults = [];
async function doSearch(query) {
  query = (query || '').trim();
  const box = q('#music-results');
  if (!query || !box) return;
  box.innerHTML = `<div class="music-empty">Searching…</div>`;
  try {
    const { results } = await api(`/search?q=${encodeURIComponent(query)}&limit=30`);
    _lastResults = results;
    box.innerHTML = trackList(results, { empty: 'No results.' });
    wireList(box, results, {});
  } catch (err) {
    box.innerHTML = `<div class="music-empty">Search failed: ${esc(err.message)}</div>`;
  }
}

function renderHome(main) {
  main.innerHTML = `
    <div class="music-home">
      <div class="music-section-title">Browse</div>
      <div class="music-chips">${GENRES.map(g => `<button class="music-chip" data-g="${esc(g)}">${esc(g)}</button>`).join('')}</div>
      <div class="music-section-title">Recently played</div>
      <div id="music-recent">${trackList(_lib.recent, { empty: 'Play something to see it here.' })}</div>
    </div>`;
  main.querySelectorAll('.music-chip').forEach(c => c.addEventListener('click', () => { setView('search'); const i = q('#music-q'); if (i) i.value = c.dataset.g; doSearch(c.dataset.g); }));
  const rc = main.querySelector('#music-recent');
  wireList(rc, _lib.recent, {});
}

function renderLibrary(main) {
  main.innerHTML = `
    <div class="music-library">
      <div class="music-section-row">
        <div class="music-section-title">Liked songs</div>
        ${_lib.liked.length ? `<button class="music-btn" id="music-play-liked">Play all</button>` : ''}
      </div>
      <div id="music-liked">${trackList(_lib.liked, { empty: 'Tap the heart on any track to save it.' })}</div>
      <div class="music-section-row">
        <div class="music-section-title">Playlists</div>
        <button class="music-btn" id="music-new-pl">New playlist</button>
      </div>
      <div id="music-playlists">${renderPlaylists()}</div>
    </div>`;
  const liked = main.querySelector('#music-liked');
  wireList(liked, _lib.liked, {});
  const pl = main.querySelector('#music-play-liked');
  if (pl) pl.addEventListener('click', () => { if (_lib.liked.length) playFromList(_lib.liked, 0); });
  main.querySelector('#music-new-pl').addEventListener('click', newPlaylist);
  main.querySelectorAll('[data-pl-toggle]').forEach(h => h.addEventListener('click', () => {
    const body = main.querySelector(`[data-pl-body="${h.dataset.plToggle}"]`);
    if (body) body.classList.toggle('open');
  }));
  main.querySelectorAll('[data-pl-del]').forEach(b => b.addEventListener('click', async (e) => {
    e.stopPropagation();
    await api(`/playlist/${b.dataset.plDel}`, { method: 'DELETE' }).catch(() => {});
    _lib.playlists = _lib.playlists.filter(p => p.id !== b.dataset.plDel);
    refreshView();
  }));
  main.querySelectorAll('[data-pl-play]').forEach(b => b.addEventListener('click', (e) => {
    e.stopPropagation();
    const p = _lib.playlists.find(x => x.id === b.dataset.plPlay);
    if (p && p.tracks.length) playFromList(p.tracks, 0);
  }));
  _lib.playlists.forEach(p => {
    const body = main.querySelector(`[data-pl-body="${p.id}"]`);
    if (body) wireList(body, p.tracks, { removable: true, onRemove: async (t) => { await api(`/playlist/${p.id}/tracks/${t.id}`, { method: 'DELETE' }).catch(() => {}); p.tracks = p.tracks.filter(x => x.id !== t.id); } });
  });
}

function renderPlaylists() {
  if (!_lib.playlists.length) return `<div class="music-empty">No playlists yet.</div>`;
  return _lib.playlists.map(p => `
    <div class="music-pl">
      <div class="music-pl-head" data-pl-toggle="${esc(p.id)}">
        <span class="music-pl-name">${esc(p.name)}</span>
        <span class="music-pl-count">${p.tracks.length}</span>
        <button class="music-row-btn" data-pl-play="${esc(p.id)}" title="Play">${IC.play}</button>
        <button class="music-row-btn" data-pl-del="${esc(p.id)}" title="Delete">${IC.trash}</button>
      </div>
      <div class="music-pl-body" data-pl-body="${esc(p.id)}">${trackList(p.tracks, { empty: 'Empty playlist.', removable: true })}</div>
    </div>`).join('');
}

async function newPlaylist() {
  const name = prompt('Playlist name:');
  if (!name || !name.trim()) return;
  try { const { playlist } = await api('/playlist', { method: 'POST', body: JSON.stringify({ name: name.trim() }) }); _lib.playlists.unshift(playlist); refreshView(); } catch (_) {}
}

// add-to-playlist popover
function openPlaylistMenu(track) {
  closeMenu();
  const menu = document.createElement('div');
  menu.className = 'music-menu';
  menu.innerHTML = `
    <div class="music-menu-title">Add to playlist</div>
    ${_lib.playlists.map(p => `<button data-pl="${esc(p.id)}">${esc(p.name)}</button>`).join('') || '<div class="music-menu-empty">No playlists yet</div>'}
    <button class="music-menu-new" data-new="1">+ New playlist</button>`;
  document.body.appendChild(menu);
  const close = () => closeMenu();
  menu.querySelectorAll('[data-pl]').forEach(b => b.addEventListener('click', async () => {
    const pid = b.dataset.pl;
    await api(`/playlist/${pid}/tracks`, { method: 'POST', body: JSON.stringify(track) }).catch(() => {});
    const p = _lib.playlists.find(x => x.id === pid); if (p) { p.tracks = p.tracks.filter(x => x.id !== track.id); p.tracks.push(track); }
    close(); if (_view === 'library') refreshView();
  }));
  menu.querySelector('[data-new]')?.addEventListener('click', async () => {
    const name = prompt('Playlist name:'); if (!name || !name.trim()) return;
    try { const { playlist } = await api('/playlist', { method: 'POST', body: JSON.stringify({ name: name.trim() }) }); await api(`/playlist/${playlist.id}/tracks`, { method: 'POST', body: JSON.stringify(track) }); playlist.tracks = [track]; _lib.playlists.unshift(playlist); } catch (_) {}
    close(); if (_view === 'library') refreshView();
  });
  // position center-ish; simple fixed center
  setTimeout(() => document.addEventListener('click', _menuOutside, { once: true }), 0);
}
function _menuOutside(e) { if (!e.target.closest('.music-menu')) closeMenu(); else document.addEventListener('click', _menuOutside, { once: true }); }
function closeMenu() { document.querySelector('.music-menu')?.remove(); }

function renderBar() {
  const bar = q('#music-bar');
  if (!bar) return;
  if (!_current) { bar.classList.remove('active'); return; }
  bar.classList.add('active');
  const liked = isLiked(_current.id);
  bar.innerHTML = `
    <div class="music-bar-track">
      <div class="mb-art">
        <img id="mb-thumb" src="${esc(_current.thumb)}" alt="">
        <div class="mb-eq"><span></span><span></span><span></span><span></span></div>
      </div>
      <div class="music-bar-meta">
        <div class="music-bar-title">${esc(_current.title)}</div>
        <div class="music-bar-artist">${esc(_current.artist || '')}</div>
      </div>
      <button class="music-row-btn mb-like${liked ? ' on' : ''}" id="mb-like" title="Like">${liked ? IC.heartFull : IC.heart}</button>
    </div>
    <div class="music-bar-center">
      <div class="music-bar-controls">
        <button id="mb-shuffle" class="mb-ctl${_shuffle ? ' on' : ''}" title="Shuffle">${IC.shuffle}</button>
        <button id="mb-prev" class="mb-ctl" title="Previous">${IC.prev}</button>
        <button id="mb-play" class="mb-play" title="Play/Pause">${_audio && !_audio.paused ? IC.pause : IC.play}</button>
        <button id="mb-next" class="mb-ctl" title="Next">${IC.next}</button>
        <button id="mb-repeat" class="mb-ctl${_repeat !== 'off' ? ' on' : ''}" title="Repeat: ${_repeat}">${IC.repeat}${_repeat === 'one' ? '<sup>1</sup>' : ''}</button>
      </div>
      <div class="music-bar-seek">
        <span id="mb-cur">0:00</span>
        <input type="range" id="mb-seek" min="0" max="100" value="0">
        <span id="mb-total">0:00</span>
      </div>
    </div>
    <div class="music-bar-right">
      ${IC.vol}
      <input type="range" id="mb-vol" min="0" max="1" step="0.01" value="${_audio ? _audio.volume : 1}" title="Volume">
    </div>`;
  q('#mb-play').addEventListener('click', togglePlay);
  q('#mb-prev').addEventListener('click', prev);
  q('#mb-next').addEventListener('click', () => next(false));
  q('#mb-shuffle').addEventListener('click', () => { _shuffle = !_shuffle; renderBar(); });
  q('#mb-repeat').addEventListener('click', () => { _repeat = _repeat === 'off' ? 'all' : _repeat === 'all' ? 'one' : 'off'; renderBar(); });
  q('#mb-like').addEventListener('click', () => handleRowAction('like', _current, {}).then(() => renderBar()));
  const seek = q('#mb-seek');
  seek.addEventListener('input', () => { if (_audio) _audio.currentTime = Number(seek.value); onTime(); });
  const vol = q('#mb-vol');
  const setVolFill = () => vol.style.setProperty('--fill', (Number(vol.value) * 100) + '%');
  vol.addEventListener('input', () => { if (_audio) _audio.volume = Number(vol.value); setVolFill(); });
  setVolFill();
  bar.classList.toggle('playing', !!(_audio && !_audio.paused));
  onTime();
}

// --- window shell (MAGI pattern) ---------------------------------------------

function closePanel() { if (_modal) _modal.classList.add('hidden'); }

function ensureModal() {
  if (_modal) return _modal;
  const modal = document.createElement('div');
  modal.id = 'music-modal';
  modal.className = 'modal hidden';
  modal.innerHTML = `
    <div class="modal-content music-modal-content" role="dialog" aria-modal="true" aria-label="Music" style="background:var(--bg)">
      <div class="modal-header">
        <h4>${MUSIC_ICON}Music</h4>
        <button type="button" class="close-btn" data-music-close aria-label="Close">✖</button>
      </div>
      <div class="modal-body music-body">
        <div class="music-nav">
          <button class="music-nav-btn active" data-view="search">Search</button>
          <button class="music-nav-btn" data-view="home">Home</button>
          <button class="music-nav-btn" data-view="library">Library</button>
        </div>
        <div class="music-main" id="music-main"></div>
        <div class="music-bar" id="music-bar"></div>
      </div>
    </div>`;
  document.body.appendChild(modal);
  _modal = modal;

  modal.querySelector('[data-music-close]')?.addEventListener('click', closePanel);
  modal.querySelectorAll('.music-nav-btn').forEach(b => b.addEventListener('click', () => setView(b.dataset.view)));

  const content = modal.querySelector('.modal-content');
  const header = modal.querySelector('.modal-header');
  makeWindowDraggable(modal, { content, header, skipSelector: 'button, input, select, label, .music-row', enableDock: true, enableLeftDock: true });

  Modals.register('music-modal', {
    restoreFn: () => modal.classList.remove('hidden'),
    closeFn: () => modal.classList.add('hidden'),
    sidebarBtnId: 'tool-music-btn',
    label: 'Music',
    icon: MUSIC_ICON,
  });
  Modals.injectMinimizeButton(modal, 'music-modal');
  return modal;
}

async function openPanel() {
  const modal = ensureModal();
  await loadLibrary();
  refreshView();
  if (_current) renderBar();
  modal.classList.remove('hidden');
  const card = modal.querySelector('.modal-content');
  if (card) { card.style.animation = 'none'; void card.offsetWidth; card.style.animation = ''; }
}

function init(apiBase) {
  API_BASE = apiBase || '';
  document.getElementById('tool-music-btn')?.addEventListener('click', openPanel);
}

export default { init, openPanel };
