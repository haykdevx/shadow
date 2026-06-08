/**
 * Shadow — Telegram Desktop-like web client
 * Real MTProto user account via Telethon backend at /api/telegram/*
 * ES module, no framework, no build step.
 */

const API = '/api/telegram';

// ── State ──────────────────────────────────────────────────────────────────
let root = null;
let dialogs = [];
let activePeer = null;   // { id, title, type, username, has_photo }
let messages = [];       // loaded messages for active peer (oldest→newest)
let pollTimer = null;
let pollCursor = 0;
let dialogRefreshTimer = null;
let isPolling = false;   // in-flight guard

// Compose mode: { mode: 'reply'|'edit'|'forward', msgId, msg }
let composeMode = null;

// Lightbox: list of {url, caption, type:'photo'|'video'} in current thread
let lightboxMedia = [];
let lightboxIdx = 0;

// Typing throttle
let typingTimer = null;
let typingActive = false;

// Recording state
let mediaRecorder = null;
let recChunks = [];
let recTimerInterval = null;
let recSeconds = 0;

// Quick-react emoji list
const QUICK_EMOJIS = ['👍', '❤️', '🔥', '😁', '😮', '😢', '🙏'];

// ── New Wave-2 state ────────────────────────────────────────────────────────
let viewingArchive = false;          // true when left pane shows archived
let pinnedMessages = [];             // [{message_id, ...}] for active peer
let pinnedCycleIdx = 0;              // cycle index for pinned banner
let chatSearchActive = false;        // in-chat search bar visible
let chatSearchResults = [];          // message ids matching in-chat search
let chatSearchIdx = 0;               // current hit index
let globalSearchDebounce = null;     // debounce timer for left-pane search
let draftSaveTimer = null;           // idle draft save timer
let draftJustSent = false;           // suppress draft save after send
let profilePanelOpen = false;        // profile slide-over open
let headerMenuOpen = false;          // header ⋮ menu visible

// ── Helpers ────────────────────────────────────────────────────────────────

/** HTML-escape a value to safely insert into the DOM as text */
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

/** Minimal linkifier — converts http(s) URLs to <a> tags */
function linkify(html) {
  // html is already escaped; URLs won't contain < or > so this is safe.
  return html.replace(/(https?:\/\/[^\s<>"&]+)/g, (url) => {
    return `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`;
  });
}

/**
 * Fetch wrapper: returns parsed JSON or throws {status, detail}.
 * A 409 with not_authorized detail is treated specially.
 * Pass rawBody=true to skip JSON Content-Type header (for FormData).
 */
async function api(path, options = {}) {
  const isFormData = options.body instanceof FormData;
  const headers = isFormData
    ? { ...(options.headers || {}) }
    : { 'Content-Type': 'application/json', ...(options.headers || {}) };
  const res = await fetch(`${API}${path}`, {
    credentials: 'same-origin',
    headers,
    ...options,
  });
  let payload;
  try { payload = await res.json(); } catch (_) { payload = {}; }
  if (!res.ok) {
    const err = new Error(payload.detail || payload.error || `HTTP ${res.status}`);
    err.status = res.status;
    err.detail = payload.detail || '';
    throw err;
  }
  return payload;
}

/**
 * Derive a stable background color from a numeric peer id.
 * Returns one of a palette of Telegram-style colors.
 */
const AVATAR_COLORS = [
  '#e17055', '#6c5ce7', '#00b894', '#0984e3',
  '#d63031', '#00cec9', '#e84393', '#fdcb6e',
  '#636e72', '#2d3436',
];
function avatarColor(id) {
  const n = Math.abs(Number(id) || 0);
  return AVATAR_COLORS[n % AVATAR_COLORS.length];
}

/** Format epoch seconds to a relative time string (Today: HH:MM, else date) */
function formatDialogTime(epochSec) {
  if (!epochSec) return '';
  const d = new Date(epochSec * 1000);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const msgDay = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const diffDays = Math.floor((today - msgDay) / 86400000);
  if (diffDays === 0) {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } else if (diffDays === 1) {
    return 'Yesterday';
  } else if (diffDays < 7) {
    return d.toLocaleDateString([], { weekday: 'short' });
  }
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

/** Format epoch seconds to HH:MM for message timestamps */
function formatMsgTime(epochSec) {
  if (!epochSec) return '';
  return new Date(epochSec * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/** Format epoch seconds to a human-readable date for separators */
function formatDateSep(epochSec) {
  const d = new Date(epochSec * 1000);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const msgDay = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const diffDays = Math.floor((today - msgDay) / 86400000);
  if (diffDays === 0) return 'Today';
  if (diffDays === 1) return 'Yesterday';
  return d.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
}

/** Return ISO date string YYYY-MM-DD from epoch seconds */
function isoDay(epochSec) {
  const d = new Date(epochSec * 1000);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

/** Get initials from a title */
function initials(title) {
  if (!title) return '?';
  const parts = title.trim().split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return title.slice(0, 2).toUpperCase();
}

/** Format seconds to M:SS */
function formatDuration(sec) {
  if (!sec && sec !== 0) return '';
  const s = Math.round(sec);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** Format bytes to human-readable size */
function formatSize(bytes) {
  if (!bytes) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

/** Build an avatar element (img or colored initials circle) */
function buildAvatarEl(peer) {
  const wrap = document.createElement('div');
  wrap.className = 'tg-avatar';
  if (peer.has_photo) {
    const img = document.createElement('img');
    img.src = `${API}/avatar/${encodeURIComponent(peer.id)}`;
    img.alt = peer.title || '';
    img.loading = 'lazy';
    img.onerror = () => {
      // Fall back to initials if avatar 404s
      img.remove();
      const circle = buildInitialsCircle(peer);
      wrap.appendChild(circle);
    };
    wrap.appendChild(img);
  } else {
    wrap.appendChild(buildInitialsCircle(peer));
  }
  return wrap;
}

/** Build a colored initials circle div for a peer */
function buildInitialsCircle(peer) {
  const circle = document.createElement('div');
  circle.className = 'tg-avatar-initials';
  circle.style.background = avatarColor(peer.id);
  circle.textContent = initials(peer.title);
  return circle;
}

// ── Toast ─────────────────────────────────────────────────────────────────

function showToast(msg, isError = false) {
  let toast = document.querySelector('.tg-toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.className = 'tg-toast';
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.classList.toggle('tg-toast-error', isError);
  toast.classList.add('visible');
  clearTimeout(toast._hideTimer);
  toast._hideTimer = setTimeout(() => toast.classList.remove('visible'), 3000);
}

// ── CSS injection ───────────────────────────────────────────────────────────

function ensureStyles() {
  if (document.getElementById('telegram-app-css')) return;
  const link = document.createElement('link');
  link.id = 'telegram-app-css';
  link.rel = 'stylesheet';
  link.href = '/static/telegram.css';
  document.head.appendChild(link);
}

// ── DOM queries ─────────────────────────────────────────────────────────────

const q = (sel) => root ? root.querySelector(sel) : null;

// ── Build the page shell ────────────────────────────────────────────────────

function build() {
  const node = document.createElement('section');
  node.id = 'telegram-app';
  node.hidden = true;
  node.innerHTML = `
    <div class="tg-topbar">
      <span class="tg-topbar-title">Shadow // Telegram</span>
      <div class="tg-topbar-actions">
        <button class="tg-logout-btn" data-tg="logout" hidden>Log out</button>
        <button class="tg-close-btn" data-tg="close" aria-label="Close Telegram">✕</button>
      </div>
    </div>

    <!-- Login flow (shown when not authorized) -->
    <div class="tg-login-wrap" data-tg-view="login" hidden>
      <div class="tg-login-card">
        <div class="tg-login-logo">✈️</div>
        <div class="tg-login-title">Sign in to Telegram</div>

        <!-- Step 1: phone -->
        <div class="tg-login-step active" data-step="phone">
          <div class="tg-login-sub">Enter your phone number in international format, e.g. +1 650 555 1234</div>
          <div class="tg-login-error" data-login-err></div>
          <input class="tg-login-input" type="tel" placeholder="+1 650 555 1234" autocomplete="tel" data-login-phone />
          <button class="tg-login-btn" data-tg="send-code">Send code</button>
        </div>

        <!-- Step 2: OTP code -->
        <div class="tg-login-step" data-step="code">
          <div class="tg-login-sub">Enter the code sent to your Telegram app</div>
          <div class="tg-login-error" data-login-err></div>
          <input class="tg-login-input" type="text" inputmode="numeric" placeholder="12345" autocomplete="one-time-code" data-login-code />
          <button class="tg-login-btn" data-tg="verify">Verify code</button>
          <button class="tg-login-btn tg-login-btn-secondary" data-tg="login-back">← Back</button>
        </div>

        <!-- Step 3: 2FA password -->
        <div class="tg-login-step" data-step="password">
          <div class="tg-login-sub">Two-step verification enabled. Enter your cloud password.</div>
          <div class="tg-login-error" data-login-err></div>
          <input class="tg-login-input" type="password" placeholder="Password" autocomplete="current-password" data-login-password />
          <button class="tg-login-btn" data-tg="submit-password">Confirm</button>
        </div>

        <!-- Not configured notice -->
        <div class="tg-login-step" data-step="not-configured">
          <div class="tg-login-sub" style="color:#ff9aa4;">
            The server operator must set TELEGRAM_API_ID / TELEGRAM_API_HASH before this client can be used.
          </div>
        </div>
      </div>
    </div>

    <!-- Main Telegram layout (shown when authorized) -->
    <div class="tg-layout" data-tg-view="main" hidden>
      <!-- Left: dialog list -->
      <div class="tg-dialogs-pane" data-tg-dialogs-pane>
        <div class="tg-search-wrap">
          <input class="tg-search-input" type="search" placeholder="Search" data-tg-search />
          <button class="tg-new-btn" data-tg="new-menu" title="New chat">✏</button>
          <div class="tg-new-menu" data-tg-new-menu hidden>
            <div class="tg-new-menu-item" data-tg="new-group">New Group</div>
            <div class="tg-new-menu-item" data-tg="new-channel">New Channel</div>
          </div>
        </div>
        <div class="tg-archive-bar" data-tg-archive-bar hidden>
          <span class="tg-archive-bar-icon">🗂</span>
          <span class="tg-archive-bar-label">Archived</span>
          <span class="tg-archive-bar-count" data-tg-archive-count></span>
        </div>
        <div class="tg-archive-back" data-tg-archive-back hidden>
          <button class="tg-archive-back-btn" data-tg="archive-back">‹ Back</button>
          <span>Archived chats</span>
        </div>
        <!-- Global search results (replaces dialog list when searching) -->
        <div class="tg-search-results" data-tg-search-results hidden></div>
        <div class="tg-dialogs-list" data-tg-dialogs-list>
          <!-- skeleton rows injected here, then real dialogs -->
        </div>
      </div>

      <!-- Right: chat view -->
      <div class="tg-chat-pane" data-tg-chat-pane>
        <!-- Shown when no chat is selected -->
        <div class="tg-no-chat" data-tg-no-chat>
          <div class="tg-no-chat-icon">💬</div>
          <div class="tg-no-chat-text">Select a chat to start messaging</div>
        </div>

        <!-- Shown when a chat is open -->
        <div class="tg-active-chat" data-tg-active-chat hidden>
          <div class="tg-chat-header" data-tg-chat-header>
            <button class="tg-back-btn" data-tg="back" aria-label="Back to dialogs">‹</button>
            <div class="tg-avatar" data-tg-chat-avatar data-tg="open-profile" style="cursor:pointer"></div>
            <div class="tg-chat-header-info" data-tg="open-profile" style="cursor:pointer">
              <div class="tg-chat-header-title" data-tg-chat-title>Chat</div>
              <div class="tg-chat-header-sub" data-tg-chat-sub>
                <span class="tg-presence" data-tg-presence></span>
                <span class="tg-typing" data-tg-typing hidden>
                  <span class="tg-typing-dot"></span>
                  <span class="tg-typing-dot"></span>
                  <span class="tg-typing-dot"></span>
                </span>
              </div>
            </div>
            <div class="tg-header-actions">
              <button class="tg-header-btn" data-act="search" title="Search in chat">🔍</button>
              <button class="tg-header-btn" data-act="profile" title="Profile / Info">ℹ</button>
              <button class="tg-header-btn" data-act="menu" title="More actions">⋮</button>
              <div class="tg-header-menu" data-tg-header-menu hidden></div>
            </div>
          </div>

          <!-- In-chat search bar -->
          <div class="tg-chat-search" data-tg-chat-search hidden>
            <input class="tg-chat-search-input" type="search" placeholder="Search in chat…" data-tg-chat-search-input />
            <span class="tg-chat-search-count" data-tg-chat-search-count></span>
            <button class="tg-chat-search-prev" data-tg="chat-search-prev">↑</button>
            <button class="tg-chat-search-next" data-tg="chat-search-next">↓</button>
            <button class="tg-chat-search-close" data-tg="chat-search-close">✕</button>
          </div>

          <!-- Pinned message banner -->
          <div class="tg-pinned-banner" data-tg-pinned-banner hidden>
            <span class="tg-pinned-icon">📌</span>
            <div class="tg-pinned-body">
              <div class="tg-pinned-label">Pinned message</div>
              <div class="tg-pinned-text" data-tg-pinned-text></div>
            </div>
            <button class="tg-pinned-close" data-tg="pinned-close" title="Dismiss">✕</button>
          </div>

          <!-- Drop overlay -->
          <div class="tg-drop" data-tg-drop hidden>Drop files to send</div>

          <div class="tg-messages" data-tg-messages>
            <div class="tg-load-more" data-tg-load-more hidden>
              <button class="tg-load-more-btn" data-tg="load-more">Load older messages</button>
            </div>
            <!-- messages rendered here -->
            <button class="tg-scroll-btn" data-tg="scroll-bottom" title="Scroll to bottom">↓</button>
          </div>

          <!-- Recording bar -->
          <div class="tg-rec-bar" data-tg-rec-bar hidden>
            <span class="tg-rec-dot"></span>
            <span class="tg-rec-time" data-tg-rec-time>0:00</span>
            <button class="tg-rec-cancel" data-tg="rec-cancel">Cancel</button>
            <button class="tg-rec-send" data-tg="rec-send">Send</button>
          </div>

          <div class="tg-compose" data-tg-compose>
            <!-- Compose action bar (reply/edit/forward mode) -->
            <div class="tg-compose-action" data-tg-compose-action hidden>
              <span class="tg-ca-icon" data-tg-ca-icon></span>
              <div class="tg-ca-body">
                <div class="tg-ca-title" data-tg-ca-title></div>
                <div class="tg-ca-text" data-tg-ca-text></div>
              </div>
              <button class="tg-ca-close" data-tg="compose-cancel" aria-label="Cancel">✕</button>
            </div>

            <div class="tg-compose-row">
              <!-- Attach button -->
              <button class="tg-attach-btn" data-tg="attach" title="Attach">+</button>
              <!-- Attach menu popover -->
              <div class="tg-attach-menu" data-tg-attach-menu hidden>
                <button class="tg-attach-item" data-attach="media">Photo / Video</button>
                <button class="tg-attach-item" data-attach="file">File</button>
                <button class="tg-attach-item" data-attach="voice">Voice</button>
              </div>
              <!-- Hidden file inputs -->
              <input type="file" multiple accept="image/*,video/*" data-file-input="media" hidden />
              <input type="file" multiple data-file-input="file" hidden />

              <textarea class="tg-compose-textarea" placeholder="Write a message…" rows="1" data-tg-textarea></textarea>
              <button class="tg-rec-btn" data-tg="rec-start" title="Record voice">🎤</button>
              <button class="tg-send-btn" data-tg="send" disabled title="Send (Enter)">➤</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Profile panel -->
    <div class="tg-profile-backdrop" data-tg-profile-backdrop hidden></div>
    <div class="tg-profile-panel" data-tg-profile-panel hidden>
      <button class="tg-profile-close" data-tg="profile-close">✕</button>
      <div class="tg-profile-header">
        <div class="tg-profile-avatar" data-tg-profile-avatar></div>
        <div class="tg-profile-name" data-tg-profile-name></div>
        <div class="tg-profile-presence" data-tg-profile-presence></div>
      </div>
      <div class="tg-profile-sections" data-tg-profile-sections></div>
      <div class="tg-profile-tabs" data-tg-profile-tabs>
        <button class="tg-profile-tab active" data-kind="photo">Photos</button>
        <button class="tg-profile-tab" data-kind="video">Videos</button>
        <button class="tg-profile-tab" data-kind="file">Files</button>
        <button class="tg-profile-tab" data-kind="link">Links</button>
        <button class="tg-profile-tab" data-kind="voice">Voice</button>
      </div>
      <div class="tg-profile-media-grid" data-tg-profile-media-grid></div>
    </div>

    <!-- Lightbox -->
    <div class="tg-lightbox" data-tg-lightbox hidden>
      <div class="tg-lb-backdrop" data-tg="lb-close"></div>
      <div class="tg-lb-content" data-tg-lb-content></div>
      <button class="tg-lb-close" data-tg="lb-close">✕</button>
      <button class="tg-lb-prev" data-tg="lb-prev">‹</button>
      <button class="tg-lb-next" data-tg="lb-next">›</button>
      <div class="tg-lb-caption" data-tg-lb-caption></div>
      <a class="tg-lb-dl" data-tg-lb-dl download>⬇</a>
    </div>

    <!-- Generic modal shell -->
    <div class="tg-modal" data-tg-modal hidden>
      <div class="tg-modal-card" data-tg-modal-card>
        <div class="tg-modal-head">
          <div class="tg-modal-title" data-tg-modal-title></div>
          <button class="tg-modal-close" data-tg="modal-close">✕</button>
        </div>
        <div class="tg-modal-body" data-tg-modal-body></div>
        <div class="tg-modal-foot" data-tg-modal-foot></div>
      </div>
    </div>

    <!-- Context menu (appended to body dynamically) -->
  `;

  document.body.appendChild(node);
  wireEvents(node);
  return node;
}

// ── Event wiring ────────────────────────────────────────────────────────────

function wireEvents(node) {
  // General click delegation (handles all data-tg elements + dialog rows)
  node.addEventListener('click', handleClick);

  // Context menu on right-click of bubbles
  node.addEventListener('contextmenu', handleContextMenu);

  // Compose textarea: Enter to send, Shift+Enter = newline; auto-grow
  const textarea = node.querySelector('[data-tg-textarea]');
  if (textarea) {
    textarea.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        doSend();
      }
      if (e.key === 'Escape') {
        cancelComposeMode();
      }
    });
    textarea.addEventListener('input', () => {
      autoGrow(textarea);
      const sendBtn = node.querySelector('[data-tg="send"]');
      if (sendBtn) sendBtn.disabled = !textarea.value.trim();
      throttleTyping();
      // Idle draft save (3 s after last keystroke)
      if (activePeer && !draftJustSent) {
        clearTimeout(draftSaveTimer);
        draftSaveTimer = setTimeout(() => {
          if (activePeer && !draftJustSent) saveDraftForPeer(activePeer.id);
        }, 3000);
      }
    });
    textarea.addEventListener('blur', () => {
      if (activePeer && typingActive) {
        typingActive = false;
        api('/typing', { method: 'POST', body: JSON.stringify({ peer_id: activePeer.id, cancel: true }) }).catch(() => {});
      }
    });
  }

  // Attach menu items
  node.addEventListener('click', (e) => {
    const attachItem = e.target.closest('[data-attach]');
    if (!attachItem) return;
    const type = attachItem.dataset.attach;
    if (type === 'media') {
      node.querySelector('[data-file-input="media"]')?.click();
    } else if (type === 'file') {
      node.querySelector('[data-file-input="file"]')?.click();
    } else if (type === 'voice') {
      startRecording();
    }
    // Close attach menu
    const menu = node.querySelector('[data-tg-attach-menu]');
    if (menu) menu.hidden = true;
  });

  // File inputs
  node.querySelectorAll('[data-file-input]').forEach((inp) => {
    inp.addEventListener('change', () => {
      if (inp.files && inp.files.length) {
        openUploadPreview(Array.from(inp.files));
        inp.value = '';
      }
    });
  });

  // Search filtering (global search with debounce)
  const searchInput = node.querySelector('[data-tg-search]');
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      const q2 = searchInput.value.trim();
      if (!q2) {
        clearTimeout(globalSearchDebounce);
        hideGlobalSearch();
        return;
      }
      clearTimeout(globalSearchDebounce);
      globalSearchDebounce = setTimeout(() => doGlobalSearch(q2), 300);
    });
  }

  // In-chat search input
  node.addEventListener('input', (e) => {
    if (e.target.matches('[data-tg-chat-search-input]')) {
      clearTimeout(e.target._debounce);
      e.target._debounce = setTimeout(() => doChatSearch(e.target.value.trim()), 300);
    }
  });

  // Header action buttons (search / profile / menu)
  node.addEventListener('click', (e) => {
    const hBtn = e.target.closest('.tg-header-btn');
    if (!hBtn) return;
    const act = hBtn.dataset.act;
    if (act === 'search') toggleChatSearch();
    else if (act === 'profile') openProfilePanel(activePeer?.id);
    else if (act === 'menu') toggleHeaderMenu();
  });

  // Profile tabs
  node.addEventListener('click', (e) => {
    const tab = e.target.closest('.tg-profile-tab');
    if (!tab) return;
    node.querySelectorAll('.tg-profile-tab').forEach((t) => t.classList.remove('active'));
    tab.classList.add('active');
    loadProfileMedia(activePeer?.id, tab.dataset.kind);
  });

  // Archive bar click
  node.addEventListener('click', (e) => {
    if (e.target.closest('[data-tg-archive-bar]') && !viewingArchive) openArchive();
  });

  // Pinned banner click → jump to message
  node.addEventListener('click', (e) => {
    if (e.target.closest('[data-tg-pinned-banner]') && !e.target.closest('[data-tg="pinned-close"]')) {
      jumpToPinned();
    }
  });

  // Profile backdrop click → close
  node.addEventListener('click', (e) => {
    if (e.target.matches('[data-tg-profile-backdrop]')) closeProfilePanel();
  });

  // Scroll-to-bottom affordance
  const msgWrap = node.querySelector('[data-tg-messages]');
  if (msgWrap) {
    msgWrap.addEventListener('scroll', () => {
      const btn = msgWrap.querySelector('[data-tg="scroll-bottom"]');
      if (!btn) return;
      const distFromBottom = msgWrap.scrollHeight - msgWrap.scrollTop - msgWrap.clientHeight;
      btn.classList.toggle('visible', distFromBottom > 120);
    });
  }

  // Drag-and-drop on chat pane
  const chatPane = node.querySelector('[data-tg-chat-pane]');
  if (chatPane) {
    chatPane.addEventListener('dragover', (e) => {
      e.preventDefault();
      const drop = node.querySelector('[data-tg-drop]');
      if (drop) drop.hidden = false;
      drop && drop.classList.add('active');
    });
    chatPane.addEventListener('dragleave', (e) => {
      if (!chatPane.contains(e.relatedTarget)) {
        const drop = node.querySelector('[data-tg-drop]');
        if (drop) { drop.hidden = true; drop.classList.remove('active'); }
      }
    });
    chatPane.addEventListener('drop', (e) => {
      e.preventDefault();
      const drop = node.querySelector('[data-tg-drop]');
      if (drop) { drop.hidden = true; drop.classList.remove('active'); }
      const files = Array.from(e.dataTransfer.files);
      if (files.length) openUploadPreview(files);
    });
  }

  // Paste images from clipboard
  document.addEventListener('paste', (e) => {
    if (!activePeer || !root || root.hidden) return;
    const items = Array.from(e.clipboardData.items || []);
    const files = items.filter((i) => i.kind === 'file').map((i) => i.getAsFile()).filter(Boolean);
    if (files.length) {
      e.preventDefault();
      openUploadPreview(files);
    }
  });

  // Global key handler (Esc for lightbox/context menu/modals)
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeContextMenu();
      closeLightbox();
      closeModal();
      cancelComposeMode();
      if (profilePanelOpen) closeProfilePanel();
      if (headerMenuOpen) closeHeaderMenu();
      if (chatSearchActive) closeChatSearch();
    }
    if (e.key === 'ArrowLeft') navigateLightbox(-1);
    if (e.key === 'ArrowRight') navigateLightbox(1);
  });

  // Click outside to close attach menu / context menu / react picker / header menu / new menu
  document.addEventListener('click', (e) => {
    if (root && !e.target.closest('[data-tg-attach-menu]') && !e.target.closest('[data-tg="attach"]')) {
      const menu = root.querySelector('[data-tg-attach-menu]');
      if (menu) menu.hidden = true;
    }
    if (!e.target.closest('.tg-ctx-menu')) closeContextMenu();
    if (!e.target.closest('.tg-react-picker') && !e.target.closest('[data-act="react"]')) {
      root && root.querySelectorAll('.tg-react-picker').forEach((p) => p.remove());
    }
    if (root && !e.target.closest('[data-tg-header-menu]') && !e.target.closest('.tg-header-btn[data-act="menu"]')) {
      closeHeaderMenu();
    }
    if (root && !e.target.closest('[data-tg-new-menu]') && !e.target.closest('[data-tg="new-menu"]')) {
      const nm = root.querySelector('[data-tg-new-menu]');
      if (nm) nm.hidden = true;
    }
  });

  // Login inputs: Enter advances the current step; clear error on input
  node.querySelectorAll('.tg-login-input').forEach((inp) => {
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        // Find the primary (first) tg-login-btn (not secondary) in this step
        const step = inp.closest('.tg-login-step');
        const btn = step?.querySelector('.tg-login-btn:not(.tg-login-btn-secondary)');
        btn?.click();
      }
    });
    inp.addEventListener('input', () => {
      // Clear only this step's error as the user types
      const step = inp.closest('.tg-login-step');
      const errEl = step?.querySelector('[data-login-err]');
      if (errEl) {
        errEl.textContent = '';
        errEl.classList.remove('visible');
      }
    });
  });

  // Spoiler reveal (delegated)
  node.addEventListener('click', (e) => {
    const spoiler = e.target.closest('.tg-spoiler');
    if (spoiler && !spoiler.classList.contains('revealed')) {
      spoiler.classList.add('revealed');
    }
  });

  // Reply quote jump (delegated)
  node.addEventListener('click', (e) => {
    const quote = e.target.closest('[data-jump]');
    if (!quote) return;
    const targetId = quote.dataset.jump;
    const wrap = node.querySelector('[data-tg-messages]');
    const targetRow = wrap?.querySelector(`[data-msg-id="${CSS.escape(String(targetId))}"]`);
    if (targetRow) {
      targetRow.scrollIntoView({ behavior: 'smooth', block: 'center' });
      targetRow.classList.add('tg-flash');
      setTimeout(() => targetRow.classList.remove('tg-flash'), 1200);
    }
  });
}

function autoGrow(textarea) {
  textarea.style.height = 'auto';
  textarea.style.height = Math.min(textarea.scrollHeight, 160) + 'px';
}

// ── Typing indicator ────────────────────────────────────────────────────────

function throttleTyping() {
  if (!activePeer) return;
  if (typingTimer) return; // already scheduled
  typingActive = true;
  api('/typing', { method: 'POST', body: JSON.stringify({ peer_id: activePeer.id, cancel: false }) }).catch(() => {});
  typingTimer = setTimeout(() => { typingTimer = null; }, 4000);
}

let typingClearTimer = null;

function showTypingIndicator(name) {
  const typingEl = q('[data-tg-typing]');
  const presenceEl = q('[data-tg-presence]');
  if (!typingEl) return;
  typingEl.hidden = false;
  if (presenceEl) presenceEl.hidden = true;
  clearTimeout(typingClearTimer);
  typingClearTimer = setTimeout(() => {
    typingEl.hidden = true;
    if (presenceEl) presenceEl.hidden = false;
  }, 5000);
}

// ── handleClick ────────────────────────────────────────────────────────────

function handleClick(e) {
  const btn = e.target.closest('[data-tg]');
  if (!btn) {
    // Dialog row?
    const row = e.target.closest('[data-dialog-id]');
    if (row) openChat(row.dataset.dialogId);
    return;
  }
  const action = btn.dataset.tg;
  switch (action) {
    case 'close':             return closePage();
    case 'logout':            return doLogout();
    case 'back':              return showDialogPane();
    case 'login-back':        return loginStep('phone');
    case 'scroll-bottom':     return scrollToBottom(true);
    case 'load-more':         return loadOlderMessages();
    case 'send':              return doSend();
    case 'send-code':         return doLoginSendCode();
    case 'verify':            return doLoginVerify();
    case 'submit-password':   return doLoginPassword();
    case 'compose-cancel':    return cancelComposeMode();
    case 'attach':            return toggleAttachMenu();
    case 'rec-start':         return startRecording();
    case 'rec-cancel':        return cancelRecording();
    case 'rec-send':          return sendRecording();
    case 'lb-close':          return closeLightbox();
    case 'lb-prev':           return navigateLightbox(-1);
    case 'lb-next':           return navigateLightbox(1);
    case 'modal-close':       return closeModal();
    case 'open-profile':      return openProfilePanel(activePeer?.id);
    case 'profile-close':     return closeProfilePanel();
    case 'pinned-close':      return closePinnedBanner();
    case 'chat-search-prev':  return navigateChatSearch(-1);
    case 'chat-search-next':  return navigateChatSearch(1);
    case 'chat-search-close': return closeChatSearch();
    case 'archive-back':      return closeArchive();
    case 'new-menu':          return toggleNewMenu();
    case 'new-group':         return openCreateChatModal('group');
    case 'new-channel':       return openCreateChatModal('channel');
  }
}

// Context menu handler
function handleContextMenu(e) {
  const bubble = e.target.closest('.tg-bubble');
  if (!bubble) return;
  e.preventDefault();
  const row = bubble.closest('.tg-bubble-row');
  const msgId = row ? row.dataset.msgId : null;
  if (!msgId) return;
  const msg = messages.find((m) => String(m.id) === String(msgId));
  openContextMenu(e.clientX, e.clientY, msg, row);
}

// ── Views ───────────────────────────────────────────────────────────────────

function showView(name) {
  root.querySelector('[data-tg-view="login"]').hidden = (name !== 'login');
  root.querySelector('[data-tg-view="main"]').hidden  = (name !== 'main');
}

function showLoginStep(step) {
  root.querySelectorAll('.tg-login-step').forEach((el) => {
    el.classList.toggle('active', el.dataset.step === step);
  });
  // Focus first input in step
  const inp = root.querySelector(`.tg-login-step[data-step="${step}"] input`);
  if (inp) setTimeout(() => inp.focus(), 80);
}

/** Set error only on the active login step's error element */
function setLoginError(msg) {
  const activeStep = root.querySelector('.tg-login-step.active');
  if (!activeStep) return;
  const errEl = activeStep.querySelector('[data-login-err]');
  if (!errEl) return;
  errEl.textContent = msg || '';
  errEl.classList.toggle('visible', !!msg);
}

/** Clear all login errors (used when switching steps) */
function clearAllLoginErrors() {
  root.querySelectorAll('[data-login-err]').forEach((el) => {
    el.textContent = '';
    el.classList.remove('visible');
  });
}

function showDialogPane() {
  const dialogsPane = q('[data-tg-dialogs-pane]');
  const chatPane    = q('[data-tg-chat-pane]');
  if (dialogsPane) dialogsPane.classList.remove('slide-left');
  if (chatPane)    chatPane.classList.remove('slide-in');
}

function showChatPane() {
  const dialogsPane = q('[data-tg-dialogs-pane]');
  const chatPane    = q('[data-tg-chat-pane]');
  if (dialogsPane) dialogsPane.classList.add('slide-left');
  if (chatPane)    chatPane.classList.add('slide-in');
}

/** Set busy state on a login button (disable + spinner class) */
function setLoginBusy(selector, busy) {
  const btn = q(selector);
  if (!btn) return;
  btn.disabled = busy;
  btn.classList.toggle('loading', busy);
}

// ── Dialog list rendering ───────────────────────────────────────────────────

function renderDialogSkeletons() {
  const list = q('[data-tg-dialogs-list]');
  if (!list) return;
  list.innerHTML = Array.from({ length: 8 }).map(() => `
    <div class="tg-skeleton-row">
      <div class="tg-skeleton tg-skeleton-avatar"></div>
      <div class="tg-skeleton-lines">
        <div class="tg-skeleton tg-skeleton-line" style="width:${55 + Math.random() * 30}%"></div>
        <div class="tg-skeleton tg-skeleton-line" style="width:${35 + Math.random() * 40}%"></div>
      </div>
    </div>
  `).join('');
}

function renderDialogs(filter = '') {
  const list = q('[data-tg-dialogs-list]');
  if (!list) return;

  // Update archive bar visibility
  refreshArchiveBar();

  let rows = dialogs;
  if (filter) {
    rows = dialogs.filter((d) => d.title?.toLowerCase().includes(filter));
  }

  if (!rows.length) {
    list.innerHTML = `<div class="tg-empty-state">${filter ? 'No matching conversations' : 'No conversations yet'}</div>`;
    return;
  }

  list.innerHTML = '';
  rows.forEach((dialog) => {
    const row = document.createElement('div');
    row.className = `tg-dialog-row${dialog.pinned ? ' pinned' : ''}${activePeer && String(dialog.id) === String(activePeer.id) ? ' active' : ''}`;
    row.dataset.dialogId = dialog.id;

    // Avatar
    row.appendChild(buildAvatarEl(dialog));

    // Info
    const info = document.createElement('div');
    info.className = 'tg-dialog-info';
    const nameLine = document.createElement('div');
    nameLine.className = 'tg-dialog-name';
    nameLine.innerHTML = `${dialog.pinned ? '<span class="tg-dialog-pin" title="Pinned">📌</span>' : ''}${dialog.verified ? '<span class="tg-verified-icon" title="Verified">✔</span>' : ''}${dialog.muted ? '<span class="tg-dialog-mute" title="Muted">🔕</span>' : ''}<span>${esc(dialog.title || 'Unknown')}</span>`;
    const preview = document.createElement('div');
    preview.className = 'tg-dialog-preview';
    // Draft takes precedence over last_message
    if (dialog.draft) {
      preview.innerHTML = `<span class="tg-dialog-draft">Draft: ${esc(dialog.draft.slice(0, 60))}</span>`;
    } else {
      preview.textContent = dialog.last_message || '';
    }
    info.appendChild(nameLine);
    info.appendChild(preview);
    row.appendChild(info);

    // Meta (time + badge)
    const meta = document.createElement('div');
    meta.className = 'tg-dialog-meta';
    const timeEl = document.createElement('div');
    timeEl.className = 'tg-dialog-time';
    timeEl.textContent = formatDialogTime(dialog.last_date);
    meta.appendChild(timeEl);
    if (dialog.unread) {
      const badge = document.createElement('div');
      badge.className = `tg-unread-badge${dialog.muted ? ' muted' : ''}`;
      badge.textContent = dialog.unread > 99 ? '99+' : dialog.unread;
      meta.appendChild(badge);
    } else if (dialog.unread_mark) {
      // Unread dot when manually marked unread but no actual unread count
      const dot = document.createElement('div');
      dot.className = 'tg-unread-dot';
      meta.appendChild(dot);
    }
    row.appendChild(meta);

    list.appendChild(row);
  });
}

// ── Chat / message rendering ─────────────────────────────────────────────────

function renderChatHeader(peer) {
  const avatarEl = q('[data-tg-chat-avatar]');
  const titleEl  = q('[data-tg-chat-title]');
  const subEl    = q('[data-tg-chat-sub]');

  if (avatarEl) {
    avatarEl.innerHTML = '';
    avatarEl.style.cssText = 'width:40px;height:40px;border-radius:50%;overflow:hidden;flex-shrink:0;';
    if (peer.has_photo) {
      const img = document.createElement('img');
      img.src = `${API}/avatar/${encodeURIComponent(peer.id)}`;
      img.alt = peer.title || '';
      img.loading = 'lazy';
      img.style.cssText = 'width:100%;height:100%;object-fit:cover;';
      img.onerror = () => {
        img.remove();
        const circle = buildInitialsCircle(peer);
        circle.style.width = circle.style.height = '100%';
        avatarEl.appendChild(circle);
      };
      avatarEl.appendChild(img);
    } else {
      const circle = buildInitialsCircle(peer);
      circle.style.width = circle.style.height = '100%';
      avatarEl.appendChild(circle);
    }
  }
  if (titleEl) titleEl.textContent = peer.title || 'Chat';
  if (subEl) {
    // The sub element now contains presence/typing spans injected in build()
    // We preserve those children but rebuild the static text if needed.
    const parts = [];
    if (peer.type) parts.push(peer.type.charAt(0).toUpperCase() + peer.type.slice(1));
    if (peer.username) parts.push('@' + peer.username);
    const presenceEl = subEl.querySelector('[data-tg-presence]');
    if (presenceEl) {
      presenceEl.textContent = parts.join(' · ');
      presenceEl.classList.remove('online');
      presenceEl.hidden = false;
    } else {
      subEl.textContent = parts.join(' · ');
    }
    const typingEl = subEl.querySelector('[data-tg-typing]');
    if (typingEl) typingEl.hidden = true;
  }
}

/** Fetch and update presence in chat header */
async function fetchPresence(peerId) {
  try {
    const status = await api(`/peer-status/${encodeURIComponent(peerId)}`);
    const presenceEl = q('[data-tg-presence]');
    if (!presenceEl) return;
    presenceEl.textContent = status.label || '';
    presenceEl.classList.toggle('online', !!status.online);
    presenceEl.hidden = false;
  } catch (_) {
    // non-critical
  }
}

/** Build media element for a message */
function buildMediaEl(msg) {
  const media = msg.media;
  if (!media || !media.type) return null;
  const { type, url, caption, filename, size, duration, width, height, mime } = media;

  const wrapper = document.createElement('div');

  if (type === 'photo') {
    const img = document.createElement('img');
    img.className = 'tg-media-photo';
    img.src = url || '';
    img.alt = caption || 'Photo';
    img.loading = 'lazy';
    if (width && height) { img.width = Math.min(width, 320); }
    img.addEventListener('click', () => openLightbox(msg));
    wrapper.appendChild(img);

  } else if (type === 'video') {
    const videoWrap = document.createElement('div');
    videoWrap.className = 'tg-media-video';
    const poster = document.createElement('img');
    poster.src = url || '';
    poster.loading = 'lazy';
    poster.alt = 'Video';
    videoWrap.appendChild(poster);
    const overlay = document.createElement('div');
    overlay.className = 'tg-play-overlay';
    overlay.textContent = '▶';
    videoWrap.appendChild(overlay);
    if (duration) {
      const dur = document.createElement('span');
      dur.className = 'tg-media-dur';
      dur.textContent = formatDuration(duration);
      videoWrap.appendChild(dur);
    }
    videoWrap.addEventListener('click', () => openLightbox(msg));
    wrapper.appendChild(videoWrap);

  } else if (type === 'gif') {
    const vid = document.createElement('video');
    vid.className = 'tg-media-gif';
    vid.src = url || '';
    vid.autoplay = true;
    vid.loop = true;
    vid.muted = true;
    vid.playsInline = true;
    wrapper.appendChild(vid);

  } else if (type === 'voice') {
    const voiceWrap = document.createElement('div');
    voiceWrap.className = 'tg-voice';
    const audio = document.createElement('audio');
    audio.src = url || '';
    audio.preload = 'none';
    const playBtn = document.createElement('button');
    playBtn.className = 'tg-voice-play';
    playBtn.textContent = '▶';
    playBtn.addEventListener('click', () => {
      if (audio.paused) {
        audio.play().catch(() => {});
        playBtn.textContent = '⏸';
      } else {
        audio.pause();
        playBtn.textContent = '▶';
      }
    });
    audio.addEventListener('ended', () => { playBtn.textContent = '▶'; });
    const wave = document.createElement('div');
    wave.className = 'tg-voice-wave';
    // Static bars
    for (let i = 0; i < 20; i++) {
      const bar = document.createElement('span');
      bar.style.height = (20 + Math.random() * 60) + '%';
      wave.appendChild(bar);
    }
    const durEl = document.createElement('span');
    durEl.className = 'tg-voice-dur';
    durEl.textContent = duration ? formatDuration(duration) : '';
    voiceWrap.appendChild(playBtn);
    voiceWrap.appendChild(wave);
    voiceWrap.appendChild(durEl);
    voiceWrap.appendChild(audio);
    wrapper.appendChild(voiceWrap);

  } else if (type === 'audio') {
    const audioWrap = document.createElement('div');
    audioWrap.className = 'tg-audio';
    const audio = document.createElement('audio');
    audio.src = url || '';
    audio.preload = 'none';
    const playBtn = document.createElement('button');
    playBtn.className = 'tg-voice-play';
    playBtn.textContent = '▶';
    playBtn.addEventListener('click', () => {
      if (audio.paused) {
        audio.play().catch(() => {});
        playBtn.textContent = '⏸';
      } else {
        audio.pause();
        playBtn.textContent = '▶';
      }
    });
    audio.addEventListener('ended', () => { playBtn.textContent = '▶'; });
    const nameEl = document.createElement('span');
    nameEl.textContent = filename || 'Audio';
    const durEl = document.createElement('span');
    durEl.className = 'tg-voice-dur';
    durEl.textContent = duration ? formatDuration(duration) : '';
    audioWrap.appendChild(playBtn);
    audioWrap.appendChild(nameEl);
    audioWrap.appendChild(durEl);
    audioWrap.appendChild(audio);
    wrapper.appendChild(audioWrap);

  } else if (type === 'sticker') {
    const img = document.createElement('img');
    img.className = 'tg-sticker';
    img.src = url || '';
    img.alt = 'Sticker';
    img.loading = 'lazy';
    wrapper.appendChild(img);

  } else if (type === 'document') {
    const fileWrap = document.createElement('div');
    fileWrap.className = 'tg-file';
    const icon = document.createElement('span');
    icon.className = 'tg-file-icon';
    icon.textContent = '📎';
    const nameEl = document.createElement('span');
    nameEl.className = 'tg-file-name';
    nameEl.textContent = filename || 'File';
    const sizeEl = document.createElement('span');
    sizeEl.className = 'tg-file-size';
    sizeEl.textContent = formatSize(size);
    fileWrap.appendChild(icon);
    fileWrap.appendChild(nameEl);
    fileWrap.appendChild(sizeEl);
    if (url) {
      fileWrap.style.cursor = 'pointer';
      fileWrap.addEventListener('click', () => {
        const a = document.createElement('a');
        a.href = url;
        a.download = filename || 'file';
        a.rel = 'noopener noreferrer';
        a.click();
      });
    }
    wrapper.appendChild(fileWrap);

  } else if (type === 'webpage') {
    // Minimal webpage preview
    if (url) {
      const link = document.createElement('a');
      link.className = 'tg-file-chip';
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.innerHTML = `<span class="tg-file-icon">🔗</span><span class="tg-file-name">${esc(filename || url)}</span>`;
      wrapper.appendChild(link);
    }

  } else if (url) {
    // Fallback for unknown types with a URL
    const chip = document.createElement('a');
    chip.className = 'tg-file-chip';
    chip.href = url;
    chip.target = '_blank';
    chip.rel = 'noopener noreferrer';
    chip.download = filename || '';
    chip.innerHTML = `<span class="tg-file-icon">📎</span><span class="tg-file-name">${esc(filename || type || 'File')}</span>`;
    wrapper.appendChild(chip);
  }

  return wrapper.firstChild ? wrapper : null;
}

/**
 * Build and insert DOM for a list of messages.
 * Handles: date separators, service messages, grouping, bubbles, media.
 */
function buildMessageDOM(msgList, showChannel) {
  const frag = document.createDocumentFragment();
  let lastDay = null;
  let lastSenderId = null;

  msgList.forEach((msg, idx) => {
    const nextMsg = msgList[idx + 1];

    // Date separator
    const day = isoDay(msg.date);
    if (day !== lastDay) {
      const sep = document.createElement('div');
      sep.className = 'tg-date-sep';
      sep.innerHTML = `<span class="tg-date-sep-pill">${esc(formatDateSep(msg.date))}</span>`;
      frag.appendChild(sep);
      lastDay = day;
      // Force group break on day change
      lastSenderId = null;
    }

    // Service message
    if (msg.service) {
      const svc = document.createElement('div');
      svc.className = 'tg-service-msg';
      svc.innerHTML = `<span class="tg-service-pill">${esc(msg.text || 'Service message')}</span>`;
      svc.dataset.msgId = msg.id;
      frag.appendChild(svc);
      lastSenderId = null;
      return;
    }

    // Determine grouping
    const senderId = msg.out ? '__out__' : (msg.sender?.id ?? '__unknown__');
    const sameAsPrev = senderId === lastSenderId;
    const sameAsNext = nextMsg && !nextMsg.service &&
      (nextMsg.out ? '__out__' : (nextMsg.sender?.id ?? '__unknown__')) === senderId &&
      isoDay(nextMsg.date) === day;

    let groupClass;
    if (sameAsPrev && sameAsNext) groupClass = 'group-mid';
    else if (sameAsPrev && !sameAsNext) groupClass = 'group-last';
    else if (!sameAsPrev && sameAsNext) groupClass = 'group-first';
    else groupClass = 'solo';

    lastSenderId = senderId;

    // Bubble row
    const row = document.createElement('div');
    row.className = `tg-bubble-row ${msg.out ? 'out' : 'in'} ${groupClass}`;
    row.dataset.msgId = msg.id;

    // Hover actions bar
    const actionsBar = document.createElement('div');
    actionsBar.className = 'tg-msg-actions';
    [
      { act: 'react', icon: '😊' },
      { act: 'reply', icon: '↩' },
      { act: 'forward', icon: '⤷' },
      { act: 'more', icon: '⋯' },
    ].forEach(({ act, icon }) => {
      const ab = document.createElement('button');
      ab.className = 'tg-msg-act';
      ab.dataset.act = act;
      ab.dataset.msgId = msg.id;
      ab.title = act.charAt(0).toUpperCase() + act.slice(1);
      ab.textContent = icon;
      ab.addEventListener('click', (e) => {
        e.stopPropagation();
        handleMsgAction(act, msg, row, ab);
      });
      actionsBar.appendChild(ab);
    });
    row.appendChild(actionsBar);

    // Sender name (show for groups/channels, only on first in group)
    if (!msg.out && showChannel && (groupClass === 'solo' || groupClass === 'group-first') && msg.sender?.name) {
      const senderEl = document.createElement('div');
      senderEl.className = 'tg-sender-name';
      senderEl.style.color = avatarColor(msg.sender.id);
      senderEl.textContent = msg.sender.name;
      row.appendChild(senderEl);
    }

    // Bubble
    const bubble = document.createElement('div');
    bubble.className = `tg-bubble ${msg.out ? 'out' : 'in'}`;

    // Forward-from header
    if (msg.forward_from) {
      const fwd = document.createElement('div');
      fwd.className = 'tg-fwd';
      const fwdName = document.createElement('span');
      fwdName.className = 'tg-fwd-name';
      fwdName.textContent = msg.forward_from.name || '';
      fwd.appendChild(fwdName);
      bubble.appendChild(fwd);
    }

    // Reply quote
    if (msg.reply_quote) {
      const quote = document.createElement('div');
      quote.className = 'tg-reply-quote';
      if (msg.reply_to_id) quote.dataset.jump = msg.reply_to_id;
      quote.style.cursor = 'pointer';
      const qName = document.createElement('div');
      qName.className = 'tg-reply-quote-name';
      qName.textContent = msg.reply_quote.name || '';
      const qText = document.createElement('div');
      qText.className = 'tg-reply-quote-text';
      qText.textContent = msg.reply_quote.text || '';
      quote.appendChild(qName);
      quote.appendChild(qText);
      bubble.appendChild(quote);
    }

    // Media
    if (msg.media && msg.media.type) {
      const mediaEl = buildMediaEl(msg);
      if (mediaEl) bubble.appendChild(mediaEl);
      // Caption
      if (msg.media.caption) {
        const cap = document.createElement('div');
        cap.className = 'tg-bubble-text';
        cap.innerHTML = msg.text_html ? msg.text_html : linkify(esc(msg.media.caption));
        bubble.appendChild(cap);
      }
    } else if (!msg.media || !msg.media.type) {
      // Legacy media fallback (old format)
      if (msg.media && msg.media.url) {
        const { url, caption, filename, type: mtype } = msg.media;
        const chip = document.createElement('a');
        chip.className = 'tg-file-chip';
        chip.href = url;
        chip.target = '_blank';
        chip.rel = 'noopener noreferrer';
        chip.download = filename || '';
        chip.innerHTML = `<span class="tg-file-icon">📎</span><span class="tg-file-name">${esc(filename || mtype || 'File')}</span>`;
        bubble.appendChild(chip);
        if (caption) {
          const cap = document.createElement('div');
          cap.innerHTML = linkify(esc(caption));
          bubble.appendChild(cap);
        }
      }
    }

    // Text (rich HTML or plain)
    if (msg.text || msg.text_html) {
      const textNode = document.createElement('span');
      textNode.className = 'tg-bubble-text';
      if (msg.text_html) {
        textNode.innerHTML = msg.text_html;
      } else {
        textNode.innerHTML = linkify(esc(msg.text || ''));
      }
      // Only append if we didn't already append as caption
      if (!msg.media || !msg.media.caption) {
        bubble.appendChild(textNode);
      }
    }

    // Footer (time + tick)
    const footer = document.createElement('span');
    footer.className = 'tg-bubble-footer';
    const timeEl = document.createElement('span');
    timeEl.className = 'tg-bubble-time';
    timeEl.textContent = formatMsgTime(msg.date) + (msg.edited ? ' (edited)' : '');
    footer.appendChild(timeEl);
    if (msg.out) {
      const tick = document.createElement('span');
      tick.className = `tg-tick${msg.read === true ? ' read' : ''}`;
      tick.textContent = msg.read === true ? '✓✓' : '✓';
      footer.appendChild(tick);
    }
    bubble.appendChild(footer);

    row.appendChild(bubble);

    // Reactions
    if (msg.reactions && msg.reactions.length) {
      const reactionsEl = buildReactionsEl(msg);
      row.appendChild(reactionsEl);
    }

    frag.appendChild(row);
  });

  return frag;
}

/** Build reactions bar element */
function buildReactionsEl(msg) {
  const el = document.createElement('div');
  el.className = 'tg-reactions';
  el.dataset.msgId = msg.id;
  (msg.reactions || []).forEach((r) => {
    const btn = document.createElement('button');
    btn.className = `tg-reaction${r.chosen ? ' chosen' : ''}`;
    btn.dataset.emoji = r.emoji;
    btn.dataset.msgId = msg.id;
    const emojiSpan = document.createElement('span');
    emojiSpan.className = 'tg-reaction-emoji';
    emojiSpan.textContent = r.emoji;
    const countSpan = document.createElement('span');
    countSpan.className = 'tg-reaction-count';
    countSpan.textContent = r.count;
    btn.appendChild(emojiSpan);
    btn.appendChild(countSpan);
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleReaction(msg.id, r.emoji, r.chosen);
    });
    el.appendChild(btn);
  });
  return el;
}

function renderMessages(msgList) {
  const wrap = q('[data-tg-messages]');
  if (!wrap) return;

  // Preserve scroll-btn and load-more before clearing
  const scrollBtn  = wrap.querySelector('[data-tg="scroll-bottom"]');
  const loadMoreEl = wrap.querySelector('[data-tg-load-more]');
  wrap.innerHTML = '';
  if (loadMoreEl) wrap.appendChild(loadMoreEl);

  if (!msgList.length) {
    const empty = document.createElement('div');
    empty.className = 'tg-empty-state';
    empty.textContent = 'No messages yet';
    wrap.appendChild(empty);
  } else {
    // Collect media for lightbox
    lightboxMedia = [];
    const showChannel = activePeer && (activePeer.type === 'channel' || activePeer.type === 'group');
    msgList.forEach((m) => {
      if (m.media && (m.media.type === 'photo' || m.media.type === 'video')) {
        lightboxMedia.push({ url: m.media.url, caption: m.media.caption || m.text || '', type: m.media.type, msgId: m.id });
      }
    });
    const frag = buildMessageDOM(msgList, showChannel);
    wrap.appendChild(frag);
  }

  if (scrollBtn) wrap.appendChild(scrollBtn);
}

/** Append a single new message without re-rendering the whole list */
function appendMessage(msg) {
  const wrap = q('[data-tg-messages]');
  if (!wrap) return;
  const scrollBtn = wrap.querySelector('[data-tg="scroll-bottom"]');
  const showChannel = activePeer && (activePeer.type === 'channel' || activePeer.type === 'group');

  // Remove "No messages yet" empty state if present
  const emptyState = wrap.querySelector('.tg-empty-state');
  if (emptyState) emptyState.remove();

  // Date separator — compare against last message currently in the array
  const lastMsg = messages.length ? messages[messages.length - 1] : null;
  if (!lastMsg || isoDay(lastMsg.date) !== isoDay(msg.date)) {
    const sep = document.createElement('div');
    sep.className = 'tg-date-sep';
    sep.innerHTML = `<span class="tg-date-sep-pill">${esc(formatDateSep(msg.date))}</span>`;
    wrap.insertBefore(sep, scrollBtn || null);
  }

  // Push to local list
  messages.push(msg);

  // Track media for lightbox
  if (msg.media && (msg.media.type === 'photo' || msg.media.type === 'video')) {
    lightboxMedia.push({ url: msg.media.url, caption: msg.media.caption || msg.text || '', type: msg.media.type, msgId: msg.id });
  }

  // Re-render last 3 messages so group-class of the preceding bubble updates too.
  const tail = messages.slice(-3);
  const existingRows = Array.from(wrap.querySelectorAll('[data-msg-id]'));
  const removeCount = Math.min(existingRows.length, tail.length - 1);
  for (let i = existingRows.length - removeCount; i < existingRows.length; i++) {
    existingRows[i].remove();
  }
  const frag = buildMessageDOM(tail, showChannel);
  wrap.insertBefore(frag, scrollBtn || null);
}

/** Edit/replace an existing message in the DOM */
function updateMessage(msg) {
  const wrap = q('[data-tg-messages]');
  if (!wrap) return;
  const idx = messages.findIndex((m) => m.id === msg.id);
  if (idx !== -1) messages[idx] = msg;

  const row = wrap.querySelector(`[data-msg-id="${CSS.escape(String(msg.id))}"]`);
  if (!row) return;
  const bubble = row.querySelector('.tg-bubble');
  if (!bubble) return;
  // Update text span
  const textSpan = bubble.querySelector('.tg-bubble-text');
  if (textSpan) {
    if (msg.text_html) {
      textSpan.innerHTML = msg.text_html;
    } else {
      textSpan.innerHTML = linkify(esc(msg.text || ''));
    }
  }
  const timeEl = bubble.querySelector('.tg-bubble-time');
  if (timeEl) timeEl.textContent = formatMsgTime(msg.date) + ' (edited)';
}

function scrollToBottom(smooth = false) {
  const wrap = q('[data-tg-messages]');
  if (!wrap) return;
  wrap.scrollTo({ top: wrap.scrollHeight, behavior: smooth ? 'smooth' : 'instant' });
}

// ── Reactions ───────────────────────────────────────────────────────────────

async function toggleReaction(msgId, emoji, chosen) {
  if (!activePeer) return;
  const sendEmoji = chosen ? '' : emoji;
  try {
    await api('/react', {
      method: 'POST',
      body: JSON.stringify({ peer_id: activePeer.id, message_id: msgId, emoji: sendEmoji }),
    });
    // Update local state
    const msg = messages.find((m) => String(m.id) === String(msgId));
    if (msg && msg.reactions) {
      const r = msg.reactions.find((x) => x.emoji === emoji);
      if (r) {
        r.chosen = !chosen;
        r.count = chosen ? Math.max(0, r.count - 1) : r.count + 1;
        if (r.count === 0) msg.reactions = msg.reactions.filter((x) => x.emoji !== emoji);
      } else if (!chosen) {
        msg.reactions = msg.reactions || [];
        msg.reactions.push({ emoji, count: 1, chosen: true });
      }
      // Re-render reactions
      const wrap = q('[data-tg-messages]');
      const row = wrap?.querySelector(`[data-msg-id="${CSS.escape(String(msgId))}"]`);
      if (row) {
        const old = row.querySelector('.tg-reactions');
        if (old) old.remove();
        if (msg.reactions && msg.reactions.length) {
          row.appendChild(buildReactionsEl(msg));
        }
      }
    }
  } catch (err) {
    showToast('Failed to react: ' + err.message, true);
  }
}

// ── Message hover actions ────────────────────────────────────────────────────

function handleMsgAction(act, msg, row, btn) {
  switch (act) {
    case 'react':    openReactPicker(msg, btn); break;
    case 'reply':    setComposeReply(msg); break;
    case 'forward':  openForwardPicker([msg.id]); break;
    case 'more':     openContextMenu(
      btn.getBoundingClientRect().left,
      btn.getBoundingClientRect().bottom,
      msg, row
    ); break;
  }
}

// ── React picker ─────────────────────────────────────────────────────────────

function openReactPicker(msg, anchorEl) {
  // Remove any existing pickers
  document.querySelectorAll('.tg-react-picker').forEach((p) => p.remove());

  const picker = document.createElement('div');
  picker.className = 'tg-react-picker';

  QUICK_EMOJIS.forEach((emoji) => {
    const btn = document.createElement('button');
    btn.className = 'tg-react-pick';
    btn.textContent = emoji;
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const existing = msg.reactions?.find((r) => r.emoji === emoji);
      toggleReaction(msg.id, emoji, existing?.chosen || false);
      picker.remove();
    });
    picker.appendChild(btn);
  });

  document.body.appendChild(picker);

  // Position near anchor
  const rect = anchorEl.getBoundingClientRect();
  picker.style.position = 'fixed';
  picker.style.left = rect.left + 'px';
  picker.style.top = (rect.top - picker.offsetHeight - 4) + 'px';
  // Recalc after paint
  requestAnimationFrame(() => {
    const pr = picker.getBoundingClientRect();
    picker.style.top = Math.max(4, rect.top - pr.height - 4) + 'px';
    const rightEdge = rect.left + pr.width;
    if (rightEdge > window.innerWidth - 8) {
      picker.style.left = Math.max(4, window.innerWidth - pr.width - 8) + 'px';
    }
  });
}

// ── Context menu ─────────────────────────────────────────────────────────────

let activeCtxMenu = null;

function openContextMenu(x, y, msg, row) {
  closeContextMenu();
  if (!msg) return;

  const isOwn = msg.out;
  const isText = !msg.media || !msg.media.type;

  const menu = document.createElement('div');
  menu.className = 'tg-ctx-menu';

  const items = [
    { act: 'reply',   label: 'Reply' },
    { act: 'forward', label: 'Forward' },
    { act: 'copy',    label: 'Copy text' },
    ...(isOwn && isText ? [{ act: 'edit', label: 'Edit' }] : []),
    'sep',
    { act: msg.pinned ? 'unpin' : 'pin', label: msg.pinned ? 'Unpin' : 'Pin' },
    'sep',
    { act: 'delete', label: 'Delete', danger: true },
  ];

  items.forEach((item) => {
    if (item === 'sep') {
      const sep = document.createElement('div');
      sep.className = 'tg-ctx-sep';
      menu.appendChild(sep);
      return;
    }
    const el = document.createElement('div');
    el.className = `tg-ctx-item${item.danger ? ' danger' : ''}`;
    el.dataset.act = item.act;
    el.textContent = item.label;
    el.addEventListener('click', () => {
      handleCtxAction(item.act, msg);
      closeContextMenu();
    });
    menu.appendChild(el);
  });

  document.body.appendChild(menu);
  activeCtxMenu = menu;

  // Position
  menu.style.position = 'fixed';
  menu.style.left = x + 'px';
  menu.style.top  = y + 'px';
  requestAnimationFrame(() => {
    const mr = menu.getBoundingClientRect();
    if (mr.right > window.innerWidth - 8)  menu.style.left = Math.max(4, window.innerWidth - mr.width - 8) + 'px';
    if (mr.bottom > window.innerHeight - 8) menu.style.top  = Math.max(4, window.innerHeight - mr.height - 8) + 'px';
  });
}

function closeContextMenu() {
  if (activeCtxMenu) { activeCtxMenu.remove(); activeCtxMenu = null; }
}

async function handleCtxAction(act, msg) {
  switch (act) {
    case 'reply':
      setComposeReply(msg);
      break;
    case 'forward':
      openForwardPicker([msg.id]);
      break;
    case 'copy':
      if (msg.text) {
        try { await navigator.clipboard.writeText(msg.text); showToast('Copied'); } catch (_) {}
      }
      break;
    case 'edit':
      setComposeEdit(msg);
      break;
    case 'pin':
    case 'unpin':
      await doPinMessage(msg, act === 'pin');
      break;
    case 'delete':
      await doDeleteMessage(msg);
      break;
  }
}

async function doDeleteMessage(msg) {
  if (!activePeer) return;
  const confirmed = confirm('Delete this message?');
  if (!confirmed) return;
  try {
    await api('/delete', {
      method: 'POST',
      body: JSON.stringify({ peer_id: activePeer.id, message_ids: [msg.id], revoke: true }),
    });
    // Remove from local array and DOM
    const idx = messages.findIndex((m) => String(m.id) === String(msg.id));
    if (idx !== -1) messages.splice(idx, 1);
    const wrap = q('[data-tg-messages]');
    const row = wrap?.querySelector(`[data-msg-id="${CSS.escape(String(msg.id))}"]`);
    if (row) row.remove();
    showToast('Message deleted');
  } catch (err) {
    showToast('Failed to delete: ' + err.message, true);
  }
}

// ── Compose mode (reply / edit / forward) ───────────────────────────────────

function setComposeReply(msg) {
  composeMode = { mode: 'reply', msgId: msg.id, msg };
  showComposeAction('↩ Reply', msg.sender?.name || (msg.out ? 'You' : 'Them'), msg.text || '');
  q('[data-tg-textarea]')?.focus();
}

function setComposeEdit(msg) {
  composeMode = { mode: 'edit', msgId: msg.id, msg };
  showComposeAction('✏ Edit', 'Editing message', msg.text || '');
  const textarea = q('[data-tg-textarea]');
  if (textarea) {
    textarea.value = msg.text || '';
    autoGrow(textarea);
    textarea.focus();
    const sendBtn = q('[data-tg="send"]');
    if (sendBtn) sendBtn.disabled = !textarea.value.trim();
  }
}

function showComposeAction(icon, title, text) {
  const bar = q('[data-tg-compose-action]');
  if (!bar) return;
  bar.hidden = false;
  const iconEl = q('[data-tg-ca-icon]');
  const titleEl = q('[data-tg-ca-title]');
  const textEl  = q('[data-tg-ca-text]');
  if (iconEl) iconEl.textContent = icon;
  if (titleEl) titleEl.textContent = title;
  if (textEl)  textEl.textContent = text;
}

function cancelComposeMode() {
  composeMode = null;
  const bar = q('[data-tg-compose-action]');
  if (bar) bar.hidden = true;
  const textarea = q('[data-tg-textarea]');
  if (textarea) { textarea.value = ''; autoGrow(textarea); }
  const sendBtn = q('[data-tg="send"]');
  if (sendBtn) sendBtn.disabled = true;
}

// ── Chat opening ────────────────────────────────────────────────────────────

async function openChat(peerId) {
  // Save draft for previous peer before switching
  if (activePeer && String(activePeer.id) !== String(peerId)) {
    saveDraftForPeer(activePeer.id);
  }

  // Find dialog in main list or archived list
  let dialog = dialogs.find((d) => String(d.id) === String(peerId));
  if (!dialog) {
    // Could be from search results — create a minimal peer object
    dialog = { id: peerId, title: String(peerId), type: 'user', has_photo: false };
  }
  activePeer = dialog;
  composeMode = null;
  lightboxMedia = [];
  pinnedMessages = [];
  pinnedCycleIdx = 0;
  chatSearchActive = false;
  chatSearchResults = [];
  draftJustSent = false;

  // Close chat search if open
  const chatSearchBar = q('[data-tg-chat-search]');
  if (chatSearchBar) chatSearchBar.hidden = true;

  // Hide pinned banner until loaded
  const pinnedBanner = q('[data-tg-pinned-banner]');
  if (pinnedBanner) pinnedBanner.hidden = true;

  // Cancel any compose mode action bar
  const composeBar = q('[data-tg-compose-action]');
  if (composeBar) composeBar.hidden = true;

  // Update active row styling
  renderDialogs(q('[data-tg-search]')?.value?.trim().toLowerCase() || '');

  // Show chat pane
  const noChat    = q('[data-tg-no-chat]');
  const activeChat = q('[data-tg-active-chat]');
  if (noChat)    noChat.hidden = true;
  if (activeChat) activeChat.hidden = false;

  // Header
  renderChatHeader(dialog);

  // Fetch presence async (non-blocking)
  fetchPresence(peerId);

  // Clear messages, show spinner
  const msgWrap = q('[data-tg-messages]');
  const scrollBtnEl  = msgWrap?.querySelector('[data-tg="scroll-bottom"]');
  const loadMoreEl = msgWrap?.querySelector('[data-tg-load-more]');
  if (msgWrap) {
    msgWrap.innerHTML = '';
    if (loadMoreEl) msgWrap.appendChild(loadMoreEl);
    if (loadMoreEl) loadMoreEl.hidden = true;
    const spinWrap = document.createElement('div');
    spinWrap.className = 'tg-spinner-wrap';
    spinWrap.innerHTML = '<div class="tg-spinner"></div>';
    msgWrap.appendChild(spinWrap);
    if (scrollBtnEl) msgWrap.appendChild(scrollBtnEl);
  }

  // Reset and enable compose; prefill draft
  messages = [];
  const textarea = q('[data-tg-textarea]');
  const sendBtn  = q('[data-tg="send"]');
  if (textarea) {
    textarea.disabled = false;
    const draftText = dialog.draft || '';
    textarea.value = draftText;
    textarea.style.height = '';
    if (draftText) autoGrow(textarea);
  }
  if (sendBtn) sendBtn.disabled = !(textarea?.value.trim());

  // On mobile, slide to chat pane
  showChatPane();

  try {
    const payload = await api(`/messages/${encodeURIComponent(peerId)}?limit=50`);
    messages = payload.messages || [];
    renderMessages(messages);
    if (msgWrap) {
      const lm = msgWrap.querySelector('[data-tg-load-more]');
      if (lm) lm.hidden = !payload.has_more;
    }
    scrollToBottom(false);
    // Mark as read
    api('/read', { method: 'POST', body: JSON.stringify({ peer_id: peerId }) }).catch(() => {});
    // Clear unread badge in dialog
    const d = dialogs.find((x) => String(x.id) === String(peerId));
    if (d) { d.unread = 0; d.unread_mark = false; renderDialogs(q('[data-tg-search]')?.value?.trim().toLowerCase() || ''); }
    // Load pinned messages
    loadPinnedBanner(peerId);
    // Start idle draft save
    scheduleDraftSave();
  } catch (err) {
    handleApiError(err);
    if (msgWrap) {
      const spinWrap = msgWrap.querySelector('.tg-spinner-wrap');
      if (spinWrap) spinWrap.remove();
      const errDiv = document.createElement('div');
      errDiv.className = 'tg-empty-state';
      errDiv.textContent = `Failed to load messages: ${err.message}`;
      const lm = msgWrap.querySelector('[data-tg-load-more]');
      msgWrap.insertBefore(errDiv, lm ? lm.nextSibling : null);
      if (scrollBtnEl && !msgWrap.contains(scrollBtnEl)) msgWrap.appendChild(scrollBtnEl);
    }
  }

  if (textarea) textarea.focus();
}

async function loadOlderMessages() {
  if (!activePeer || !messages.length) return;
  const oldestId = messages[0].id;
  const loadMoreEl = q('[data-tg-load-more]');
  const loadMoreBtn = loadMoreEl?.querySelector('[data-tg="load-more"]');
  if (loadMoreEl) loadMoreEl.hidden = true;

  // Show busy on the button
  if (loadMoreBtn) { loadMoreBtn.disabled = true; loadMoreBtn.textContent = 'Loading…'; }

  try {
    const payload = await api(`/messages/${encodeURIComponent(activePeer.id)}?limit=50&before_id=${oldestId}`);
    const older = payload.messages || [];

    const msgWrap = q('[data-tg-messages]');

    if (!older.length) {
      // No more messages — keep load-more hidden
      return;
    }

    // Save scroll anchor
    const prevHeight = msgWrap ? msgWrap.scrollHeight : 0;

    // Prepend to local list
    messages = [...older, ...messages];

    // Add older media to lightbox list
    older.forEach((m) => {
      if (m.media && (m.media.type === 'photo' || m.media.type === 'video')) {
        lightboxMedia.unshift({ url: m.media.url, caption: m.media.caption || m.text || '', type: m.media.type, msgId: m.id });
      }
    });

    // Rebuild messages
    renderMessages(messages);
    if (msgWrap) {
      const lmEl = msgWrap.querySelector('[data-tg-load-more]');
      if (lmEl) lmEl.hidden = !payload.has_more;
    }

    // Restore scroll so the user stays at the same visual position
    if (msgWrap) {
      const newHeight = msgWrap.scrollHeight;
      msgWrap.scrollTop = newHeight - prevHeight;
    }
  } catch (err) {
    if (loadMoreEl) loadMoreEl.hidden = false;
    console.error('[telegram] load older messages failed', err);
  } finally {
    if (loadMoreBtn) { loadMoreBtn.disabled = false; loadMoreBtn.textContent = 'Load older messages'; }
  }
}

// ── Send ────────────────────────────────────────────────────────────────────

async function doSend() {
  if (!activePeer) return;
  const textarea = q('[data-tg-textarea]');
  const sendBtn  = q('[data-tg="send"]');
  const text = textarea?.value.trim();
  if (!text) return;

  const savedText = textarea.value;
  textarea.value = '';
  textarea.style.height = '';
  if (sendBtn) sendBtn.disabled = true;
  draftJustSent = true;
  clearTimeout(draftSaveTimer);
  // Clear saved draft for this peer
  if (activePeer) {
    const d = dialogs.find((x) => String(x.id) === String(activePeer.id));
    if (d) d.draft = '';
    api('/draft', { method: 'POST', body: JSON.stringify({ peer_id: activePeer.id, text: '' }) }).catch(() => {});
  }

  // Cancel typing indicator
  if (typingActive) {
    typingActive = false;
    clearTimeout(typingTimer);
    typingTimer = null;
    api('/typing', { method: 'POST', body: JSON.stringify({ peer_id: activePeer.id, cancel: true }) }).catch(() => {});
  }

  // --- Edit mode ---
  if (composeMode && composeMode.mode === 'edit') {
    const { msgId } = composeMode;
    cancelComposeMode();
    try {
      const res = await api('/edit', {
        method: 'POST',
        body: JSON.stringify({ peer_id: activePeer.id, message_id: msgId, text }),
      });
      if (res.message) updateMessage(res.message);
    } catch (err) {
      showToast('Failed to edit: ' + err.message, true);
      if (textarea) { textarea.value = savedText; autoGrow(textarea); }
    }
    if (textarea) textarea.focus();
    return;
  }

  // --- Reply / Normal send ---
  const replyToId = (composeMode && composeMode.mode === 'reply') ? composeMode.msgId : null;
  cancelComposeMode();

  // Optimistic append
  const optimisticId = `opt_${Date.now()}`;
  const optimistic = {
    id: optimisticId,
    out: true,
    text,
    text_html: null,
    date: Math.floor(Date.now() / 1000),
    sender: null,
    service: false,
    media: null,
    edited: false,
    reactions: [],
    read: null,
    reply_quote: null,
    forward_from: null,
  };
  appendMessage(optimistic);
  scrollToBottom(true);

  try {
    const body = { peer_id: activePeer.id, text };
    if (replyToId) body.reply_to_id = replyToId;
    const res = await api('/send', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    // Replace optimistic with real message id in both the array and the DOM
    if (res.message) {
      const idx = messages.findIndex((m) => m.id === optimisticId);
      if (idx !== -1) messages[idx] = res.message;
      const wrap = q('[data-tg-messages]');
      const row = wrap?.querySelector(`[data-msg-id="${CSS.escape(String(optimisticId))}"]`);
      if (row) row.dataset.msgId = res.message.id;
    }
  } catch (err) {
    // Remove optimistic message
    const idx = messages.findIndex((m) => m.id === optimisticId);
    if (idx !== -1) messages.splice(idx, 1);
    const wrap = q('[data-tg-messages]');
    const row = wrap?.querySelector(`[data-msg-id="${CSS.escape(String(optimisticId))}"]`);
    if (row) row.remove();
    // Restore text so user doesn't lose their message
    if (textarea) {
      textarea.value = savedText;
      autoGrow(textarea);
    }
    showComposeError(`Failed to send: ${err.message}`);
    console.error('[telegram] send failed', err);
  }

  // Refocus textarea
  if (textarea) {
    textarea.focus();
    if (sendBtn) sendBtn.disabled = !textarea.value.trim();
  }
}

/** Show a transient error below the compose area */
function showComposeError(msg) {
  const compose = q('[data-tg-compose]');
  if (!compose) return;
  let errEl = compose.querySelector('.tg-compose-error');
  if (!errEl) {
    errEl = document.createElement('div');
    errEl.className = 'tg-compose-error';
    compose.appendChild(errEl);
  }
  errEl.textContent = msg;
  errEl.classList.add('visible');
  clearTimeout(errEl._clearTimer);
  errEl._clearTimer = setTimeout(() => {
    errEl.textContent = '';
    errEl.classList.remove('visible');
  }, 4000);
}

// ── Attach menu ──────────────────────────────────────────────────────────────

function toggleAttachMenu() {
  const menu = q('[data-tg-attach-menu]');
  if (menu) menu.hidden = !menu.hidden;
}

// ── Upload preview modal ─────────────────────────────────────────────────────

function openUploadPreview(files) {
  if (!activePeer) { showToast('Open a chat first', true); return; }

  const body = document.createElement('div');
  const grid = document.createElement('div');
  grid.className = 'tg-upload-grid';

  files.forEach((file) => {
    const thumb = document.createElement('div');
    thumb.className = 'tg-upload-thumb';
    if (file.type.startsWith('image/')) {
      const img = document.createElement('img');
      img.style.cssText = 'width:100%;height:100%;object-fit:cover;';
      const reader = new FileReader();
      reader.onload = (e) => { img.src = e.target.result; };
      reader.readAsDataURL(file);
      thumb.appendChild(img);
    } else {
      thumb.style.cssText = 'display:flex;align-items:center;justify-content:center;flex-direction:column;gap:4px;';
      thumb.innerHTML = `<span style="font-size:24px">📎</span><span style="font-size:11px;word-break:break-all;text-align:center;">${esc(file.name)}</span>`;
    }
    grid.appendChild(thumb);
  });

  const captionInput = document.createElement('input');
  captionInput.type = 'text';
  captionInput.className = 'tg-upload-caption';
  captionInput.placeholder = 'Caption (optional)';

  body.appendChild(grid);
  body.appendChild(captionInput);

  const replyToId = (composeMode && composeMode.mode === 'reply') ? composeMode.msgId : null;

  openModal('Send File', body, [
    {
      label: 'Send',
      primary: true,
      action: async () => {
        closeModal();
        cancelComposeMode();
        const caption = captionInput.value.trim();
        const fd = new FormData();
        fd.append('peer_id', String(activePeer.id));
        if (caption) fd.append('caption', caption);
        if (replyToId) fd.append('reply_to_id', String(replyToId));
        files.forEach((f) => fd.append('files', f, f.name));
        try {
          const res = await api('/send-media', { method: 'POST', body: fd });
          if (res.message) appendMessage(res.message);
          scrollToBottom(true);
        } catch (err) {
          showToast('Upload failed: ' + err.message, true);
        }
      },
    },
    { label: 'Cancel', action: closeModal },
  ]);
}

// ── Voice recording ──────────────────────────────────────────────────────────

async function startRecording() {
  if (!activePeer) { showToast('Open a chat first', true); return; }
  if (mediaRecorder && mediaRecorder.state !== 'inactive') return;

  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (_) {
    showToast('Microphone permission denied', true);
    return;
  }

  recChunks = [];
  recSeconds = 0;
  mediaRecorder = new MediaRecorder(stream);
  mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) recChunks.push(e.data); };
  mediaRecorder.start();

  const recBar = q('[data-tg-rec-bar]');
  const recTime = q('[data-tg-rec-time]');
  if (recBar) recBar.hidden = false;

  recTimerInterval = setInterval(() => {
    recSeconds++;
    if (recTime) recTime.textContent = formatDuration(recSeconds);
  }, 1000);
}

function cancelRecording() {
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stream.getTracks().forEach((t) => t.stop());
    mediaRecorder.stop();
  }
  mediaRecorder = null;
  recChunks = [];
  clearInterval(recTimerInterval);
  const recBar = q('[data-tg-rec-bar]');
  if (recBar) recBar.hidden = true;
}

async function sendRecording() {
  if (!mediaRecorder || mediaRecorder.state === 'inactive') return;
  clearInterval(recTimerInterval);

  const recBar = q('[data-tg-rec-bar]');

  await new Promise((resolve) => {
    mediaRecorder.onstop = resolve;
    mediaRecorder.stream.getTracks().forEach((t) => t.stop());
    mediaRecorder.stop();
  });

  if (recBar) recBar.hidden = true;

  const mimeType = recChunks[0]?.type || 'audio/webm';
  const blob = new Blob(recChunks, { type: mimeType });
  const ext = mimeType.includes('ogg') ? 'ogg' : 'webm';
  // TODO: server may need .ogg/opus format; webm is fine for most modern Telegram clients
  const file = new File([blob], `voice.${ext}`, { type: mimeType });

  const fd = new FormData();
  fd.append('peer_id', String(activePeer.id));
  fd.append('voice', '1');
  fd.append('files', file, file.name);

  try {
    const res = await api('/send-media', { method: 'POST', body: fd });
    if (res.message) { appendMessage(res.message); scrollToBottom(true); }
  } catch (err) {
    showToast('Failed to send voice: ' + err.message, true);
  }

  mediaRecorder = null;
  recChunks = [];
}

// ── Forward picker ────────────────────────────────────────────────────────────

let forwardMsgIds = [];
let forwardPickerSelected = null;

function openForwardPicker(msgIds) {
  forwardMsgIds = msgIds;
  forwardPickerSelected = null;

  const body = document.createElement('div');

  const search = document.createElement('input');
  search.className = 'tg-picker-search';
  search.type = 'search';
  search.placeholder = 'Search chats…';

  const list = document.createElement('div');
  list.className = 'tg-picker-list';

  function renderPickerList(filter) {
    list.innerHTML = '';
    let rows = dialogs;
    if (filter) rows = dialogs.filter((d) => d.title?.toLowerCase().includes(filter));
    rows.forEach((d) => {
      const row = document.createElement('div');
      row.className = `tg-picker-row${forwardPickerSelected === d.id ? ' selected' : ''}`;
      row.appendChild(buildAvatarEl(d));
      const name = document.createElement('span');
      name.textContent = d.title || '';
      row.appendChild(name);
      row.addEventListener('click', () => {
        forwardPickerSelected = d.id;
        list.querySelectorAll('.tg-picker-row').forEach((r) => r.classList.remove('selected'));
        row.classList.add('selected');
      });
      list.appendChild(row);
    });
  }

  renderPickerList('');
  search.addEventListener('input', () => renderPickerList(search.value.trim().toLowerCase()));

  body.appendChild(search);
  body.appendChild(list);

  openModal('Forward to…', body, [
    {
      label: 'Forward',
      primary: true,
      action: async () => {
        if (!forwardPickerSelected) { showToast('Select a chat first', true); return; }
        const toPeerId = forwardPickerSelected;
        closeModal();
        try {
          await api('/forward', {
            method: 'POST',
            body: JSON.stringify({
              from_peer_id: activePeer.id,
              message_ids: forwardMsgIds,
              to_peer_id: toPeerId,
            }),
          });
          showToast('Forwarded');
        } catch (err) {
          showToast('Forward failed: ' + err.message, true);
        }
      },
    },
    { label: 'Cancel', action: closeModal },
  ]);
}

// ── Generic modal ─────────────────────────────────────────────────────────────

/**
 * Opens the shared modal shell with given title, body element, and buttons.
 * buttons: [{label, primary, danger, action}]
 */
function openModal(title, bodyEl, buttons = []) {
  const modal = q('[data-tg-modal]');
  if (!modal) return;
  const titleEl = q('[data-tg-modal-title]');
  const bodyContainer = q('[data-tg-modal-body]');
  const foot = q('[data-tg-modal-foot]');

  if (titleEl) titleEl.textContent = title;
  if (bodyContainer) { bodyContainer.innerHTML = ''; bodyContainer.appendChild(bodyEl); }
  if (foot) {
    foot.innerHTML = '';
    buttons.forEach(({ label, primary, danger, action }) => {
      const btn = document.createElement('button');
      btn.className = `tg-btn${primary ? ' tg-btn-primary' : ''}${danger ? ' tg-btn-danger' : ''}`;
      btn.textContent = label;
      btn.addEventListener('click', action);
      foot.appendChild(btn);
    });
  }
  modal.hidden = false;
}

function closeModal() {
  const modal = q('[data-tg-modal]');
  if (modal) modal.hidden = true;
}

// ── Lightbox ──────────────────────────────────────────────────────────────────

function openLightbox(msg) {
  // Find index in lightboxMedia
  const idx = lightboxMedia.findIndex((m) => String(m.msgId) === String(msg.id));
  lightboxIdx = idx >= 0 ? idx : 0;

  const lb = q('[data-tg-lightbox]');
  if (!lb) return;
  lb.hidden = false;
  renderLightboxItem();
}

function renderLightboxItem() {
  const lb = q('[data-tg-lightbox]');
  if (!lb || !lightboxMedia.length) return;

  const item = lightboxMedia[lightboxIdx];
  const content = q('[data-tg-lb-content]');
  const captionEl = q('[data-tg-lb-caption]');
  const dlEl = q('[data-tg-lb-dl]');
  const prevBtn = lb.querySelector('[data-tg="lb-prev"]');
  const nextBtn = lb.querySelector('[data-tg="lb-next"]');

  if (content) {
    content.innerHTML = '';
    if (item.type === 'video') {
      const vid = document.createElement('video');
      vid.className = 'tg-lb-video';
      vid.src = item.url || '';
      vid.controls = true;
      vid.autoplay = true;
      content.appendChild(vid);
    } else {
      const img = document.createElement('img');
      img.className = 'tg-lb-img';
      img.src = item.url || '';
      img.alt = item.caption || '';
      content.appendChild(img);
    }
  }
  if (captionEl) captionEl.textContent = item.caption || '';
  if (dlEl) { dlEl.href = item.url || '#'; dlEl.download = ''; }
  if (prevBtn) prevBtn.hidden = lightboxIdx === 0;
  if (nextBtn) nextBtn.hidden = lightboxIdx === lightboxMedia.length - 1;
}

function navigateLightbox(dir) {
  const lb = q('[data-tg-lightbox]');
  if (!lb || lb.hidden) return;
  const newIdx = lightboxIdx + dir;
  if (newIdx < 0 || newIdx >= lightboxMedia.length) return;
  lightboxIdx = newIdx;
  renderLightboxItem();
}

function closeLightbox() {
  const lb = q('[data-tg-lightbox]');
  if (lb) lb.hidden = true;
  // Stop any playing video
  const vid = lb?.querySelector('video');
  if (vid) { vid.pause(); vid.src = ''; }
}

// ── Login steps ──────────────────────────────────────────────────────────────

/** Light phone sanitization: keep leading +, digits, and spaces */
function sanitizePhone(raw) {
  const stripped = raw.replace(/[^\d\s+]/g, '');
  // Ensure leading + if user typed one
  return raw.startsWith('+') ? '+' + stripped.replace(/\+/g, '') : stripped;
}

function loginStep(step) {
  clearAllLoginErrors();
  showLoginStep(step);
}

async function doLoginSendCode() {
  const phoneInput = q('[data-login-phone]');
  const raw = phoneInput?.value.trim() || '';
  const phone = sanitizePhone(raw);
  if (!phone) { setLoginError('Please enter your phone number'); return; }
  setLoginBusy('[data-tg="send-code"]', true);
  setLoginError('');
  try {
    await api('/login/send-code', { method: 'POST', body: JSON.stringify({ phone }) });
    loginStep('code');
  } catch (err) {
    setLoginError(err.detail || err.message);
  } finally {
    setLoginBusy('[data-tg="send-code"]', false);
  }
}

async function doLoginVerify() {
  const code = q('[data-login-code]')?.value.trim();
  if (!code) { setLoginError('Please enter the code'); return; }
  setLoginBusy('[data-tg="verify"]', true);
  setLoginError('');
  try {
    const res = await api('/login/verify', { method: 'POST', body: JSON.stringify({ code }) });
    if (res.needs_password) {
      loginStep('password');
    } else {
      await onAuthorized();
    }
  } catch (err) {
    setLoginError(err.detail || err.message);
  } finally {
    setLoginBusy('[data-tg="verify"]', false);
  }
}

async function doLoginPassword() {
  const password = q('[data-login-password]')?.value;
  if (!password) { setLoginError('Please enter your password'); return; }
  setLoginBusy('[data-tg="submit-password"]', true);
  setLoginError('');
  try {
    await api('/login/password', { method: 'POST', body: JSON.stringify({ password }) });
    await onAuthorized();
  } catch (err) {
    setLoginError(err.detail || err.message);
  } finally {
    setLoginBusy('[data-tg="submit-password"]', false);
  }
}

async function doLogout() {
  try {
    await api('/logout', { method: 'POST' });
  } catch (_) {}
  resetMainState();
  showView('login');
  loginStep('phone');
}

// ── Dialogs loading ──────────────────────────────────────────────────────────

async function loadDialogs() {
  try {
    const payload = await api('/dialogs?limit=50');
    dialogs = payload.dialogs || [];
    renderDialogs(q('[data-tg-search]')?.value?.trim().toLowerCase() || '');
  } catch (err) {
    if (err.status === 409 && err.detail === 'not_authorized') {
      resetMainState();
      showView('login');
      loginStep('phone');
      return;
    }
    const list = q('[data-tg-dialogs-list]');
    if (list) list.innerHTML = `<div class="tg-empty-state">Failed to load chats: ${esc(err.message)}</div>`;
  }
}

// ── Real-time polling ────────────────────────────────────────────────────────

function startPolling() {
  stopPolling();
  pollTimer = setInterval(doPoll, 2000);
  dialogRefreshTimer = setInterval(loadDialogs, 15000);
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (dialogRefreshTimer) { clearInterval(dialogRefreshTimer); dialogRefreshTimer = null; }
}

async function doPoll() {
  if (document.hidden) return;
  if (isPolling) return;
  isPolling = true;
  try {
    const payload = await api(`/updates?cursor=${pollCursor}`);
    pollCursor = payload.cursor ?? pollCursor;
    const events = payload.events || [];
    for (const ev of events) {
      applyEvent(ev);
    }
  } catch (err) {
    if (err.status === 409) {
      stopPolling();
      resetMainState();
      showView('login');
      loginStep('phone');
    }
    // other errors: silent (network blip)
  } finally {
    isPolling = false;
  }
}

function applyEvent(ev) {
  switch (ev.type) {
    case 'message': {
      const isActive = activePeer && String(ev.peer_id) === String(activePeer.id);

      // Bump dialog list
      const dIdx = dialogs.findIndex((d) => String(d.id) === String(ev.peer_id));
      if (dIdx !== -1) {
        const d = dialogs[dIdx];
        d.last_message = ev.message?.text || '';
        d.last_date = ev.message?.date;
        if (!isActive) d.unread = (d.unread || 0) + 1;
        // Move to top (pinned items stay pinned)
        if (!d.pinned) {
          dialogs.splice(dIdx, 1);
          const firstNonPinned = dialogs.findIndex((x) => !x.pinned);
          dialogs.splice(firstNonPinned === -1 ? 0 : firstNonPinned, 0, d);
        }
        renderDialogs(q('[data-tg-search]')?.value?.trim().toLowerCase() || '');
      }

      if (isActive && ev.message) {
        // Don't duplicate optimistic or already-loaded messages
        if (!messages.find((m) => String(m.id) === String(ev.message.id))) {
          const msgWrap = q('[data-tg-messages]');
          const wasAtBottom = msgWrap
            ? msgWrap.scrollHeight - msgWrap.scrollTop - msgWrap.clientHeight < 100
            : true;
          appendMessage(ev.message);
          if (wasAtBottom || ev.message.out) scrollToBottom(true);
        }
        // Mark read
        api('/read', { method: 'POST', body: JSON.stringify({ peer_id: ev.peer_id }) }).catch(() => {});
      }
      break;
    }
    case 'read': {
      const dIdx = dialogs.findIndex((d) => String(d.id) === String(ev.peer_id));
      if (dIdx !== -1) {
        dialogs[dIdx].unread = 0;
        renderDialogs(q('[data-tg-search]')?.value?.trim().toLowerCase() || '');
      }
      // Update read ticks on outgoing messages
      if (activePeer && String(ev.peer_id) === String(activePeer.id)) {
        const wrap = q('[data-tg-messages]');
        if (wrap) {
          wrap.querySelectorAll('.tg-tick:not(.read)').forEach((tick) => {
            tick.classList.add('read');
            tick.textContent = '✓✓';
          });
        }
      }
      break;
    }
    case 'edit': {
      if (activePeer && String(ev.peer_id) === String(activePeer.id) && ev.message) {
        updateMessage(ev.message);
      }
      break;
    }
    case 'typing': {
      if (activePeer && String(ev.peer_id) === String(activePeer.id)) {
        showTypingIndicator(ev.name || '');
      }
      break;
    }
  }
}

// ── Authorization flow ──────────────────────────────────────────────────────

/** Reset all main-view state (called on logout or 409) */
function resetMainState() {
  stopPolling();
  activePeer = null;
  messages = [];
  dialogs = [];
  isPolling = false;
  composeMode = null;
  lightboxMedia = [];
  pinnedMessages = [];
  chatSearchActive = false;
  chatSearchResults = [];
  viewingArchive = false;
  profilePanelOpen = false;
  headerMenuOpen = false;
  clearTimeout(draftSaveTimer);
  draftSaveTimer = null;
  // Hide logout button
  const logoutBtn = q('[data-tg="logout"]');
  if (logoutBtn) logoutBtn.hidden = true;
  // Reset chat pane to "no chat selected"
  const noChat    = q('[data-tg-no-chat]');
  const activeChat = q('[data-tg-active-chat]');
  if (noChat)    noChat.hidden = false;
  if (activeChat) activeChat.hidden = true;
  // Clear dialogs list
  const list = q('[data-tg-dialogs-list]');
  if (list) list.innerHTML = '';
  // Close any open overlays
  closeContextMenu();
  closeLightbox();
  closeModal();
}

async function onAuthorized() {
  showView('main');
  const logoutBtn = q('[data-tg="logout"]');
  if (logoutBtn) logoutBtn.hidden = false;
  pollCursor = 0;
  activePeer = null;
  messages = [];
  dialogs = [];
  // Ensure chat pane starts at "no chat selected"
  const noChat    = q('[data-tg-no-chat]');
  const activeChat = q('[data-tg-active-chat]');
  if (noChat)    noChat.hidden = false;
  if (activeChat) activeChat.hidden = true;
  renderDialogSkeletons();
  await loadDialogs();
  startPolling();
}

function handleApiError(err) {
  if (err.status === 409 && err.detail === 'not_authorized') {
    resetMainState();
    showView('login');
    loginStep('phone');
  }
}

// ── Page lifecycle ──────────────────────────────────────────────────────────

async function openPage(options = {}) {
  ensureStyles();
  root = root || document.getElementById('telegram-app') || build();
  root.hidden = false;
  document.body.classList.add('telegram-app-open');
  if (options.push !== false && window.location.pathname !== '/telegram') {
    window.history.pushState({}, '', '/telegram');
  }

  // Start: check status
  try {
    const status = await api('/status');
    if (!status.configured) {
      showView('login');
      showLoginStep('not-configured');
      return;
    }
    if (!status.authorized) {
      showView('login');
      loginStep('phone');
      return;
    }
    // Already authorized
    await onAuthorized();
  } catch (err) {
    // Any error (409 or network) — fall back to login
    showView('login');
    loginStep('phone');
    if (err.status !== 409) {
      console.warn('[telegram] status check failed:', err.message);
    }
  }
}

function closePage() {
  if (activePeer) saveDraftForPeer(activePeer.id);
  if (root) root.hidden = true;
  document.body.classList.remove('telegram-app-open');
  stopPolling();
  closeContextMenu();
  if (profilePanelOpen) closeProfilePanel();
  if (window.location.pathname === '/telegram') {
    window.history.pushState({}, '', '/');
  }
}

function init() {
  document.getElementById('tool-telegram-btn')?.addEventListener('click', () => openPage());
  document.getElementById('rail-telegram')?.addEventListener('click', () => openPage());
  window.addEventListener('popstate', () => {
    if (window.location.pathname === '/telegram') openPage({ push: false });
    else if (root && !root.hidden) closePage();
  });
}

// ── Feature 1: Header menu (Mute/Pin/Archive/Mark unread/Clear/Delete) ───────

function toggleHeaderMenu() {
  const menu = q('[data-tg-header-menu]');
  if (!menu) return;
  if (!menu.hidden) { closeHeaderMenu(); return; }
  if (!activePeer) return;
  const dialog = dialogs.find((d) => String(d.id) === String(activePeer.id)) || activePeer;
  menu.innerHTML = '';
  const items = [
    { act: 'hm-mute',         label: dialog.muted    ? 'Unmute'      : 'Mute' },
    { act: 'hm-pin',          label: dialog.pinned   ? 'Unpin chat'  : 'Pin chat' },
    { act: 'hm-archive',      label: dialog.archived ? 'Unarchive'   : 'Archive' },
    { act: 'hm-mark-unread',  label: 'Mark as unread' },
    { act: 'hm-clear',        label: 'Clear history', danger: true },
    { act: 'hm-delete',       label: dialog.type === 'channel' ? 'Leave channel' : 'Delete chat', danger: true },
  ];
  items.forEach(({ act, label, danger }) => {
    const el = document.createElement('div');
    el.className = `tg-hm-item${danger ? ' danger' : ''}`;
    el.dataset.act = act;
    el.textContent = label;
    el.addEventListener('click', () => { closeHeaderMenu(); handleHeaderMenuAction(act, dialog); });
    menu.appendChild(el);
  });
  menu.hidden = false;
  headerMenuOpen = true;
}

function closeHeaderMenu() {
  const menu = q('[data-tg-header-menu]');
  if (menu) menu.hidden = true;
  headerMenuOpen = false;
}

async function handleHeaderMenuAction(act, dialog) {
  if (!activePeer) return;
  const peerId = activePeer.id;
  try {
    if (act === 'hm-mute') {
      const mute = !dialog.muted;
      await api('/chat/mute', { method: 'POST', body: JSON.stringify({ peer_id: peerId, mute }) });
      const d = dialogs.find((x) => String(x.id) === String(peerId));
      if (d) d.muted = mute;
      renderDialogs(q('[data-tg-search]')?.value?.trim().toLowerCase() || '');
      showToast(mute ? 'Muted' : 'Unmuted');

    } else if (act === 'hm-pin') {
      const pin = !dialog.pinned;
      await api('/chat/pin', { method: 'POST', body: JSON.stringify({ peer_id: peerId, pin }) });
      const d = dialogs.find((x) => String(x.id) === String(peerId));
      if (d) d.pinned = pin;
      renderDialogs(q('[data-tg-search]')?.value?.trim().toLowerCase() || '');
      showToast(pin ? 'Chat pinned' : 'Chat unpinned');

    } else if (act === 'hm-archive') {
      const archive = !dialog.archived;
      await api('/chat/archive', { method: 'POST', body: JSON.stringify({ peer_id: peerId, archive }) });
      if (archive) {
        dialogs = dialogs.filter((d) => String(d.id) !== String(peerId));
        renderDialogs(q('[data-tg-search]')?.value?.trim().toLowerCase() || '');
        showDialogPane();
      } else {
        await loadDialogs();
      }
      showToast(archive ? 'Archived' : 'Unarchived');

    } else if (act === 'hm-mark-unread') {
      await api('/chat/mark-unread', { method: 'POST', body: JSON.stringify({ peer_id: peerId, unread: true }) });
      const d = dialogs.find((x) => String(x.id) === String(peerId));
      if (d) d.unread_mark = true;
      renderDialogs(q('[data-tg-search]')?.value?.trim().toLowerCase() || '');
      showToast('Marked as unread');

    } else if (act === 'hm-clear') {
      confirmModal('Clear all messages in this chat?', async () => {
        await api('/chat/delete', { method: 'POST', body: JSON.stringify({ peer_id: peerId, leave: false, just_clear: true }) });
        messages = [];
        renderMessages([]);
        showToast('History cleared');
      });

    } else if (act === 'hm-delete') {
      const label = dialog.type === 'channel' ? 'Leave and delete this channel?' : 'Delete this chat?';
      confirmModal(label, async () => {
        await api('/chat/delete', { method: 'POST', body: JSON.stringify({ peer_id: peerId, leave: true, just_clear: false }) });
        dialogs = dialogs.filter((d) => String(d.id) !== String(peerId));
        activePeer = null;
        const noChat = q('[data-tg-no-chat]');
        const activeChat = q('[data-tg-active-chat]');
        if (noChat) noChat.hidden = false;
        if (activeChat) activeChat.hidden = true;
        showDialogPane();
        renderDialogs(q('[data-tg-search]')?.value?.trim().toLowerCase() || '');
        showToast('Chat deleted');
      });
    }
  } catch (err) {
    showToast('Action failed: ' + err.message, true);
  }
}

/** Open a confirm modal with a single Confirm button */
function confirmModal(message, onConfirm) {
  const body = document.createElement('div');
  body.textContent = message;
  openModal('Confirm', body, [
    { label: 'Confirm', danger: true, action: async () => { closeModal(); await onConfirm(); } },
    { label: 'Cancel', action: closeModal },
  ]);
}

// ── Feature 2: Pinned banner ─────────────────────────────────────────────────

async function loadPinnedBanner(peerId) {
  try {
    const data = await api(`/pinned/${encodeURIComponent(peerId)}`);
    pinnedMessages = data.messages || [];
    pinnedCycleIdx = 0;
    renderPinnedBanner();
  } catch (_) {
    pinnedMessages = [];
    const banner = q('[data-tg-pinned-banner]');
    if (banner) banner.hidden = true;
  }
}

function renderPinnedBanner() {
  const banner = q('[data-tg-pinned-banner]');
  const textEl = q('[data-tg-pinned-text]');
  if (!banner) return;
  if (!pinnedMessages.length) { banner.hidden = true; return; }
  const msg = pinnedMessages[pinnedCycleIdx % pinnedMessages.length];
  if (textEl) textEl.textContent = (msg.text || msg.media?.caption || 'Media message').slice(0, 80);
  banner.hidden = false;
}

function jumpToPinned() {
  if (!pinnedMessages.length) return;
  const msg = pinnedMessages[pinnedCycleIdx % pinnedMessages.length];
  pinnedCycleIdx = (pinnedCycleIdx + 1) % pinnedMessages.length;
  renderPinnedBanner();
  const wrap = q('[data-tg-messages]');
  const targetRow = wrap?.querySelector(`[data-msg-id="${CSS.escape(String(msg.message_id || msg.id))}"]`);
  if (targetRow) {
    targetRow.scrollIntoView({ behavior: 'smooth', block: 'center' });
    targetRow.classList.add('tg-flash');
    setTimeout(() => targetRow.classList.remove('tg-flash'), 1200);
  }
}

function closePinnedBanner() {
  const banner = q('[data-tg-pinned-banner]');
  if (banner) banner.hidden = true;
}

async function doPinMessage(msg, pin) {
  if (!activePeer) return;
  try {
    await api('/message/pin', { method: 'POST', body: JSON.stringify({ peer_id: activePeer.id, message_id: msg.id, pin }) });
    showToast(pin ? 'Message pinned' : 'Message unpinned');
    await loadPinnedBanner(activePeer.id);
  } catch (err) {
    showToast('Failed: ' + err.message, true);
  }
}

// ── Feature 3: In-chat search ────────────────────────────────────────────────

function toggleChatSearch() {
  const bar = q('[data-tg-chat-search]');
  if (!bar) return;
  if (!bar.hidden) { closeChatSearch(); return; }
  bar.hidden = false;
  chatSearchActive = true;
  const inp = q('[data-tg-chat-search-input]');
  if (inp) { inp.value = ''; inp.focus(); }
  const countEl = q('[data-tg-chat-search-count]');
  if (countEl) countEl.textContent = '';
  chatSearchResults = [];
  chatSearchIdx = 0;
}

function closeChatSearch() {
  const bar = q('[data-tg-chat-search]');
  if (bar) bar.hidden = true;
  chatSearchActive = false;
  // Remove all highlights
  const wrap = q('[data-tg-messages]');
  wrap?.querySelectorAll('.tg-search-hit').forEach((el) => {
    const parent = el.parentNode;
    parent.replaceChild(document.createTextNode(el.textContent), el);
    parent.normalize();
  });
  chatSearchResults = [];
  chatSearchIdx = 0;
}

async function doChatSearch(query) {
  if (!activePeer || !query) {
    const countEl = q('[data-tg-chat-search-count]');
    if (countEl) countEl.textContent = '';
    chatSearchResults = [];
    return;
  }
  try {
    const data = await api(`/search?q=${encodeURIComponent(query)}&peer_id=${encodeURIComponent(activePeer.id)}&limit=30`);
    const results = data.results || [];
    chatSearchResults = results.map((r) => r.message?.id || r.message_id).filter(Boolean);
    chatSearchIdx = chatSearchResults.length > 0 ? 0 : -1;
    const countEl = q('[data-tg-chat-search-count]');
    if (countEl) countEl.textContent = chatSearchResults.length ? `1/${chatSearchResults.length}` : '0';
    highlightChatSearchResults(query);
    if (chatSearchIdx >= 0) scrollToChatSearchResult(chatSearchResults[chatSearchIdx]);
  } catch (err) {
    showToast('Search failed: ' + err.message, true);
  }
}

function highlightChatSearchResults(query) {
  const wrap = q('[data-tg-messages]');
  if (!wrap) return;
  // Remove old highlights
  wrap.querySelectorAll('.tg-search-hit').forEach((el) => {
    const parent = el.parentNode;
    if (parent) { parent.replaceChild(document.createTextNode(el.textContent), el); parent.normalize(); }
  });
  if (!query) return;
  const lower = query.toLowerCase();
  wrap.querySelectorAll('.tg-bubble-text').forEach((textNode) => {
    const html = textNode.innerHTML;
    const escaped = esc(query);
    // Case-insensitive replace in the text content
    const re = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
    textNode.innerHTML = html.replace(re, (m) => `<span class="tg-search-hit">${m}</span>`);
  });
  // Mark active hit
  const allHits = wrap.querySelectorAll('.tg-search-hit');
  allHits.forEach((h, i) => h.classList.toggle('active', i === 0));
}

function navigateChatSearch(dir) {
  if (!chatSearchResults.length) return;
  chatSearchIdx = (chatSearchIdx + dir + chatSearchResults.length) % chatSearchResults.length;
  const countEl = q('[data-tg-chat-search-count]');
  if (countEl) countEl.textContent = `${chatSearchIdx + 1}/${chatSearchResults.length}`;
  scrollToChatSearchResult(chatSearchResults[chatSearchIdx]);
  // Update active hit highlight
  const wrap = q('[data-tg-messages]');
  const hits = wrap?.querySelectorAll('.tg-search-hit') || [];
  hits.forEach((h, i) => h.classList.toggle('active', i === chatSearchIdx));
}

function scrollToChatSearchResult(msgId) {
  const wrap = q('[data-tg-messages]');
  const row = wrap?.querySelector(`[data-msg-id="${CSS.escape(String(msgId))}"]`);
  if (row) {
    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    row.classList.add('tg-flash');
    setTimeout(() => row.classList.remove('tg-flash'), 1000);
  }
}

// ── Feature 4: Global search (left pane) ────────────────────────────────────

async function doGlobalSearch(query) {
  if (!query) { hideGlobalSearch(); return; }
  const resultsEl = q('[data-tg-search-results]');
  const listEl = q('[data-tg-dialogs-list]');
  if (!resultsEl || !listEl) return;
  resultsEl.innerHTML = '<div class="tg-empty-state">Searching…</div>';
  resultsEl.hidden = false;
  listEl.hidden = true;
  const archiveBar = q('[data-tg-archive-bar]');
  if (archiveBar) archiveBar.hidden = true;

  try {
    const [chatsData, msgsData] = await Promise.all([
      api(`/search-dialogs?q=${encodeURIComponent(query)}&limit=20`),
      api(`/search?q=${encodeURIComponent(query)}&peer_id=0&limit=30`),
    ]);
    const chatRows = chatsData.dialogs || [];
    const msgRows = msgsData.results || [];

    resultsEl.innerHTML = '';

    if (chatRows.length) {
      const section = document.createElement('div');
      section.className = 'tg-search-section';
      const title = document.createElement('div');
      title.className = 'tg-search-section-title';
      title.textContent = 'Chats';
      section.appendChild(title);
      chatRows.forEach((d) => {
        const row = document.createElement('div');
        row.className = 'tg-dialog-row';
        row.dataset.dialogId = d.id;
        row.appendChild(buildAvatarEl(d));
        const info = document.createElement('div');
        info.className = 'tg-dialog-info';
        const name = document.createElement('div');
        name.className = 'tg-dialog-name';
        name.textContent = d.title || '';
        info.appendChild(name);
        row.appendChild(info);
        row.addEventListener('click', () => {
          // Merge into dialogs if not present
          if (!dialogs.find((x) => String(x.id) === String(d.id))) dialogs.unshift(d);
          hideGlobalSearch();
          openChat(d.id);
        });
        section.appendChild(row);
      });
      resultsEl.appendChild(section);
    }

    if (msgRows.length) {
      const section = document.createElement('div');
      section.className = 'tg-search-section';
      const title = document.createElement('div');
      title.className = 'tg-search-section-title';
      title.textContent = 'Messages';
      section.appendChild(title);
      msgRows.forEach((r) => {
        const row = document.createElement('div');
        row.className = 'tg-search-msg-row';
        const peer = { id: r.peer_id, title: r.peer_title, type: r.peer_type, has_photo: r.has_photo };
        row.appendChild(buildAvatarEl(peer));
        const body = document.createElement('div');
        body.className = 'tg-smr-body';
        const titleEl = document.createElement('div');
        titleEl.className = 'tg-smr-title';
        titleEl.textContent = r.peer_title || '';
        const snippet = document.createElement('div');
        snippet.className = 'tg-smr-snippet';
        snippet.textContent = (r.message?.text || '').slice(0, 80);
        const dateEl = document.createElement('div');
        dateEl.className = 'tg-smr-date';
        dateEl.textContent = formatDialogTime(r.message?.date);
        body.appendChild(titleEl);
        body.appendChild(snippet);
        row.appendChild(body);
        row.appendChild(dateEl);
        row.addEventListener('click', () => {
          if (!dialogs.find((x) => String(x.id) === String(r.peer_id))) {
            dialogs.unshift({ id: r.peer_id, title: r.peer_title, type: r.peer_type, has_photo: r.has_photo });
          }
          hideGlobalSearch();
          const inp = q('[data-tg-search]');
          if (inp) inp.value = '';
          openChat(r.peer_id);
        });
        section.appendChild(row);
      });
      resultsEl.appendChild(section);
    }

    if (!chatRows.length && !msgRows.length) {
      resultsEl.innerHTML = '<div class="tg-empty-state">No results</div>';
    }
  } catch (err) {
    resultsEl.innerHTML = `<div class="tg-empty-state">Search error: ${esc(err.message)}</div>`;
  }
}

function hideGlobalSearch() {
  const resultsEl = q('[data-tg-search-results]');
  const listEl = q('[data-tg-dialogs-list]');
  if (resultsEl) resultsEl.hidden = true;
  if (listEl) listEl.hidden = false;
  refreshArchiveBar();
}

// ── Feature 5: Profile/info panel ───────────────────────────────────────────

async function openProfilePanel(peerId) {
  if (!peerId) return;
  const panel = q('[data-tg-profile-panel]');
  const backdrop = q('[data-tg-profile-backdrop]');
  if (!panel) return;
  panel.hidden = false;
  if (backdrop) backdrop.hidden = false;
  profilePanelOpen = true;

  // Reset
  const avatarEl = q('[data-tg-profile-avatar]');
  const nameEl = q('[data-tg-profile-name]');
  const presenceEl = q('[data-tg-profile-presence]');
  const sectionsEl = q('[data-tg-profile-sections]');
  const gridEl = q('[data-tg-profile-media-grid]');
  if (avatarEl) avatarEl.innerHTML = '…';
  if (nameEl) nameEl.textContent = '…';
  if (presenceEl) presenceEl.textContent = '';
  if (sectionsEl) sectionsEl.innerHTML = '';
  if (gridEl) gridEl.innerHTML = '';

  // Reset tab selection
  panel.querySelectorAll('.tg-profile-tab').forEach((t, i) => t.classList.toggle('active', i === 0));

  try {
    const profile = await api(`/profile/${encodeURIComponent(peerId)}`);

    if (avatarEl) {
      avatarEl.innerHTML = '';
      avatarEl.appendChild(buildAvatarEl({ id: profile.id, title: profile.title, has_photo: profile.has_photo }));
    }
    if (nameEl) nameEl.textContent = profile.title || '';
    if (presenceEl) presenceEl.textContent = profile.status_label || (profile.online ? 'online' : '');

    if (sectionsEl) {
      sectionsEl.innerHTML = '';
      const rows = [
        { label: 'Username',    value: profile.username ? '@' + profile.username : null },
        { label: 'Phone',       value: profile.phone },
        { label: 'Bio',         value: profile.bio },
        { label: 'Members',     value: profile.members_count ? String(profile.members_count) : null },
        { label: 'Common chats',value: profile.common_chats_count ? String(profile.common_chats_count) : null },
      ];
      rows.forEach(({ label, value }) => {
        if (!value) return;
        const section = document.createElement('div');
        section.className = 'tg-profile-section';
        const row = document.createElement('div');
        row.className = 'tg-profile-row';
        const lbl = document.createElement('div');
        lbl.className = 'tg-profile-row-label';
        lbl.textContent = label;
        const val = document.createElement('div');
        val.className = 'tg-profile-row-value';
        val.textContent = value;
        row.appendChild(lbl);
        row.appendChild(val);
        section.appendChild(row);
        sectionsEl.appendChild(section);
      });
    }

    // Load first tab (photos)
    loadProfileMedia(peerId, 'photo');
  } catch (err) {
    if (nameEl) nameEl.textContent = 'Failed to load profile';
    showToast('Profile error: ' + err.message, true);
  }
}

async function loadProfileMedia(peerId, kind) {
  if (!peerId) return;
  const gridEl = q('[data-tg-profile-media-grid]');
  if (!gridEl) return;
  gridEl.innerHTML = '<div class="tg-empty-state">Loading…</div>';
  try {
    const data = await api(`/shared-media/${encodeURIComponent(peerId)}?kind=${kind}&limit=30&before_id=0`);
    const items = data.items || [];
    gridEl.innerHTML = '';
    if (!items.length) {
      gridEl.innerHTML = '<div class="tg-empty-state">Nothing here</div>';
      return;
    }
    items.forEach((item) => {
      const el = document.createElement('div');
      el.className = 'tg-profile-media-item';
      if (kind === 'photo' && item.media?.url) {
        const img = document.createElement('img');
        img.src = item.media.url;
        img.loading = 'lazy';
        img.addEventListener('click', () => openLightbox({ id: item.message_id, media: item.media }));
        el.appendChild(img);
      } else if (kind === 'video' && item.media?.url) {
        const wrap = document.createElement('div');
        wrap.className = 'tg-media-video';
        const thumb = document.createElement('img');
        thumb.src = item.media.url;
        thumb.loading = 'lazy';
        const overlay = document.createElement('div');
        overlay.className = 'tg-play-overlay';
        overlay.textContent = '▶';
        wrap.appendChild(thumb);
        wrap.appendChild(overlay);
        wrap.addEventListener('click', () => openLightbox({ id: item.message_id, media: item.media }));
        el.appendChild(wrap);
      } else {
        el.textContent = item.text || item.media?.filename || kind;
        el.style.cssText = 'display:flex;align-items:center;justify-content:center;font-size:12px;padding:8px;word-break:break-all;';
        if (item.media?.url) {
          el.style.cursor = 'pointer';
          el.addEventListener('click', () => { window.open(item.media.url, '_blank', 'noopener'); });
        }
      }
      gridEl.appendChild(el);
    });
  } catch (err) {
    gridEl.innerHTML = `<div class="tg-empty-state">Error: ${esc(err.message)}</div>`;
  }
}

function closeProfilePanel() {
  const panel = q('[data-tg-profile-panel]');
  const backdrop = q('[data-tg-profile-backdrop]');
  if (panel) panel.hidden = true;
  if (backdrop) backdrop.hidden = true;
  profilePanelOpen = false;
}

// ── Feature 7: Archive ────────────────────────────────────────────────────────

async function refreshArchiveBar() {
  if (viewingArchive) return;
  const bar = q('[data-tg-archive-bar]');
  if (!bar) return;
  try {
    const data = await api('/dialogs?folder=1&limit=1');
    const archived = data.dialogs || [];
    const countEl = q('[data-tg-archive-count]');
    if (archived.length > 0) {
      if (countEl) countEl.textContent = '';
      bar.hidden = false;
    } else {
      bar.hidden = true;
    }
  } catch (_) {
    bar.hidden = true;
  }
}

async function openArchive() {
  viewingArchive = true;
  const list = q('[data-tg-dialogs-list]');
  const backEl = q('[data-tg-archive-back]');
  const barEl = q('[data-tg-archive-bar]');
  if (barEl) barEl.hidden = true;
  if (backEl) backEl.hidden = false;
  if (list) list.innerHTML = '<div class="tg-empty-state">Loading archived…</div>';
  try {
    const data = await api('/dialogs?folder=1&limit=50');
    const archivedDialogs = data.dialogs || [];
    if (list) {
      list.innerHTML = '';
      if (!archivedDialogs.length) {
        list.innerHTML = '<div class="tg-empty-state">No archived chats</div>';
        return;
      }
      archivedDialogs.forEach((dialog) => {
        const row = document.createElement('div');
        row.className = 'tg-dialog-row';
        row.dataset.dialogId = dialog.id;
        row.appendChild(buildAvatarEl(dialog));
        const info = document.createElement('div');
        info.className = 'tg-dialog-info';
        const name = document.createElement('div');
        name.className = 'tg-dialog-name';
        name.textContent = dialog.title || '';
        const preview = document.createElement('div');
        preview.className = 'tg-dialog-preview';
        preview.textContent = dialog.last_message || '';
        info.appendChild(name);
        info.appendChild(preview);
        row.appendChild(info);
        row.addEventListener('click', () => {
          if (!dialogs.find((d) => String(d.id) === String(dialog.id))) {
            dialogs.unshift(dialog);
          }
          closeArchive();
          openChat(dialog.id);
        });
        list.appendChild(row);
      });
    }
  } catch (err) {
    if (list) list.innerHTML = `<div class="tg-empty-state">Error: ${esc(err.message)}</div>`;
    showToast('Failed to load archive: ' + err.message, true);
  }
}

function closeArchive() {
  viewingArchive = false;
  const backEl = q('[data-tg-archive-back]');
  if (backEl) backEl.hidden = true;
  renderDialogs(q('[data-tg-search]')?.value?.trim().toLowerCase() || '');
}

// ── Feature 8: Create group/channel ─────────────────────────────────────────

function toggleNewMenu() {
  const menu = q('[data-tg-new-menu]');
  if (!menu) return;
  menu.hidden = !menu.hidden;
}

async function openCreateChatModal(kind) {
  const menu = q('[data-tg-new-menu]');
  if (menu) menu.hidden = true;

  let contacts = [];
  try {
    const data = await api('/contacts');
    contacts = data.contacts || [];
  } catch (_) {}

  const selectedIds = new Set();

  const body = document.createElement('div');

  // Title input
  const titleWrap = document.createElement('div');
  titleWrap.className = 'tg-field';
  const titleInput = document.createElement('input');
  titleInput.className = 'tg-field-input';
  titleInput.placeholder = kind === 'group' ? 'Group name' : 'Channel name';
  titleInput.type = 'text';
  titleWrap.appendChild(titleInput);
  body.appendChild(titleWrap);

  // About (channel only)
  let aboutInput = null;
  if (kind === 'channel') {
    const aboutWrap = document.createElement('div');
    aboutWrap.className = 'tg-field';
    aboutInput = document.createElement('textarea');
    aboutInput.className = 'tg-field-input';
    aboutInput.placeholder = 'Description (optional)';
    aboutInput.rows = 2;
    aboutWrap.appendChild(aboutInput);
    body.appendChild(aboutWrap);
  }

  // Selected chips area
  const chipsEl = document.createElement('div');
  chipsEl.className = 'tg-member-chips';
  body.appendChild(chipsEl);

  // Contact picker
  const pickerList = document.createElement('div');
  pickerList.className = 'tg-picker-list';

  function refreshChips() {
    chipsEl.innerHTML = '';
    selectedIds.forEach((id) => {
      const c = contacts.find((x) => String(x.id) === String(id));
      if (!c) return;
      const chip = document.createElement('span');
      chip.className = 'tg-member-chip';
      chip.textContent = c.name || '';
      chip.addEventListener('click', () => {
        selectedIds.delete(id);
        refreshChips();
        pickerList.querySelectorAll(`.tg-picker-row[data-uid="${CSS.escape(String(id))}"]`).forEach((r) => r.classList.remove('selected'));
      });
      chipsEl.appendChild(chip);
    });
  }

  contacts.forEach((c) => {
    const row = document.createElement('div');
    row.className = 'tg-picker-row';
    row.dataset.uid = c.id;
    row.appendChild(buildAvatarEl({ id: c.id, title: c.name, has_photo: c.has_photo }));
    const name = document.createElement('span');
    name.textContent = c.name || (c.username ? '@' + c.username : String(c.id));
    row.appendChild(name);
    row.addEventListener('click', () => {
      if (selectedIds.has(c.id)) {
        selectedIds.delete(c.id);
        row.classList.remove('selected');
      } else {
        selectedIds.add(c.id);
        row.classList.add('selected');
      }
      refreshChips();
    });
    pickerList.appendChild(row);
  });

  body.appendChild(pickerList);

  openModal(kind === 'group' ? 'New Group' : 'New Channel', body, [
    {
      label: 'Create',
      primary: true,
      action: async () => {
        const title = titleInput.value.trim();
        if (!title) { showToast('Enter a name', true); return; }
        closeModal();
        try {
          const payload = {
            kind,
            title,
            about: aboutInput?.value.trim() || '',
            user_ids: Array.from(selectedIds),
          };
          const res = await api('/chat/create', { method: 'POST', body: JSON.stringify(payload) });
          showToast(`${kind === 'group' ? 'Group' : 'Channel'} created`);
          await loadDialogs();
          if (res.peer_id) openChat(res.peer_id);
        } catch (err) {
          showToast('Failed to create: ' + err.message, true);
        }
      },
    },
    { label: 'Cancel', action: closeModal },
  ]);
}

// ── Feature 9: Drafts ────────────────────────────────────────────────────────

function saveDraftForPeer(peerId) {
  if (!peerId || draftJustSent) return;
  const textarea = q('[data-tg-textarea]');
  const text = textarea?.value || '';
  const d = dialogs.find((x) => String(x.id) === String(peerId));
  if (d) d.draft = text;
  api('/draft', { method: 'POST', body: JSON.stringify({ peer_id: peerId, text }) }).catch(() => {});
}

function scheduleDraftSave() {
  // Draft saving is handled by the textarea input listener in wireEvents.
  // Reset the sent flag so next keystroke schedules a save.
  draftJustSent = false;
}

export default { init, openPage, closePage };
