// ============================================================
// commandPalette.js — the Console's ⌘K command palette.
//
// A grouped, fuzzy-filtered runner for the app's tools and actions
// (the mockup's palette). Opens from the top-bar ⌘K pill and from
// Cmd/Ctrl+K — taking those over from the old conversation-search
// overlay, which stays reachable as a "Search conversations…"
// command inside the palette. Each entry just clicks the element
// that already performs the action, so nothing is reimplemented.
// ============================================================

const ICONS = {
  chat: '<svg viewBox="0 0 24 24" fill="none"><path d="M21 15a2 2 0 0 1-2 2H8l-4 4V5a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>',
  search: '<svg viewBox="0 0 24 24" fill="none"><circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="1.7"/><path d="m20 20-3.2-3.2" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  cal: '<svg viewBox="0 0 24 24" fill="none"><rect x="3" y="4" width="18" height="18" rx="2" stroke="currentColor" stroke-width="1.7"/><path d="M3 10h18M8 2v4M16 2v4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  cmd: '<svg viewBox="0 0 24 24" fill="none"><rect x="3" y="4" width="18" height="13" rx="2" stroke="currentColor" stroke-width="1.7"/><path d="M8 20h8M12 17v3" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  book: '<svg viewBox="0 0 24 24" fill="none"><path d="M4 5a2 2 0 0 1 2-2h12v18H6a2 2 0 0 1-2-2V5Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>',
  compare: '<svg viewBox="0 0 24 24" fill="none"><circle cx="18" cy="18" r="3" stroke="currentColor" stroke-width="1.7"/><circle cx="6" cy="6" r="3" stroke="currentColor" stroke-width="1.7"/><path d="M13 6h3a2 2 0 0 1 2 2v7M11 18H8a2 2 0 0 1-2-2V9" stroke="currentColor" stroke-width="1.7"/></svg>',
  research: '<svg viewBox="0 0 24 24" fill="none"><circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="1.7"/><path d="m20 20-3.2-3.2M11 8v6M8 11h6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  image: '<svg viewBox="0 0 24 24" fill="none"><rect x="3" y="3" width="18" height="18" rx="2" stroke="currentColor" stroke-width="1.7"/><circle cx="8.5" cy="8.5" r="1.5" fill="currentColor"/><path d="M21 15l-5-5L5 21" stroke="currentColor" stroke-width="1.7"/></svg>',
  lib: '<svg viewBox="0 0 24 24" fill="none"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>',
  brain: '<svg viewBox="0 0 24 24" fill="none"><path d="M12 5a3 3 0 1 0-5.9.1 4 4 0 0 0-2.5 5.8 4 4 0 0 0 .5 6.6A4 4 0 1 0 12 18Z" stroke="currentColor" stroke-width="1.5"/><path d="M12 5a3 3 0 1 1 5.9.1 4 4 0 0 1 2.5 5.8 4 4 0 0 1-.5 6.6A4 4 0 1 1 12 18Z" stroke="currentColor" stroke-width="1.5"/></svg>',
  note: '<svg viewBox="0 0 24 24" fill="none"><path d="M5 3h10l4 4v14H5z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M15 3v5h5" stroke="currentColor" stroke-width="1.7"/></svg>',
  send: '<svg viewBox="0 0 24 24" fill="none"><path d="M22 2 9.5 14.5M22 2l-8 20-4.5-7.5L2 10z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>',
  task: '<svg viewBox="0 0 24 24" fill="none"><rect x="3" y="4" width="18" height="18" rx="2" stroke="currentColor" stroke-width="1.7"/><path d="M8 2v4M16 2v4M9 15l2 2 4-4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  theme: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.7"/><path d="M12 3a9 9 0 0 0 0 18 4.5 4.5 0 0 1 0-9 4.5 4.5 0 0 0 0-9" stroke="currentColor" stroke-width="1.5"/></svg>',
  mail: '<svg viewBox="0 0 24 24" fill="none"><rect x="2" y="4" width="20" height="16" rx="2" stroke="currentColor" stroke-width="1.7"/><path d="m22 7-10 6L2 7" stroke="currentColor" stroke-width="1.7"/></svg>',
  mic: '<svg viewBox="0 0 24 24" fill="none"><rect x="9" y="2" width="6" height="12" rx="3" stroke="currentColor" stroke-width="1.7"/><path d="M5 11a7 7 0 0 0 14 0M12 18v4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  gear: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="1.7"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 6.8 19.4l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0-1.1-2.7H3a2 2 0 1 1 0-4h.1A1.6 1.6 0 0 0 4.6 6.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 2.7-1.1V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 2.7 1.1l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0 1.1 2.7H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z" stroke="currentColor" stroke-width="1.4"/></svg>',
  bolt: '<svg viewBox="0 0 24 24" fill="none"><path d="M13 2 4 14h6l-1 8 9-12h-6l1-8Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
};

// group, label, hint, icon, click-target id (or a custom run fn)
function COMMANDS() {
  return [
    { g: 'Chat', l: 'New chat', h: 'start a fresh conversation', i: ICONS.chat, id: 'sidebar-new-chat-btn' },
    { g: 'Chat', l: 'Search conversations', h: 'find past chats', i: ICONS.search, run: () => click('rail-search-btn') || click('sidebar-search-btn') },
    { g: 'Chat', l: 'Talk to Shadow (voice)', h: 'open the voice console', i: ICONS.mic, run: () => window.shadowVoice ? window.shadowVoice.open() : click('topbar-voice-btn') },

    { g: 'Tools', l: 'Command — devices', h: 'device dashboard + terminal', i: ICONS.cmd, id: 'tool-command-btn' },
    { g: 'Tools', l: 'Calendar', h: 'events & reminders', i: ICONS.cal, id: 'tool-calendar-btn' },
    { g: 'Tools', l: 'Cookbook', h: 'local model recipes', i: ICONS.book, id: 'tool-cookbook-btn' },
    { g: 'Tools', l: 'Compare models', h: 'side-by-side', i: ICONS.compare, id: 'tool-compare-btn' },
    { g: 'Tools', l: 'Deep Research', h: 'multi-step research', i: ICONS.research, id: 'tool-research-btn' },
    { g: 'Tools', l: 'Gallery', h: 'generated images', i: ICONS.image, id: 'tool-gallery-btn' },
    { g: 'Tools', l: 'Library', h: 'documents', i: ICONS.lib, id: 'tool-library-btn' },
    { g: 'Tools', l: 'Brain', h: 'memories', i: ICONS.brain, id: 'tool-memory-btn' },
    { g: 'Tools', l: 'Notes', h: 'quick notes', i: ICONS.note, id: 'tool-notes-btn' },
    { g: 'Tools', l: 'Telegram', h: 'messages', i: ICONS.send, id: 'tool-telegram-btn' },
    { g: 'Tools', l: 'Tasks', h: 'scheduled & background', i: ICONS.task, id: 'tool-tasks-btn' },
    { g: 'Tools', l: 'Missions', h: 'agent missions', i: ICONS.bolt, id: 'tool-missions-btn' },
    { g: 'Tools', l: 'MAGI', h: 'council', i: ICONS.bolt, id: 'tool-magi-btn' },
    { g: 'Tools', l: 'Music', h: 'audio', i: ICONS.bolt, id: 'tool-music-btn' },
    { g: 'Tools', l: 'Email', h: 'inbox', i: ICONS.mail, run: () => click('email-section-title') || click('tool-email-btn') },

    { g: 'Settings', l: 'Theme', h: 'change appearance', i: ICONS.theme, id: 'tool-theme-btn' },
    { g: 'Settings', l: 'Settings', h: 'preferences & account', i: ICONS.gear, run: () => click('user-bar-settings') || click('rail-settings') },
  ];
}

let veil = null, inputEl = null, listEl = null, sel = 0, flat = [];

function click(id) { const el = document.getElementById(id); if (el) { el.click(); return true; } return false; }

function build() {
  veil = document.createElement('div');
  veil.id = 'cmdk-veil';
  veil.innerHTML = `
    <div id="cmdk">
      <div class="cmdk-input-row">
        ${ICONS.search}
        <input id="cmdk-input" type="text" placeholder="Search devices, tools, actions…" autocomplete="off" spellcheck="false">
        <span class="cmdk-kbd">esc</span>
      </div>
      <div class="cmdk-results" id="cmdk-results"></div>
      <div class="cmdk-hint">
        <span><span class="cmdk-kbd">↑↓</span> navigate</span>
        <span><span class="cmdk-kbd">↵</span> run</span>
        <span><span class="cmdk-kbd">esc</span> close</span>
      </div>
    </div>`;
  document.body.appendChild(veil);
  inputEl = veil.querySelector('#cmdk-input');
  listEl = veil.querySelector('#cmdk-results');
  veil.addEventListener('click', (e) => { if (e.target === veil) close(); });
  inputEl.addEventListener('input', () => { sel = 0; renderList(); });
  inputEl.addEventListener('keydown', onKey);
}

function score(cmd, q) {
  if (!q) return 1;
  const hay = (cmd.l + ' ' + cmd.g + ' ' + (cmd.h || '')).toLowerCase();
  q = q.toLowerCase();
  if (hay.includes(q)) return 100 - hay.indexOf(q);
  // subsequence fuzzy
  let i = 0; for (const c of hay) { if (c === q[i]) i++; if (i === q.length) return 20; }
  return 0;
}

function renderList() {
  const q = inputEl.value.trim();
  const scored = COMMANDS().map((c) => ({ c, s: score(c, q) })).filter((x) => x.s > 0)
    .sort((a, b) => b.s - a.s);
  flat = scored.map((x) => x.c);
  if (!flat.length) { listEl.innerHTML = `<div class="cmdk-empty">No matches</div>`; return; }
  // group in first-seen order
  const groups = [];
  const byG = {};
  flat.forEach((c) => { if (!byG[c.g]) { byG[c.g] = []; groups.push(c.g); } byG[c.g].push(c); });
  let idx = 0;
  listEl.innerHTML = groups.map((g) => `<div class="cmdk-group">${g}</div>` + byG[g].map((c) => {
    const i = idx++;
    return `<div class="cmdk-row ${i === sel ? 'sel' : ''}" data-i="${i}"><span class="ic">${c.i}</span><span class="t">${c.l}</span><span class="h">${c.h || ''}</span></div>`;
  }).join('')).join('');
  listEl.querySelectorAll('.cmdk-row').forEach((r) => {
    r.addEventListener('mousemove', () => { const i = +r.dataset.i; if (i !== sel) { sel = i; paintSel(); } });
    r.addEventListener('click', () => run(+r.dataset.i));
  });
}
function paintSel() {
  listEl.querySelectorAll('.cmdk-row').forEach((r) => r.classList.toggle('sel', +r.dataset.i === sel));
  const cur = listEl.querySelector('.cmdk-row.sel');
  if (cur) cur.scrollIntoView({ block: 'nearest' });
}
function onKey(e) {
  if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(sel + 1, flat.length - 1); paintSel(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(sel - 1, 0); paintSel(); }
  else if (e.key === 'Enter') { e.preventDefault(); run(sel); }
  else if (e.key === 'Escape') { e.preventDefault(); close(); }
}
function run(i) {
  const c = flat[i]; if (!c) return;
  close();
  setTimeout(() => { if (c.run) c.run(); else if (c.id) click(c.id); }, 40);
}

function open() {
  if (!veil) build();
  veil.classList.add('open');
  document.body.classList.add('cmdk-open');
  inputEl.value = ''; sel = 0; renderList();
  setTimeout(() => inputEl.focus(), 20);
}
function close() {
  if (!veil) return;
  veil.classList.remove('open');
  document.body.classList.remove('cmdk-open');
}
function toggle() { (veil && veil.classList.contains('open')) ? close() : open(); }

function init() {
  // Take over Cmd/Ctrl+K (capture phase → beats the old search binding).
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault(); e.stopImmediatePropagation();
      toggle();
    }
  }, true);
  // Take over the top-bar ⌘K pill click (capture → beats app.js opening search).
  document.addEventListener('click', (e) => {
    const pill = e.target.closest && e.target.closest('#topbar-cmd-btn');
    if (pill) { e.preventDefault(); e.stopImmediatePropagation(); open(); }
  }, true);
  window.shadowCommandPalette = { open, close, toggle };
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

export default { open, close, toggle };
