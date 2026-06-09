// Dedicated Shadow Command dashboard for private home-PC control.

const API_ROOT = '/api/shadow';
const REFRESH_MS = 10000;
let root = null;
let refreshTimer = null;
let enrollmentTimer = null;
let currentPath = '';
let lastReadFile = null;
let selectedDeviceId = '';
let latest = {
  devices: null,
  overview: null,
  processes: null,
  windows: null,
  clipboard: null,
  files: null,
  screen: null,
  timeline: null,
  runbooks: null,
  automations: null,
  inspector: null,
};
let generatedPanels = [];

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}

function bytes(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) return 'n/a';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let size = n;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit < 2 ? 0 : 1)} ${units[unit]}`;
}

function pct(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 'n/a';
  return `${Math.max(0, Math.min(100, n)).toFixed(1)}%`;
}

function uptime(seconds) {
  const n = Number(seconds);
  if (!Number.isFinite(n) || n <= 0) return 'n/a';
  const days = Math.floor(n / 86400);
  const hours = Math.floor((n % 86400) / 3600);
  const mins = Math.floor((n % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${mins}m`;
  return `${mins}m`;
}

function toast(message, isError = false) {
  const ui = window.uiModule;
  if (ui?.showToast) ui.showToast(message);
  else if (isError) console.error(message);
  else console.log(message);
}

function ensureStyles() {
  if (document.getElementById('command-page-css')) return;
  const link = document.createElement('link');
  link.id = 'command-page-css';
  link.rel = 'stylesheet';
  link.href = '/static/command-page.css';
  document.head.appendChild(link);
}

async function request(path, options = {}) {
  const response = await fetch(`${API_ROOT}${path}`, {
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function panelBody(id) {
  return root?.querySelector(`[data-panel-body="${id}"]`) || null;
}

function panelNote(id) {
  return root?.querySelector(`[data-panel-note="${id}"]`) || null;
}

function setNote(id, message, kind = '') {
  const el = panelNote(id);
  if (!el) return;
  el.textContent = message || '';
  el.dataset.kind = kind;
  el.hidden = !message;
}

function setBody(id, html) {
  const el = panelBody(id);
  if (el) el.innerHTML = html;
}

function actionLabel(action) {
  return String(action || '').replaceAll('_', ' ');
}

function navTo(path) {
  if (!path || window.location.pathname === path) return;
  try {
    window.history.pushState({}, '', path);
  } catch (_) {}
}

function buildShell() {
  const node = document.createElement('section');
  node.id = 'command-page';
  node.className = 'command-page';
  node.hidden = true;
  node.innerHTML = `
    <div class="command-page-inner">
      <header class="command-top">
        <div>
          <div class="command-kicker">SHADOW // COMMAND</div>
          <h2>PC Control</h2>
          <p id="command-summary">Private home link dashboard. Waiting for host telemetry.</p>
        </div>
        <div class="command-top-actions">
          <button type="button" class="command-btn" data-command-action="ai-panel">+ Add panel (AI)</button>
          <button type="button" class="command-btn" data-command-action="refresh">Refresh all</button>
          <button type="button" class="command-btn danger" data-command-action="lock">Lock PC</button>
          <button type="button" class="command-close" data-command-action="close" aria-label="Close Command">✕</button>
        </div>
      </header>

      <div class="command-grid">
        ${panel('access', 'Your Devices', 'private to this Shadow account', { wide: true, open: true })}
        ${panel('vitals', 'Host / Vitals', 'live telemetry', { open: true })}
        ${panel('screen', 'Screen', 'live-ish capture')}
        ${panel('processes', 'Processes', 'top CPU/RAM')}
        ${panel('windows', 'Apps / Windows', 'focus, close, launch')}
        ${panel('files', 'Files', 'allowed roots only')}
        ${panel('terminal', 'Terminal', 'approval-gated shell')}
        ${panel('system', 'Media / System', 'clipboard, media, power')}
        ${panel('quick', 'Quick Actions', 'navigation and common actions')}
        ${panel('runbooks', 'Runbooks', 'approval-gated routines')}
        ${panel('automations', 'Automations', 'safe triggers, gated actions')}
        ${panel('timeline', 'Timeline / Watchdog', 'audit trail and health')}
        <section class="command-panel command-panel-wide" id="command-ai-panel" hidden>
          <div class="command-panel-head">
            <div>
              <div class="command-panel-title">Generated Panels</div>
              <div class="command-panel-sub">safe specs only, no arbitrary code</div>
            </div>
          </div>
          <div class="command-panel-note" data-panel-note="ai" hidden></div>
          <div class="command-panel-body generated-grid" data-panel-body="ai"></div>
        </section>
      </div>
    </div>`;
  document.body.appendChild(node);
  return node;
}

function panel(id, title, sub, opts = {}) {
  // Back-compat: a bare `true` used to mean "wide".
  const { wide = false, open = false } = (opts === true ? { wide: true } : opts) || {};
  return `
    <section class="command-panel${wide ? ' command-panel-wide' : ''}${open ? '' : ' collapsed'}" data-command-panel="${esc(id)}">
      <button type="button" class="command-panel-head" data-panel-toggle="${esc(id)}" aria-expanded="${open ? 'true' : 'false'}">
        <div>
          <div class="command-panel-title">${esc(title)}</div>
          <div class="command-panel-sub">${esc(sub)}</div>
        </div>
        <span class="command-panel-chevron" aria-hidden="true">▾</span>
        <span class="command-panel-dot" data-panel-state="${esc(id)}"></span>
      </button>
      <div class="command-panel-note" data-panel-note="${esc(id)}" hidden></div>
      <div class="command-panel-body" data-panel-body="${esc(id)}">
        <div class="command-loading">Loading...</div>
      </div>
    </section>`;
}

function setState(id, online) {
  const el = root?.querySelector(`[data-panel-state="${id}"]`);
  if (el) el.dataset.online = online ? '1' : '0';
}

function setDeviceLocked(locked) {
  root?.querySelectorAll('[data-command-panel]').forEach((node) => {
    if (node.dataset.commandPanel !== 'access') node.hidden = locked;
  });
  root?.querySelectorAll('.command-top-actions [data-command-action]').forEach((node) => {
    if (!['close', 'add-device'].includes(node.dataset.commandAction)) node.hidden = locked;
  });
}

function platformLabel(value = '') {
  const text = String(value).toLowerCase();
  if (text.includes('windows')) return 'Windows';
  if (text.includes('darwin') || text.includes('mac')) return 'macOS';
  if (text.includes('linux')) return 'Linux';
  return value || 'Unknown OS';
}

function renderDevices(data) {
  latest.devices = data;
  const devices = Array.isArray(data?.devices) ? data.devices : [];
  const selected = devices.find((row) => row.selected) || devices[0] || null;
  selectedDeviceId = selected?.id || '';
  setDeviceLocked(!selected);

  const rows = devices.map((row) => `
    <div class="command-device-row ${row.id === selectedDeviceId ? 'selected' : ''}">
      <button type="button" class="command-device-select" data-device-select="${esc(row.id)}">
        <span class="command-device-status" data-online="${row.online ? '1' : '0'}"></span>
        <span><strong>${esc(row.name)}</strong><small>${esc(platformLabel(row.platform))} · ${row.online ? 'online' : 'offline'}${row.legacy ? ' · private migrated link' : ''}</small></span>
      </button>
      ${row.legacy ? '' : `<button type="button" class="command-mini danger" data-device-remove="${esc(row.id)}">Remove</button>`}
    </div>`).join('');

  setBody('access', `
    <div class="command-device-head">
      <div>
        <strong>${selected ? `${esc(selected.name)} is selected` : 'Connect your first PC'}</strong>
        <small>${selected ? 'Only this Shadow account can see or control these devices.' : 'Each user enrolls their own machine. No access request is sent to another account.'}</small>
      </div>
      <div class="command-inline-actions">
        <button type="button" class="command-btn approve" data-device-enroll>Create setup code</button>
        <button type="button" class="command-btn" data-access-action="telegram">Pair Telegram</button>
        ${data?.telegram_linked ? '<button type="button" class="command-btn danger" data-access-action="telegram-unlink">Unlink Telegram</button>' : ''}
      </div>
    </div>
    <div class="command-device-list">${rows || '<div class="command-empty">No device is linked to this account yet.</div>'}</div>
    <div class="command-enrollment" data-device-enrollment ${selected ? 'hidden' : ''}>
      <div class="command-enrollment-copy">
        <strong>Three-step setup</strong>
        <small>1. Create a code. 2. Pick your OS. 3. Run the one-line command on your PC. The agent connects outbound over HTTPS and starts on login.</small>
      </div>
      <div class="command-os-tabs">
        <button type="button" class="command-chip static">Linux</button>
        <button type="button" class="command-chip static">macOS</button>
        <button type="button" class="command-chip static">Windows</button>
      </div>
      <div class="command-empty">Create a setup code to generate commands for this Shadow server.</div>
    </div>
    <div class="command-pair-code" data-command-pair-code hidden></div>`);
  setState('access', true);
  return Boolean(selected);
}

async function refreshDevices() {
  try {
    const [devices, telegram] = await Promise.all([
      request('/devices'),
      request('/telegram/status').catch(() => ({})),
    ]);
    devices.telegram_linked = Boolean(telegram.linked);
    setNote('access', '');
    return renderDevices(devices);
  } catch (error) {
    setDeviceLocked(true);
    setBody('access', '<div class="command-empty">Device state unavailable.</div>');
    setNote('access', error.message, 'error');
    setState('access', false);
    return false;
  }
}

async function createDeviceEnrollment() {
  try {
    const data = await request('/devices/enrollment', { method: 'POST' });
    const node = root.querySelector('[data-device-enrollment]');
    if (!node) return;
    node.hidden = false;
    const commandRows = [
      ['Linux', data.commands?.linux],
      ['macOS', data.commands?.macos],
      ['Windows PowerShell', data.commands?.windows],
    ].map(([label, command]) => `
      <div class="command-install-row">
        <div><strong>${esc(label)}</strong><small>Code expires ${esc(new Date(data.expires_at * 1000).toLocaleTimeString())}</small></div>
        <code>${esc(command || '')}</code>
        <button type="button" class="command-mini" data-copy-command="${esc(command || '')}">Copy</button>
      </div>`).join('');
    node.innerHTML = `<div class="command-enrollment-copy"><strong>Setup code ${esc(data.code)}</strong><small>Run one command on the PC you want this account to own. The code is single-use.</small></div>${commandRows}`;
    toast('Device setup code created');
    if (enrollmentTimer) clearInterval(enrollmentTimer);
    enrollmentTimer = setInterval(async () => {
      try {
        const devices = await request('/devices');
        if ((devices.devices || []).length) {
          clearInterval(enrollmentTimer);
          enrollmentTimer = null;
          await bootstrapPage();
          toast('Device connected');
        }
      } catch (_) {}
    }, 3000);
  } catch (error) {
    toast(`Device setup failed: ${error.message}`, true);
  }
}

async function selectDevice(id) {
  try {
    await request(`/devices/${encodeURIComponent(id)}/select`, { method: 'POST' });
    selectedDeviceId = id;
    latest.screen = null;
    currentPath = '';
    await bootstrapPage();
  } catch (error) {
    toast(`Device selection failed: ${error.message}`, true);
  }
}

async function removeDevice(id) {
  if (!window.confirm('Remove this device from your Shadow account?')) return;
  try {
    await request(`/devices/${encodeURIComponent(id)}`, { method: 'DELETE' });
    toast('Device removed');
    await bootstrapPage();
  } catch (error) {
    toast(`Remove failed: ${error.message}`, true);
  }
}

async function createTelegramPairCode() {
  try {
    const data = await request('/telegram/pair-code', { method: 'POST' });
    const node = root.querySelector('[data-command-pair-code]');
    if (node) {
      node.hidden = false;
      node.innerHTML = `<strong>/pair ${esc(data.code)}</strong><small>Send this to your Shadow Telegram bot within 10 minutes. Telegram will control only devices owned by this Shadow account.</small>`;
    }
  } catch (error) {
    toast(`Telegram pairing failed: ${error.message}`, true);
  }
}

function renderPending(rows = []) {
  const body = panelBody('quick');
  if (!body) return;
  const pending = rows.length ? `
    <div class="command-pending">
      <div class="command-pending-title">Approval required</div>
      ${rows.map((item) => `
        <div class="command-pending-row">
          <div>
            <strong>${esc(actionLabel(item.action))} <em class="command-risk" data-risk="${esc(item.risk || 'high')}">${esc(item.risk || 'high')}</em></strong>
            <small>${esc(new Date((item.created_at || 0) * 1000).toLocaleTimeString())}</small>
          </div>
          <div class="command-inline-actions">
            <button type="button" class="command-btn approve" data-pending-approve="${esc(item.id)}">Approve</button>
            <button type="button" class="command-btn" data-pending-cancel="${esc(item.id)}">Cancel</button>
          </div>
        </div>`).join('')}
    </div>` : '<div class="command-empty">No pending approvals.</div>';
  body.innerHTML = `
    <div class="command-button-grid">
      <button type="button" class="command-btn" data-command-action="refresh">Refresh all</button>
      <button type="button" class="command-btn" data-command-action="screen">Capture screen</button>
      <button type="button" class="command-btn danger" data-command-action="lock">Lock PC</button>
      <button type="button" class="command-btn" data-command-action="tasks">Tasks</button>
      <button type="button" class="command-btn" data-command-action="memory">Brain</button>
    </div>
    ${pending}`;
}

function renderVitals(data) {
  const pc = data?.pc || {};
  const mem = pc.memory || {};
  const disk = pc.disk || {};
  const cpu = pc.cpu || {};
  const net = pc.network || {};
  const gpu = Array.isArray(pc.gpu) ? pc.gpu : [];
  document.getElementById('command-summary').textContent = data?.online
    ? `${pc.hostname || 'home PC'} online. ${pc.platform || ''}`
    : (data?.configured ? `Home link offline: ${data.error || 'check companion service'}` : 'Home link is not configured.');
  setBody('vitals', `
    <div class="command-vitals">
      ${metric('Host', pc.hostname || 'home PC', pc.platform || 'n/a')}
      ${metric('Uptime', uptime(pc.uptime_seconds), pc.os?.release || '')}
      ${metric('CPU', pct(cpu.percent), `${cpu.count || 'n/a'} cores`)}
      ${metric('RAM', mem.total ? `${bytes(mem.used)} / ${bytes(mem.total)}` : 'n/a', mem.total ? bar((mem.used / mem.total) * 100) : '')}
      ${metric('Disk', disk.total ? `${bytes(disk.free)} free` : 'n/a', disk.total ? bar((disk.used / disk.total) * 100) : '')}
      ${metric('Load', Array.isArray(pc.load) ? pc.load.map((n) => Number(n).toFixed(2)).join(' / ') : 'n/a', '')}
      ${metric('Network', `${bytes(net.rx_bytes)} down`, `${bytes(net.tx_bytes)} up`)}
      ${gpu.length ? gpu.map((g) => metric('GPU', `${esc(g.name || 'GPU')} ${pct(g.util_percent)}`, `${g.temp_c ?? 'n/a'}C / ${g.memory_used_mib ?? 'n/a'} MiB`)).join('') : metric('GPU', 'n/a', 'nvidia-smi not available')}
    </div>`);
  setState('vitals', Boolean(data?.online));
  renderPending(data?.pending || []);
}

function metric(label, value, detail) {
  const detailText = String(detail || "");
  const detailHtml = detailText.startsWith('<i class="command-bar"') ? detailText : esc(detailText);
  return `
    <div class="command-metric">
      <span>${esc(label)}</span>
      <strong>${esc(value)}</strong>
      <small>${detailHtml}</small>
    </div>`;
}


function bar(value) {
  const v = Math.max(0, Math.min(100, Number(value) || 0));
  return `<i class="command-bar"><b style="width:${v}%"></b></i>`;
}

function parseProcesses(payload) {
  const lines = Array.isArray(payload?.processes) ? payload.processes.slice(1) : [];
  return lines.map((line) => {
    const parts = String(line).trim().split(/\s+/);
    return {
      pid: parts[0],
      name: parts[1] || '',
      cpu: parts[2] || '0',
      mem: parts[3] || '0',
    };
  }).filter((p) => p.pid);
}

function renderProcesses(payload) {
  latest.processes = payload;
  const rows = parseProcesses(payload);
  setBody('processes', rows.length ? `
    <div class="command-table">
      <div class="command-table-head"><span>PID</span><span>Name</span><span>CPU</span><span>RAM</span><span></span></div>
      ${rows.map((row) => `
        <div class="command-table-row">
          <span>${esc(row.pid)}</span>
          <span title="${esc(row.name)}">${esc(row.name)}</span>
          <span>${esc(row.cpu)}%</span>
          <span>${esc(row.mem)}%</span>
          <span><button type="button" class="command-mini danger" data-kill-pid="${esc(row.pid)}">Kill</button></span>
        </div>`).join('')}
    </div>` : '<div class="command-empty">No process data.</div>');
  setState('processes', true);
}

function renderWindows(payload) {
  latest.windows = payload;
  const rows = Array.isArray(payload?.windows) ? payload.windows : [];
  setBody('windows', `
    <div class="command-form compact">
      <input id="command-app-name" placeholder="Allowed app name (from SHADOW_ALLOWED_APPS)">
      <button type="button" class="command-btn" data-command-action="launch-app">Launch</button>
    </div>
    ${rows.length ? `<div class="command-window-list">${rows.map((row) => `
      <div class="command-window-row">
        <div>
          <strong>${esc(row.title || row.class || row.id)}</strong>
          <small>${esc(row.class || '')} ${esc(row.pid ? `pid ${row.pid}` : '')}</small>
        </div>
        <div class="command-inline-actions">
          <button type="button" class="command-mini" data-window-focus="${esc(row.id || '')}" data-window-title="${esc(row.title || '')}">Focus</button>
          <button type="button" class="command-mini danger" data-window-close="${esc(row.id || '')}" data-window-title="${esc(row.title || '')}">Close</button>
        </div>
      </div>`).join('')}</div>` : '<div class="command-empty">No window list. Install wmctrl on the home PC.</div>'}`);
  setState('windows', true);
}

function renderFiles(payload) {
  latest.files = payload;
  const entries = Array.isArray(payload?.entries) ? payload.entries : [];
  currentPath = payload?.path || currentPath || (payload?.roots || [])[0] || '';
  setBody('files', `
    <div class="command-form">
      <input id="command-file-path" value="${esc(currentPath)}" placeholder="Allowed path">
      <button type="button" class="command-btn" data-command-action="file-list">Open</button>
      <button type="button" class="command-btn" data-command-action="file-up">Up</button>
    </div>
    <div class="command-form compact">
      <input id="command-file-query" placeholder="Search names in current path">
      <button type="button" class="command-btn" data-command-action="file-search">Search</button>
      <label class="command-btn file-upload">Upload<input type="file" id="command-file-upload" hidden></label>
      <button type="button" class="command-btn" data-command-action="file-download" ${lastReadFile?.text ? '' : 'disabled'}>Download preview</button>
    </div>
    <div class="command-roots">${(payload?.roots || []).map((rootPath) => `<button type="button" class="command-chip" data-root-path="${esc(rootPath)}">${esc(rootPath)}</button>`).join('')}</div>
    <div class="command-files">
      ${entries.map((entry) => `
        <button type="button" class="command-file ${entry.type === 'dir' ? 'dir' : ''}" data-file-path="${esc(entry.path)}" data-file-type="${esc(entry.type || '')}">
          <span>${esc(entry.name || entry.path)}</span>
          <small>${entry.type === 'dir' ? 'dir' : bytes(entry.size)}</small>
        </button>`).join('') || '<div class="command-empty">No entries.</div>'}
    </div>
    <pre class="command-preview" id="command-file-preview">${lastReadFile ? esc(lastReadFile.text || `[binary] ${lastReadFile.name || lastReadFile.path}`) : 'Select a file to preview.'}</pre>`);
  setState('files', true);
}

function renderSearchResults(payload) {
  renderFiles({
    path: payload.path,
    roots: latest.files?.roots || [],
    entries: payload.results || [],
  });
  setNote('files', payload.truncated ? 'Search stopped after the safety limit. Narrow the path or query.' : `Search results for "${payload.query}".`);
}

function renderScreen(payload) {
  latest.screen = payload || latest.screen || null;
  const body = panelBody('screen');
  if (!body) return;
  const inspector = latest.inspector?.analysis || 'Capture or inspect the screen manually. No automatic screenshot loop runs here.';
  const controls = `
    <div class="command-form compact">
      <button type="button" class="command-btn" data-command-action="screen">Capture now</button>
      <input id="command-screen-prompt" placeholder="Ask about the current screen" value="Describe the current screen and risks.">
      <button type="button" class="command-btn" data-command-action="screen-inspect">Inspect</button>
    </div>
    <pre class="command-terminal-output command-inspector-output" id="command-screen-analysis">${esc(inspector)}</pre>`;
  if (!payload?.image_b64) {
    body.innerHTML = `<div class="command-empty">No screen capture yet.</div>${controls}`;
    return;
  }
  const width = payload.width || '';
  const height = payload.height || '';
  body.innerHTML = `
    <button type="button" class="command-screen-wrap" data-command-action="screen-open" title="Click to enlarge. Shift-click sends a confirmed mouse click to the PC.">
      <img src="data:${esc(payload.mime || 'image/png')};base64,${payload.image_b64}" alt="Home PC screen" data-screen-width="${esc(width)}" data-screen-height="${esc(height)}">
    </button>
    <div class="command-muted">${new Date().toLocaleTimeString()}${width && height ? ` - ${width}x${height}` : ''} - Shift-click to send mouse click</div>
    ${controls}`;
  setState('screen', true);
}

function screenClickPayload(event) {
  const img = root?.querySelector('.command-screen-wrap img');
  if (!img) return null;
  const rect = img.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  const width = Number(img.dataset.screenWidth || img.naturalWidth || latest.screen?.width);
  const height = Number(img.dataset.screenHeight || img.naturalHeight || latest.screen?.height);
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return null;
  const x = Math.round(Math.max(0, Math.min(width - 1, (event.clientX - rect.left) * (width / rect.width))));
  const y = Math.round(Math.max(0, Math.min(height - 1, (event.clientY - rect.top) * (height / rect.height))));
  return { x, y, button: 1 };
}

function renderTerminalOutput(result) {
  const out = result?.result || result || {};
  const text = [
    out.stdout ? `$ stdout\n${out.stdout}` : '',
    out.stderr ? `$ stderr\n${out.stderr}` : '',
    Number.isFinite(out.returncode) ? `exit ${out.returncode}` : '',
  ].filter(Boolean).join('\n\n') || JSON.stringify(out, null, 2);
  const box = root?.querySelector('#command-terminal-output');
  if (box) box.textContent = text;
}

function renderTerminal() {
  setBody('terminal', `
    <div class="command-form">
      <input id="command-shell-cwd" placeholder="cwd inside allowed roots" value="${esc(currentPath || '')}">
      <input id="command-shell-input" placeholder="Command. Approval is required before execution.">
      <button type="button" class="command-btn danger" data-command-action="shell">Run</button>
    </div>
    <pre class="command-terminal-output" id="command-terminal-output">Shell output will appear here after approval.</pre>`);
  setState('terminal', true);
}

function renderSystem() {
  const text = latest.clipboard?.text || '';
  setBody('system', `
    <div class="command-button-grid">
      <button type="button" class="command-btn" data-media="play-pause">Play/Pause</button>
      <button type="button" class="command-btn" data-media="next">Next</button>
      <button type="button" class="command-btn" data-media="previous">Previous</button>
      <button type="button" class="command-btn danger" data-command-action="sleep">Sleep</button>
    </div>
    <div class="command-form compact">
      <input id="command-volume" type="number" min="0" max="150" value="50">
      <button type="button" class="command-btn" data-command-action="volume">Set volume %</button>
      <button type="button" class="command-btn danger" data-command-action="shutdown">Shutdown</button>
    </div>
    <div class="command-form compact">
      <input id="command-type-text" placeholder="Type text into focused PC window">
      <button type="button" class="command-btn" data-command-action="type-text">Type</button>
      <input id="command-keypress" placeholder="Key combo, e.g. ctrl+l">
      <button type="button" class="command-btn" data-command-action="keypress">Key</button>
    </div>
    <textarea id="command-clipboard-text" class="command-textarea" placeholder="Clipboard text">${esc(text)}</textarea>
    <div class="command-inline-actions">
      <button type="button" class="command-btn" data-command-action="clipboard-get">Read clipboard</button>
      <button type="button" class="command-btn" data-command-action="clipboard-set">Set clipboard</button>
    </div>`);
  setState('system', true);
}

function runbookExample() {
  return JSON.stringify([
    { action: 'status', args: {} },
    { action: 'processes', args: { limit: 8 } },
    { action: 'screenshot', args: {} },
  ], null, 2);
}

function riskBadge(risk) {
  return `<em class="command-risk" data-risk="${esc(risk || 'high')}">${esc(risk || 'high')}</em>`;
}

function renderRunbooks(payload) {
  const rows = Array.isArray(payload?.runbooks) ? payload.runbooks : [];
  latest.runbooks = rows;
  const list = rows.length ? `
    <div class="command-runbooks">
      ${rows.map((row) => `
        <div class="command-runbook">
          <div>
            <strong>${esc(row.label || row.name)} ${riskBadge(row.risk)} <em class="command-kind">${row.builtin ? 'built-in' : 'custom'}</em></strong>
            <small>${esc(row.description || '')}</small>
            <span>${(row.steps || []).map(actionLabel).map(esc).join(' -> ')}</span>
          </div>
          <div class="command-inline-actions">
            <button type="button" class="command-btn danger" data-runbook="${esc(row.name)}">Run</button>
            ${row.builtin ? '' : `<button type="button" class="command-btn" data-runbook-delete="${esc(row.name)}">Delete</button>`}
          </div>
        </div>`).join('')}
    </div>` : '<div class="command-empty">No runbooks configured.</div>';
  setBody('runbooks', `
    ${list}
    <div class="command-builder">
      <div class="command-panel-sub">Custom runbook builder</div>
      <div class="command-form compact">
        <input id="command-runbook-name" placeholder="name, e.g. morning_check">
        <input id="command-runbook-label" placeholder="Label">
      </div>
      <input id="command-runbook-description" class="command-wide-input" placeholder="Description">
      <textarea id="command-runbook-steps" class="command-textarea" spellcheck="false">${esc(runbookExample())}</textarea>
      <div class="command-inline-actions">
        <button type="button" class="command-btn approve" data-command-action="save-runbook">Save custom runbook</button>
      </div>
    </div>`);
  setState('runbooks', true);
}

function renderAutomations(payload) {
  const rows = Array.isArray(payload?.automations) ? payload.automations : [];
  const templates = Array.isArray(payload?.templates) ? payload.templates : [];
  latest.automations = rows;
  const runbookOptions = (latest.runbooks || []).map((row) => `<option value="${esc(row.name)}">${esc(row.label || row.name)}</option>`).join('');
  setBody('automations', `
    <div class="command-button-grid">
      <button type="button" class="command-btn" data-command-action="evaluate-automations">Evaluate now</button>
      <button type="button" class="command-btn" data-command-action="load-automation-template">Load template</button>
    </div>
    <div class="command-automation-list">
      ${rows.map((row) => `
        <div class="command-automation-row">
          <div>
            <strong>${esc(row.name)} <em class="command-kind">${row.enabled ? 'enabled' : 'off'}</em></strong>
            <small>${esc(describeAutomation(row))}</small>
            <span>${esc(row.last_status || 'never run')}${row.last_detail ? ` - ${esc(row.last_detail)}` : ''}</span>
          </div>
          <div class="command-inline-actions">
            <button type="button" class="command-mini" data-automation-evaluate="${esc(row.id)}">Run check</button>
            <button type="button" class="command-mini danger" data-automation-delete="${esc(row.id)}">Delete</button>
          </div>
        </div>`).join('') || '<div class="command-empty">No automations yet.</div>'}
    </div>
    <div class="command-builder">
      <div class="command-panel-sub">Metric threshold automation</div>
      <div class="command-form compact">
        <input id="command-automation-name" placeholder="Automation name">
        <select id="command-automation-metric">
          <option value="cpu_percent">CPU %</option>
          <option value="memory_percent">Memory %</option>
          <option value="disk_used_percent">Disk used %</option>
          <option value="disk_free_gb">Disk free GB</option>
          <option value="gpu_temp_c">GPU temp C</option>
        </select>
        <select id="command-automation-op"><option>&gt;</option><option>&gt;=</option><option>&lt;</option><option>&lt;=</option></select>
        <input id="command-automation-value" type="number" value="90">
      </div>
      <div class="command-form compact">
        <select id="command-automation-runbook">${runbookOptions}</select>
        <input id="command-automation-cooldown" type="number" min="0" value="1800" title="Cooldown seconds">
        <label class="command-check"><input id="command-automation-enabled" type="checkbox" checked> enabled</label>
        <button type="button" class="command-btn approve" data-command-action="save-automation">Save automation</button>
      </div>
      <script type="application/json" id="command-automation-templates">${esc(JSON.stringify(templates || []))}</script>
    </div>`);
  setState('automations', true);
}

function describeAutomation(row) {
  const trigger = row?.trigger || {};
  const action = row?.action || {};
  const triggerText = trigger.type === 'metric_threshold'
    ? `${trigger.metric} ${trigger.op || '>'} ${trigger.value}`
    : 'manual';
  const actionText = action.type === 'runbook' ? `runbook ${action.name}` : `${action.action || 'action'}`;
  return `${triggerText} -> ${actionText}; cooldown ${row.cooldown_seconds || 0}s`;
}

function renderTimeline(payload) {
  const events = Array.isArray(payload?.events) ? payload.events : [];
  latest.timeline = events;
  const overview = latest.overview || {};
  const pending = Array.isArray(overview.pending) ? overview.pending.length : 0;
  setBody('timeline', `
    <div class="command-watchdog">
      <div><span>Bridge</span><strong>${overview.online ? 'online' : (overview.configured ? 'offline' : 'not configured')}</strong></div>
      <div><span>Pending</span><strong>${pending}</strong></div>
      <div><span>Events</span><strong>${events.length}</strong></div>
    </div>
    <div class="command-timeline">
      ${events.map((item) => `
        <div class="command-timeline-row" data-status="${esc(item.status || '')}">
          <span>${esc(new Date((item.ts || 0) * 1000).toLocaleString())}</span>
          <strong>${esc(actionLabel(item.action))} - ${esc(item.status || '')}</strong>
          <small>${esc(item.detail || item.requested_by || '')}</small>
        </div>`).join('') || '<div class="command-empty">No audit events yet.</div>'}
    </div>`);
  setState('timeline', true);
}

async function pcAction(action, args = {}, after) {
  try {
    const result = await request('/pc/action', {
      method: 'POST',
      body: JSON.stringify({ action, args, device_id: selectedDeviceId || null }),
    });
    if (result?.status === 'pending_confirmation') {
      toast(`${actionLabel(action)} is waiting for approval`);
      await refreshOverview();
      return result;
    }
    if (after) after(result);
    return result;
  } catch (error) {
    toast(`${actionLabel(action)} failed: ${error.message}`, true);
    throw error;
  }
}

async function refreshOverview() {
  try {
    const data = await request(`/overview${selectedDeviceId ? `?device_id=${encodeURIComponent(selectedDeviceId)}` : ''}`);
    latest.overview = data;
    renderVitals(data);
    setNote('vitals', data.error || '', data.online ? '' : 'warn');
  } catch (error) {
    setNote('vitals', error.message, 'error');
    setState('vitals', false);
  }
}

async function refreshProcesses() {
  try {
    const payload = await pcAction('processes', { limit: 12 }, renderProcesses);
    if (payload?.processes) renderProcesses(payload);
    setNote('processes', '');
  } catch (error) {
    setNote('processes', error.message, 'error');
    setState('processes', false);
  }
}

async function refreshWindows() {
  try {
    const payload = await pcAction('windows', {}, renderWindows);
    if (payload?.windows) renderWindows(payload);
    setNote('windows', '');
  } catch (error) {
    setNote('windows', error.message, 'error');
    setState('windows', false);
  }
}

async function refreshFiles(path = currentPath) {
  try {
    const payload = await pcAction('file_list', { path, limit: 160 }, renderFiles);
    if (payload?.entries) renderFiles(payload);
    setNote('files', '');
  } catch (error) {
    setNote('files', error.message, 'error');
    setState('files', false);
  }
}

async function refreshRunbooks() {
  try {
    const payload = await request('/runbooks');
    renderRunbooks(payload);
    setNote('runbooks', '');
  } catch (error) {
    setNote('runbooks', error.message, 'error');
    setState('runbooks', false);
  }
}

async function refreshAutomations() {
  try {
    const payload = await request('/automations');
    renderAutomations(payload);
    setNote('automations', '');
  } catch (error) {
    setNote('automations', error.message, 'error');
    setState('automations', false);
  }
}

async function refreshTimeline() {
  try {
    const payload = await request('/timeline?limit=30');
    renderTimeline(payload);
    setNote('timeline', '');
  } catch (error) {
    setNote('timeline', error.message, 'error');
    setState('timeline', false);
  }
}

async function refreshClipboard() {
  try {
    const payload = await pcAction('clipboard_get', {}, (result) => {
      latest.clipboard = result;
    });
    if (payload?.text !== undefined) latest.clipboard = payload;
  } catch (_) {
    latest.clipboard = null;
  }
  renderSystem();
}

async function captureScreen() {
  setNote('screen', 'Capturing screen...');
  try {
    await pcAction('screenshot', {}, (payload) => {
      renderScreen(payload);
      setNote('screen', '');
    });
  } catch (error) {
    setNote('screen', error.message, 'error');
    setState('screen', false);
  }
}

async function refreshAll({ includeScreen = false, skipAccess = false } = {}) {
  if (!skipAccess && !(await refreshDevices())) return;
  if (!selectedDeviceId) return;
  await Promise.allSettled([
    refreshOverview(),
    refreshProcesses(),
    refreshWindows(),
    refreshFiles(currentPath),
    refreshClipboard(),
  ]);
  await refreshRunbooks();
  await Promise.allSettled([
    refreshAutomations(),
    refreshTimeline(),
  ]);
  renderTerminal();
  renderScreen(latest.screen);
  renderGeneratedPanels();
  if (includeScreen) await captureScreen();
}

async function approvePending(id) {
  try {
    const result = await request(`/pc/confirm/${encodeURIComponent(id)}`, { method: 'POST' });
    toast(`${actionLabel(result.action)} executed`);
    if (result.action === 'shell') renderTerminalOutput(result);
    await refreshAll();
  } catch (error) {
    toast(`Approval failed: ${error.message}`, true);
  }
}

async function cancelPending(id) {
  try {
    await request(`/pc/pending/${encodeURIComponent(id)}`, { method: 'DELETE' });
    await refreshOverview();
  } catch (error) {
    toast(`Cancel failed: ${error.message}`, true);
  }
}

function openPreviewImage() {
  const img = root?.querySelector('.command-screen-wrap img');
  if (!img) return;
  const overlay = document.createElement('div');
  overlay.className = 'command-lightbox';
  overlay.innerHTML = `<button type="button" aria-label="Close">x</button><img src="${esc(img.src)}" alt="Home PC screen enlarged">`;
  overlay.addEventListener('click', () => overlay.remove());
  document.body.appendChild(overlay);
}

function buildGeneratedSpec(prompt) {
  const text = String(prompt || '').toLowerCase();
  if (text.includes('process') || text.includes('cpu') || text.includes('hot')) {
    return { title: prompt, source: 'processes', limit: 5 };
  }
  if (text.includes('window') || text.includes('app')) {
    return { title: prompt, source: 'windows', limit: 6 };
  }
  return { title: prompt, source: 'vitals' };
}

function addAiPanel() {
  const prompt = window.prompt('Describe a safe panel. Shadow will compose only from existing read-only data sources.');
  if (!prompt) return;
  generatedPanels.push(buildGeneratedSpec(prompt));
  renderGeneratedPanels();
}

function renderGeneratedPanels() {
  const wrap = document.getElementById('command-ai-panel');
  if (!wrap) return;
  wrap.hidden = generatedPanels.length === 0;
  if (!generatedPanels.length) return;
  setBody('ai', generatedPanels.map((spec, index) => {
    let body = '';
    if (spec.source === 'processes') {
      body = parseProcesses(latest.processes).slice(0, spec.limit || 5)
        .map((row) => `<div class="generated-row"><span>${esc(row.name)}</span><strong>${esc(row.cpu)}%</strong></div>`).join('') || 'No process data.';
    } else if (spec.source === 'windows') {
      const windows = latest.windows?.windows || [];
      body = windows.slice(0, spec.limit || 6)
        .map((row) => `<div class="generated-row"><span>${esc(row.title || row.class)}</span><strong>${esc(row.pid || '')}</strong></div>`).join('') || 'No window data.';
    } else {
      const pc = latest.overview?.pc || {};
      body = `
        <div class="generated-row"><span>Host</span><strong>${esc(pc.hostname || 'n/a')}</strong></div>
        <div class="generated-row"><span>CPU</span><strong>${pct(pc.cpu?.percent)}</strong></div>
        <div class="generated-row"><span>Uptime</span><strong>${uptime(pc.uptime_seconds)}</strong></div>`;
    }
    return `
      <div class="generated-panel">
        <div class="generated-head"><strong>${esc(spec.title)}</strong><button type="button" data-generated-remove="${index}">Remove</button></div>
        <div class="generated-body">${body}</div>
      </div>`;
  }).join(''));
}

async function saveCustomRunbook() {
  const name = root.querySelector('#command-runbook-name')?.value || '';
  const label = root.querySelector('#command-runbook-label')?.value || name;
  const description = root.querySelector('#command-runbook-description')?.value || '';
  const rawSteps = root.querySelector('#command-runbook-steps')?.value || '[]';
  let steps;
  try {
    steps = JSON.parse(rawSteps);
  } catch (error) {
    toast(`Runbook JSON is invalid: ${error.message}`, true);
    return;
  }
  try {
    await request('/runbooks', {
      method: 'POST',
      body: JSON.stringify({ name, label, description, steps }),
    });
    toast('Custom runbook saved');
    await Promise.allSettled([refreshRunbooks(), refreshAutomations()]);
  } catch (error) {
    toast(`Save runbook failed: ${error.message}`, true);
  }
}

async function deleteCustomRunbook(name) {
  try {
    await request(`/runbooks/${encodeURIComponent(name)}`, { method: 'DELETE' });
    toast('Custom runbook deleted');
    await Promise.allSettled([refreshRunbooks(), refreshAutomations()]);
  } catch (error) {
    toast(`Delete runbook failed: ${error.message}`, true);
  }
}

async function saveAutomation() {
  const name = root.querySelector('#command-automation-name')?.value || '';
  const metric = root.querySelector('#command-automation-metric')?.value || 'cpu_percent';
  const op = root.querySelector('#command-automation-op')?.value || '>';
  const value = Number(root.querySelector('#command-automation-value')?.value || 0);
  const runbook = root.querySelector('#command-automation-runbook')?.value || 'health_report';
  const cooldown = Number(root.querySelector('#command-automation-cooldown')?.value || 300);
  const enabled = Boolean(root.querySelector('#command-automation-enabled')?.checked);
  try {
    await request('/automations', {
      method: 'POST',
      body: JSON.stringify({
        name,
        enabled,
        trigger: { type: 'metric_threshold', metric, op, value },
        action: { type: 'runbook', name: runbook },
        cooldown_seconds: cooldown,
      }),
    });
    toast('Automation saved');
    await refreshAutomations();
  } catch (error) {
    toast(`Save automation failed: ${error.message}`, true);
  }
}

async function deleteAutomation(id) {
  try {
    await request(`/automations/${encodeURIComponent(id)}`, { method: 'DELETE' });
    await refreshAutomations();
  } catch (error) {
    toast(`Delete automation failed: ${error.message}`, true);
  }
}

async function evaluateAutomations(id = '') {
  try {
    const qs = id ? `?automation_id=${encodeURIComponent(id)}` : '';
    const result = await request(`/automations/evaluate${qs}`, { method: 'POST' });
    const changed = (result.events || []).filter((event) => !['idle', 'disabled', 'cooldown'].includes(event.status));
    toast(changed.length ? `${changed.length} automation action(s) requested` : 'No automation fired');
    await Promise.allSettled([refreshOverview(), refreshAutomations()]);
  } catch (error) {
    toast(`Automation check failed: ${error.message}`, true);
  }
}

function loadAutomationTemplate() {
  let templates = [];
  try {
    templates = JSON.parse(root.querySelector('#command-automation-templates')?.textContent || '[]');
  } catch (_) {}
  const tpl = templates[0];
  if (!tpl) return toast('No automation templates available', true);
  root.querySelector('#command-automation-name').value = tpl.name || '';
  if (tpl.trigger?.metric) root.querySelector('#command-automation-metric').value = tpl.trigger.metric;
  if (tpl.trigger?.op) root.querySelector('#command-automation-op').value = tpl.trigger.op;
  if (tpl.trigger?.value !== undefined) root.querySelector('#command-automation-value').value = tpl.trigger.value;
  if (tpl.action?.name) root.querySelector('#command-automation-runbook').value = tpl.action.name;
  if (tpl.cooldown_seconds !== undefined) root.querySelector('#command-automation-cooldown').value = tpl.cooldown_seconds;
}

async function inspectScreen() {
  const prompt = root.querySelector('#command-screen-prompt')?.value || 'Describe the current screen.';
  setNote('screen', 'Inspecting screen...');
  try {
    const result = await request('/screen/inspect', {
      method: 'POST',
      body: JSON.stringify({ prompt, device_id: selectedDeviceId || null }),
    });
    latest.inspector = result;
    if (result.screenshot?.image_b64) renderScreen(result.screenshot);
    const box = root.querySelector('#command-screen-analysis');
    if (box) box.textContent = result.analysis || 'Screen captured.';
    setNote('screen', result.needs_vision_model ? 'Vision model integration pending; deterministic capture is live.' : '');
  } catch (error) {
    setNote('screen', error.message, 'error');
  }
}

function bindEvents() {
  root.addEventListener('click', async (event) => {
    const target = event.target.closest('button, .command-file, .command-chip, label');
    if (!target) return;

    const toggleId = target.dataset.panelToggle;
    if (toggleId) {
      const section = target.closest('.command-panel');
      if (section) {
        const collapsed = section.classList.toggle('collapsed');
        target.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      }
      return;
    }

    const accessAction = target.dataset.accessAction;
    if (target.dataset.deviceEnroll !== undefined) return createDeviceEnrollment();
    if (target.dataset.deviceSelect) return selectDevice(target.dataset.deviceSelect);
    if (target.dataset.deviceRemove) return removeDevice(target.dataset.deviceRemove);
    if (target.dataset.copyCommand !== undefined) {
      await navigator.clipboard.writeText(target.dataset.copyCommand || '');
      return toast('Setup command copied');
    }
    if (accessAction === 'telegram') return createTelegramPairCode();
    if (accessAction === 'telegram-unlink') {
      try {
        await request('/telegram/link', { method: 'DELETE' });
        toast('Telegram account unlinked');
        return refreshDevices();
      } catch (error) {
        return toast(`Telegram unlink failed: ${error.message}`, true);
      }
    }

    const approveId = target.dataset.pendingApprove;
    if (approveId) return approvePending(approveId);
    const cancelId = target.dataset.pendingCancel;
    if (cancelId) return cancelPending(cancelId);
    const removeIndex = target.dataset.generatedRemove;
    if (removeIndex !== undefined) {
      generatedPanels.splice(Number(removeIndex), 1);
      renderGeneratedPanels();
      return;
    }
    const killPid = target.dataset.killPid;
    if (killPid) return pcAction('kill_process', { pid: killPid });
    const focusId = target.dataset.windowFocus;
    if (focusId !== undefined) return pcAction('app_focus', { id: focusId, title: target.dataset.windowTitle || '' });
    const closeId = target.dataset.windowClose;
    if (closeId !== undefined) return pcAction('app_close', { id: closeId, title: target.dataset.windowTitle || '' });
    const rootPath = target.dataset.rootPath;
    if (rootPath) return refreshFiles(rootPath);
    const filePath = target.dataset.filePath;
    if (filePath) {
      if (target.dataset.fileType === 'dir') return refreshFiles(filePath);
      return pcAction('file_read', { path: filePath }, (payload) => {
        lastReadFile = payload;
        const preview = root.querySelector('#command-file-preview');
        if (preview) preview.textContent = payload.binary ? `[binary] ${payload.name || payload.path}` : (payload.text || '');
      });
    }
    const media = target.dataset.media;
    if (media) return pcAction('media', { command: media });
    const runbookDelete = target.dataset.runbookDelete;
    if (runbookDelete) return deleteCustomRunbook(runbookDelete);
    const automationDelete = target.dataset.automationDelete;
    if (automationDelete) return deleteAutomation(automationDelete);
    const automationEvaluate = target.dataset.automationEvaluate;
    if (automationEvaluate) return evaluateAutomations(automationEvaluate);
    const runbook = target.dataset.runbook;
    if (runbook) return pcAction('runbook', { name: runbook });

    const action = target.dataset.commandAction;
    if (!action) return;
    if (action === 'close') return closePage();
    if (action === 'refresh') return refreshAll({ includeScreen: false });
    if (action === 'screen') return captureScreen();
    if (action === 'screen-inspect') return inspectScreen();
    if (action === 'screen-open') {
      if (event.shiftKey) {
        const coords = screenClickPayload(event);
        if (coords) return pcAction('mouse_click', coords);
        toast('Could not map screen click coordinates', true);
        return;
      }
      return openPreviewImage();
    }
    if (action === 'lock') return pcAction('lock');
    if (action === 'sleep') return pcAction('sleep');
    if (action === 'shutdown') return pcAction('shutdown');
    if (action === 'tasks') return document.getElementById('tool-tasks-btn')?.click();
    if (action === 'memory') return document.getElementById('tool-memory-btn')?.click();
    if (action === 'ai-panel') return addAiPanel();
    if (action === 'save-runbook') return saveCustomRunbook();
    if (action === 'save-automation') return saveAutomation();
    if (action === 'evaluate-automations') return evaluateAutomations();
    if (action === 'load-automation-template') return loadAutomationTemplate();
    if (action === 'launch-app') return pcAction('app_launch', { app: root.querySelector('#command-app-name')?.value || '' });
    if (action === 'file-list') return refreshFiles(root.querySelector('#command-file-path')?.value || currentPath);
    if (action === 'file-up') {
      const path = root.querySelector('#command-file-path')?.value || currentPath;
      return refreshFiles(path.replace(/\/+$/, '').split('/').slice(0, -1).join('/') || '/');
    }
    if (action === 'file-search') {
      const query = root.querySelector('#command-file-query')?.value || '';
      return pcAction('file_search', { path: currentPath, query }, renderSearchResults);
    }
    if (action === 'file-download' && lastReadFile?.text !== undefined) {
      const blob = new Blob([lastReadFile.text], { type: lastReadFile.mime || 'text/plain' });
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = lastReadFile.name || 'shadow-file.txt';
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 500);
      return;
    }
    if (action === 'shell') {
      return pcAction('shell', {
        command: root.querySelector('#command-shell-input')?.value || '',
        cwd: root.querySelector('#command-shell-cwd')?.value || currentPath,
      });
    }
    if (action === 'volume') return pcAction('volume', { percent: root.querySelector('#command-volume')?.value || 50 });
    if (action === 'type-text') return pcAction('type_text', { text: root.querySelector('#command-type-text')?.value || '' });
    if (action === 'keypress') return pcAction('keypress', { key: root.querySelector('#command-keypress')?.value || '' });
    if (action === 'clipboard-get') return refreshClipboard();
    if (action === 'clipboard-set') return pcAction('clipboard_set', { text: root.querySelector('#command-clipboard-text')?.value || '' });
  });

  root.addEventListener('change', async (event) => {
    const input = event.target.closest('#command-file-upload');
    if (!input?.files?.length) return;
    const file = input.files[0];
    const text = await file.text();
    const base = (root.querySelector('#command-file-path')?.value || currentPath || '').replace(/\/+$/, '');
    await pcAction('file_write', { path: `${base}/${file.name}`, text });
    input.value = '';
  });
}

function startTimers() {
  stopTimers();
  if (!selectedDeviceId) return;
  refreshTimer = setInterval(() => {
    if (!document.hidden && root && !root.hidden) refreshAll({ includeScreen: false });
  }, REFRESH_MS);
}

function stopTimers() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = null;
}

function stopEnrollmentTimer() {
  if (enrollmentTimer) clearInterval(enrollmentTimer);
  enrollmentTimer = null;
}

function ensureRoot() {
  ensureStyles();
  if (!root) {
    root = document.getElementById('command-page') || buildShell();
    bindEvents();
  }
  return root;
}

async function bootstrapPage() {
  stopTimers();
  const allowed = await refreshDevices();
  if (!allowed) return;
  startTimers();
  await refreshAll({ includeScreen: false, skipAccess: true });
}

function openPage(options = {}) {
  const push = options.push !== false;
  const node = ensureRoot();
  node.hidden = false;
  document.body.classList.add('command-page-open');
  if (push) navTo('/command');
  bootstrapPage();
}

function closePage() {
  if (root) root.hidden = true;
  document.body.classList.remove('command-page-open');
  stopTimers();
  stopEnrollmentTimer();
  if (window.location.pathname === '/command') navTo('/');
}

function init() {
  document.getElementById('tool-command-btn')?.addEventListener('click', () => openPage());
  document.addEventListener('visibilitychange', () => {
    if (!root || root.hidden) return;
    if (document.hidden) stopTimers();
    else {
      bootstrapPage();
    }
  });
  window.addEventListener('popstate', () => {
    if (window.location.pathname === '/command') openPage({ push: false });
    else if (root && !root.hidden) closePage();
  });
}

export default { init, openPage, closePage, refreshAll };
