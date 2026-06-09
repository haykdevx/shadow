// Telegram — embeds the real Telegram Web K client (self-hosted) inside Shadow.
//
// The previous hand-built Telethon UI was replaced with the genuine Telegram Web
// client (github.com/morethanwords/tweb), built with our own API credentials and
// served same-origin at /static/tweb/. It talks straight to Telegram's servers
// over MTProto (WebSocket), so this is the original client with full features —
// not a re-implementation. The Shadow Telegram backend routes are no longer used
// by this view.

let root = null;
let iframe = null;

// Explicit index.html — Shadow's static mount doesn't auto-serve directory indexes.
const TWEB_SRC = '/static/tweb/index.html';

function ensureStyles() {
  if (document.getElementById('tg-embed-styles')) return;
  const style = document.createElement('style');
  style.id = 'tg-embed-styles';
  style.textContent = `
    #telegram-app {
      position: fixed;
      inset: 0 0 0 calc(var(--icon-rail-w, 48px) + var(--sidebar-w, 0px));
      z-index: 2450;
      display: flex;
      flex-direction: column;
      background: var(--bg, #0f0f0f);
    }
    #telegram-app[hidden] { display: none !important; }
    #telegram-app .tg-embed-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 6px 14px;
      background: var(--panel, #17212b);
      border-bottom: 1px solid var(--border, rgba(255,255,255,.08));
      flex-shrink: 0;
    }
    #telegram-app .tg-embed-title {
      font-size: 11px;
      letter-spacing: .12em;
      text-transform: uppercase;
      font-weight: 600;
      color: #3390ec;
    }
    #telegram-app .tg-embed-close {
      width: 30px; height: 30px;
      display: grid; place-items: center;
      border: 1px solid var(--border, rgba(255,255,255,.12));
      border-radius: 6px;
      background: transparent;
      color: var(--fg, #fff);
      cursor: pointer;
      font-size: 14px; line-height: 1;
    }
    #telegram-app .tg-embed-close:hover {
      background: var(--border, rgba(255,255,255,.12));
    }
    #telegram-app .tg-embed-frame {
      flex: 1;
      width: 100%;
      min-height: 0;
      border: 0;
      display: block;
    }
  `;
  document.head.appendChild(style);
}

function build() {
  const node = document.createElement('section');
  node.id = 'telegram-app';
  node.hidden = true;
  node.innerHTML = `
    <div class="tg-embed-bar">
      <span class="tg-embed-title">Telegram</span>
      <button class="tg-embed-close" data-tg-embed-close aria-label="Close Telegram">✕</button>
    </div>
    <iframe class="tg-embed-frame" title="Telegram"
      allow="clipboard-read; clipboard-write; microphone; camera; autoplay; fullscreen; web-share; notifications"
      allowfullscreen></iframe>
  `;
  iframe = node.querySelector('.tg-embed-frame');
  node.querySelector('[data-tg-embed-close]')
    .addEventListener('click', () => closePage());
  document.body.appendChild(node);
  return node;
}

function openPage(options = {}) {
  ensureStyles();
  root = root || document.getElementById('telegram-app') || build();
  if (!iframe) iframe = root.querySelector('.tg-embed-frame');
  // Lazy-load the client on first open so we don't connect to Telegram until asked.
  if (iframe && !iframe.getAttribute('src')) iframe.setAttribute('src', TWEB_SRC);
  root.hidden = false;
  document.body.classList.add('telegram-app-open');
  if (options.push !== false && window.location.pathname !== '/telegram') {
    window.history.pushState({}, '', '/telegram');
  }
}

function closePage() {
  if (root) root.hidden = true;
  document.body.classList.remove('telegram-app-open');
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

export default { init, openPage, closePage };
