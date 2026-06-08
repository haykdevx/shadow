// Account-scoped Telegram bot inbox. Mirrors the conversations handled by
// Shadow's Telegram gateway; it does not access a user's personal Telegram.

const API = '/api/shadow/telegram';
let root = null;
let timer = null;
let activeChat = null;
let chats = [];

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

async function request(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || payload.error || `HTTP ${response.status}`);
  return payload;
}

function ensureStyles() {
  if (document.getElementById('telegram-inbox-css')) return;
  const link = document.createElement('link');
  link.id = 'telegram-inbox-css';
  link.rel = 'stylesheet';
  link.href = '/static/telegram-inbox.css';
  document.head.appendChild(link);
}

function build() {
  const node = document.createElement('section');
  node.id = 'telegram-inbox';
  node.className = 'telegram-inbox';
  node.hidden = true;
  node.innerHTML = `
    <header class="tg-head">
      <div><span>SHADOW // TELEGRAM</span><h2>Messages</h2><p>One account, one paired bot identity, one private device boundary.</p></div>
      <div class="tg-head-actions">
        <button type="button" data-tg-action="pair">Pair account</button>
        <button type="button" data-tg-action="refresh">Refresh</button>
        <button type="button" class="tg-close" data-tg-action="close" aria-label="Close">x</button>
      </div>
    </header>
    <div class="tg-notice" data-tg-notice hidden></div>
    <div class="tg-layout">
      <aside class="tg-chats" data-tg-chats><div class="tg-empty">Loading chats...</div></aside>
      <main class="tg-thread">
        <div class="tg-thread-head" data-tg-thread-head>Choose a Telegram conversation</div>
        <div class="tg-messages" data-tg-messages><div class="tg-empty">Messages sent to your Shadow bot appear here.</div></div>
        <form class="tg-compose" data-tg-compose>
          <textarea rows="2" placeholder="Reply through your Shadow bot" disabled></textarea>
          <button type="submit" disabled>Send</button>
        </form>
      </main>
    </div>`;
  document.body.appendChild(node);
  node.addEventListener('click', handleClick);
  node.querySelector('[data-tg-compose]').addEventListener('submit', sendMessage);
  return node;
}

function notice(text = '', error = false) {
  const el = root?.querySelector('[data-tg-notice]');
  if (!el) return;
  el.hidden = !text;
  el.textContent = text;
  el.dataset.error = error ? '1' : '0';
}

function renderChats() {
  const wrap = root?.querySelector('[data-tg-chats]');
  if (!wrap) return;
  wrap.innerHTML = chats.length ? chats.map((chat) => `
    <button type="button" class="tg-chat ${Number(chat.chat_id) === Number(activeChat) ? 'active' : ''}" data-chat-id="${esc(chat.chat_id)}">
      <span class="tg-avatar">${esc((chat.title || 'T').slice(0, 1).toUpperCase())}</span>
      <span class="tg-chat-copy"><strong>${esc(chat.title || `Telegram ${chat.chat_id}`)}</strong><small>${esc(chat.last_message || 'No messages')}</small></span>
      ${chat.unread ? `<em>${esc(chat.unread)}</em>` : ''}
    </button>`).join('') : '<div class="tg-empty">No bot conversations yet. Pair Telegram, then send /start to the bot.</div>';
}

function renderMessages(rows) {
  const wrap = root?.querySelector('[data-tg-messages]');
  const chat = chats.find((row) => Number(row.chat_id) === Number(activeChat));
  root.querySelector('[data-tg-thread-head]').textContent = chat?.title || 'Telegram';
  if (!wrap) return;
  wrap.innerHTML = rows.length ? rows.map((row) => `
    <article class="tg-message ${row.direction === 'out' ? 'out' : 'in'}">
      <div>${esc(row.text)}</div>
      <time>${esc(new Date((row.created_at || 0) * 1000).toLocaleString())}</time>
    </article>`).join('') : '<div class="tg-empty">No messages in this conversation.</div>';
  wrap.scrollTop = wrap.scrollHeight;
  const textarea = root.querySelector('[data-tg-compose] textarea');
  const button = root.querySelector('[data-tg-compose] button');
  textarea.disabled = !activeChat;
  button.disabled = !activeChat;
}

async function loadStatus() {
  const status = await request('/status');
  if (!status.configured) notice('The Telegram bot token is not configured on this Shadow server.', true);
  else if (!status.linked) notice('Pair your Telegram identity to this Shadow account. It will control only this account’s enrolled PCs.');
  else notice('');
  return status;
}

async function loadChats({ preserve = true } = {}) {
  try {
    await loadStatus();
    const payload = await request('/chats');
    chats = payload.chats || [];
    if (!preserve || !chats.some((row) => Number(row.chat_id) === Number(activeChat))) {
      activeChat = chats[0]?.chat_id || null;
    }
    renderChats();
    if (activeChat) await loadMessages(activeChat);
  } catch (error) {
    notice(error.message, true);
  }
}

async function loadMessages(chatId) {
  activeChat = Number(chatId);
  renderChats();
  try {
    const payload = await request(`/chats/${encodeURIComponent(activeChat)}/messages`);
    renderMessages(payload.messages || []);
    await request(`/chats/${encodeURIComponent(activeChat)}/read`, { method: 'POST' });
  } catch (error) {
    notice(error.message, true);
  }
}

async function pair() {
  try {
    const data = await request('/pair-code', { method: 'POST' });
    notice(`Send /pair ${data.code} to your Shadow bot. This single-use code expires in 10 minutes.`);
  } catch (error) {
    notice(error.message, true);
  }
}

async function sendMessage(event) {
  event.preventDefault();
  if (!activeChat) return;
  const textarea = root.querySelector('[data-tg-compose] textarea');
  const text = textarea.value.trim();
  if (!text) return;
  textarea.disabled = true;
  try {
    await request(`/chats/${encodeURIComponent(activeChat)}/send`, {
      method: 'POST',
      body: JSON.stringify({ text }),
    });
    textarea.value = '';
    await loadChats();
  } catch (error) {
    notice(error.message, true);
  } finally {
    textarea.disabled = false;
    textarea.focus();
  }
}

function handleClick(event) {
  const target = event.target.closest('button');
  if (!target) return;
  if (target.dataset.chatId) return loadMessages(target.dataset.chatId);
  if (target.dataset.tgAction === 'pair') return pair();
  if (target.dataset.tgAction === 'refresh') return loadChats();
  if (target.dataset.tgAction === 'close') return closePage();
}

function start() {
  stop();
  timer = setInterval(() => {
    if (!document.hidden && root && !root.hidden) loadChats();
  }, 4000);
}

function stop() {
  if (timer) clearInterval(timer);
  timer = null;
}

function openPage(options = {}) {
  ensureStyles();
  root = root || document.getElementById('telegram-inbox') || build();
  root.hidden = false;
  document.body.classList.add('telegram-inbox-open');
  if (options.push !== false && window.location.pathname !== '/telegram') {
    window.history.pushState({}, '', '/telegram');
  }
  loadChats({ preserve: false });
  start();
}

function closePage() {
  if (root) root.hidden = true;
  document.body.classList.remove('telegram-inbox-open');
  stop();
  if (window.location.pathname === '/telegram') window.history.pushState({}, '', '/');
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
