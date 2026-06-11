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
const KEY_WEIGHTED = 'shadow-magi-weighted';
const KEY_EVIDENCE = 'shadow-magi-evidence';

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

function getWeighted() {
  return localStorage.getItem(KEY_WEIGHTED) === '1';
}

function getEvidence() {
  return localStorage.getItem(KEY_EVIDENCE) === '1';
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
    <label class="magi-config-toggle">
      <span><strong>Confidence-weighted vote</strong><em>Rank stances by summed unit confidence instead of head count.</em></span>
      <label class="admin-switch"><input type="checkbox" id="magi-config-weighted"${getWeighted() ? ' checked' : ''}><span class="admin-slider"></span></label>
    </label>
    <label class="magi-config-toggle">
      <span><strong>Evidence grounding</strong><em>Fetch one shared web source first; all units reason over the same text.</em></span>
      <label class="admin-switch"><input type="checkbox" id="magi-config-evidence"${getEvidence() ? ' checked' : ''}><span class="admin-slider"></span></label>
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
      localStorage.setItem(KEY_WEIGHTED, body.querySelector('#magi-config-weighted')?.checked ? '1' : '0');
      localStorage.setItem(KEY_EVIDENCE, body.querySelector('#magi-config-evidence')?.checked ? '1' : '0');
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

// ---------------------------------------------------------------------------
// NERV deliberation UI. Each MAGI unit is a node in a triangular layout around
// a central verdict readout. Units update independently as their SSE events
// arrive; the backend reports confidence on a 0..1 scale which we render as %.
// ---------------------------------------------------------------------------

const ROLE_JP = { melchior: 'MELCHIOR', balthasar: 'BALTHASAR', casper: 'CASPER' };
const ROLE_NUM = { melchior: '01', balthasar: '02', casper: '03' };

function shortModel(model) {
  return String(model || 'unknown').split('/').pop();
}

function setText(root, selector, value) {
  const el = root && root.querySelector(selector);
  if (el) el.textContent = value == null ? '' : String(value);
}

function confPercent(value) {
  return typeof value === 'number' ? `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%` : '';
}

function metaParts(unit) {
  const parts = [];
  const conf = confPercent(unit && unit.confidence);
  if (conf) parts.push(conf);
  if (unit && unit.latency_ms) parts.push(`${unit.latency_ms}ms`);
  if (unit && unit.repaired) parts.push('repaired');
  if (unit && unit.debated) parts.push('debated');
  return parts.join(' · ');
}

function nodeMarkup(role) {
  return `
    <div class="magi-node" data-role="${esc(role)}" data-state="deliberating" data-stance="">
      <div class="magi-node-id">${esc(ROLE_JP[role])}<b>・${esc(ROLE_NUM[role])}</b></div>
      <div class="magi-node-model">resolving…</div>
      <div class="magi-node-stance">DELIBERATING</div>
      <div class="magi-node-meta"></div>
    </div>`;
}

function createNervPanel() {
  const box = document.getElementById('chat-history');
  if (!box) return null;
  const wrap = document.createElement('div');
  wrap.className = 'msg msg-ai magi-message streaming';
  const mode = getMode().toUpperCase();
  wrap.innerHTML = `
    <div class="role">MAGI <span class="role-timestamp">${esc(nowStamp())}</span></div>
    <div class="body">
      <div class="magi-nerv" data-agreement="deliberating" data-mode="${esc(mode)}" role="group" aria-label="MAGI deliberation">
        <div class="magi-nerv-frame">
          <div class="magi-nerv-head">
            <span class="magi-nerv-mark">MAGI // ${esc(mode)}</span>
            <span class="magi-nerv-phase">DELIBERATION</span>
          </div>
          <div class="magi-nerv-stage">
            ${nodeMarkup('melchior')}
            <div class="magi-core" data-state="deliberating" aria-live="polite">
              <div class="magi-core-en">DELIBERATING</div>
              <div class="magi-core-jp">審議中</div>
              <div class="magi-core-tag"></div>
            </div>
            ${nodeMarkup('balthasar')}
            ${nodeMarkup('casper')}
          </div>
          <div class="magi-verdict" aria-live="polite"></div>
          <div class="magi-details-host"></div>
        </div>
      </div>
    </div>`;
  box.appendChild(wrap);
  uiModule.scrollHistory();
  return wrap.querySelector('.magi-nerv');
}

function applyUnit(nerv, role, unit, phase) {
  const node = nerv && nerv.querySelector(`.magi-node[data-role="${role}"]`);
  if (!node) return;
  if (unit && unit.model) setText(node, '.magi-node-model', shortModel(unit.model));
  if (phase === 'deliberating') {
    node.dataset.state = 'deliberating';
    setText(node, '.magi-node-stance', 'DELIBERATING');
    return;
  }
  if (phase === 'peer_review') {
    node.dataset.state = 'reviewing';
    setText(node, '.magi-node-stance', 'PEER REVIEW');
    return;
  }
  const ok = unit && unit.ok;
  if (!ok || phase === 'malfunction') {
    node.dataset.state = 'malfunction';
    node.dataset.stance = 'MALFUNCTION';
    setText(node, '.magi-node-stance', 'MALFUNCTION');
    setText(node, '.magi-node-meta', unit && unit.latency_ms ? `${unit.latency_ms}ms` : '');
    node._unit = unit;
    return;
  }
  const stance = unit.stance || 'ANSWER';
  node.dataset.state = unit.debated ? 'debated' : 'answered';
  node.dataset.stance = stance;
  setText(node, '.magi-node-stance', stance);
  setText(node, '.magi-node-meta', metaParts(unit));
  node._unit = unit;
}

function coreState(result) {
  const agreement = String(result.agreement || '').toLowerCase();
  if (agreement === 'malfunction') return { en: 'MALFUNCTION', jp: '停止', cls: 'malfunction' };
  const decision = String(result.decision || '').toUpperCase();
  if (decision === 'APPROVE') return { en: 'APPROVED', jp: '可決', cls: 'approve' };
  if (decision === 'REJECT') return { en: 'REJECTED', jp: '否決', cls: 'reject' };
  if (decision === 'CONDITIONAL') return { en: 'CONDITIONAL', jp: '条件付', cls: 'conditional' };
  if (agreement === 'deadlock') return { en: 'DEADLOCK', jp: '対立', cls: 'deadlock' };
  if (agreement === 'insufficient') return { en: 'INSUFFICIENT', jp: '定足数不足', cls: 'deadlock' };
  return { en: 'RESOLVED', jp: '解決', cls: 'answered' };
}

function unitDetail(unit) {
  const ok = unit.ok;
  const stance = unit.stance || (ok ? 'ANSWER' : 'ERROR');
  const risks = Array.isArray(unit.risks) ? unit.risks : [];
  const conf = confPercent(unit.confidence) || 'n/a';
  const meta = [conf, unit.latency_ms ? `${unit.latency_ms}ms` : '', unit.repaired ? 'repaired' : '', unit.debated ? 'debated' : '']
    .filter(Boolean).join(' · ');
  return `
    <details class="magi-detail" data-stance="${esc(stance)}">
      <summary>
        <span class="magi-detail-id">${esc(unit.display || unit.label || unit.role)}</span>
        <span class="magi-detail-stance">${esc(stance)}</span>
        <span class="magi-detail-meta">${esc(meta)}</span>
      </summary>
      <div class="magi-detail-body">
        <div class="magi-detail-model">${esc(unit.model || 'unknown')}</div>
        <div>${textHtml(unit.answer || unit.error || '')}</div>
        ${unit.reason ? `<p class="magi-detail-line"><b>REASON</b> ${textHtml(unit.reason)}</p>` : ''}
        ${risks.length ? `<div class="magi-detail-risks"><b>RISKS</b>${risks.map(r => `<span>${esc(r)}</span>`).join('')}</div>` : ''}
        ${unit.dissent ? `<p class="magi-detail-line"><b>DISSENT</b> ${textHtml(unit.dissent)}</p>` : ''}
        ${unit.next_step ? `<p class="magi-detail-line"><b>NEXT</b> ${textHtml(unit.next_step)}</p>` : ''}
        ${unit.debate_error ? `<p class="magi-detail-warn">Debate fallback: ${esc(unit.debate_error)}</p>` : ''}
      </div>
    </details>`;
}

function renderResolved(nerv, result) {
  if (!nerv) return;
  const msg = nerv.closest('.magi-message');
  if (msg) msg.classList.remove('streaming');

  // result.magi is authoritative for final node state.
  (result.magi || []).forEach(unit => {
    applyUnit(nerv, unit.role, unit, unit.ok ? (unit.debated ? 'debated' : 'answered') : 'malfunction');
  });

  const core = nerv.querySelector('.magi-core');
  const state = coreState(result);
  if (core) {
    core.dataset.state = state.cls;
    setText(core, '.magi-core-en', state.en);
    setText(core, '.magi-core-jp', state.jp);
    const tags = [String(result.agreement || '').toUpperCase()];
    if (result.degraded) tags.push('DEGRADED');
    setText(core, '.magi-core-tag', tags.filter(Boolean).join(' · '));
  }
  nerv.dataset.agreement = String(result.agreement || '').toLowerCase();
  nerv.querySelector('.magi-nerv-phase').textContent = 'RESOLVED';

  const verdict = nerv.querySelector('.magi-verdict');
  if (verdict) verdict.innerHTML = textHtml(result.final || '');

  const judge = result.judge
    ? `<details class="magi-detail magi-detail-judge" data-stance="${esc(result.judge.verdict || '')}">
        <summary><span class="magi-detail-id">JUDGE SYNTHESIS</span><span class="magi-detail-stance">${esc(result.judge.verdict || '')}</span></summary>
        <div class="magi-detail-body">
          ${result.judge.agreement_summary ? `<p class="magi-detail-line"><b>AGREEMENT</b> ${textHtml(result.judge.agreement_summary)}</p>` : ''}
          ${result.judge.dissent_summary ? `<p class="magi-detail-line"><b>DISSENT</b> ${textHtml(result.judge.dissent_summary)}</p>` : ''}
        </div>
      </details>`
    : '';
  const judgeErr = result.judge_error ? `<div class="magi-nerv-warn">Judge fallback: ${esc(result.judge_error)}</div>` : '';
  const host = nerv.querySelector('.magi-details-host');
  if (host) {
    host.innerHTML = `${judgeErr}<div class="magi-details">${(result.magi || []).map(unitDetail).join('')}${judge}</div>`;
  }
  nerv.dataset.raw = result.final || '';
  uiModule.scrollHistory();
}

function renderError(nerv, err) {
  if (!nerv) return;
  const msg = nerv.closest('.magi-message');
  if (msg) msg.classList.remove('streaming');
  const core = nerv.querySelector('.magi-core');
  if (core) {
    core.dataset.state = 'malfunction';
    setText(core, '.magi-core-en', 'MALFUNCTION');
    setText(core, '.magi-core-jp', '停止');
    setText(core, '.magi-core-tag', 'SYSTEM ERROR');
  }
  nerv.dataset.agreement = 'malfunction';
  const verdict = nerv.querySelector('.magi-verdict');
  if (verdict) verdict.innerHTML = `<div class="magi-nerv-warn">MAGI failed: ${esc(err && err.message ? err.message : err)}</div>`;
  uiModule.scrollHistory();
}

function handleEvent(nerv, event) {
  if (!event || typeof event !== 'object') return;
  if (event.type === 'unit') {
    applyUnit(nerv, event.role, event.unit || event, event.phase);
  } else if (event.type === 'system') {
    const labels = {
      judge: 'JUDGE',
      evidence: 'GATHERING EVIDENCE',
      evidence_ready: 'EVIDENCE READY',
      evidence_failed: 'EVIDENCE FAILED — PROCEEDING',
      diversity_warning: 'LOW MODEL DIVERSITY',
      peer_review: 'PEER REVIEW',
    };
    const text = labels[event.phase] || 'PEER REVIEW';
    const phase = nerv.querySelector('.magi-nerv-phase');
    if (phase) phase.textContent = text;
    const core = nerv.querySelector('.magi-core');
    if (core && core.dataset.state === 'deliberating') {
      setText(core, '.magi-core-tag', event.phase === 'judge' ? 'SYNTHESIZING' : text);
    }
  } else if (event.type === 'resolved') {
    renderResolved(nerv, event.result || {});
  } else if (event.type === 'error') {
    renderError(nerv, new Error(event.error || 'deliberation failed'));
  }
}

function rolePayload() {
  const saved = getRoleSelections();
  return ROLE_ORDER.map(role => saved[role]).filter(Boolean);
}

async function runDeliberation({ query, displayQuery, sessionId }) {
  const nerv = createNervPanel();
  let resolved = false;
  try {
    const res = await fetch(`${API_BASE}/api/magi/deliberate/stream`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query,
        display_query: displayQuery || query,
        session_id: sessionId || '',
        mode: getMode(),
        roles: rolePayload(),
        weighted: getWeighted(),
        evidence: getEvidence(),
      }),
    });
    if (!res.ok || !res.body) throw new Error((await res.text().catch(() => '')) || `HTTP ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const dataLine = frame.split('\n').find(line => line.startsWith('data:'));
        if (!dataLine) continue; // keep-alive comment
        const json = dataLine.slice(5).trim();
        if (!json) continue;
        let event;
        try { event = JSON.parse(json); } catch (_) { continue; }
        handleEvent(nerv, event);
        if (event.type === 'resolved') resolved = true;
      }
    }
    if (!resolved) throw new Error('MAGI stream ended before a verdict was produced.');
    return nerv;
  } catch (err) {
    renderError(nerv, err);
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
