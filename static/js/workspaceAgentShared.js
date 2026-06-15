// Shared helpers for the desktop workspace agent:
// the Codex-style permission-mode selector, full-access arm/disarm flow,
// and small request/escape/toast utilities used by in-chat Computer mode.

const API = '/api/workspace-agent';

export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

export function ensureWorkspaceAgentStyles() {
  if (document.getElementById('workspace-agent-css')) return;
  const link = document.createElement('link');
  link.id = 'workspace-agent-css';
  link.rel = 'stylesheet';
  link.href = '/static/workspace-agent.css';
  document.head.appendChild(link);
}

export async function request(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  let payload = null;
  try { payload = await response.json(); } catch (_) { payload = null; }
  if (!response.ok) {
    const detail = payload && (payload.detail || payload.error);
    const err = new Error(detail || `HTTP ${response.status}`);
    err.status = response.status;
    throw err;
  }
  return payload;
}

export function toast(message, isError = false) {
  let node = document.getElementById('missions-toast');
  if (!node) {
    node = document.createElement('div');
    node.id = 'missions-toast';
    node.className = 'missions-toast';
    document.body.appendChild(node);
  }
  node.textContent = message;
  node.dataset.kind = isError ? 'error' : 'ok';
  node.hidden = false;
  clearTimeout(node._t);
  node._t = setTimeout(() => { node.hidden = true; }, 4200);
}

// ── permission mode metadata (Codex-style) ────────────────────────────

export const MODES = [
  {
    id: 'ask',
    title: 'Ask for approval',
    body: 'Shadow can read files and inspect the workspace. Every edit, command, network access, or out-of-workspace touch asks you first.',
  },
  {
    id: 'auto',
    title: 'Approve for me',
    badge: 'Recommended',
    body: 'Shadow reads, edits, and runs ordinary project commands in this workspace without interrupting. Destructive, privileged, networked, or out-of-workspace actions still ask first. Approvals last only for the mission or session.',
  },
  {
    id: 'unattended',
    title: 'Unattended',
    badge: 'Autonomous',
    body: 'Shadow works in this workspace without ever asking: edits, project commands, tests, package installs, network access, and safe git operations run immediately. Privilege escalation, destructive git, secrets, and anything outside the workspace are refused automatically. Stays enabled until you disable it.',
  },
  {
    id: 'full',
    title: 'Full access',
    badge: 'Dangerous',
    body: 'Shadow edits and runs commands across all authorized device roots without routine approval, including network access. Requires your password, shows a persistent FULL ACCESS indicator, can be time-limited, and is fully audited. Never on by default.',
  },
];

// ── permission selector modal (arrow keys / enter / escape) ───────────

export function openModeSelector({ current, deviceId, onPick, onFullAccess }) {
  closeModeSelector();
  const overlay = document.createElement('div');
  overlay.id = 'missions-mode-overlay';
  overlay.className = 'missions-modal-overlay';
  let index = Math.max(0, MODES.findIndex((m) => m.id === current));
  overlay.innerHTML = `
    <div class="missions-modal" role="dialog" aria-label="Update model permissions">
      <h3>Update model permissions</h3>
      <p class="missions-modal-hint">↑↓ select · Enter confirm · Esc cancel</p>
      <div class="missions-mode-list">
        ${MODES.map((m, i) => `
          <div class="missions-mode-option" data-mode-index="${i}" data-mode="${m.id}" tabindex="-1">
            <div class="missions-mode-title">${esc(m.title)}
              ${m.badge ? `<span class="missions-badge" data-kind="${m.id === 'full' ? 'danger' : 'ok'}">${esc(m.badge)}</span>` : ''}
              ${m.id === current ? '<span class="missions-badge" data-kind="current">Current</span>' : ''}
            </div>
            <div class="missions-mode-body">${esc(m.body)}</div>
          </div>`).join('')}
      </div>
      <div class="missions-modal-actions">
        <button type="button" class="missions-btn" data-mode-cancel>Cancel</button>
        <button type="button" class="missions-btn primary" data-mode-confirm>Confirm</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const options = [...overlay.querySelectorAll('.missions-mode-option')];
  const highlight = () => options.forEach((node, i) => node.dataset.selected = i === index ? '1' : '0');
  highlight();

  const confirm = async () => {
    const mode = MODES[index].id;
    if (mode === 'full') {
      const result = await armFullAccessFlow(deviceId);
      if (!result) return;
      if (onFullAccess) onFullAccess(deviceId, result);
    }
    if (mode === 'unattended' && current !== 'unattended') {
      const ok = window.confirm(
        'Enable Unattended mode for this workspace?\n\n'
        + 'Shadow will edit files, run project commands, install packages, and use the '
        + 'network here without asking for approval. Dangerous operations are refused '
        + 'automatically. This setting persists until you disable it.');
      if (!ok) return;
    }
    closeModeSelector();
    onPick(mode);
  };
  const onKey = (event) => {
    if (event.key === 'ArrowDown') { index = (index + 1) % MODES.length; highlight(); event.preventDefault(); }
    else if (event.key === 'ArrowUp') { index = (index + MODES.length - 1) % MODES.length; highlight(); event.preventDefault(); }
    else if (event.key === 'Enter') { event.preventDefault(); confirm(); }
    else if (event.key === 'Escape') { event.preventDefault(); closeModeSelector(); }
  };
  overlay._onKey = onKey;
  document.addEventListener('keydown', onKey);
  overlay.addEventListener('click', (event) => {
    const option = event.target.closest('.missions-mode-option');
    if (option) { index = Number(option.dataset.modeIndex); highlight(); }
    if (event.target.closest('[data-mode-confirm]')) confirm();
    if (event.target.closest('[data-mode-cancel]') || event.target === overlay) closeModeSelector();
  });
}

export function closeModeSelector() {
  const overlay = document.getElementById('missions-mode-overlay');
  if (overlay) {
    document.removeEventListener('keydown', overlay._onKey);
    overlay.remove();
  }
}

// ── full-access arm/disarm ──────────────────────────────────────────

// Returns {expires_at} on success, or false if the user cancelled / the
// server refused. Does not mutate caller state — the caller records
// `expires_at` under its own device-id map and calls renderFullAccessBanner.
export async function armFullAccessFlow(deviceId) {
  if (!deviceId) { toast('Select a workspace/device first', true); return false; }
  const password = prompt(
    '⚠ FULL ACCESS WARNING\n\nShadow will be able to edit files and run commands across every '
    + 'authorized root on this device without routine approval, including network access.\n\n'
    + 'Enter your Shadow password to confirm:');
  if (!password) return false;
  const minutes = prompt('Time limit in minutes (15–480):', '60');
  const duration = Math.max(15, Math.min(480, Number(minutes) || 60)) * 60;
  try {
    const result = await request('/policy/full-access', {
      method: 'POST',
      body: JSON.stringify({ device_id: deviceId, password, duration_seconds: duration }),
    });
    toast('Full access armed');
    return result;
  } catch (error) {
    toast(`Full access refused: ${error.message}`, true);
    return false;
  }
}

// `fullAccess` is a caller-owned map of device_id -> expires_at (epoch
// seconds). Renders a single global banner for the first still-armed entry
// and wires its Disarm button back into the same map.
export function renderFullAccessBanner(fullAccess) {
  let banner = document.getElementById('missions-full-banner');
  const armed = Object.entries(fullAccess || {}).filter(([, exp]) => exp * 1000 > Date.now());
  if (!armed.length) { if (banner) banner.remove(); return; }
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'missions-full-banner';
    banner.className = 'missions-full-banner';
    document.body.appendChild(banner);
  }
  const [deviceId, expiresAt] = armed[0];
  const minutes = Math.max(1, Math.round((expiresAt * 1000 - Date.now()) / 60000));
  banner.innerHTML = `⚠ FULL ACCESS — expires in ${minutes} min
    <button type="button" data-full-disarm="${esc(deviceId)}">Disarm</button>`;
  banner.querySelector('[data-full-disarm]').onclick = async () => {
    await request(`/policy/full-access/${encodeURIComponent(deviceId)}`, { method: 'DELETE' });
    delete fullAccess[deviceId];
    renderFullAccessBanner(fullAccess);
    toast('Full access disarmed');
  };
}
