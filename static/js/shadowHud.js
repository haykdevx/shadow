// Shadow HUD: lightweight home-PC link status and explicit approvals.

const API_BASE = '/api/shadow';
let root = null;
let statusEl = null;
let detailEl = null;
let pendingEl = null;
let previewEl = null;
let noteEl = null;

function text(value) {
  return value === undefined || value === null ? '' : String(value);
}

function bytes(value) {
  if (!Number.isFinite(value) || value <= 0) return 'n/a';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size.toFixed(unit < 2 ? 0 : 1)} ${units[unit]}`;
}

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || payload.error || `HTTP ${response.status}`);
  return payload;
}

function button(label, className, handler) {
  const el = document.createElement('button');
  el.type = 'button';
  el.className = className;
  el.textContent = label;
  el.addEventListener('click', handler);
  return el;
}

function renderPending(rows = []) {
  if (!pendingEl) return;
  pendingEl.replaceChildren();
  if (!rows.length) return;
  const title = document.createElement('div');
  title.className = 'shadow-hud-pending-title';
  title.textContent = 'Approval required';
  pendingEl.appendChild(title);
  rows.forEach((item) => {
    const row = document.createElement('div');
    row.className = 'shadow-hud-pending-row';
    const label = document.createElement('span');
    label.textContent = text(item.action).replaceAll('_', ' ');
    const controls = document.createElement('span');
    controls.className = 'shadow-hud-pending-actions';
    controls.append(
      button('Approve', 'shadow-mini-btn approve', async () => {
        await request(`/pc/confirm/${encodeURIComponent(item.id)}`, { method: 'POST' });
        await refresh();
      }),
      button('Cancel', 'shadow-mini-btn', async () => {
        await request(`/pc/pending/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
        await refresh();
      }),
    );
    row.append(label, controls);
    pendingEl.appendChild(row);
  });
}

function renderOverview(data) {
  if (!statusEl || !detailEl) return;
  statusEl.classList.toggle('online', Boolean(data.online));
  statusEl.textContent = data.online ? 'linked' : (data.configured ? 'offline' : 'not configured');
  ['shadow-hud-screen', 'shadow-hud-lock'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.disabled = !data.online;
  });
  if (noteEl) {
    noteEl.textContent = data.online
      ? ''
      : (data.error || (data.configured
        ? 'Home companion is offline. Check the host service.'
        : 'Home link is not configured. Add the private companion URL and token.'));
    noteEl.hidden = Boolean(data.online);
  }
  const pc = data.pc || {};
  const mem = pc.memory || {};
  const disk = pc.disk || {};
  detailEl.replaceChildren();
  const cells = [
    ['host', pc.hostname || 'home PC'],
    ['memory', mem.total ? `${bytes(mem.used)} / ${bytes(mem.total)}` : 'n/a'],
    ['disk', disk.total ? `${bytes(disk.free)} free` : 'n/a'],
    ['load', Array.isArray(pc.load) && pc.load.length ? Number(pc.load[0]).toFixed(2) : 'n/a'],
  ];
  cells.forEach(([label, value]) => {
    const cell = document.createElement('div');
    cell.className = 'shadow-hud-cell';
    const key = document.createElement('span');
    key.textContent = label;
    const val = document.createElement('strong');
    val.textContent = value;
    cell.append(key, val);
    detailEl.appendChild(cell);
  });
  renderPending(data.pending || []);
}

async function refresh() {
  if (!root) return;
  try {
    const data = await request('/overview');
    renderOverview(data);
    root.classList.remove('hud-error');
    root.title = data.error || '';
  } catch (error) {
    root.classList.add('hud-error');
    if (statusEl) statusEl.textContent = 'unavailable';
    root.title = error.message;
  }
}

async function invoke(action, args = {}) {
  try {
    const result = await request('/pc/action', {
      method: 'POST',
      body: JSON.stringify({ action, args }),
    });
    if (action === 'screenshot' && result.image_b64 && previewEl) {
      previewEl.src = `data:${result.mime || 'image/png'};base64,${result.image_b64}`;
      previewEl.hidden = false;
    }
    await refresh();
  } catch (error) {
    if (root) root.title = error.message;
    if (statusEl) statusEl.textContent = 'action failed';
  }
}

function init() {
  root = document.getElementById('shadow-hud');
  if (!root || root.dataset.ready === '1') return;
  root.dataset.ready = '1';
  statusEl = document.getElementById('shadow-hud-status');
  detailEl = document.getElementById('shadow-hud-detail');
  pendingEl = document.getElementById('shadow-hud-pending');
  previewEl = document.getElementById('shadow-hud-preview');
  noteEl = document.getElementById('shadow-hud-note');
  document.getElementById('shadow-hud-refresh')?.addEventListener('click', refresh);
  document.getElementById('shadow-hud-screen')?.addEventListener('click', () => invoke('screenshot'));
  document.getElementById('shadow-hud-lock')?.addEventListener('click', () => invoke('lock'));
  refresh();
}

export default { init, refresh };

