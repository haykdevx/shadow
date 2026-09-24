// static/js/magi.js
// MAGI tri-model deliberation mode.

import uiModule from './ui.js';
import { makeWindowDraggable } from './windowDrag.js';
import * as Modals from './modalManager.js';

// Matches the star icon on the MAGI sidebar button so the minimized rail chip
// and the window header read as the same feature.
const MAGI_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><path d="M12 2l2.6 6.9L22 9.2l-5.7 4.6 1.9 7.2L12 17.1 5.8 21l1.9-7.2L2 9.2l7.4-.3L12 2z"/></svg>';

let API_BASE = '';
const KEY_ENABLED = 'shadow-magi-enabled';
const KEY_MODE = 'shadow-magi-resolution';
const KEY_ROLES = 'shadow-magi-roles';

const ROLE_ORDER = ['melchior', 'balthasar', 'casper'];
const ROLE_LABELS = {
  melchior: 'MELCHIOR-01',
  balthasar: 'BALTHASAR-02',
  casper: 'CASPER-03',
};

function esc(value) {
  return uiModule.esc(String(value ?? ''));
}

function textHtml(value) {
  return esc(value || '').replace(/\n/g, '<br>');
}

function nowStamp() {
  return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function isActive() {
  return localStorage.getItem(KEY_ENABLED) === '1';
}

function getMode() {
  return localStorage.getItem(KEY_MODE) || 'vote';
}

function getRoleSelections() {
  try {
    const parsed = JSON.parse(localStorage.getItem(KEY_ROLES) || '{}');
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch (_) {
    return {};
  }
}

function saveRoleSelections(value) {
  localStorage.setItem(KEY_ROLES, JSON.stringify(value || {}));
}

function setActive(on) {
  localStorage.setItem(KEY_ENABLED, on ? '1' : '0');
  syncIndicator();
}

function syncIndicator() {
  const active = isActive();
  const indicator = document.getElementById('magi-indicator-btn');
  const toggle = document.getElementById('magi-toggle');
  const tool = document.getElementById('tool-magi-btn');
  if (indicator) indicator.style.display = active ? '' : 'none';
  if (toggle) toggle.checked = active;
  if (tool) tool.classList.toggle('active', active);
}

async function fetchConfig() {
  const res = await fetch(`${API_BASE}/api/magi/config`, { credentials: 'same-origin' });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function flattenAvailable(config) {
  const out = [];
  for (const ep of config.available || []) {
    for (const model of ep.models || []) {
      out.push({
        endpoint_id: ep.endpoint_id,
        endpoint: ep.endpoint,
        endpoint_name: ep.endpoint_name || 'Endpoint',
        model,
        label: `${model.split('/').pop()} / ${ep.endpoint_name || 'Endpoint'}`,
      });
    }
  }
  return out;
}

function selectionValue(item) {
  if (!item) return '';
  return `${item.endpoint_id || ''}||${item.model || ''}`;
}

function parseSelectionValue(value, role) {
  const [endpointId, model] = String(value || '').split('||');
  if (!endpointId || !model) return null;
  return { role, endpoint_id: endpointId, model };
}

function buildRoleSelect(role, config, options, saved) {
  const roleInfo = (config.roles || []).find(r => r.role === role) || {};
  const current = saved[role] ? selectionValue(saved[role]) : '';
  const auto = roleInfo.default
    ? `Auto: ${roleInfo.default.model?.split('/').pop() || 'model'} / ${roleInfo.default.endpoint_name || 'endpoint'}`
    : 'Auto';
  const opts = [`<option value="">${esc(auto)}</option>`].concat(options.map(item => {
    const val = selectionValue(item);
    return `<option value="${esc(val)}"${val === current ? ' selected' : ''}>${esc(item.label)}</option>`;
  }));
  return `
    <label class="magi-config-row">
      <span><strong>${esc(ROLE_LABELS[role])}</strong><em>${esc(roleInfo.title || '')}</em></span>
      <select data-magi-role="${esc(role)}">${opts.join('')}</select>
    </label>
  `;
}

let _magiModal = null;

function closePanel() {
  if (_magiModal) _magiModal.classList.add('hidden');
}

// Build the window shell once, as a standard app modal so it inherits the
// shared chrome: themed panel, draggable/snappable title bar, minimize-to-rail,
// and the modal-enter open animation - same as Brain/Calendar/etc.
function ensureMagiModal() {
  if (_magiModal) return _magiModal;
  const modal = document.createElement('div');
  modal.id = 'magi-modal';
  modal.className = 'modal hidden';
  modal.innerHTML = `
    <div class="modal-content magi-modal-content" role="dialog" aria-modal="true" aria-label="MAGI Deliberation" style="background:var(--bg)">
      <div class="modal-header">
        <h4>${MAGI_ICON}MAGI Deliberation</h4>
        <button type="button" class="close-btn" data-magi-close aria-label="Close">✖</button>
      </div>
      <div class="modal-body" id="magi-modal-body"></div>
    </div>`;
  document.body.appendChild(modal);
  _magiModal = modal;

  modal.querySelector('[data-magi-close]')?.addEventListener('click', closePanel);

  const content = modal.querySelector('.modal-content');
  const header = modal.querySelector('.modal-header');
  makeWindowDraggable(modal, {
    content,
    header,
    skipSelector: 'button, input, select, label',
    enableDock: true,
    enableLeftDock: true,
  });

  Modals.register('magi-modal', {
    restoreFn: () => modal.classList.remove('hidden'),
    closeFn: () => modal.classList.add('hidden'),
    sidebarBtnId: 'tool-magi-btn',
    label: 'MAGI',
    icon: MAGI_ICON,
  });
  Modals.injectMinimizeButton(modal, 'magi-modal');
  return modal;
}

async function openPanel() {
  let config;
  try {
    config = await fetchConfig();
  } catch (err) {
    if (uiModule.showError) uiModule.showError('MAGI config failed: ' + err.message);
    return;
  }

  const options = flattenAvailable(config);
  const saved = getRoleSelections();
  const modal = ensureMagiModal();
  const body = modal.querySelector('#magi-modal-body');
  body.innerHTML = `
    <label class="magi-config-toggle">
      <span>Enable MAGI mode for chat sends</span>
      <label class="admin-switch"><input type="checkbox" id="magi-config-enabled"${isActive() ? ' checked' : ''}><span class="admin-slider"></span></label>
    </label>
    <label class="magi-config-row">
      <span><strong>Resolution</strong><em>Vote is fast. Debate adds a peer-review round. Judge asks one model to synthesize.</em></span>
      <select id="magi-config-mode">
        <option value="vote"${getMode() === 'vote' ? ' selected' : ''}>VOTE</option>
        <option value="debate"${getMode() === 'debate' ? ' selected' : ''}>DEBATE</option>
        <option value="judge"${getMode() === 'judge' ? ' selected' : ''}>JUDGE</option>
      </select>
    </label>
    <div class="magi-config-grid">
      ${ROLE_ORDER.map(role => buildRoleSelect(role, config, options, saved)).join('')}
    </div>
    <div class="magi-config-foot">
      <button type="button" class="magi-btn" data-magi-action="deactivate">Deactivate</button>
      <button type="button" class="magi-btn" data-magi-action="save">Save</button>
      <button type="button" class="magi-btn magi-btn-primary" data-magi-action="activate">Save and Activate</button>
    </div>`;

  body.querySelectorAll('[data-magi-action]').forEach(btn => {
    btn.addEventListener('click', () => {
      const mode = body.querySelector('#magi-config-mode')?.value || 'vote';
      localStorage.setItem(KEY_MODE, mode);
      const nextRoles = {};
      body.querySelectorAll('[data-magi-role]').forEach(sel => {
        const role = sel.getAttribute('data-magi-role');
        const parsed = parseSelectionValue(sel.value, role);
        if (parsed) nextRoles[role] = parsed;
      });
      saveRoleSelections(nextRoles);
      const action = btn.getAttribute('data-magi-action');
      if (action === 'deactivate') setActive(false);
      else if (action === 'activate') setActive(true);
      else setActive(!!body.querySelector('#magi-config-enabled')?.checked);
      closePanel();
    });
  });

  // Reveal + replay the shared open animation on every open (the element
  // persists between opens, so re-trigger modal-enter by reflow).
  modal.classList.remove('hidden');
  const card = modal.querySelector('.modal-content');
  if (card) { card.style.animation = 'none'; void card.offsetWidth; card.style.animation = ''; }
}

function createThinkingPanel() {
  const box = document.getElementById('chat-history');
  if (!box) return null;
  const wrap = document.createElement('div');
  wrap.className = 'msg msg-ai magi-message streaming';
  wrap.innerHTML = `
    <div class="role">MAGI <span class="role-timestamp">${esc(nowStamp())}</span></div>
    <div class="body">
      <div class="magi-panel">
        <div class="magi-panel-head">
          <span class="magi-mark">MAGI // ${esc(getMode().toUpperCase())}</span>
          <span class="magi-status">independent answers${getMode() === 'debate' ? ' -> peer debate' : ''} -> verdict</span>
        </div>
        <div class="magi-grid">
          ${ROLE_ORDER.map(role => `
            <div class="magi-card thinking" data-role="${esc(role)}">
              <div class="magi-card-title">${esc(ROLE_LABELS[role])}</div>
              <div class="magi-card-stance">THINKING</div>
              <div class="magi-card-answer">Awaiting independent answer...</div>
            </div>
          `).join('')}
        </div>
      </div>
    </div>
  `;
  box.appendChild(wrap);
  uiModule.scrollHistory();
  return wrap;
}

function renderResult(holder, data) {
  if (!holder) return;
  holder.classList.remove('streaming');
  const body = holder.querySelector('.body');
  const final = textHtml(data.final || '');
  const cards = (data.magi || []).map(item => {
    const risks = Array.isArray(item.risks) ? item.risks : [];
    return `
      <div class="magi-card ${item.ok ? 'answered' : 'failed'} ${item.debated ? 'debated' : ''}">
        <div class="magi-card-title">${esc(item.label || item.role)}</div>
        <div class="magi-card-model">${esc(item.model || 'unknown')}</div>
        <div class="magi-card-stance">${esc(item.stance || (item.ok ? 'ANSWER' : 'ERROR'))}</div>
        <div class="magi-card-meta">Confidence: ${item.confidence ?? 'n/a'}${item.status ? ` / ${esc(item.status)}` : ''}</div>
        <div class="magi-card-answer">${textHtml(item.answer || item.error || '')}</div>
        ${risks.length ? `<div class="magi-card-risks"><strong>Risks</strong>${risks.map(r => `<span>${esc(r)}</span>`).join('')}</div>` : ''}
        ${item.dissent ? `<div class="magi-card-note"><strong>Dissent</strong>${textHtml(item.dissent)}</div>` : ''}
        ${item.next_step ? `<div class="magi-card-note"><strong>Next</strong>${textHtml(item.next_step)}</div>` : ''}
        ${item.debate_error ? `<div class="magi-error">Debate fallback: ${esc(item.debate_error)}</div>` : ''}
      </div>
    `;
  }).join('');
  body.innerHTML = `
    <div class="magi-panel ${data.degraded ? 'degraded' : ''}">
      <div class="magi-panel-head">
        <span class="magi-mark">MAGI // ${esc(String(data.mode || 'vote').toUpperCase())}</span>
        <span class="magi-status">${esc(data.agreement || 'split')}${data.vote?.avg_confidence !== undefined && data.vote?.avg_confidence !== null ? ` / ${data.vote.avg_confidence}%` : ''}${data.degraded ? ' / degraded' : ''}</span>
      </div>
      <div class="magi-final">${final}</div>
      ${data.judge_error ? `<div class="magi-error">Judge fallback: ${esc(data.judge_error)}</div>` : ''}
      <div class="magi-grid">${cards}</div>
    </div>
  `;
  holder.dataset.raw = data.final || '';
  uiModule.scrollHistory();
}

function renderError(holder, err) {
  if (!holder) return;
  holder.classList.remove('streaming');
  const body = holder.querySelector('.body');
  body.innerHTML = `<div class="magi-panel degraded"><div class="magi-error">MAGI failed: ${esc(err.message || err)}</div></div>`;
}

function rolePayload() {
  const saved = getRoleSelections();
  return ROLE_ORDER.map(role => saved[role]).filter(Boolean);
}

async function runDeliberation({ query, displayQuery, sessionId }) {
  const holder = createThinkingPanel();
  try {
    const res = await fetch(`${API_BASE}/api/magi/deliberate`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query,
        display_query: displayQuery || query,
        session_id: sessionId || '',
        mode: getMode(),
        roles: rolePayload(),
      }),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    renderResult(holder, data);
    return data;
  } catch (err) {
    renderError(holder, err);
    throw err;
  }
}

function init(apiBase) {
  API_BASE = apiBase || '';
  syncIndicator();
  document.getElementById('tool-magi-btn')?.addEventListener('click', openPanel);
  document.getElementById('magi-indicator-btn')?.addEventListener('click', () => setActive(false));
}

export default {
  init,
  isActive,
  setActive,
  openPanel,
  runDeliberation,
};
