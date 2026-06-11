// Autonomous Missions + Desktop Workspace page.
// Follows the commandPage.js full-page module pattern: openPage/closePage,
// route '/missions', polling refresh, single scoped stylesheet.

const API = '/api/missions';
const REFRESH_MS = 3500;

let root = null;
let refreshTimer = null;
let state = {
  workspaces: [],
  missions: [],
  sessions: [],
  models: [],
  devices: [],
  activeMission: null,     // full mission record
  activeSession: null,     // full agent-session record
  activeWorkspace: null,   // workspace id selected in explorer
  tree: null,
  openFile: null,          // {path, text, sha256, dirty}
  git: null,
  tab: 'overview',
  fullAccess: {},          // device_id -> expires_at
};

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

function navTo(path) {
  if (!path || window.location.pathname === path) return;
  try { window.history.pushState({}, '', path); } catch (_) {}
}

function ensureStyles() {
  if (document.getElementById('missions-page-css')) return;
  const link = document.createElement('link');
  link.id = 'missions-page-css';
  link.rel = 'stylesheet';
  link.href = '/static/missions.css';
  document.head.appendChild(link);
}

async function request(path, options = {}) {
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

function toast(message, isError = false) {
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

const MODES = [
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

function openModeSelector({ current, deviceId, onPick }) {
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
      const ok = await armFullAccessFlow(deviceId);
      if (!ok) return;
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

function closeModeSelector() {
  const overlay = document.getElementById('missions-mode-overlay');
  if (overlay) {
    document.removeEventListener('keydown', overlay._onKey);
    overlay.remove();
  }
}

async function armFullAccessFlow(deviceId) {
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
    state.fullAccess[deviceId] = result.expires_at;
    renderFullAccessBanner();
    toast('Full access armed');
    return true;
  } catch (error) {
    toast(`Full access refused: ${error.message}`, true);
    return false;
  }
}

function renderFullAccessBanner() {
  let banner = document.getElementById('missions-full-banner');
  const armed = Object.entries(state.fullAccess).filter(([, exp]) => exp * 1000 > Date.now());
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
    delete state.fullAccess[deviceId];
    renderFullAccessBanner();
    toast('Full access disarmed');
  };
}

// ── shell ──────────────────────────────────────────────────────────────

function buildShell() {
  const node = document.createElement('section');
  node.id = 'missions-page';
  node.className = 'missions-page';
  node.hidden = true;
  node.innerHTML = `
    <div class="missions-inner">
      <header class="missions-top">
        <div>
          <div class="missions-kicker">SHADOW // AGENT</div>
          <h2>Agent</h2>
          <p>Agent sessions for direct coding tasks. Missions for large multi-stage goals.</p>
        </div>
        <div class="missions-top-actions">
          <button type="button" class="missions-btn" data-act="permissions">Update model permissions</button>
          <button type="button" class="missions-btn" data-act="rules">Permission rules</button>
          <button type="button" class="missions-btn" data-act="refresh">Refresh</button>
          <button type="button" class="missions-close" data-act="close" aria-label="Close">✕</button>
        </div>
      </header>
      <div id="missions-unattended-strip"></div>
      <div class="missions-layout">
        <aside class="missions-side">
          <div class="missions-panel" id="missions-agent-composer">
            <h4>New agent session</h4>
            <select id="agent-workspace" aria-label="Workspace"></select>
            <select id="agent-model" aria-label="Model"></select>
            <textarea id="agent-task" rows="3" placeholder="e.g. Fix the failing test in tests/test_utils.py and run the suite"></textarea>
            <label class="missions-checkbox"><input type="checkbox" id="agent-fallback" />
              Fall back to other models automatically</label>
            <div class="missions-mode-row">
              <span>Mode: <strong id="agent-ws-mode">—</strong></span>
              <button type="button" class="missions-btn small" data-act="agent-ws-mode">Change</button>
            </div>
            <button type="button" class="missions-btn primary" data-act="agent-run">Run</button>
          </div>
          <div class="missions-panel">
            <h4>Sessions</h4>
            <div id="agent-sessions"></div>
          </div>
          <div class="missions-panel" id="missions-workspaces-panel">
            <h4>Workspaces</h4>
            <div id="missions-workspaces"></div>
            <button type="button" class="missions-btn" data-act="add-workspace">+ Authorize folder</button>
          </div>
          <div class="missions-panel">
            <h4>Missions</h4>
            <div id="missions-list"></div>
          </div>
          <div class="missions-panel" id="missions-composer">
            <h4>New mission</h4>
            <textarea id="missions-goal" rows="3" placeholder="e.g. Fix the failing installer test and prepare a release checklist"></textarea>
            <div id="missions-roles"></div>
            <label class="missions-checkbox"><input type="checkbox" id="missions-allow-network" />
              Allow network access for this mission</label>
            <div class="missions-mode-row">
              <span>Permissions: <strong id="missions-mode-label">auto</strong></span>
              <button type="button" class="missions-btn small" data-act="composer-mode">Change</button>
            </div>
            <button type="button" class="missions-btn primary" data-act="create-mission">Plan mission</button>
          </div>
        </aside>
        <main class="missions-main">
          <div id="missions-detail"><div class="missions-empty">Select or create a mission — or open a workspace to browse files.</div></div>
        </main>
      </div>
    </div>`;
  document.body.appendChild(node);
  return node;
}

// ── workspaces ─────────────────────────────────────────────────────────

async function refreshWorkspaces() {
  const data = await request('/workspaces');
  state.workspaces = data.workspaces || [];
  state.fullAccess = {};
  for (const ws of state.workspaces) {
    if (ws.full_access) state.fullAccess[ws.device_id] = (Date.now() / 1000) + 600;
  }
  const container = root.querySelector('#missions-workspaces');
  if (!container) return;
  container.innerHTML = state.workspaces.map((ws) => `
    <div class="missions-row" data-workspace="${esc(ws.id)}">
      <div>
        <strong>${esc(ws.name)}
          ${ws.mode === 'unattended' ? '<span class="missions-badge" data-kind="unattended">UNATTENDED</span>' : ''}</strong>
        <small>${esc(ws.device_name || ws.device_id)} · ${esc(ws.root)} · mode: ${esc(ws.mode)}</small>
      </div>
      <div class="missions-row-actions">
        <button type="button" class="missions-btn small" data-open-workspace="${esc(ws.id)}">Browse</button>
        <button type="button" class="missions-btn small" data-workspace-mode="${esc(ws.id)}">Mode</button>
        <button type="button" class="missions-btn small danger" data-workspace-remove="${esc(ws.id)}">✕</button>
      </div>
    </div>`).join('') || '<div class="missions-empty">No folders authorized yet.</div>';
  renderAgentWorkspaceSelect();
  renderUnattendedStrip();
  renderFullAccessBanner();
}

function renderAgentWorkspaceSelect() {
  const select = root.querySelector('#agent-workspace');
  if (!select) return;
  const previous = select.value;
  select.innerHTML = state.workspaces.map((ws) => `
    <option value="${esc(ws.id)}">${esc(ws.name)} — ${esc(ws.device_name || ws.device_id)}</option>`)
    .join('') || '<option value="">No workspaces authorized</option>';
  if (previous && state.workspaces.some((w) => w.id === previous)) select.value = previous;
  updateAgentModeLabel();
}

function updateAgentModeLabel() {
  const select = root.querySelector('#agent-workspace');
  const label = root.querySelector('#agent-ws-mode');
  if (!select || !label) return;
  const ws = state.workspaces.find((w) => w.id === select.value);
  label.textContent = ws ? ws.mode : '—';
  label.dataset.unattended = ws?.mode === 'unattended' ? '1' : '0';
}

function renderUnattendedStrip() {
  const strip = root.querySelector('#missions-unattended-strip');
  if (!strip) return;
  const unattended = state.workspaces.filter((w) => w.mode === 'unattended');
  strip.innerHTML = unattended.map((ws) => `
    <div class="missions-unattended">
      <span>● UNATTENDED — <strong>${esc(ws.name)}</strong> runs without approval prompts</span>
      <button type="button" class="missions-btn small" data-unattended-off="${esc(ws.id)}">Disable</button>
    </div>`).join('');
}

async function addWorkspaceFlow() {
  try {
    const devices = await fetch('/api/shadow/devices', { credentials: 'same-origin' }).then((r) => r.json());
    const online = (devices.devices || []).filter((d) => d.online && (d.capabilities || []).includes('ws_tree'));
    if (!online.length) {
      toast('No online device with the workspace agent. Update the device agent first.', true);
      return;
    }
    const names = online.map((d, i) => `${i + 1}. ${d.name} (${d.platform})`).join('\n');
    const pick = online.length === 1 ? 1 : Number(prompt(`Which device?\n${names}`, '1'));
    const device = online[(pick || 1) - 1];
    if (!device) return;
    const folder = prompt('Absolute folder path to authorize on that device\n(must be inside the agent\'s SHADOW_ALLOWED_ROOTS):');
    if (!folder) return;
    const name = prompt('Workspace name:', folder.split(/[\\/]/).filter(Boolean).pop() || 'project') || '';
    await request('/workspaces', {
      method: 'POST',
      body: JSON.stringify({ device_id: device.id, root: folder, name }),
    });
    toast('Workspace authorized');
    await refreshWorkspaces();
  } catch (error) {
    toast(error.message, true);
  }
}

// ── mission list + composer ────────────────────────────────────────────

let composerMode = 'auto';
let composerWorkspace = '';

async function refreshMissions() {
  const data = await request('');
  state.missions = data.missions || [];
  const container = root.querySelector('#missions-list');
  if (!container) return;
  container.innerHTML = state.missions.map((m) => {
    const counts = Object.entries(m.task_counts || {}).map(([k, v]) => `${v} ${k}`).join(' · ');
    return `
    <div class="missions-row" data-selected="${state.activeMission?.id === m.id ? '1' : '0'}">
      <div>
        <strong>${esc(m.goal.slice(0, 70))}</strong>
        <small>${esc(m.status)}${m.pending_approvals ? ` · ⚠ ${m.pending_approvals} approval(s)` : ''}${counts ? ` · ${counts}` : ''}</small>
      </div>
      <button type="button" class="missions-btn small" data-open-mission="${esc(m.id)}">Open</button>
    </div>`;
  }).join('') || '<div class="missions-empty">No missions yet.</div>';
}

function modelOptions() {
  return state.models.flatMap((ep) =>
    (ep.models || []).map((m) => ({ id: `${ep.endpoint_id}|${m}`, label: `${m} @ ${ep.endpoint_name}` })));
}

async function refreshModels() {
  try {
    const data = await request('/models');
    state.models = data.available || [];
  } catch (_) { state.models = []; }
  const agentSelect = root.querySelector('#agent-model');
  if (agentSelect) {
    const previous = agentSelect.value;
    const opts = modelOptions();
    agentSelect.innerHTML = opts.map((o) => `<option value="${esc(o.id)}">${esc(o.label)}</option>`).join('')
      || '<option value="">No model endpoints configured</option>';
    if (previous && opts.some((o) => o.id === previous)) agentSelect.value = previous;
  }
  const container = root.querySelector('#missions-roles');
  if (!container) return;
  const options = modelOptions();
  if (!options.length) {
    container.innerHTML = '<div class="missions-empty">No model endpoints configured.</div>';
    return;
  }
  const select = (role, fallbackIndex) => `
    <label class="missions-role">${role}
      <select data-role="${role}">
        ${options.map((o, i) => `<option value="${esc(o.id)}" ${i === Math.min(fallbackIndex, options.length - 1) ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}
      </select>
    </label>`;
  container.innerHTML = ['planner', 'researcher', 'implementer', 'reviewer', 'tester']
    .map((role, i) => select(role, i % options.length)).join('');
}

async function createMission() {
  const goal = root.querySelector('#missions-goal')?.value?.trim();
  if (!goal || goal.length < 8) { toast('Describe the goal first', true); return; }
  const workspaceId = composerWorkspace || state.workspaces[0]?.id;
  if (!workspaceId) { toast('Authorize a workspace folder first', true); return; }
  const roles = {};
  root.querySelectorAll('#missions-roles select').forEach((select) => {
    const [endpointId, ...model] = select.value.split('|');
    roles[select.dataset.role] = { endpoint_id: endpointId, model: model.join('|') };
  });
  const allowNetwork = root.querySelector('#missions-allow-network')?.checked || false;
  try {
    toast('Planning…');
    const mission = await request('', {
      method: 'POST',
      body: JSON.stringify({ workspace_id: workspaceId, goal, mode: composerMode, roles, allow_network: allowNetwork }),
    });
    state.activeMission = mission;
    state.tab = 'overview';
    await refreshMissions();
    renderMission();
  } catch (error) {
    toast(error.message, true);
  }
}

// ── agent sessions (direct tool loop) ──────────────────────────────────

async function refreshSessions() {
  try {
    const data = await request('/sessions');
    state.sessions = data.sessions || [];
  } catch (_) { state.sessions = []; }
  const container = root.querySelector('#agent-sessions');
  if (!container) return;
  container.innerHTML = state.sessions.map((s) => `
    <div class="missions-row" data-selected="${state.activeSession?.id === s.id ? '1' : '0'}">
      <div>
        <strong>${esc((s.task || '').slice(0, 70))}</strong>
        <small>${esc(s.status)}${s.retryable ? ' · retryable' : ''}${s.pending_approvals ? ` · ⚠ ${s.pending_approvals} approval(s)` : ''}
          · ${esc(s.model || '')}${s.files_changed ? ` · ${s.files_changed} file(s)` : ''}</small>
      </div>
      <button type="button" class="missions-btn small" data-open-session="${esc(s.id)}">Open</button>
    </div>`).join('') || '<div class="missions-empty">No sessions yet.</div>';
}

async function runAgentSession() {
  const task = root.querySelector('#agent-task')?.value?.trim();
  const workspaceId = root.querySelector('#agent-workspace')?.value;
  const modelValue = root.querySelector('#agent-model')?.value;
  const autoFallback = root.querySelector('#agent-fallback')?.checked || false;
  if (!task || task.length < 4) { toast('Describe the task first', true); return; }
  if (!workspaceId) { toast('Authorize a workspace folder first', true); return; }
  if (!modelValue) { toast('Configure a model endpoint first', true); return; }
  const [endpointId, ...modelParts] = modelValue.split('|');
  const fallbacks = autoFallback
    ? modelOptions().filter((o) => o.id !== modelValue).slice(0, 3).map((o) => {
        const [eid, ...m] = o.id.split('|');
        return { endpoint_id: eid, model: m.join('|') };
      })
    : [];
  try {
    toast('Starting session…');
    const session = await request('/sessions', {
      method: 'POST',
      body: JSON.stringify({
        workspace_id: workspaceId,
        task,
        endpoint_id: endpointId,
        model: modelParts.join('|'),
        fallbacks,
        auto_fallback: autoFallback,
      }),
    });
    root.querySelector('#agent-task').value = '';
    state.activeSession = session;
    state.activeMission = null;
    state.activeWorkspace = null;
    await refreshSessions();
    renderSession();
  } catch (error) {
    toast(error.message, true);
  }
}

async function openSession(sessionId) {
  try {
    state.activeSession = await request(`/sessions/${encodeURIComponent(sessionId)}`);
    state.activeMission = null;
    state.activeWorkspace = null;
    renderSession();
  } catch (error) {
    toast(error.message, true);
  }
}

function renderSessionApprovals(session) {
  const pending = (session.approvals || []).filter((a) => a.status === 'pending');
  if (!pending.length) return '';
  return pending.map((a) => `
    <div class="missions-approval" data-risk="${esc(a.risk)}">
      <div>
        <strong>${esc(a.summary)}</strong>
        <small>${esc(a.reason)} · capability: ${esc(a.capability)} · risk: ${esc(a.risk)}</small>
        ${a.args?.command ? `<code>${esc(a.args.command)}</code>` : ''}
      </div>
      <div class="missions-approval-actions">
        <button type="button" class="missions-btn small" data-session-approve="${a.id}|allow_once">Allow once</button>
        <button type="button" class="missions-btn small" data-session-approve="${a.id}|allow_session">Allow for this session</button>
        <button type="button" class="missions-btn small" data-session-approve="${a.id}|allow_always">Always allow in this workspace</button>
        <button type="button" class="missions-btn small danger" data-session-approve="${a.id}|decline">Decline</button>
        <button type="button" class="missions-btn small danger" data-session-approve="${a.id}|stop_session">Stop session</button>
      </div>
    </div>`).join('');
}

function renderSessionTimeline(session) {
  return `<div class="missions-events">${(session.events || []).slice(-150).reverse().map((e) => `
    <div class="missions-event" data-kind="${esc(e.kind)}">
      <span>${new Date(e.ts * 1000).toLocaleTimeString()}</span>
      <strong>${esc(e.kind)}</strong>
      <span>${esc(e.text)}</span>
    </div>`).join('') || '<div class="missions-empty">No activity yet.</div>'}</div>`;
}

function renderSessionTerminal(session) {
  const blocks = (session.terminal || []).slice(-12);
  if (!blocks.length) return '<div class="missions-empty">No commands executed yet.</div>';
  return blocks.map((t) => `
    <div class="missions-terminal-block">
      <h5>$ ${esc(t.command)} <small>(exit ${t.returncode ?? '?'}, ${t.seconds ?? '?'}s)</small></h5>
      <pre class="missions-output">${esc(t.stdout || '')}${t.stderr ? `\n--- stderr ---\n${esc(t.stderr)}` : ''}</pre>
    </div>`).join('');
}

function renderSession() {
  const session = state.activeSession;
  const container = root.querySelector('#missions-detail');
  if (!container) return;
  if (!session) { container.innerHTML = '<div class="missions-empty">Nothing selected.</div>'; return; }
  const workspace = state.workspaces.find((w) => w.id === session.workspace_id);
  const running = ['running', 'waiting_approval'].includes(session.status);
  const failed = session.status === 'failed';
  const finished = ['completed', 'rolled_back'].includes(session.status);
  const opts = modelOptions();
  container.innerHTML = `
    <div class="missions-detail-head">
      <div>
        <h3>${esc((session.task || '').slice(0, 120))}</h3>
        <small>status: <strong>${esc(session.status)}</strong>
          · ${esc(session.workspace_name || session.workspace_id)}
          · model: ${esc((session.model || {}).model || '')}</small>
      </div>
      <div class="missions-detail-actions">
        ${running ? '<button type="button" class="missions-btn small danger" data-act="session-stop">Stop</button>' : ''}
        ${['stopped', 'failed'].includes(session.status) ? '<button type="button" class="missions-btn small primary" data-act="session-resume">Resume</button>' : ''}
        ${!running && session.checkpoint ? '<button type="button" class="missions-btn small danger" data-act="session-rollback">Roll back</button>' : ''}
      </div>
    </div>
    ${workspace?.mode === 'unattended' ? `
      <div class="missions-unattended">
        <span>● UNATTENDED — Shadow acts in <strong>${esc(workspace.name)}</strong> without approval prompts</span>
        <button type="button" class="missions-btn small" data-unattended-off="${esc(workspace.id)}">Disable</button>
      </div>` : ''}
    ${failed ? `
      <div class="missions-provider-error">
        <strong>✘ ${esc(session.error || 'Session failed')}</strong>
        ${session.provider_error ? `<small>${esc(session.provider_error)}</small>` : ''}
        <div class="missions-clarify-row">
          <select id="session-retry-model">${opts.map((o) => `<option value="${esc(o.id)}">${esc(o.label)}</option>`).join('')}</select>
          <button type="button" class="missions-btn small primary" data-act="session-retry">Retry with this model</button>
        </div>
      </div>` : ''}
    ${renderSessionApprovals(session)}
    <div class="missions-session-grid">
      <div>
        <h5>Activity</h5>
        ${renderSessionTimeline(session)}
      </div>
      <div>
        <h5>Terminal</h5>
        ${renderSessionTerminal(session)}
      </div>
    </div>
    <h5>Files changed (${(session.files_changed || []).length})</h5>
    <div class="missions-chips">${(session.files_changed || []).map((f) =>
      `<span class="missions-chip">${esc(f)} <button type="button" data-session-revert="${esc(f)}" title="Revert this file">↩</button></span>`).join('') || 'none yet'}</div>
    <div class="missions-clarify-row" style="margin-top:6px">
      <button type="button" class="missions-btn small" data-act="session-diff">View diff</button>
    </div>
    <div id="agent-session-diff"></div>
    ${session.report ? `<h5>Report</h5><div class="missions-report">${esc(session.report).replace(/\n/g, '<br/>')}</div>` : ''}
    <h5>Follow-up</h5>
    <div class="missions-clarify-row">
      <input type="text" id="session-followup" placeholder="${finished ? 'Send a follow-up task to continue this session' : 'Message the agent (queued between steps)'}" />
      <button type="button" class="missions-btn small primary" data-act="session-send">Send</button>
    </div>`;
}

// ── mission detail ─────────────────────────────────────────────────────

async function openMission(missionId) {
  try {
    state.activeMission = await request(`/${encodeURIComponent(missionId)}`);
    state.activeSession = null;
    state.activeWorkspace = null;
    state.tab = state.tab === 'explorer' ? 'overview' : state.tab;
    renderMission();
  } catch (error) {
    toast(error.message, true);
  }
}

function taskBadge(status) {
  return `<span class="missions-badge" data-kind="${esc(status)}">${esc(status)}</span>`;
}

function renderGraph(mission) {
  // Dependency-aware columns: tasks grouped by topological depth.
  const tasks = mission.tasks || [];
  const depth = {};
  const byId = Object.fromEntries(tasks.map((t) => [t.id, t]));
  const compute = (id, seen = new Set()) => {
    if (depth[id] !== undefined) return depth[id];
    if (seen.has(id)) return 0;
    seen.add(id);
    const deps = byId[id]?.depends_on || [];
    depth[id] = deps.length ? 1 + Math.max(...deps.map((d) => compute(d, seen))) : 0;
    return depth[id];
  };
  tasks.forEach((t) => compute(t.id));
  const levels = [];
  tasks.forEach((t) => {
    (levels[depth[t.id]] = levels[depth[t.id]] || []).push(t);
  });
  return `<div class="missions-graph">${levels.map((level) => `
    <div class="missions-graph-col">${level.map((t) => `
      <div class="missions-node" data-status="${esc(t.status)}" title="${esc(t.goal || '')}">
        <span class="missions-node-role">${esc(t.role)}</span>
        <strong>${esc(t.title)}</strong>
        ${t.depends_on?.length ? `<small>← ${t.depends_on.map(esc).join(', ')}</small>` : ''}
        ${taskBadge(t.status)}${t.attempts > 1 ? ` <small>retry ${t.attempts - 1}</small>` : ''}
      </div>`).join('')}
    </div>`).join('<div class="missions-graph-arrow">→</div>')}</div>`;
}

function renderUsage(mission) {
  const usage = mission.usage || {};
  const rows = Object.entries(usage.by_model || {}).map(([model, u]) => `
    <tr><td>${esc(model)}</td><td>${u.calls}</td>
        <td>~${u.est_input_tokens || 0} / ~${u.est_output_tokens || 0}</td>
        <td>${u.seconds || 0}s</td></tr>`).join('');
  return `
    <p>LLM calls: ${usage.llm_calls || 0} · agent actions: ${usage.actions || 0}
       <small>(token counts are estimates — chars/4)</small></p>
    ${rows ? `<table class="missions-table"><tr><th>Model</th><th>Calls</th><th>Tokens in/out (est.)</th><th>Time</th></tr>${rows}</table>` : ''}`;
}

function renderApprovals(mission) {
  const pending = (mission.approvals || []).filter((a) => a.status === 'pending');
  const resolved = (mission.approvals || []).filter((a) => a.status !== 'pending');
  const card = (a) => `
    <div class="missions-approval" data-risk="${esc(a.risk)}">
      <div>
        <strong>${esc(a.summary)}</strong>
        <small>${esc(a.reason)} · capability: ${esc(a.capability)} · risk: ${esc(a.risk)}</small>
        ${a.args?.command ? `<code>${esc(a.args.command)}</code>` : ''}
      </div>
      <div class="missions-approval-actions">
        <button type="button" class="missions-btn small" data-approve="${a.id}|allow_once">Allow once</button>
        <button type="button" class="missions-btn small" data-approve="${a.id}|allow_mission">Allow for this mission</button>
        <button type="button" class="missions-btn small" data-approve="${a.id}|allow_always">Always allow in this workspace</button>
        <button type="button" class="missions-btn small danger" data-approve="${a.id}|decline">Decline</button>
        <button type="button" class="missions-btn small danger" data-approve="${a.id}|stop_mission">Stop mission</button>
      </div>
    </div>`;
  return `
    ${pending.length ? pending.map(card).join('') : '<div class="missions-empty">No pending approvals.</div>'}
    ${resolved.length ? `<h5>Resolved</h5>${resolved.slice(-10).reverse().map((a) =>
      `<div class="missions-row"><div><strong>${esc(a.summary)}</strong><small>${esc(a.status)}${a.scope ? ` (${esc(a.scope)})` : ''}</small></div></div>`).join('')}` : ''}`;
}

function renderDiffText(diff) {
  return `<pre class="missions-diff">${(diff || '').split('\n').map((line) => {
    let kind = '';
    if (line.startsWith('+') && !line.startsWith('+++')) kind = 'add';
    else if (line.startsWith('-') && !line.startsWith('---')) kind = 'del';
    else if (line.startsWith('@@')) kind = 'hunk';
    else if (line.startsWith('diff ') || line.startsWith('+++') || line.startsWith('---')) kind = 'meta';
    return `<span class="missions-diff-line" data-kind="${kind}">${esc(line)}</span>`;
  }).join('\n')}</pre>`;
}

function renderMission() {
  const mission = state.activeMission;
  const container = root.querySelector('#missions-detail');
  if (!container) return;
  if (!mission) { container.innerHTML = '<div class="missions-empty">Nothing selected.</div>'; return; }
  const tabs = ['overview', 'tasks', 'approvals', 'files', 'logs', 'report'];
  const pendingCount = (mission.approvals || []).filter((a) => a.status === 'pending').length;
  const running = ['running', 'paused_approval'].includes(mission.status);
  container.innerHTML = `
    <div class="missions-detail-head">
      <div>
        <h3>${esc(mission.goal.slice(0, 120))}</h3>
        <small>status: <strong>${esc(mission.status)}</strong> · mode: ${esc(mission.mode)}
          ${mission.allow_network ? ' · network allowed' : ''} · workspace ${esc(mission.workspace_id)}</small>
      </div>
      <div class="missions-detail-actions">
        <button type="button" class="missions-btn small" data-act="mission-mode">Permissions</button>
        ${mission.status === 'ready' || mission.status === 'created' ? '<button type="button" class="missions-btn small primary" data-act="mission-start">Start</button>' : ''}
        ${running ? '<button type="button" class="missions-btn small" data-act="mission-pause">Pause</button>' : ''}
        ${mission.status === 'paused' ? '<button type="button" class="missions-btn small primary" data-act="mission-resume">Resume</button>' : ''}
        ${running || mission.status === 'paused' ? '<button type="button" class="missions-btn small danger" data-act="mission-stop">Stop</button>' : ''}
        ${['completed', 'completed_with_failures', 'failed', 'cancelled'].includes(mission.status) && mission.checkpoint
          ? '<button type="button" class="missions-btn small danger" data-act="mission-rollback">Roll back mission</button>' : ''}
      </div>
    </div>
    ${mission.status === 'clarifying' ? `
      <div class="missions-clarify">
        <strong>Planner question:</strong> ${esc(mission.clarification || '')}
        <div class="missions-clarify-row">
          <input type="text" id="missions-clarify-answer" placeholder="Your answer" />
          <button type="button" class="missions-btn small primary" data-act="mission-clarify">Answer</button>
        </div>
      </div>` : ''}
    <nav class="missions-tabs">
      ${tabs.map((tab) => `<button type="button" data-tab="${tab}" data-selected="${state.tab === tab ? '1' : '0'}">
        ${tab}${tab === 'approvals' && pendingCount ? ` (${pendingCount})` : ''}</button>`).join('')}
    </nav>
    <div class="missions-tab-body" id="missions-tab-body">${renderTab(mission)}</div>`;
}

function renderTab(mission) {
  if (state.tab === 'overview') {
    return `
      ${renderGraph(mission)}
      <h5>Usage & cost</h5>${renderUsage(mission)}
      <h5>Files changed (${(mission.files_changed || []).length})</h5>
      <div class="missions-chips">${(mission.files_changed || []).map((f) =>
        `<span class="missions-chip">${esc(f)} <button type="button" data-revert-file="${esc(f)}" title="Revert this file">↩</button></span>`).join('') || 'none yet'}</div>
      <h5>Commands executed</h5>
      ${(mission.commands || []).slice(-12).reverse().map((c) => `<code class="missions-cmd">${esc(c.command)}</code>`).join('') || '<div class="missions-empty">none yet</div>'}`;
  }
  if (state.tab === 'tasks') {
    return (mission.tasks || []).map((t) => `
      <div class="missions-task">
        <div class="missions-task-head">
          <strong>${esc(t.id)} · ${esc(t.title)}</strong>
          <span>${esc(t.role)} ${taskBadge(t.status)} ${t.attempts ? `attempts: ${t.attempts}` : ''}</span>
        </div>
        <p>${esc(t.goal || '')}</p>
        ${t.result ? `<div class="missions-task-result">✔ ${esc(t.result)}</div>` : ''}
        ${t.error ? `<div class="missions-task-error">✘ ${esc(t.error)}</div>` : ''}
        ${(t.logs || []).slice(-6).map((l) => `<small class="missions-log">step ${l.step}: ${esc(l.action)} → ${esc(String(l.summary).slice(0, 150))}</small>`).join('')}
      </div>`).join('') || '<div class="missions-empty">No tasks planned.</div>';
  }
  if (state.tab === 'approvals') return renderApprovals(mission);
  if (state.tab === 'files') {
    return `
      <button type="button" class="missions-btn small" data-act="mission-diff">Load final diff</button>
      <div id="missions-mission-diff"></div>`;
  }
  if (state.tab === 'logs') {
    return `<div class="missions-events">${(mission.events || []).slice(-120).reverse().map((e) => `
      <div class="missions-event" data-kind="${esc(e.kind)}">
        <span>${new Date(e.ts * 1000).toLocaleTimeString()}</span>
        <strong>${esc(e.kind)}</strong>${e.task_id ? `<em>${esc(e.task_id)}</em>` : ''}
        <span>${esc(e.text)}</span>
      </div>`).join('')}</div>`;
  }
  if (state.tab === 'report') {
    return mission.report
      ? `<div class="missions-report">${esc(mission.report).replace(/\n/g, '<br/>')}</div>`
      : '<div class="missions-empty">The report is generated when the mission finishes.</div>';
  }
  return '';
}

// ── workspace explorer ─────────────────────────────────────────────────

async function wsAction(workspaceId, action, args = {}) {
  const payload = await request(`/workspaces/${encodeURIComponent(workspaceId)}/action`, {
    method: 'POST',
    body: JSON.stringify({ action, args }),
  });
  if (payload.approval_required) {
    const granted = await inlineApprovalFlow(workspaceId, payload);
    if (granted) return wsAction(workspaceId, action, args);
    throw new Error(`Not approved: ${payload.summary}`);
  }
  return payload.result;
}

async function inlineApprovalFlow(workspaceId, info) {
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'missions-modal-overlay';
    overlay.innerHTML = `
      <div class="missions-modal" role="dialog">
        <h3>Approval required</h3>
        <p><strong>${esc(info.summary)}</strong></p>
        <p class="missions-modal-hint">${esc(info.reason)} · risk: ${esc(info.risk)}</p>
        <div class="missions-modal-actions missions-approval-actions">
          <button type="button" class="missions-btn" data-grant="session">Allow for this session</button>
          <button type="button" class="missions-btn" data-grant="workspace">Always allow in this workspace</button>
          <button type="button" class="missions-btn danger" data-grant="">Decline</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', async (event) => {
      const button = event.target.closest('[data-grant]');
      if (!button && event.target !== overlay) return;
      const scope = button?.dataset.grant || '';
      overlay.remove();
      if (!scope) { resolve(false); return; }
      try {
        await request('/policy/grants', {
          method: 'POST',
          body: JSON.stringify({ grant_key: info.grant_key, scope, workspace_id: workspaceId, summary: info.summary }),
        });
        resolve(true);
      } catch (error) {
        toast(error.message, true);
        resolve(false);
      }
    });
  });
}

async function openWorkspace(workspaceId) {
  state.activeWorkspace = workspaceId;
  state.activeMission = null;
  state.activeSession = null;
  state.openFile = null;
  state.tab = 'explorer';
  const container = root.querySelector('#missions-detail');
  container.innerHTML = '<div class="missions-empty">Loading workspace…</div>';
  try {
    const [tree, git] = await Promise.all([
      wsAction(workspaceId, 'ws_tree', { depth: 3, limit: 400 }),
      wsAction(workspaceId, 'git_info', {}).catch(() => null),
    ]);
    state.tree = tree;
    state.git = git;
    renderExplorer();
  } catch (error) {
    container.innerHTML = `<div class="missions-empty">Workspace unavailable: ${esc(error.message)}</div>`;
  }
}

function renderTreeNodes(entries) {
  return `<ul class="missions-tree">${(entries || []).map((entry) => `
    <li data-type="${esc(entry.type)}">
      ${entry.type === 'dir'
        ? `<details><summary>${esc(entry.name)}/</summary>${entry.children ? renderTreeNodes(entry.children) : ''}</details>`
        : `<button type="button" class="missions-file" data-open-file="${esc(entry.path)}">${esc(entry.name)}</button>`}
    </li>`).join('')}</ul>`;
}

function renderExplorer() {
  const workspace = state.workspaces.find((w) => w.id === state.activeWorkspace);
  const container = root.querySelector('#missions-detail');
  if (!workspace || !container) return;
  const git = state.git;
  container.innerHTML = `
    <div class="missions-detail-head">
      <div>
        <h3>${esc(workspace.name)}</h3>
        <small>${esc(workspace.root)} on ${esc(workspace.device_name || workspace.device_id)} · mode: ${esc(workspace.mode)}</small>
      </div>
      <div class="missions-detail-actions">
        <button type="button" class="missions-btn small" data-act="ws-mode">Permissions</button>
        <button type="button" class="missions-btn small" data-act="ws-refresh">Refresh</button>
        <button type="button" class="missions-btn small" data-act="ws-new-file">New file</button>
        <button type="button" class="missions-btn small" data-act="ws-upload">Upload</button>
        <button type="button" class="missions-btn small" data-act="ws-run">Run command</button>
      </div>
    </div>
    ${git?.is_repo ? `
      <div class="missions-git">git: <strong>${esc(git.branch)}</strong>${git.dirty ? ' · dirty' : ' · clean'}
        · ${(git.branches || []).length} branches
        <button type="button" class="missions-btn small" data-act="ws-git-log">History</button>
        <button type="button" class="missions-btn small" data-act="ws-git-diff">Diff</button>
        <button type="button" class="missions-btn small" data-act="ws-git-commit">Commit…</button>
        ${git.status?.length ? `<details><summary>${git.status.length} changed</summary><pre>${esc(git.status.join('\n'))}</pre></details>` : ''}
      </div>` : ''}
    <div class="missions-explorer">
      <div class="missions-explorer-side">
        <input type="text" id="missions-search" placeholder="Search content… (Enter)" />
        <label class="missions-checkbox"><input type="checkbox" id="missions-search-names" /> filenames only</label>
        <div id="missions-search-results"></div>
        ${renderTreeNodes(state.tree?.entries)}
      </div>
      <div class="missions-explorer-main" id="missions-editor-pane">
        <div class="missions-empty">Open a file from the tree.</div>
      </div>
    </div>
    <div id="missions-ws-output"></div>`;
}

async function openFile(path) {
  try {
    const file = await wsAction(state.activeWorkspace, 'ws_read', { path });
    state.openFile = { path, text: file.text, sha256: file.sha256, binary: file.binary };
    const pane = root.querySelector('#missions-editor-pane');
    if (!pane) return;
    if (file.binary) {
      pane.innerHTML = `<div class="missions-empty">${esc(path)} is binary (${file.size} bytes).
        <button type="button" class="missions-btn small" data-act="file-download">Download</button></div>`;
      return;
    }
    pane.innerHTML = `
      <div class="missions-editor-head">
        <strong>${esc(path)}</strong>${file.truncated ? ' <small>(truncated)</small>' : ''}
        <span class="missions-editor-actions">
          <button type="button" class="missions-btn small" data-act="file-diff">Diff</button>
          <button type="button" class="missions-btn small primary" data-act="file-save">Save</button>
          <button type="button" class="missions-btn small" data-act="file-download">Download</button>
          <button type="button" class="missions-btn small" data-act="file-rename">Rename</button>
          <button type="button" class="missions-btn small danger" data-act="file-delete">Delete</button>
        </span>
      </div>
      <textarea id="missions-editor" spellcheck="false">${esc(file.text)}</textarea>
      <div id="missions-file-diff"></div>`;
  } catch (error) {
    toast(error.message, true);
  }
}

async function saveOpenFile() {
  const editor = root.querySelector('#missions-editor');
  const file = state.openFile;
  if (!editor || !file) return;
  try {
    const result = await wsAction(state.activeWorkspace, 'ws_write', {
      path: file.path, text: editor.value, expect_sha256: file.sha256,
    });
    file.sha256 = result.sha256;
    file.text = editor.value;
    toast('Saved');
  } catch (error) {
    if (/Conflict/.test(error.message)) {
      toast('Conflict: the file changed on disk. Review the diff before saving.', true);
      await showFileDiff();
    } else toast(error.message, true);
  }
}

async function showFileDiff() {
  const editor = root.querySelector('#missions-editor');
  const file = state.openFile;
  if (!editor || !file) return;
  try {
    const result = await wsAction(state.activeWorkspace, 'ws_diff', { path: file.path, text: editor.value });
    const target = root.querySelector('#missions-file-diff');
    target.innerHTML = result.diff
      ? `${renderDiffText(result.diff)}
         <div class="missions-modal-actions">
           <button type="button" class="missions-btn small primary" data-act="file-save">Accept (save)</button>
           <button type="button" class="missions-btn small danger" data-act="file-discard">Reject (reload from disk)</button>
         </div>`
      : '<div class="missions-empty">No changes.</div>';
  } catch (error) {
    toast(error.message, true);
  }
}

async function runCommandFlow() {
  const command = prompt('Command to run in the workspace root:');
  if (!command) return;
  const output = root.querySelector('#missions-ws-output');
  output.innerHTML = '<div class="missions-empty">Running…</div>';
  try {
    const result = await wsAction(state.activeWorkspace, 'ws_run', { command, timeout: 300 });
    output.innerHTML = `
      <h5>$ ${esc(command)} <small>(exit ${result.returncode}, ${result.seconds}s)</small></h5>
      <pre class="missions-output">${esc(result.stdout || '')}${result.stderr ? `\n--- stderr ---\n${esc(result.stderr)}` : ''}</pre>`;
  } catch (error) {
    output.innerHTML = `<div class="missions-empty">✘ ${esc(error.message)}</div>`;
  }
}

// ── persistent rules modal ─────────────────────────────────────────────

async function openRulesModal() {
  const data = await request('/policy/rules');
  const overlay = document.createElement('div');
  overlay.className = 'missions-modal-overlay';
  overlay.innerHTML = `
    <div class="missions-modal" role="dialog">
      <h3>Persistent permission rules</h3>
      <p class="missions-modal-hint">"Always allow" decisions per workspace. Revoking takes effect immediately.</p>
      ${(data.rules || []).map((rule) => `
        <div class="missions-row">
          <div><strong>${esc(rule.summary || rule.grant_key)}</strong>
            <small>workspace ${esc(rule.workspace_id)} · ${esc(rule.grant_key)}</small></div>
          <button type="button" class="missions-btn small danger" data-rule-revoke="${esc(rule.id)}">Revoke</button>
        </div>`).join('') || '<div class="missions-empty">No persistent rules.</div>'}
      <div class="missions-modal-actions"><button type="button" class="missions-btn" data-close-modal>Close</button></div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.addEventListener('click', async (event) => {
    const revoke = event.target.closest('[data-rule-revoke]');
    if (revoke) {
      await request(`/policy/rules/${encodeURIComponent(revoke.dataset.ruleRevoke)}`, { method: 'DELETE' });
      revoke.closest('.missions-row').remove();
      toast('Rule revoked');
    }
    if (event.target.closest('[data-close-modal]') || event.target === overlay) overlay.remove();
  });
}

// ── event wiring ───────────────────────────────────────────────────────

function bindEvents() {
  root.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && event.target?.id === 'missions-search') {
      event.preventDefault();
      runSearch();
    }
    if (event.key === 'Enter' && event.target?.id === 'session-followup') {
      event.preventDefault();
      root.querySelector('[data-act="session-send"]')?.click();
    }
  });
  root.addEventListener('change', (event) => {
    if (event.target?.id === 'agent-workspace') updateAgentModeLabel();
  });
  root.addEventListener('click', async (event) => {
    const target = event.target.closest('button');
    if (!target) return;
    const act = target.dataset.act;
    const mission = state.activeMission;

    try {
      if (target.dataset.openMission) return openMission(target.dataset.openMission);
      if (target.dataset.openSession) return openSession(target.dataset.openSession);
      if (target.dataset.openWorkspace) return openWorkspace(target.dataset.openWorkspace);
      if (target.dataset.unattendedOff) {
        await request(`/workspaces/${encodeURIComponent(target.dataset.unattendedOff)}/mode`, {
          method: 'PUT', body: JSON.stringify({ mode: 'auto' }),
        });
        toast('Unattended mode disabled');
        await refreshWorkspaces();
        if (state.activeSession) renderSession();
        return;
      }
      if (target.dataset.sessionApprove && state.activeSession) {
        const [approvalId, decision] = target.dataset.sessionApprove.split('|');
        await request(`/sessions/${state.activeSession.id}/approvals/${approvalId}`, {
          method: 'POST', body: JSON.stringify({ decision }),
        });
        toast(`Approval: ${decision.replaceAll('_', ' ')}`);
        return openSession(state.activeSession.id);
      }
      if (target.dataset.sessionRevert && state.activeSession) {
        if (!confirm(`Revert ${target.dataset.sessionRevert} to its pre-session state?`)) return;
        await request(`/sessions/${state.activeSession.id}/rollback`, {
          method: 'POST', body: JSON.stringify({ paths: [target.dataset.sessionRevert] }),
        });
        toast('File reverted');
        return openSession(state.activeSession.id);
      }
      if (target.dataset.workspaceRemove) {
        if (!confirm('Revoke this workspace authorization?')) return;
        await request(`/workspaces/${encodeURIComponent(target.dataset.workspaceRemove)}`, { method: 'DELETE' });
        return refreshWorkspaces();
      }
      if (target.dataset.workspaceMode) {
        const ws = state.workspaces.find((w) => w.id === target.dataset.workspaceMode);
        return openModeSelector({
          current: ws?.mode || 'auto',
          deviceId: ws?.device_id,
          onPick: async (mode) => {
            await request(`/workspaces/${encodeURIComponent(ws.id)}/mode`, {
              method: 'PUT', body: JSON.stringify({ mode }),
            });
            toast(`Workspace mode: ${mode}`);
            refreshWorkspaces();
          },
        });
      }
      if (target.dataset.openFile) return openFile(target.dataset.openFile);
      if (target.dataset.revertFile && mission) {
        if (!confirm(`Revert ${target.dataset.revertFile} to its pre-mission state?`)) return;
        await request(`/${mission.id}/rollback`, { method: 'POST', body: JSON.stringify({ paths: [target.dataset.revertFile] }) });
        toast('File reverted');
        return openMission(mission.id);
      }
      if (target.dataset.approve && mission) {
        const [approvalId, decision] = target.dataset.approve.split('|');
        await request(`/${mission.id}/approvals/${approvalId}`, {
          method: 'POST', body: JSON.stringify({ decision }),
        });
        toast(`Approval: ${decision.replaceAll('_', ' ')}`);
        return openMission(mission.id);
      }
      if (target.dataset.tab) { state.tab = target.dataset.tab; return renderMission(); }

      switch (act) {
        case 'close': return closePage();
        case 'refresh': return bootstrapPage();
        case 'rules': return openRulesModal();
        case 'agent-run': return runAgentSession();
        case 'agent-ws-mode': {
          const ws = state.workspaces.find((w) => w.id === root.querySelector('#agent-workspace')?.value);
          if (!ws) return toast('Authorize a workspace first', true);
          return openModeSelector({
            current: ws.mode, deviceId: ws.device_id,
            onPick: async (mode) => {
              await request(`/workspaces/${encodeURIComponent(ws.id)}/mode`, {
                method: 'PUT', body: JSON.stringify({ mode }),
              });
              toast(`Workspace mode: ${mode}`);
              await refreshWorkspaces();
            },
          });
        }
        case 'session-stop': {
          await request(`/sessions/${state.activeSession.id}/stop`, { method: 'POST' });
          return openSession(state.activeSession.id);
        }
        case 'session-resume': {
          await request(`/sessions/${state.activeSession.id}/resume`, { method: 'POST' });
          return openSession(state.activeSession.id);
        }
        case 'session-retry': {
          const pick = root.querySelector('#session-retry-model')?.value || '';
          const [endpointId, ...modelParts] = pick.split('|');
          await request(`/sessions/${state.activeSession.id}/retry`, {
            method: 'POST',
            body: JSON.stringify(pick ? { endpoint_id: endpointId, model: modelParts.join('|') } : {}),
          });
          toast('Retrying…');
          return openSession(state.activeSession.id);
        }
        case 'session-rollback': {
          if (!confirm('Roll back ALL files this session changed?')) return;
          await request(`/sessions/${state.activeSession.id}/rollback`, {
            method: 'POST', body: JSON.stringify({}),
          });
          toast('Session rolled back');
          return openSession(state.activeSession.id);
        }
        case 'session-send': {
          const input = root.querySelector('#session-followup');
          const text = input?.value?.trim();
          if (!text) return;
          await request(`/sessions/${state.activeSession.id}/message`, {
            method: 'POST', body: JSON.stringify({ text }),
          });
          input.value = '';
          toast('Message sent');
          return openSession(state.activeSession.id);
        }
        case 'session-diff': {
          const out = root.querySelector('#agent-session-diff');
          out.innerHTML = '<div class="missions-empty">Loading diff…</div>';
          const diff = await wsAction(state.activeSession.workspace_id, 'git_diff', {});
          out.innerHTML = diff.diff ? renderDiffText(diff.diff) : '<div class="missions-empty">No uncommitted diff.</div>';
          return;
        }
        case 'permissions': {
          const ws = state.workspaces.find((w) => w.id === (state.activeWorkspace || mission?.workspace_id)) || state.workspaces[0];
          if (!ws) return toast('Authorize a workspace first', true);
          return openModeSelector({
            current: ws.mode, deviceId: ws.device_id,
            onPick: async (mode) => {
              await request(`/workspaces/${ws.id}/mode`, { method: 'PUT', body: JSON.stringify({ mode }) });
              toast(`Mode: ${mode}`);
              refreshWorkspaces();
            },
          });
        }
        case 'add-workspace': return addWorkspaceFlow();
        case 'composer-mode': {
          const ws = state.workspaces.find((w) => w.id === composerWorkspace) || state.workspaces[0];
          return openModeSelector({
            current: composerMode, deviceId: ws?.device_id,
            onPick: (mode) => {
              composerMode = mode;
              root.querySelector('#missions-mode-label').textContent = mode;
            },
          });
        }
        case 'create-mission': return createMission();
        case 'mission-start': await request(`/${mission.id}/start`, { method: 'POST' }); return openMission(mission.id);
        case 'mission-pause': await request(`/${mission.id}/pause`, { method: 'POST' }); return openMission(mission.id);
        case 'mission-resume': await request(`/${mission.id}/resume`, { method: 'POST' }); return openMission(mission.id);
        case 'mission-stop':
          if (!confirm('Emergency-stop this mission?')) return;
          await request(`/${mission.id}/stop`, { method: 'POST' });
          return openMission(mission.id);
        case 'mission-rollback':
          if (!confirm('Roll back ALL files this mission changed?')) return;
          await request(`/${mission.id}/rollback`, { method: 'POST', body: JSON.stringify({}) });
          toast('Mission rolled back');
          return openMission(mission.id);
        case 'mission-clarify': {
          const answer = root.querySelector('#missions-clarify-answer')?.value?.trim();
          if (!answer) return;
          state.activeMission = await request(`/${mission.id}/clarify`, {
            method: 'POST', body: JSON.stringify({ answer }),
          });
          return renderMission();
        }
        case 'mission-mode':
          return openModeSelector({
            current: mission.mode,
            deviceId: mission.device_id,
            onPick: () => toast('Mode applies to new missions; stop and recreate to change a running one.', true),
          });
        case 'mission-diff': {
          const out = root.querySelector('#missions-mission-diff');
          out.innerHTML = '<div class="missions-empty">Loading diff…</div>';
          const diff = await wsAction(mission.workspace_id, 'git_diff', {});
          out.innerHTML = diff.diff ? renderDiffText(diff.diff) : '<div class="missions-empty">No uncommitted diff.</div>';
          return;
        }
        case 'ws-mode': {
          const ws = state.workspaces.find((w) => w.id === state.activeWorkspace);
          return openModeSelector({
            current: ws.mode, deviceId: ws.device_id,
            onPick: async (mode) => {
              await request(`/workspaces/${ws.id}/mode`, { method: 'PUT', body: JSON.stringify({ mode }) });
              ws.mode = mode;
              renderExplorer();
            },
          });
        }
        case 'ws-refresh': return openWorkspace(state.activeWorkspace);
        case 'ws-new-file': {
          const path = prompt('New file path (relative to workspace root):');
          if (!path) return;
          await wsAction(state.activeWorkspace, 'ws_write', { path, text: '' });
          return openWorkspace(state.activeWorkspace);
        }
        case 'ws-upload': {
          const input = document.createElement('input');
          input.type = 'file';
          input.onchange = async () => {
            const picked = input.files?.[0];
            if (!picked) return;
            if (picked.size > 1_500_000) return toast('Uploads are limited to 1.5 MB text files', true);
            const text = await picked.text();
            await wsAction(state.activeWorkspace, 'ws_write', { path: picked.name, text });
            toast(`Uploaded ${picked.name}`);
            openWorkspace(state.activeWorkspace);
          };
          return input.click();
        }
        case 'ws-run': return runCommandFlow();
        case 'ws-git-log': {
          const log = await wsAction(state.activeWorkspace, 'git_log', { limit: 30 });
          const out = root.querySelector('#missions-ws-output');
          out.innerHTML = `<h5>History</h5>${(log.commits || []).map((c) => `
            <div class="missions-row"><div><strong>${esc(c.subject)}</strong>
              <small>${esc(c.hash.slice(0, 10))} · ${esc(c.author)} · ${new Date(c.time * 1000).toLocaleString()}</small></div></div>`).join('')}`;
          return;
        }
        case 'ws-git-diff': {
          const diff = await wsAction(state.activeWorkspace, 'git_diff', {});
          root.querySelector('#missions-ws-output').innerHTML =
            diff.diff ? renderDiffText(diff.diff) : '<div class="missions-empty">Working tree is clean.</div>';
          return;
        }
        case 'ws-git-commit': {
          const message = prompt('Commit message:');
          if (!message) return;
          const result = await wsAction(state.activeWorkspace, 'git_commit', { message });
          toast(`Committed ${String(result.head).slice(0, 10)}`);
          return openWorkspace(state.activeWorkspace);
        }
        case 'file-save': return saveOpenFile();
        case 'file-diff': return showFileDiff();
        case 'file-discard': return openFile(state.openFile.path);
        case 'file-download': {
          const file = state.openFile;
          const blob = new Blob([root.querySelector('#missions-editor')?.value ?? file.text ?? ''], { type: 'text/plain' });
          const a = document.createElement('a');
          a.href = URL.createObjectURL(blob);
          a.download = file.path.split('/').pop();
          a.click();
          URL.revokeObjectURL(a.href);
          return;
        }
        case 'file-rename': {
          const to = prompt('New path:', state.openFile.path);
          if (!to || to === state.openFile.path) return;
          await wsAction(state.activeWorkspace, 'ws_rename', { path: state.openFile.path, to });
          toast('Renamed');
          return openWorkspace(state.activeWorkspace);
        }
        case 'file-delete': {
          if (!confirm(`Move ${state.openFile.path} to the workspace trash?`)) return;
          await wsAction(state.activeWorkspace, 'ws_delete', { path: state.openFile.path });
          toast('Moved to trash');
          return openWorkspace(state.activeWorkspace);
        }
        default:
      }
    } catch (error) {
      toast(error.message, true);
    }
  });
}

async function runSearch() {
  const box = root.querySelector('#missions-search');
  const namesOnly = root.querySelector('#missions-search-names')?.checked;
  const results = root.querySelector('#missions-search-results');
  if (!box?.value.trim()) { results.innerHTML = ''; return; }
  results.innerHTML = '<div class="missions-empty">Searching…</div>';
  try {
    const found = await wsAction(state.activeWorkspace, 'ws_search', {
      query: box.value.trim(), mode: namesOnly ? 'name' : 'content', limit: 40,
    });
    results.innerHTML = (found.results || []).map((r) => `
      <button type="button" class="missions-file" data-open-file="${esc(r.path)}">
        ${esc(r.path)}${r.line ? `:${r.line}` : ''}</button>
      ${r.text ? `<small class="missions-log">${esc(r.text)}</small>` : ''}`).join('')
      || '<div class="missions-empty">No matches.</div>';
  } catch (error) {
    results.innerHTML = `<div class="missions-empty">${esc(error.message)}</div>`;
  }
}

// ── lifecycle ──────────────────────────────────────────────────────────

async function bootstrapPage() {
  stopTimers();
  try {
    await Promise.all([refreshWorkspaces(), refreshMissions(), refreshSessions(), refreshModels()]);
  } catch (error) {
    toast(error.message, true);
  }
  startTimers();
}

function startTimers() {
  stopTimers();
  refreshTimer = setInterval(async () => {
    if (document.hidden || !root || root.hidden) return;
    try {
      await refreshMissions();
      await refreshSessions();
      const mission = state.activeMission;
      if (mission && ['running', 'paused_approval', 'planning'].includes(mission.status)) {
        state.activeMission = await request(`/${mission.id}`);
        renderMission();
      }
      const session = state.activeSession;
      if (session && ['running', 'waiting_approval', 'created'].includes(session.status)) {
        const followup = root.querySelector('#session-followup');
        const draft = followup?.value || '';
        state.activeSession = await request(`/sessions/${session.id}`);
        renderSession();
        const restored = root.querySelector('#session-followup');
        if (restored && draft) restored.value = draft;
      }
      renderFullAccessBanner();
    } catch (_) { /* transient */ }
  }, REFRESH_MS);
}

function stopTimers() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = null;
}

function ensureRoot() {
  ensureStyles();
  if (!root) {
    root = document.getElementById('missions-page') || buildShell();
    bindEvents();
  }
  return root;
}

function openPage(options = {}) {
  const push = options.push !== false;
  const node = ensureRoot();
  node.hidden = false;
  document.body.classList.add('missions-page-open');
  if (push) navTo('/missions');
  bootstrapPage();
}

function closePage() {
  if (root) root.hidden = true;
  document.body.classList.remove('missions-page-open');
  stopTimers();
  closeModeSelector();
  if (window.location.pathname === '/missions') navTo('/');
}

function init() {
  document.getElementById('tool-missions-btn')?.addEventListener('click', () => openPage());
  window.addEventListener('popstate', () => {
    if (window.location.pathname === '/missions') openPage({ push: false });
    else if (root && !root.hidden) closePage();
  });
}

export default { init, openPage, closePage };
