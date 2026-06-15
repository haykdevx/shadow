// "Computer" mode for the main chat: lets the AI read/write files, run
// commands, and use git on an authorized folder on one of the user's
// devices — directly from a normal chat conversation. Built on top of the
// already-tested Direct Agent Sessions engine (src/agent_sessions.py) and
// the shared workspace-agent permission UI.
//
// Each chat conversation maps to at most one agent session (persisted in
// localStorage), so follow-up messages continue the same session instead of
// starting a new one.

import uiModule from './ui.js';
import {
  esc, request, toast, ensureWorkspaceAgentStyles,
  openModeSelector, renderFullAccessBanner, armFullAccessFlow,
} from './workspaceAgentShared.js';

const STORAGE_KEY = 'shadow.computerMode.v1';
const POLL_MS = 1800;
const RUNNING_STATUSES = ['running', 'waiting_approval'];

let state = {
  workspaceId: '',
  workspaces: [],
  fullAccess: {},
  chatSessions: {}, // chat session id -> agent session id
};

let toggleBtn = null;
let popoverEl = null;
const pollers = new Map(); // agent session id -> timeout id

// ── persistence ─────────────────────────────────────────────────────────

function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);
    if (saved && typeof saved === 'object') {
      state.workspaceId = saved.workspaceId || '';
      state.chatSessions = saved.chatSessions || {};
    }
  } catch (_) { /* ignore corrupt storage */ }
}

function saveState() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      workspaceId: state.workspaceId,
      chatSessions: state.chatSessions,
    }));
  } catch (_) { /* storage unavailable */ }
}

// ── public API ─────────────────────────────────────────────────────────

export async function init() {
  loadState();
  ensureWorkspaceAgentStyles();
  try { await refreshWorkspaces(); } catch (_) { /* shown on demand */ }
  syncToggleButton();
}

// Computer access is always on for every chat once a folder is authorized —
// there is no per-chat opt-in/out, matching a real desktop coding assistant
// that always has access to the project it's pointed at.
export function isEnabled() {
  return !!(state.workspaceId
    && state.workspaces.some((w) => w.id === state.workspaceId));
}

export function attachToggle(button) {
  toggleBtn = button;
  if (!toggleBtn) return;
  toggleBtn.addEventListener('click', (event) => {
    event.preventDefault();
    togglePopover(toggleBtn);
  });
  syncToggleButton();
}

function syncToggleButton() {
  if (!toggleBtn) return;
  const on = isEnabled();
  toggleBtn.classList.toggle('active', on);
  toggleBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
}

// ── workspaces ─────────────────────────────────────────────────────────

async function refreshWorkspaces() {
  const data = await request('/workspaces');
  state.workspaces = data.workspaces || [];
  state.fullAccess = {};
  for (const ws of state.workspaces) {
    if (ws.full_access) state.fullAccess[ws.device_id] = (Date.now() / 1000) + 600;
  }
  if (state.workspaceId && !state.workspaces.some((w) => w.id === state.workspaceId)) {
    state.workspaceId = '';
  }
  // Auto-select an authorized folder so Computer access just works without
  // a per-chat toggle. `list_workspaces` returns most-recently-created first.
  if (!state.workspaceId && state.workspaces.length) {
    state.workspaceId = state.workspaces[0].id;
  }
  saveState();
  syncToggleButton();
  renderFullAccessBanner(state.fullAccess);
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
    const name = prompt('Folder name:', folder.split(/[\\/]/).filter(Boolean).pop() || 'project') || '';
    const workspace = await request('/workspaces', {
      method: 'POST',
      body: JSON.stringify({ device_id: device.id, root: folder, name }),
    });
    toast('Folder authorized');
    await refreshWorkspaces();
    state.workspaceId = workspace.id;
    saveState();
    renderPopover();
  } catch (error) {
    toast(error.message, true);
  }
}

// ── settings popover ──────────────────────────────────────────────────

function togglePopover(anchorEl) {
  if (popoverEl) { closePopover(); return; }
  openPopover(anchorEl);
}

function openPopover(anchorEl) {
  ensureWorkspaceAgentStyles();
  popoverEl = document.createElement('div');
  popoverEl.className = 'computer-mode-popover';
  document.body.appendChild(popoverEl);
  renderPopover();
  positionPopover(anchorEl);
  refreshWorkspaces().then(renderPopover).catch(() => renderPopover());
  document.addEventListener('mousedown', onOutsideClick, true);
  document.addEventListener('keydown', onEscKey, true);
}

function closePopover() {
  if (!popoverEl) return;
  document.removeEventListener('mousedown', onOutsideClick, true);
  document.removeEventListener('keydown', onEscKey, true);
  popoverEl.remove();
  popoverEl = null;
}

function onOutsideClick(event) {
  if (!popoverEl) return;
  if (popoverEl.contains(event.target)) return;
  if (toggleBtn && toggleBtn.contains(event.target)) return;
  closePopover();
}

function onEscKey(event) {
  if (event.key === 'Escape') closePopover();
}

function positionPopover(anchorEl) {
  if (!popoverEl || !anchorEl) return;
  const rect = anchorEl.getBoundingClientRect();
  popoverEl.style.position = 'fixed';
  popoverEl.style.left = `${Math.max(8, rect.left)}px`;
  popoverEl.style.bottom = `${Math.max(8, window.innerHeight - rect.top + 8)}px`;
}

function renderPopover() {
  if (!popoverEl) return;
  const ws = state.workspaces.find((w) => w.id === state.workspaceId);
  const armedUntil = ws ? state.fullAccess[ws.device_id] : null;
  const armed = !!(armedUntil && armedUntil * 1000 > Date.now());
  popoverEl.innerHTML = `
    <div class="computer-mode-head">
      <strong>Computer access</strong>
      <button type="button" class="computer-mode-close" data-act="close" aria-label="Close">✕</button>
    </div>
    <p class="computer-mode-hint">
      Shadow can read and edit files, run commands, and use git on the folder below,
      on your device. This is always on for every chat — there is no per-conversation
      switch — following the permission level set here.
    </p>
    <label class="computer-mode-field">Folder
      <select id="computer-mode-workspace">
        <option value="">Select a folder…</option>
        ${state.workspaces.map((w) => `<option value="${esc(w.id)}" ${w.id === state.workspaceId ? 'selected' : ''}>${esc(w.name)} (${esc(w.device_name || w.device_id)})</option>`).join('')}
      </select>
    </label>
    <div class="computer-mode-actions">
      <button type="button" class="missions-btn small" data-act="add-folder">+ Authorize folder</button>
      <button type="button" class="missions-btn small" data-act="permissions" ${ws ? '' : 'disabled'}>
        Permissions${ws ? `: ${esc(ws.mode)}` : ''}
      </button>
      ${ws && ws.mode === 'full' ? `<button type="button" class="missions-btn small" data-act="arm-full">${armed ? 'Re-arm full access' : 'Arm full access'}</button>` : ''}
    </div>
    ${ws
      ? `<div class="computer-mode-always-on">Always on for <code>${esc(ws.root)}</code>${ws.mode === 'full' ? (armed ? ' — full access armed' : ' — <strong>full access not armed yet</strong>: actions will be denied until you arm it') : ''}.</div>`
      : '<div class="computer-mode-empty">Authorize a folder to enable Computer access.</div>'}
  `;

  popoverEl.querySelector('[data-act="close"]').addEventListener('click', closePopover);

  popoverEl.querySelector('#computer-mode-workspace').addEventListener('change', (event) => {
    state.workspaceId = event.target.value;
    saveState();
    syncToggleButton();
    renderPopover();
  });

  popoverEl.querySelector('[data-act="add-folder"]').addEventListener('click', addWorkspaceFlow);

  const permBtn = popoverEl.querySelector('[data-act="permissions"]');
  if (ws) {
    permBtn.addEventListener('click', () => {
      openModeSelector({
        current: ws.mode,
        deviceId: ws.device_id,
        onFullAccess: (deviceId, result) => {
          state.fullAccess[deviceId] = result.expires_at;
          renderFullAccessBanner(state.fullAccess);
          renderPopover();
        },
        onPick: async (mode) => {
          try {
            await request(`/workspaces/${encodeURIComponent(ws.id)}/mode`, {
              method: 'PUT', body: JSON.stringify({ mode }),
            });
            toast(`Permissions: ${mode}`);
            await refreshWorkspaces();
            renderPopover();
          } catch (error) {
            toast(error.message, true);
          }
        },
      });
    });
  }

  const armBtn = popoverEl.querySelector('[data-act="arm-full"]');
  if (armBtn && ws) {
    armBtn.addEventListener('click', async () => {
      const result = await armFullAccessFlow(ws.device_id);
      if (!result) return;
      state.fullAccess[ws.device_id] = result.expires_at;
      renderFullAccessBanner(state.fullAccess);
      renderPopover();
    });
  }
}

// ── status labels ─────────────────────────────────────────────────────

function statusLabel(status) {
  switch (status) {
    case 'running': return 'Working…';
    case 'waiting_approval': return 'Waiting for your approval';
    case 'completed': return 'Done';
    case 'failed': return 'Failed';
    case 'stopped': return 'Stopped';
    case 'rolled_back': return 'Rolled back';
    default: return status || 'Queued';
  }
}

function phaseLabel(phase) {
  switch (phase) {
    case 'thinking': return 'thinking';
    case 'tool_running': return 'running a tool';
    case 'waiting_for_user': return 'waiting for you';
    default: return '';
  }
}

// ── in-chat rendering ────────────────────────────────────────────────

function renderApprovals(session) {
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
        <button type="button" class="missions-btn small" data-approve="${esc(a.id)}|allow_once">Allow once</button>
        <button type="button" class="missions-btn small" data-approve="${esc(a.id)}|allow_session">Allow for this session</button>
        <button type="button" class="missions-btn small" data-approve="${esc(a.id)}|allow_always">Always allow in this folder</button>
        <button type="button" class="missions-btn small danger" data-approve="${esc(a.id)}|decline">Decline</button>
        <button type="button" class="missions-btn small danger" data-approve="${esc(a.id)}|stop_session">Stop</button>
      </div>
    </div>`).join('');
}

function renderActivity(session, open) {
  const events = (session.events || []).slice(-50);
  if (!events.length) return '';
  return `<details class="computer-mode-section" ${open ? 'open' : ''}>
    <summary>Activity (${events.length})</summary>
    <div class="missions-events">${events.slice().reverse().map((e) => `
      <div class="missions-event" data-kind="${esc(e.kind)}">
        <span>${new Date(e.ts * 1000).toLocaleTimeString()}</span>
        <strong>${esc(e.kind)}</strong>
        <span>${esc(e.text)}</span>
      </div>`).join('')}</div>
  </details>`;
}

function renderTerminal(session, open) {
  const blocks = (session.terminal || []).slice(-6);
  if (!blocks.length) return '';
  return `<details class="computer-mode-section" ${open ? 'open' : ''}>
    <summary>Terminal (${blocks.length})</summary>
    ${blocks.map((t) => `
      <div class="missions-terminal-block">
        <h5>$ ${esc(t.command)} <small>(exit ${t.returncode ?? '?'}, ${t.seconds ?? '?'}s)</small></h5>
        <pre class="missions-output">${esc(t.stdout || '')}${t.stderr ? `\n--- stderr ---\n${esc(t.stderr)}` : ''}</pre>
      </div>`).join('')}
  </details>`;
}

function renderFiles(session) {
  const files = session.files_changed || [];
  if (!files.length) return '';
  return `<div class="computer-mode-files">
    <small>Files changed:</small>
    <div class="missions-chips">${files.map((f) => `<span class="missions-chip">${esc(f)}</span>`).join('')}</div>
  </div>`;
}

function renderSessionBlock(container, session) {
  const running = RUNNING_STATUSES.includes(session.status);
  const phase = running ? phaseLabel(session.phase) : '';
  container.dataset.agentSession = session.id;
  container.dataset.status = session.status;
  container.innerHTML = `
    <div class="computer-mode-status">
      <span class="missions-badge" data-kind="${esc(session.status)}">${esc(statusLabel(session.status))}</span>
      ${phase ? `<span class="missions-phase">${esc(phase)}</span>` : ''}
      ${session.intent === 'read_only' ? '<span class="missions-badge" data-kind="read_only">Read-only</span>' : ''}
      <small>${esc(session.workspace_name || '')}</small>
      ${running ? '<button type="button" class="missions-btn small danger" data-act="stop">Stop</button>' : ''}
    </div>
    ${renderApprovals(session)}
    ${session.status === 'failed' && session.error ? `<div class="computer-mode-error">✘ ${esc(session.error)}</div>` : ''}
    ${renderActivity(session, running)}
    ${renderTerminal(session, false)}
    ${renderFiles(session)}
    ${session.report ? `<div class="missions-report computer-mode-report">${esc(session.report).replace(/\n/g, '<br/>')}</div>` : ''}
  `;
  wireBlockEvents(container, session);
}

function wireBlockEvents(container, session) {
  container.querySelectorAll('[data-approve]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const [approvalId, decision] = btn.dataset.approve.split('|');
      container.querySelectorAll('[data-approve]').forEach((b) => { b.disabled = true; });
      try {
        await request(`/sessions/${encodeURIComponent(session.id)}/approvals/${encodeURIComponent(approvalId)}`, {
          method: 'POST', body: JSON.stringify({ decision }),
        });
        const updated = await request(`/sessions/${encodeURIComponent(session.id)}`);
        renderSessionBlock(container, updated);
        if (RUNNING_STATUSES.includes(updated.status)) pollSession(container, session.id);
        uiModule.scrollHistory();
      } catch (error) {
        toast(error.message, true);
        container.querySelectorAll('[data-approve]').forEach((b) => { b.disabled = false; });
      }
    });
  });

  const stopBtn = container.querySelector('[data-act="stop"]');
  if (stopBtn) {
    stopBtn.addEventListener('click', async () => {
      stopBtn.disabled = true;
      try {
        await request(`/sessions/${encodeURIComponent(session.id)}/stop`, { method: 'POST' });
        const updated = await request(`/sessions/${encodeURIComponent(session.id)}`);
        renderSessionBlock(container, updated);
      } catch (error) {
        toast(error.message, true);
        stopBtn.disabled = false;
      }
    });
  }
}

function pollSession(container, agentSessionId) {
  const existing = pollers.get(agentSessionId);
  if (existing) clearTimeout(existing);
  const tick = async () => {
    if (!document.body.contains(container)) { pollers.delete(agentSessionId); return; }
    let session;
    try {
      session = await request(`/sessions/${encodeURIComponent(agentSessionId)}`);
    } catch (error) {
      pollers.delete(agentSessionId);
      renderErrorBlock(container, error.message);
      return;
    }
    renderSessionBlock(container, session);
    uiModule.scrollHistory();
    if (RUNNING_STATUSES.includes(session.status)) {
      pollers.set(agentSessionId, setTimeout(tick, POLL_MS));
    } else {
      pollers.delete(agentSessionId);
    }
  };
  pollers.set(agentSessionId, setTimeout(tick, POLL_MS));
}

function renderErrorBlock(container, message) {
  container.innerHTML = `<div class="computer-mode-error">✘ ${esc(message)}</div>`;
}

function createBlock() {
  const wrap = document.createElement('div');
  wrap.className = 'msg msg-ai';
  const role = document.createElement('div');
  role.className = 'role computer-mode-role';
  role.textContent = 'Computer';
  wrap.appendChild(role);
  const body = document.createElement('div');
  body.className = 'body computer-mode-body';
  body.innerHTML = '<div class="missions-empty">Starting…</div>';
  wrap.appendChild(body);
  const box = document.getElementById('chat-history');
  if (box) box.appendChild(wrap);
  return body;
}

async function fetchDefaultChat() {
  try {
    const res = await fetch('/api/default-chat');
    return await res.json();
  } catch (_) {
    return {};
  }
}

// Sends `text` through the active agent session for this chat conversation
// (creating one on first use). Returns true if Computer mode handled the
// message (caller should not fall through to the normal chat stream).
export async function send(chatSessionId, text) {
  if (!isEnabled()) return false;
  if (!chatSessionId) return false;

  const body = createBlock();
  uiModule.scrollHistory();

  try {
    let session;
    let agentSessionId = state.chatSessions[chatSessionId];
    if (agentSessionId) {
      try {
        await request(`/sessions/${encodeURIComponent(agentSessionId)}/message`, {
          method: 'POST', body: JSON.stringify({ text }),
        });
        session = await request(`/sessions/${encodeURIComponent(agentSessionId)}`);
      } catch (error) {
        // The remembered session may be gone (e.g. deleted workspace). Fall
        // back to starting a fresh one rather than leaving the user stuck.
        delete state.chatSessions[chatSessionId];
        agentSessionId = null;
      }
    }
    if (!agentSessionId) {
      const dc = await fetchDefaultChat();
      if (!dc.endpoint_id || !dc.model) {
        renderErrorBlock(body, 'No default model is configured. Pick a model in the chat composer first.');
        return true;
      }
      session = await request('/sessions', {
        method: 'POST',
        body: JSON.stringify({
          workspace_id: state.workspaceId,
          task: text,
          endpoint_id: dc.endpoint_id,
          model: dc.model,
        }),
      });
      agentSessionId = session.id;
      state.chatSessions[chatSessionId] = agentSessionId;
      saveState();
    }
    renderSessionBlock(body, session);
    uiModule.scrollHistory();
    if (RUNNING_STATUSES.includes(session.status)) pollSession(body, agentSessionId);
  } catch (error) {
    renderErrorBlock(body, error.message);
  }
  return true;
}

export default { init, isEnabled, attachToggle, send };
