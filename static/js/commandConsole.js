// ============================================================
// commandConsole.js — the "Shadow Console" Command view.
//
// A full device dashboard rendered below the app top bar and to
// the right of the icon rail: device rail · workspace(header,
// tabs, body) · side(attention + live activity). Wired to the
// REAL device backend at /api/shadow:
//   /overview            device metrics + device list + pending
//   /timeline            audit trail -> live activity feed
//   /pc/action (shell)   direct terminal command execution
//   /pc/confirm/{id}     run a pending action
//   /pc/pending/{id}     (DELETE) deny a pending action
//
// The terminal is DIRECT: a command you type runs immediately
// (you are the approver). The server still gates truly dangerous
// commands (risk "critical") behind an explicit confirm, and
// every action is audit-logged.
// ============================================================

const API = '/api/shadow';
const DEVICE_KEY = 'shadow.commandDeviceId';
const POLL_MS = 8000;

const S = {
  el: null,          // #command-console
  open: false,
  deviceId: '',      // selected device id (local pref)
  overview: null,
  timeline: [],
  tab: 'overview',
  pollTimer: null,
  termHistory: [],   // {cmd, lines:[]} — full scrollback of run commands
  termInput: '',
  termCwd: '',       // persistent working directory (real-terminal feel)
  termCmds: [],      // command history for ↑/↓ recall
  termIdx: -1,       // pointer into termCmds while recalling
  _generation: 0,
  _requests: {},
  _refreshSeq: 0,
  _refreshApplied: 0,
  _refreshError: '',
  _files: null,      // files tab cache: {path, entries, roots}
  _filePreview: null, // {name, path, text, binary, mime, size, truncated} or null
  _procs: null,      // processes tab cache: {processes:[header,...rows], error?}
  _windowsList: null, // apps & windows tab cache: {windows:[...]}
  _shot: null,       // screen tab cache: {mime, image_b64, width, height, ts}
  _clip: null,       // clipboard tab cache: {text} | {error}
  _clipDraft: null,  // unsent clipboard edit — survives the 8s poll re-render
  _pathDraft: null,  // half-typed Files path — survives the 8s poll re-render
  _remote: null,     // remote desktop status cache (see /remote/status)
  _remoteInvite: '', // last-issued MeshCentral installer link
  _bots: {           // chat-bot bridges (PC control from Telegram/Discord)
    telegram: { status: null, pairCode: null },
    discord: { status: null, pairCode: null },
  },
};

function $(sel, ctx) { return (ctx || S.el || document).querySelector(sel); }
function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function fmtBytes(n) {
  n = Number(n) || 0;
  if (n >= 1e9) return (n / 1073741824).toFixed(1) + ' GB';
  if (n >= 1e6) return (n / 1048576).toFixed(0) + ' MB';
  return (n / 1024).toFixed(0) + ' KB';
}
function fmtUptime(sec) {
  sec = Number(sec) || 0;
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}
function ago(ts) {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - (Number(ts) || 0)));
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}
function clockTime(ts) {
  try { return new Date((Number(ts) || 0) * 1000).toLocaleTimeString([], { hour12: false }); } catch (_) { return ''; }
}

async function api(path, opts) {
  const res = await fetch(API + path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    const msg = (data && data.detail && (data.detail.message || data.detail)) || ('HTTP ' + res.status);
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}

function toast(message, isError) {
  const ui = window.uiModule;
  if (ui && ui.showToast) ui.showToast(message);
  else if (isError) console.error(message);
  else console.log(message);
}

function getStoredDevice() { try { return localStorage.getItem(DEVICE_KEY) || ''; } catch (_) { return ''; } }
function setStoredDevice(id) { try { id ? localStorage.setItem(DEVICE_KEY, id) : localStorage.removeItem(DEVICE_KEY); } catch (_) {} }

// ── Positioning: keep the app top bar + icon rail visible ──
function positionConsole() {
  if (!S.el) return;
  const topbar = document.querySelector('.chat-top-bar');
  const rail = document.getElementById('icon-rail');
  const top = topbar ? Math.round(topbar.getBoundingClientRect().bottom) : 48;
  let left = 0;
  if (rail && getComputedStyle(rail).display !== 'none') {
    const r = rail.getBoundingClientRect();
    left = Math.round(r.right);
  }
  S.el.style.setProperty('--cc-top', top + 'px');
  S.el.style.setProperty('--cc-left', left + 'px');
}

// ── Data ──
function selectedDevice() {
  const devs = (S.overview && S.overview.devices) || [];
  return devs.find((d) => d.id === S.deviceId) || devs.find((d) => d.selected) || devs[0] || null;
}

async function refresh() {
  const generation = S._generation, seq = ++S._refreshSeq;
  try {
    const q = S.deviceId ? ('?device_id=' + encodeURIComponent(S.deviceId)) : '';
    const [ov, tl] = await Promise.all([
      api('/overview' + q),
      api('/timeline?limit=40').catch(() => ({ events: [] })),
    ]);
    if (generation !== S._generation || seq < S._refreshApplied) return;
    S._refreshApplied = seq;
    S._refreshError = '';
    if (ov) S.overview = ov;
    S.timeline = (tl && tl.events) || [];
    if (!S.deviceId) {
      const d = selectedDevice();
      if (d) S.deviceId = d.id;
    }
    if (S.open) render();
  } catch (e) {
    if (generation !== S._generation || seq < S._refreshApplied) return;
    S._refreshApplied = seq;
    S._refreshError = String(e.message || e);
    if (S.open) render();
  }
}

// ── Render ──
// render() replaces the whole subtree, so anything the user was doing in a
// field is lost unless we carry it over. Drafts (above) keep the *value* even
// after a blur; this keeps focus and caret position for the field in hand.
function captureFocus() {
  const el = document.activeElement;
  if (!el || !S.el || !S.el.contains(el)) return null;
  if (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA') return null;
  if (!el.id) return null;
  return { id: el.id, start: el.selectionStart, end: el.selectionEnd };
}
function restoreFocus(saved) {
  if (!saved) return;
  const el = S.el.querySelector('#' + saved.id.replace(/([^\w-])/g, '\\$1'));
  if (!el) return;
  el.focus();
  try { el.setSelectionRange(saved.start, saved.end); } catch (_) { /* range n/a */ }
}

function render() {
  if (!S.el) return;
  const focused = captureFocus();
  const dev = selectedDevice();
  S.el.innerHTML = `
    <div class="cc-main">
      ${renderRail()}
      <div class="cc-workspace">
        ${renderHead(dev)}
        ${S._refreshError ? `<div class="cc-empty" role="alert">Unable to refresh devices: ${esc(S._refreshError)}. Retrying automatically.</div>` : ''}
        <div class="cc-tabs" role="tablist" aria-label="Device controls">
          ${[
            ['overview', 'Overview'], ['terminal', 'Terminal'], ['files', 'Files'],
            ['processes', 'Processes'], ['apps', 'Apps'], ['system', 'System'],
            ['screen', 'Screen'], ['clipboard', 'Clipboard'], ['remote', 'Remote'],
            ['tasks', 'Tasks'], ['timeline', 'Timeline'],
          ].map(([t, label]) =>
            `<button type="button" class="cc-tab ${S.tab === t ? 'active' : ''}" data-cc-tab="${t}" role="tab" aria-selected="${S.tab === t}" tabindex="${S.tab === t ? 0 : -1}">${esc(label)}</button>`
          ).join('')}
        </div>
        <div class="cc-body">${renderTab(dev)}</div>
      </div>
      ${renderSide(dev)}
    </div>`;
  wire();
  positionConsole();
  if (S.tab === 'terminal' && !focused) { const inp = $('#cc-term-input'); if (inp) inp.focus(); }
  restoreFocus(focused);
}

const RAIL_ICON = '<svg viewBox="0 0 24 24" fill="none"><rect x="3" y="4" width="18" height="13" rx="2" stroke="currentColor" stroke-width="1.7"/><path d="M8 20h8M12 17v3" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>';

function renderRail() {
  const devs = (S.overview && S.overview.devices) || [];
  const items = devs.map((d) => `
    <button type="button" class="cc-dev ${d.online ? 'online' : ''} ${d.id === S.deviceId ? 'active' : ''}" data-cc-device="${esc(d.id)}" aria-label="${esc(d.name)}${d.online ? '' : ' (offline)'}" aria-pressed="${d.id === S.deviceId}" title="${esc(d.name)}">
      ${RAIL_ICON}<span class="cc-dev-dot"></span>
      <span class="cc-tip">${esc(d.name)}${d.online ? '' : ' · offline'}</span>
    </button>`).join('');
  return `<aside class="cc-rail">${items}<button type="button" class="cc-add" data-cc-add aria-label="Enroll a device" title="Enroll a device">+</button></aside>`;
}

const CLOSE_BTN = `<button type="button" class="cc-close" data-cc-close aria-label="Close Command"><svg viewBox="0 0 24 24" fill="none" width="16" height="16"><path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg></button>`;

// A compact device picker for phones, where the vertical rail is hidden.
function renderMobileDevicePicker() {
  const devs = (S.overview && S.overview.devices) || [];
  if (!devs.length) return '';
  const opts = devs.map((d) => `<option value="${esc(d.id)}" ${d.id === S.deviceId ? 'selected' : ''}>${esc(d.name)}${d.online ? '' : ' (offline)'}</option>`).join('');
  return `<select class="cc-dev-select" data-cc-device-select aria-label="Device">${opts}</select>`;
}

function renderHead(dev) {
  if (!dev) {
    return `<div class="cc-head"><div class="cc-head-main"><div class="cc-title">No devices</div><div class="cc-sub">Enroll a machine to control it from here.</div></div><button type="button" class="cc-btn" data-cc-palette aria-label="Open command palette">⌘ / Ctrl K</button>${CLOSE_BTN}</div>`;
  }
  const os = (S.overview && S.overview.pc && S.overview.pc.os) || {};
  const osStr = [os.system, os.release].filter(Boolean).join(' · ') || (dev.platform || '').split('-').slice(0, 2).join(' ');
  return `
    <div class="cc-head">
      <div class="cc-head-main">
        <div class="cc-title">${esc(dev.name)}</div>
        <div class="cc-sub">
          <span class="mono">${esc(osStr)}</span>
          <span class="cc-chip ${dev.online ? '' : 'off'}"><i class="d"></i>${dev.online ? 'online' : 'offline'}</span>
          <span>last heartbeat <span class="mono">${esc(ago(dev.last_seen))}</span></span>
        </div>
      </div>
      ${renderMobileDevicePicker()}
      <button type="button" class="cc-btn" data-cc-palette aria-label="Open command palette">⌘ / Ctrl K</button>${CLOSE_BTN}
    </div>`;
}

function renderTab(dev) {
  if (S.tab === 'overview') return renderOverviewPane(dev);
  if (S.tab === 'terminal') return renderTerminalPane(dev);
  if (S.tab === 'files') return renderFilesPane(dev);
  if (S.tab === 'processes') return renderProcessesPane(dev);
  if (S.tab === 'apps') return renderAppsPane(dev);
  if (S.tab === 'system') return renderSystemPane(dev);
  if (S.tab === 'screen') return renderScreenPane(dev);
  if (S.tab === 'clipboard') return renderClipboardPane(dev);
  if (S.tab === 'remote') return renderRemotePane(dev);
  if (S.tab === 'tasks') return renderTasksPane(dev);
  if (S.tab === 'timeline') return renderTimelinePane(dev);
  return '';
}

function metricCard(lbl, val, pct) {
  const p = Math.max(0, Math.min(100, Number(pct) || 0));
  return `<div class="cc-metric"><div class="lbl">${esc(lbl)}</div><div class="val">${val}</div>${pct != null ? `<div class="bar"><i style="width:${p}%"></i></div>` : ''}</div>`;
}

function renderOverviewPane(dev) {
  const pc = (S.overview && S.overview.pc) || {};
  const cpu = pc.cpu || {};
  const mem = pc.memory || {};
  const memUsed = mem.used || 0, memTotal = mem.total || 0;
  const memPct = memTotal ? (memUsed / memTotal) * 100 : 0;
  const online = dev && dev.online;

  const metrics = online ? `
    <div class="cc-metrics">
      ${metricCard('CPU', (cpu.percent != null ? cpu.percent.toFixed(1) + '%' : 'n/a'), cpu.percent)}
      ${metricCard('Memory', memTotal ? `${fmtBytes(memUsed)} / ${fmtBytes(memTotal)}` : 'n/a', memPct)}
      ${metricCard('Uptime', pc.uptime_seconds != null ? fmtUptime(pc.uptime_seconds) : 'n/a', null)}
    </div>` : `<div class="cc-empty" style="margin-bottom:18px">Device is offline — start the Shadow device service on that machine to see live metrics.</div>`;

  const pending = (S.overview && S.overview.pending) || [];
  const waiting = pending.length ? `
    <div class="cc-sec-lbl">Waiting on you</div>
    ${pending.map(renderApprovalCard).join('')}` : '';

  const preview = renderTerminalBox(dev, false);

  return `${metrics}${waiting}<div class="cc-sec-lbl" style="margin-top:20px">Terminal</div>${preview}`;
}

function renderApprovalCard(p) {
  const cmd = p.action === 'shell' ? (p.args && p.args.command) || '' : `${p.action}`;
  return `
    <div class="cc-approval">
      <div class="ic"><svg viewBox="0 0 24 24" fill="none"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg></div>
      <div class="cc-approval-body">
        <div class="t">${esc(p.action === 'shell' ? 'Shell command needs approval' : (p.action + ' needs approval'))}</div>
        <div class="d">${cmd ? `<code>${esc(cmd)}</code> · ` : ''}requested ${esc(ago(p.created_at))}${p.risk ? ` · <b style="color:var(--warn)">${esc(p.risk)}</b>` : ''}</div>
      </div>
      <div class="cc-approval-actions">
        <button type="button" class="cc-btn" data-cc-deny="${esc(p.id)}">Deny</button>
        <button type="button" class="cc-btn primary" data-cc-approve="${esc(p.id)}">Approve</button>
      </div>
    </div>`;
}

function promptStr() {
  // cwd comes from the device (`pwd`), so it must be escaped before it is
  // dropped into innerHTML — a directory name is attacker-controllable text.
  return esc(S.termCwd || '~') + ' &#10095;';
}
function renderTerminalBox(dev, full) {
  const online = dev && dev.online;
  const host = dev ? dev.name : 'device';
  const lines = S.termHistory.length
    ? S.termHistory.map((h) => renderTermEntry(h)).join('')
    : `<div class="cc-term-line sys">Live shell on ${esc(host)}. Type a command, press Enter — full output shows here. ↑/↓ recall history, <code>cd</code> persists, <code>clear</code> wipes the screen. Dangerous commands still ask to confirm.</div>`;
  return `
    <div class="cc-term ${full ? 'full' : ''}">
      <div class="cc-term-head"><div class="dots"><i></i><i></i><i></i></div>${esc(host)} — bash · ${esc(S.termCwd || 'home')}</div>
      <div class="cc-term-body" id="cc-term-body">${lines}</div>
      <div class="cc-term-input-row">
        <span class="p" id="cc-term-prompt">${promptStr()}</span>
        <input id="cc-term-input" placeholder="${online ? 'run a command…' : 'device offline'}" ${online ? '' : 'disabled'} autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" value="${esc(S.termInput)}">
      </div>
    </div>`;
}

function renderTermEntry(h) {
  const out = (h.lines || []).map((l) => `<div class="cc-term-line ${l.cls || 'out'}">${esc(l.text)}</div>`).join('');
  const at = h.cwd ? `<span class="cc-term-cwd">${esc(h.cwd)}</span> ` : '';
  return `<div class="cc-term-line">${at}<span class="p">&#10095;</span> ${esc(h.cmd)}</div>${out}`;
}

function renderTerminalPane(dev) { return renderTerminalBox(dev, true); }

function renderFilesPane(dev) {
  if (!dev || !dev.online) return `<div class="cc-empty">Device offline — files unavailable.</div>`;
  const files = S._files;
  if (!files) loadFiles();
  if (!files || files.loading) return `<div class="cc-empty" role="status">Loading files…</div>`;
  if (files.error) return `<div class="cc-empty">Could not list files — ${esc(files.error)}</div>`;
  const path = files.path || '~';
  const roots = files.roots || [];
  const parent = path.length > 1 ? path.replace(/\/[^/]+\/?$/, '') || '/' : '';
  const atRoot = roots.includes(path);
  const rows = (files.entries || files.items || []).map((f) => {
    const isDir = f.is_dir || f.type === 'dir';
    const p = f.path || f.name;
    return `<div class="cc-file-row" role="button" tabindex="0" data-cc-${isDir ? 'cd' : 'open'}="${esc(p)}" aria-label="${esc((isDir ? 'Open folder ' : 'Open file ') + (f.name || f.path || ''))}">
      <span class="ic" aria-hidden="true">${isDir ? '📁' : '📄'}</span>
      <span class="nm">${esc(f.name || f.path)}</span>
      <span class="sz">${isDir ? '' : (f.size != null ? fmtBytes(f.size) : '')}</span>
    </div>`;
  }).join('');
  const nav = `
    <div class="cc-files-nav">
      <button type="button" class="cc-btn sm" data-cc-cd="${esc(parent || path)}" ${(!parent || atRoot) ? 'disabled' : ''}>↑ Up</button>
      <input type="text" class="cc-files-path mono" id="cc-files-path" value="${esc(S._pathDraft != null ? S._pathDraft : path)}" spellcheck="false" autocomplete="off" aria-label="Current folder path">
      <button type="button" class="cc-btn sm" data-cc-refresh-files>Refresh</button>
    </div>`;
  const preview = S._filePreview ? renderFilePreview(S._filePreview) : '';
  return `${nav}<div class="cc-files">${rows || '<div class="cc-file-row"><span class="nm">Empty</span></div>'}</div>${preview}`;
}

function renderFilePreview(p) {
  if (p.loading) {
    return `<div class="cc-file-preview"><div class="cc-file-preview-head"><span class="nm mono">${esc(p.path)}</span><button type="button" class="cc-btn sm" data-cc-close-preview>Close</button></div><div class="cc-empty">Loading ${esc(p.name || 'file')}…</div></div>`;
  }
  if (p.error) {
    return `<div class="cc-file-preview"><div class="cc-file-preview-head"><span class="nm">${esc(p.path)}</span><button type="button" class="cc-btn sm" data-cc-close-preview>Close</button></div><div class="cc-empty">${esc(p.error)}</div></div>`;
  }
  const body = p.binary
    ? `<div class="cc-empty">Binary file (${fmtBytes(p.size)}) — no text preview.</div>`
    : `<pre class="cc-file-preview-body">${esc(p.text)}${p.truncated ? '\n…(truncated)' : ''}</pre>`;
  return `
    <div class="cc-file-preview">
      <div class="cc-file-preview-head">
        <span class="nm mono">${esc(p.path)}</span>
        <span class="sz mono">${fmtBytes(p.size)}</span>
        <button type="button" class="cc-btn sm" data-cc-close-preview>Close</button>
      </div>
      ${body}
    </div>`;
}

function renderTasksPane() {
  return `<div class="cc-empty" style="line-height:1.7">Scheduled tasks &amp; automations run from the Tasks window.<br><button type="button" class="cc-btn primary" data-cc-open-tasks style="margin-top:10px">Open Tasks</button></div>`;
}

function renderTimelinePane() {
  if (!S.timeline.length) return `<div class="cc-empty">No activity yet.</div>`;
  return `<div class="cc-feed" style="padding:0">${S.timeline.map((e, i) => renderFeedItem(e, i === S.timeline.length - 1)).join('')}</div>`;
}

function feedClass(e) {
  if (e.status === 'failed') return 'critical';
  if (e.status === 'pending' || e.risk === 'high' || e.risk === 'critical') return 'warn';
  return '';
}
function humanAction(e) {
  const a = (e.action || '').replace(/_/g, ' ');
  const map = { executed: 'ran', pending: 'awaiting approval', failed: 'failed' };
  const st = map[e.status] || e.status || '';
  return `${a}${st ? ' — ' + st : ''}`;
}
function renderFeedItem(e, last) {
  return `
    <div class="cc-feed-item ${feedClass(e)}">
      <div class="dot-col"><span class="dot"></span>${last ? '' : '<span class="line"></span>'}</div>
      <div class="cc-feed-body">
        <div class="t">${esc(humanAction(e))}</div>
        <div class="m">${esc(clockTime(e.ts))}${e.detail ? ' · ' + esc(String(e.detail).slice(0, 60)) : ''}</div>
      </div>
    </div>`;
}

function renderSide(dev) {
  const pending = (S.overview && S.overview.pending) || [];
  const devs = (S.overview && S.overview.devices) || [];
  const offline = devs.filter((d) => !d.online).length;
  const failed = S.timeline.filter((e) => e.status === 'failed').length;
  const attn = [
    { n: pending.length, t: pending.length === 1 ? 'action waiting on approval' : 'actions waiting on approval', cls: pending.length ? 'critical' : 'muted' },
    { n: offline, t: offline === 1 ? 'device offline' : 'devices offline', cls: offline ? 'warn' : 'muted' },
    { n: failed, t: 'failed actions (recent)', cls: 'muted' },
  ];
  const feed = S.timeline.slice(0, 25).map((e, i) => renderFeedItem(e, i === Math.min(24, S.timeline.length - 1))).join('') || '<div class="cc-empty">Quiet.</div>';
  return `
    <aside class="cc-side">
      <div class="cc-side-sec">
        <div class="cc-sec-lbl">Attention required</div>
        <div class="cc-attn">
          ${attn.map((a) => `<div class="cc-attn-row ${a.cls}"><span class="n">${a.n}</span><span class="t">${esc(a.t)}</span></div>`).join('')}
        </div>
      </div>
      <div class="cc-side-sec"><div class="cc-sec-lbl">Live activity</div></div>
      <div class="cc-feed">${feed}</div>
    </aside>`;
}

// ── Terminal execution (direct run, real-terminal feel) ──
async function runCommand(cmd) {
  const generation = S._generation;
  cmd = (cmd || '').trim();
  if (!cmd) return;
  // remember in history for ↑/↓ recall
  if (S.termCmds[S.termCmds.length - 1] !== cmd) S.termCmds.push(cmd);
  S.termIdx = -1;
  S.termInput = '';

  // client-side builtins so it behaves like a shell
  if (cmd === 'clear' || cmd === 'cls') { S.termHistory = []; updateTerminalDom(); syncPrompt(); return; }

  const dev = selectedDevice();
  // `cd` must persist across commands — the agent runs each command in a fresh
  // subprocess, so we resolve the new directory (cd … && pwd) and keep it as
  // the cwd we pass to every later command.
  const cdMatch = cmd.match(/^cd(?:\s+(.*))?$/);
  const runCwd = S.termCwd || '';
  const entry = { cmd, cwd: runCwd, lines: [{ text: '…', cls: 'sys' }] };
  S.termHistory.push(entry);
  updateTerminalDom();

  const execCmd = cdMatch ? `cd ${cdMatch[1] || '~'} && pwd` : cmd;
  try {
    const result = await runShell(execCmd, runCwd, dev);
    if (generation !== S._generation) return;
    if (result === 'PENDING_CRITICAL') {
      entry.lines = [{ text: 'This command is flagged critical — approve it in "Waiting on you" to run.', cls: 'err' }];
    } else if (cdMatch) {
      // On a successful cd, the last stdout line is the new absolute path.
      const ok = result && (result.ok || result.returncode === 0);
      const newCwd = (result && String(result.stdout || '').trim().split('\n').pop()) || '';
      if (ok && newCwd) { S.termCwd = newCwd; entry.lines = []; }
      else entry.lines = shellResultLines(result);
    } else {
      // If the server reports the resolved cwd, keep our prompt honest.
      if (result && result.cwd && !S.termCwd) S.termCwd = result.cwd;
      entry.lines = shellResultLines(result);
    }
  } catch (e) {
    if (generation !== S._generation) return;
    entry.lines = [{ text: String(e.message || e), cls: 'err' }];
  }
  updateTerminalDom();
  syncPrompt();
  refresh(); // pull the new audit event into the activity feed
}

function shq(p) { return "'" + String(p).replace(/'/g, "'\\''") + "'"; }

// One action round-trip: request → auto-confirm (non-critical) → result.
// The server gates state-changing actions behind a pending-approval record;
// everything except "sleep"/"shutdown" (risk "critical") is confirmed here
// transparently, same as the direct terminal always has. Critical actions
// come back as { pending: {...} } instead — the caller decides how to show
// that (the terminal renders 'PENDING_CRITICAL' as a line; other tabs point
// at "Waiting on you").
async function runAction(action, args, dev) {
  const res = await api('/pc/action', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, args: args || {}, device_id: (dev && dev.id) || S.deviceId || '' }),
  });
  if (res && res.status === 'pending_confirmation' && res.pending) {
    if (res.pending.risk === 'critical') return { pending: res.pending };
    const conf = await api('/pc/confirm/' + encodeURIComponent(res.pending.id), { method: 'POST' });
    return conf && conf.result;
  }
  return res && (res.result !== undefined ? res.result : res);
}

// Shell command round-trip. We keep the working directory by prepending
// `cd <cwd> &&` to the command (like a real shell) rather than the agent's
// `cwd` arg, which is restricted to SHADOW_ALLOWED_ROOTS — this lets the
// terminal roam wherever the device user can, while the server's
// dangerous-command guard still applies.
async function runShell(command, cwd, dev) {
  const full = cwd ? `cd ${shq(cwd)} && ${command}` : command;
  const result = await runAction('shell', { command: full }, dev);
  if (result && result.pending) return 'PENDING_CRITICAL';
  return result;
}

function shellResultLines(result) {
  if (result == null) return [{ text: '(no output)', cls: 'sys' }];
  if (typeof result === 'string') return result.split('\n').map((t) => ({ text: t, cls: 'out' }));
  const lines = [];
  const stdout = result.stdout != null ? result.stdout : result.output;
  const stderr = result.stderr;
  if (stdout) String(stdout).replace(/\n$/, '').split('\n').forEach((t) => lines.push({ text: t, cls: 'out' }));
  if (stderr) String(stderr).replace(/\n$/, '').split('\n').forEach((t) => lines.push({ text: t, cls: 'err' }));
  // agent returns `returncode`; keep `exit_code` too for other shapes
  const rc = result.returncode != null ? result.returncode : result.exit_code;
  if (rc != null && rc !== 0) lines.push({ text: '[exit ' + rc + ']', cls: 'err' });
  if (!lines.length) {
    if (result.ok === true || rc === 0) return [{ text: '(no output)', cls: 'sys' }];
    lines.push({ text: JSON.stringify(result).slice(0, 2000), cls: 'out' });
  }
  return lines;
}
function syncPrompt() {
  const p = $('#cc-term-prompt'); if (p) p.innerHTML = promptStr();
  const head = S.el && S.el.querySelector('.cc-term-head');
  if (head) { const dev = selectedDevice(); head.innerHTML = `<div class="dots"><i></i><i></i><i></i></div>${esc(dev ? dev.name : 'device')} — bash · ${esc(S.termCwd || 'home')}`; }
}

function updateTerminalDom() {
  const body = $('#cc-term-body');
  if (body) {
    body.innerHTML = S.termHistory.map((h) => renderTermEntry(h)).join('') ||
      `<div class="cc-term-line sys">Ready.</div>`;
    body.scrollTop = body.scrollHeight;
  }
  const inp = $('#cc-term-input');
  if (inp && document.activeElement !== inp) inp.value = S.termInput;
}

// ── Wiring ──
function wire() {
  S.el.querySelector('.cc-tabs')?.addEventListener('keydown', e => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
    const tabs = [...S.el.querySelectorAll('[data-cc-tab]')];
    const index = tabs.indexOf(document.activeElement);
    if (index < 0) return;
    e.preventDefault();
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : (index + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    const name = tabs[next].dataset.ccTab;
    S.tab = name; render();
    S.el.querySelector(`[data-cc-tab="${name}"]`)?.focus();
  });
  $('[data-cc-palette]')?.addEventListener('click', openPalette);
  S.el.querySelectorAll('[data-cc-device]').forEach((b) => b.addEventListener('click', () => {
    const id = b.getAttribute('data-cc-device');
    if (id === S.deviceId) return;
    S.deviceId = id; setStoredDevice(id);
    resetDeviceCaches();
    S.overview = S.overview || {};
    if (S.overview) S.overview.pc = {};
    render(); refresh();
  }));
  S.el.querySelectorAll('[data-cc-tab]').forEach((b) => b.addEventListener('click', () => {
    S.tab = b.getAttribute('data-cc-tab'); render();
  }));
  const closeBtn = $('[data-cc-close]');
  if (closeBtn) closeBtn.addEventListener('click', close);
  const devSelect = $('[data-cc-device-select]');
  if (devSelect) devSelect.addEventListener('change', () => {
    const id = devSelect.value;
    if (id === S.deviceId) return;
    S.deviceId = id; setStoredDevice(id);
    resetDeviceCaches();
    if (S.overview) S.overview.pc = {};
    render(); refresh();
  });
  S.el.querySelectorAll('[data-cc-approve]').forEach((b) => b.addEventListener('click', async () => {
    b.disabled = true;
    try { await api('/pc/confirm/' + encodeURIComponent(b.getAttribute('data-cc-approve')), { method: 'POST' }); } catch (e) { toast(String(e.message || e), true); }
    refresh();
  }));
  S.el.querySelectorAll('[data-cc-deny]').forEach((b) => b.addEventListener('click', async () => {
    b.disabled = true;
    try { await api('/pc/pending/' + encodeURIComponent(b.getAttribute('data-cc-deny')), { method: 'DELETE' }); } catch (e) { toast(String(e.message || e), true); }
    refresh();
  }));
  const inp = $('#cc-term-input');
  if (inp) {
    inp.addEventListener('input', () => { S.termInput = inp.value; });
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); runCommand(inp.value); }
      else if (e.key === 'ArrowUp') {
        e.preventDefault();
        if (!S.termCmds.length) return;
        S.termIdx = S.termIdx < 0 ? S.termCmds.length - 1 : Math.max(0, S.termIdx - 1);
        inp.value = S.termCmds[S.termIdx]; S.termInput = inp.value;
        setTimeout(() => inp.setSelectionRange(inp.value.length, inp.value.length), 0);
      } else if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (S.termIdx < 0) return;
        S.termIdx++;
        if (S.termIdx >= S.termCmds.length) { S.termIdx = -1; inp.value = ''; }
        else inp.value = S.termCmds[S.termIdx];
        S.termInput = inp.value;
      }
    });
  }
  // Rows are <div role="button">, so Enter/Space have to be wired by hand.
  const activate = (el, fn) => {
    el.addEventListener('click', fn);
    if (el.getAttribute('role') === 'button') {
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fn(); }
      });
    }
  };
  S.el.querySelectorAll('[data-cc-cd]').forEach((r) => activate(r, () => {
    if (r.disabled) return;
    loadFiles(r.getAttribute('data-cc-cd'));
  }));
  S.el.querySelectorAll('[data-cc-open]').forEach((r) => activate(r, () => openFile(r.getAttribute('data-cc-open'))));
  const closePreview = $('[data-cc-close-preview]');
  if (closePreview) closePreview.addEventListener('click', () => { delete S._requests.preview; S._filePreview = null; render(); });
  const filesPath = $('#cc-files-path');
  if (filesPath) {
    filesPath.addEventListener('input', () => { S._pathDraft = filesPath.value; });
    filesPath.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); loadFiles(filesPath.value.trim()); } });
  }
  const refreshFiles = $('[data-cc-refresh-files]');
  if (refreshFiles) refreshFiles.addEventListener('click', () => loadFiles((S._files && S._files.path) || ''));
  const tasksBtn = $('[data-cc-open-tasks]');
  if (tasksBtn) tasksBtn.addEventListener('click', () => document.getElementById('tool-tasks-btn')?.click());
  const addBtn = $('[data-cc-add]');
  if (addBtn) addBtn.addEventListener('click', () => { close(); document.getElementById('tool-command-btn'); /* legacy enroll UI */ openLegacyEnroll(); });

  // Processes
  const refreshProcs = $('[data-cc-refresh-procs]');
  if (refreshProcs) refreshProcs.addEventListener('click', () => { S._procs = null; loadProcesses(); });
  S.el.querySelectorAll('[data-cc-kill]').forEach((b) => b.addEventListener('click', async () => {
    const pid = b.getAttribute('data-cc-kill');
    b.disabled = true; b.textContent = '…';
    try {
      await runAction('kill_process', { pid: Number(pid) });
      toast(`Process ${pid} killed`);
    } catch (e) { toast(String(e.message || e), true); }
    S._procs = null; loadProcesses(); refresh();
  }));

  // Apps & windows
  const refreshWins = $('[data-cc-refresh-windows]');
  if (refreshWins) refreshWins.addEventListener('click', () => { S._windowsList = null; loadWindows(); });
  S.el.querySelectorAll('[data-cc-app-focus], [data-cc-app-close]').forEach((b) => b.addEventListener('click', async () => {
    const close_ = b.hasAttribute('data-cc-app-close');
    const key = b.getAttribute(close_ ? 'data-cc-app-close' : 'data-cc-app-focus');
    let w = {}; try { w = JSON.parse(key); } catch (_) {}
    b.disabled = true;
    try {
      await runAction(close_ ? 'app_close' : 'app_focus', w);
      toast(close_ ? 'Window closed' : 'Window focused');
    } catch (e) { toast(String(e.message || e), true); }
    if (close_) { S._windowsList = null; loadWindows(); }
    b.disabled = false;
    refresh();
  }));

  // System: power, media, volume
  S.el.querySelectorAll('[data-cc-sys]').forEach((b) => b.addEventListener('click', async () => {
    const action = b.getAttribute('data-cc-sys');
    b.disabled = true;
    try {
      const r = await runAction(action, {});
      toast(r && r.pending ? `${action} queued — approve it in "Waiting on you"` : `${action === 'lock' ? 'Locked' : action} sent`);
    } catch (e) { toast(String(e.message || e), true); }
    b.disabled = false;
    refresh();
  }));
  S.el.querySelectorAll('[data-cc-media]').forEach((b) => b.addEventListener('click', async () => {
    const command = b.getAttribute('data-cc-media');
    try { await runAction('media', { command }); } catch (e) { toast(String(e.message || e), true); }
  }));
  const volSlider = $('#cc-vol-slider');
  if (volSlider) {
    const volVal = $('#cc-vol-val');
    volSlider.addEventListener('input', () => { if (volVal) volVal.textContent = volSlider.value + '%'; });
    volSlider.addEventListener('change', async () => {
      try { await runAction('volume', { percent: Number(volSlider.value) }); } catch (e) { toast(String(e.message || e), true); }
    });
  }

  // Screen
  const captureBtn = $('[data-cc-capture]');
  if (captureBtn) captureBtn.addEventListener('click', captureScreen);

  // Clipboard
  const clipGet = $('[data-cc-clip-get]');
  if (clipGet) clipGet.addEventListener('click', readClipboard);
  const clipBox = $('#cc-clip-text');
  if (clipBox) clipBox.addEventListener('input', () => { S._clipDraft = clipBox.value; });
  const clipSet = $('[data-cc-clip-set]');
  if (clipSet) clipSet.addEventListener('click', async () => {
    const box = $('#cc-clip-text');
    const text = box ? box.value : '';
    // Sending "" would blank the device clipboard — almost never intended, and
    // it used to happen by accident whenever a read had failed.
    if (!text) { toast('Nothing to send — type something first', true); return; }
    try { await runAction('clipboard_set', { text }); toast('Sent to device clipboard'); } catch (e) { toast(String(e.message || e), true); }
  });
  const clipCopy = $('[data-cc-clip-copy]');
  if (clipCopy) clipCopy.addEventListener('click', async () => {
    const box = $('#cc-clip-text');
    const text = box ? box.value : '';
    try { await navigator.clipboard.writeText(text); toast('Copied to your clipboard'); } catch (e) { toast(String(e.message || e), true); }
  });

  // Remote Desktop
  const remoteSetup = $('[data-cc-remote-setup]');
  if (remoteSetup) remoteSetup.addEventListener('click', setupRemote);
  const remoteOpen = $('[data-cc-remote-open]');
  if (remoteOpen) remoteOpen.addEventListener('click', openRemoteConsole);
  const remoteRefresh = $('[data-cc-remote-refresh]');
  if (remoteRefresh) remoteRefresh.addEventListener('click', () => { S._remote = null; loadRemote(); });
  // Close button for the remote overlay is wired once, in remoteOverlayEl() —
  // that node lives outside #command-console so wire() (rerun on every
  // render) never touches it.
  const remoteCopy = $('[data-cc-remote-copy]');
  if (remoteCopy) remoteCopy.addEventListener('click', async () => {
    if (!S._remoteInvite) return;
    try { await navigator.clipboard.writeText(new URL(S._remoteInvite, window.location.origin).href); toast('Remote installer link copied'); } catch (_) {}
  });

  // Chat bots (Telegram / Discord)
  S.el.querySelectorAll('[data-cc-bot-pair]').forEach((b) => b.addEventListener('click', () => pairBot(b.getAttribute('data-cc-bot-pair'))));
  S.el.querySelectorAll('[data-cc-bot-unlink]').forEach((b) => b.addEventListener('click', () => unlinkBot(b.getAttribute('data-cc-bot-unlink'))));
}

function beginPaneRequest(key) {
  const token = {};
  S._requests[key] = token;
  const generation = S._generation;
  return () => generation === S._generation && S._requests[key] === token;
}

async function loadFiles(path) {
  const current = beginPaneRequest('files');
  S._files = { loading: true };
  Promise.resolve().then(() => { if (S.open && S.tab === 'files' && current()) render(); });
  delete S._requests.preview;
  S._filePreview = null;
  S._pathDraft = null;
  try {
    const r = await runAction('file_list', path ? { path } : {});
    if (!current()) return;
    S._files = r && (r.entries || r.items) ? r : { entries: (r && r.files) || [], path: (r && r.path) || path || '~' };
  } catch (e) {
    if (!current()) return;
    S._files = { entries: [], path: path || '~', error: String(e.message || e) };
  }
  if (S.open && S.tab === 'files') render();
}

async function openFile(path) {
  const current = beginPaneRequest('preview');
  S._filePreview = { path, name: path.split('/').pop() || path, text: '', loading: true };
  if (S.open && S.tab === 'files') render();
  try {
    const r = await runAction('file_read', { path });
    if (!current()) return;
    S._filePreview = Object.assign({ path, name: path.split('/').pop() || path }, r || {});
  } catch (e) {
    if (!current()) return;
    S._filePreview = { path, error: String(e.message || e) };
  }
  if (S.open && S.tab === 'files') render();
}

// ── Processes ──
function renderProcessesPane(dev) {
  if (!dev || !dev.online) return `<div class="cc-empty">Device offline — processes unavailable.</div>`;
  const data = S._procs;
  if (!data) loadProcesses();
  if (!data || data.loading) return `<div class="cc-empty" role="status">Loading processes…</div>`;
  if (data.error) return `<div class="cc-empty">Could not list processes — ${esc(data.error)}</div>`;
  const all = data.processes || [];
  const head = all[0] || 'PID  COMMAND  %CPU  %MEM';
  const rows = all.slice(1).map((line) => {
    const pid = (String(line).match(/^\s*(\d+)/) || [])[1] || '';
    return `<div class="cc-proc-row">
      <span class="cc-proc-line mono">${esc(line)}</span>
      ${pid ? `<button type="button" class="cc-btn danger sm" data-cc-kill="${esc(pid)}">Kill</button>` : ''}
    </div>`;
  }).join('');
  return `
    <div class="cc-sec-lbl">Top processes <button type="button" class="cc-btn sm" data-cc-refresh-procs style="margin-left:8px">Refresh</button></div>
    <div class="cc-proc-head mono">${esc(head)}</div>
    <div class="cc-procs">${rows || '<div class="cc-empty">No processes reported.</div>'}</div>`;
}
async function loadProcesses() {
  const current = beginPaneRequest('processes');
  S._procs = { loading: true };
  Promise.resolve().then(() => { if (S.open && S.tab === 'processes' && current()) render(); });
  try {
    const r = await runAction('processes', { limit: 40 });
    if (!current()) return;
    S._procs = r || { processes: [] };
  } catch (e) {
    if (!current()) return;
    S._procs = { processes: [], error: String(e.message || e) };
  }
  if (S.open && S.tab === 'processes') render();
}

// ── Apps & windows ──
function renderAppsPane(dev) {
  if (!dev || !dev.online) return `<div class="cc-empty">Device offline — windows unavailable.</div>`;
  const data = S._windowsList;
  if (!data) loadWindows();
  if (!data || data.loading) return `<div class="cc-empty" role="status">Loading open windows…</div>`;
  if (data.error) return `<div class="cc-empty">Could not list windows — ${esc(data.error)}</div>`;
  const rows = (data.windows || []).map((w) => {
    const key = esc(JSON.stringify({ title: w.title || '', id: w.id || '', class: w.class || '' }));
    return `<div class="cc-win-row">
      <span class="nm">${esc(w.title || w.class || '(untitled)')}</span>
      <span class="sub mono">${esc(w.class || '')}${w.pid ? ' · pid ' + esc(w.pid) : ''}</span>
      <span class="cc-win-actions">
        <button type="button" class="cc-btn sm" data-cc-app-focus="${key}">Focus</button>
        <button type="button" class="cc-btn sm danger" data-cc-app-close="${key}">Close</button>
      </span>
    </div>`;
  }).join('');
  return `
    <div class="cc-sec-lbl">Open windows <button type="button" class="cc-btn sm" data-cc-refresh-windows style="margin-left:8px">Refresh</button></div>
    <div class="cc-wins">${rows || '<div class="cc-empty">No visible windows reported.</div>'}</div>`;
}
async function loadWindows() {
  const current = beginPaneRequest('windows');
  S._windowsList = { loading: true };
  Promise.resolve().then(() => { if (S.open && S.tab === 'apps' && current()) render(); });
  try {
    const r = await runAction('windows', {});
    if (!current()) return;
    S._windowsList = r || { windows: [] };
  } catch (e) {
    if (!current()) return;
    S._windowsList = { windows: [], error: String(e.message || e) };
  }
  if (S.open && S.tab === 'apps') render();
}

// ── System: power + media + volume ──
function renderSystemPane(dev) {
  if (!dev || !dev.online) return `<div class="cc-empty">Device offline — power &amp; media controls unavailable.</div>`;
  return `
    <div class="cc-sec-lbl">Power</div>
    <div class="cc-sys-row">
      <button type="button" class="cc-btn" data-cc-sys="lock">🔒 Lock screen</button>
      <button type="button" class="cc-btn danger" data-cc-sys="sleep">💤 Sleep</button>
      <button type="button" class="cc-btn danger" data-cc-sys="shutdown">⏻ Shut down</button>
    </div>
    <div class="cc-empty" style="margin-top:8px">Sleep and shut down are flagged critical — they land in "Waiting on you" (Overview) for one-tap approval instead of running instantly.</div>

    <div class="cc-sec-lbl" style="margin-top:22px">Media</div>
    <div class="cc-sys-row">
      <button type="button" class="cc-btn" data-cc-media="previous">⏮ Prev</button>
      <button type="button" class="cc-btn" data-cc-media="play-pause">⏯ Play/Pause</button>
      <button type="button" class="cc-btn" data-cc-media="next">⏭ Next</button>
    </div>

    <div class="cc-sec-lbl" style="margin-top:22px">Volume</div>
    <div class="cc-vol-row">
      <input type="range" min="0" max="100" value="50" id="cc-vol-slider" aria-label="Device volume" class="cc-slider">
      <span class="mono" id="cc-vol-val">50%</span>
    </div>`;
}

// ── Screen: on-demand screenshot ──
function renderScreenPane(dev) {
  if (!dev || !dev.online) return `<div class="cc-empty">Device offline — screen capture unavailable.</div>`;
  const shot = S._shot;
  const btn = `<button type="button" class="cc-btn primary" data-cc-capture ${shot && shot.loading ? 'disabled' : ''}>${shot && shot.loading ? 'Capturing…' : '📷 Capture screen'}</button>`;
  if (!shot || shot.loading) {
    return `<div class="cc-sec-lbl">Screen</div>${btn}${!shot ? '<div class="cc-empty" style="margin-top:12px">Take an on-demand screenshot of this device — nothing streams until you ask.</div>' : ''}`;
  }
  if (shot.error) return `<div class="cc-sec-lbl">Screen</div>${btn}<div class="cc-empty" style="margin-top:12px">${esc(shot.error)}</div>`;
  if (!shot.image_b64) return `<div class="cc-sec-lbl">Screen</div>${btn}<div class="cc-empty" style="margin-top:12px">The device returned no image. Screen capture may be blocked by the OS (on macOS, grant Screen Recording permission).</div>`;
  return `
    <div class="cc-sec-lbl">Screen <span class="mono" style="font-weight:400;text-transform:none;color:var(--fg-faint)">captured ${esc(ago(shot.ts))}${shot.width ? ` · ${shot.width}×${shot.height}` : ''}</span></div>
    ${btn}
    <div class="cc-shot"><img src="data:${esc(shot.mime || 'image/png')};base64,${shot.image_b64}" alt="Device screen"></div>`;
}
async function captureScreen() {
  const current = beginPaneRequest('screen');
  S._shot = { loading: true };
  if (S.open && S.tab === 'screen') render();
  try {
    const r = await runAction('screenshot', {});
    if (!current()) return;
    S._shot = Object.assign({}, r, { ts: Date.now() / 1000 });
  } catch (e) {
    if (!current()) return;
    S._shot = { error: String(e.message || e) };
  }
  if (S.open && S.tab === 'screen') render();
}

// ── Clipboard: get/set ──
function renderClipboardPane(dev) {
  if (!dev || !dev.online) return `<div class="cc-empty">Device offline — clipboard unavailable.</div>`;
  const clip = S._clip;
  const text = S._clipDraft != null ? S._clipDraft : ((clip && clip.text) || '');
  return `
    <div class="cc-sec-lbl">Device clipboard <button type="button" class="cc-btn sm" data-cc-clip-get ${clip && clip.loading ? 'disabled' : ''} style="margin-left:8px">${clip && clip.loading ? 'Reading…' : clip ? 'Refresh' : 'Read clipboard'}</button></div>
    ${clip && clip.error ? `<div class="cc-empty">Could not read the device clipboard — ${esc(clip.error)}</div>` : ''}
    <textarea class="cc-clip-box mono" id="cc-clip-text" aria-label="Device clipboard contents" placeholder="Nothing read yet — click Read clipboard, or type here and Send to set it.">${esc(text)}</textarea>
    <div class="cc-sys-row" style="margin-top:10px">
      <button type="button" class="cc-btn primary" data-cc-clip-set>Send to device clipboard</button>
      <button type="button" class="cc-btn" data-cc-clip-copy>Copy to my clipboard</button>
    </div>`;
}
async function readClipboard() {
  const current = beginPaneRequest('clipboard');
  const draft = S._clipDraft;
  S._clip = Object.assign({}, S._clip, { loading: true });
  if (S.open && S.tab === 'clipboard') render();
  try {
    const r = await runAction('clipboard_get', {});
    if (!current()) return;
    S._clip = r || { text: '' };
    if (S._clipDraft === draft) S._clipDraft = null;
  } catch (e) {
    if (!current()) return;
    S._clip = { text: '', error: String(e.message || e) };
  }
  if (S.open && S.tab === 'clipboard') render();
}

// ── Remote Desktop (MeshCentral, account-isolated) ──
function renderRemotePane() {
  const data = S._remote;
  if (!data) loadRemote();
  if (!data || data.loading) return `<div class="cc-empty" role="status">Checking Remote Desktop status…</div>`;
  if (!data.configured) {
    return `
      <div class="cc-sec-lbl">Remote Desktop</div>
      <div class="cc-empty">Remote Desktop is disabled on this server.<br>Enable the isolated Docker profile and proxy <code>/remote/</code> through the same HTTPS origin:</div>
      <div class="cc-file-preview-body mono" style="margin-top:8px">docker compose --profile remote up -d</div>`;
  }
  const ready = Boolean(data.account_ready);
  const devices = data.devices || [];
  const rows = devices.map((d) => `
    <div class="cc-win-row">
      <span class="cc-dev-dot-inline ${d.connected ? 'online' : ''}"></span>
      <span class="nm">${esc(d.name)}</span>
      <span class="sub mono">${esc(d.os || '')} · ${d.connected ? 'online' : 'offline'}</span>
    </div>`).join('');
  const invite = S._remoteInvite ? `
    <div class="cc-remote-invite">
      <div><strong>Remote agent installer</strong><div class="cc-empty" style="padding:0">Expires automatically · adds this PC only to your private remote group.</div></div>
      <div class="cc-file-preview-body mono">${esc(S._remoteInvite)}</div>
      <div class="cc-sys-row">
        <a class="cc-btn primary" href="${esc(S._remoteInvite)}" target="_blank" rel="noopener">Open installer</a>
        <button type="button" class="cc-btn" data-cc-remote-copy>Copy link</button>
      </div>
    </div>` : '';
  return `
    <div class="cc-sec-lbl">Remote Desktop <span class="mono" style="font-weight:400;text-transform:none;color:var(--fg-faint)">interactive mouse/keyboard/screen — no AI in the loop</span></div>
    <div class="cc-empty">${ready ? 'Private remote workspace is ready. Every Shadow account gets its own isolated MeshCentral identity and device group — nobody else can see or reach these PCs.' : 'Enable interactive remote access — a separate, fully manual control channel from the AI-driven actions above (mouse, keyboard, live screen, cross-platform: Windows, Linux, macOS).'}</div>
    <div class="cc-sys-row" style="margin-top:12px">
      <button type="button" class="cc-btn primary" data-cc-remote-setup>${ready ? '+ Add remote PC' : 'Enable Remote Desktop'}</button>
      ${ready ? '<button type="button" class="cc-btn" data-cc-remote-open>Open console</button><button type="button" class="cc-btn" data-cc-remote-refresh>Refresh agents</button>' : ''}
    </div>
    ${invite}
    <div class="cc-wins" style="margin-top:14px">${rows || (ready ? '<div class="cc-empty">No MeshCentral agent enrolled yet — generate an installer link above and run it on the target PC.</div>' : '')}</div>
    ${renderChatBotsSection()}`;
}

// ── Chat bots: control this PC by texting a Telegram/Discord bot ──
// A completely separate control surface from MeshCentral above — text
// commands (/status, /screen, /lock, /approve …) relayed through a bot your
// Shadow account pairs with, rather than an interactive desktop session.
function botLabel(platform) { return platform === 'discord' ? 'Discord' : 'Telegram'; }

function renderBotCard(platform) {
  const bot = S._bots[platform];
  const label = botLabel(platform);
  if (!bot.status) { loadBotStatus(platform); return `<div class="cc-empty">Checking ${esc(label)}…</div>`; }
  if (!bot.status.configured) {
    return `<div class="cc-empty">${esc(label)} bot is not configured on this server — set <code>SHADOW_${platform.toUpperCase()}_BOT_TOKEN</code> and run <code>docker compose --profile ${esc(platform)} up -d</code>.</div>`;
  }
  if (bot.status.linked) {
    return `
      <div class="cc-empty" style="padding:0">Paired — this PC can be controlled from your ${esc(label)} bot chat.</div>
      <div class="cc-sys-row" style="margin-top:8px">
        <button type="button" class="cc-btn danger sm" data-cc-bot-unlink="${platform}">Unlink ${esc(label)}</button>
      </div>`;
  }
  const code = bot.pairCode ? `
    <div class="cc-file-preview-body mono" style="margin-top:8px">/pair ${esc(bot.pairCode)}</div>
    <div class="cc-empty" style="padding:4px 0 0">Send that to your Shadow ${esc(label)} bot within 10 minutes.</div>` : '';
  return `
    <div class="cc-sys-row">
      <button type="button" class="cc-btn primary sm" data-cc-bot-pair="${platform}">Pair ${esc(label)}</button>
    </div>
    ${code}`;
}

function renderChatBotsSection() {
  return `
    <div class="cc-sec-lbl" style="margin-top:22px">Chat bots <span class="mono" style="font-weight:400;text-transform:none;color:var(--fg-faint)">text a bot to run PC commands — status, screenshot, approvals, and more</span></div>
    <div class="cc-metrics" style="align-items:flex-start">
      <div class="cc-metric" style="text-align:left"><div class="lbl">Telegram</div><div style="margin-top:8px">${renderBotCard('telegram')}</div></div>
      <div class="cc-metric" style="text-align:left"><div class="lbl">Discord</div><div style="margin-top:8px">${renderBotCard('discord')}</div></div>
    </div>`;
}

async function loadBotStatus(platform) {
  try {
    S._bots[platform].status = await api(`/${platform}/status`);
  } catch (e) {
    S._bots[platform].status = { configured: false, error: String(e.message || e) };
  }
  if (S.open && S.tab === 'remote') render();
}

async function pairBot(platform) {
  try {
    const data = await api(`/${platform}/pair-code`, { method: 'POST' });
    S._bots[platform].pairCode = data.code || '';
    render();
  } catch (e) {
    toast(String(e.message || e), true);
  }
}

async function unlinkBot(platform) {
  try {
    await api(`/${platform}/link`, { method: 'DELETE' });
    S._bots[platform].status = null;
    S._bots[platform].pairCode = null;
    toast(`${botLabel(platform)} unlinked`);
    loadBotStatus(platform);
  } catch (e) {
    toast(String(e.message || e), true);
  }
}

// The interactive overlay lives OUTSIDE #command-console on purpose: render()
// replaces S.el's entire innerHTML on every 8s poll tick (see refresh()), and
// re-templating a live MeshCentral iframe on a timer would tear down and
// restart the session underneath the user mid-use. Build it once, lazily,
// as its own body-level node so nothing but open/close ever touches it.
function remoteOverlayEl() {
  let el = document.getElementById('cc-remote-overlay-root');
  if (el) return el;
  el = document.createElement('div');
  el.id = 'cc-remote-overlay-root';
  el.className = 'cc-remote-overlay';
  el.hidden = true;
  el.innerHTML = `
    <div class="cc-remote-overlay-head">
      <span>Interactive Remote Desktop</span>
      <button type="button" class="cc-close" data-cc-remote-close aria-label="Close">✕</button>
    </div>
    <iframe name="cc-remote-frame" data-cc-remote-frame title="Shadow Remote Desktop"></iframe>`;
  document.body.appendChild(el);
  el.querySelector('[data-cc-remote-close]').addEventListener('click', closeRemoteConsole);
  return el;
}
async function loadRemote() {
  const current = beginPaneRequest('remote');
  S._remote = { loading: true };
  try {
    const result = await api('/remote/status');
    if (!current()) return;
    S._remote = result;
  } catch (e) {
    if (!current()) return;
    S._remote = { configured: false, error: String(e.message || e) };
  }
  if (S.open && S.tab === 'remote') render();
}
async function setupRemote() {
  try {
    const data = await api('/remote/setup', { method: 'POST' });
    S._remoteInvite = data.invite_url || '';
    await loadRemote();
    toast('Remote Desktop installer is ready');
  } catch (e) {
    toast(String(e.message || e), true);
  }
}
async function openRemoteConsole() {
  try {
    const session = await api('/remote/session', { method: 'POST' });
    const overlay = remoteOverlayEl();
    const frame = overlay.querySelector('[data-cc-remote-frame]');
    frame.src = 'about:blank';
    overlay.hidden = false;
    document.body.classList.add('command-remote-open');
    const form = document.createElement('form');
    form.method = 'POST';
    form.action = session.login_url;
    form.target = frame.name;
    form.hidden = true;
    for (const [name, value] of [['action', 'login'], ['username', session.token_user], ['password', session.token_pass]]) {
      const input = document.createElement('input');
      input.type = 'hidden'; input.name = name; input.value = value;
      form.appendChild(input);
    }
    document.body.appendChild(form);
    form.submit();
    form.remove();
    toast('Session expires in three minutes; the console itself stays account-scoped.');
  } catch (e) {
    toast(String(e.message || e), true);
  }
}
function closeRemoteConsole() {
  const overlay = document.getElementById('cc-remote-overlay-root');
  if (!overlay) return;
  const frame = overlay.querySelector('[data-cc-remote-frame]');
  if (frame) frame.src = 'about:blank';
  overlay.hidden = true;
  document.body.classList.remove('command-remote-open');
}

function resetDeviceCaches() {
  S._generation++; S._requests = {};
  if (S._remote && S._remote.loading) S._remote = null;
  S.termCwd = ''; S.termInput = ''; S.termCmds = []; S.termIdx = -1;
  S._files = null; S._filePreview = null; S.termHistory = [];
  S._procs = null; S._windowsList = null; S._shot = null; S._clip = null;
  S._clipDraft = null; S._pathDraft = null;
}

function openLegacyEnroll() {
  // Fall back to the original command page's enrollment flow.
  if (window.__legacyCommandOpen) window.__legacyCommandOpen();
}

// ── Open / close ──
function open() {
  if (!S.el) {
    S.el = document.createElement('section');
    S.el.id = 'command-console';
    document.body.appendChild(S.el);
    window.addEventListener('resize', () => { if (S.open) positionConsole(); });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && S.open && !S.el.querySelector('#cc-term-input:focus')) close();
    });
  }
  S.deviceId = S.deviceId || getStoredDevice();
  S.open = true;
  S.el.classList.add('open');
  document.body.classList.add('command-console-open');
  render();
  // Prime the view. The first /overview can 503 on a cold backend, so retry a
  // few times at increasing delays until devices land, rather than showing
  // "No devices" until the 8s poll recovers.
  (async () => {
    for (const delay of [0, 900, 2000, 4000]) {
      if (!S.open) return;
      if (delay) await new Promise((r) => setTimeout(r, delay));
      await refresh();
      if (S.overview && (S.overview.devices || []).length) return;
    }
  })();
  if (S.pollTimer) clearInterval(S.pollTimer);
  S.pollTimer = setInterval(() => { if (S.open) refresh(); }, POLL_MS);
}
function close() {
  S.open = false;
  closeRemoteConsole();
  if (S.el) S.el.classList.remove('open');
  document.body.classList.remove('command-console-open');
  if (S.pollTimer) { clearInterval(S.pollTimer); S.pollTimer = null; }
  try { if (location.pathname === '/command') history.replaceState(null, '', '/'); } catch (_) {}
}

// Global command entry lives outside the Console's polling subtree.
const PALETTE_ACTIONS = new Set(['status', 'processes', 'screenshot', 'clipboard_get', 'windows', 'file_list', 'file_read', 'file_search', 'clipboard_set', 'media', 'volume', 'app_launch', 'app_focus', 'type_text', 'keypress', 'mouse_move', 'mouse_click', 'app_close', 'kill_process', 'file_write', 'shell', 'lock', 'runbook', 'sleep', 'shutdown']);
function parseCommandIntent(value) {
  const text = String(value || '').trim();
  if (/^(?:open |search )?chats$|^search conversations$/i.test(text)) return {tab: 'search'};
  const nav = text.match(/^(?:go to |show |open )?(overview|terminal|files|processes|apps|system|screen|clipboard|remote|tasks|timeline|missions)$/i);
  if (nav) return {tab: nav[1].toLowerCase()};
  const simple = {
    'device status': 'status', 'check status': 'status', 'list processes': 'processes',
    'take screenshot': 'screenshot', 'take a screenshot': 'screenshot', 'capture screen': 'screenshot',
    'read clipboard': 'clipboard_get', 'list windows': 'windows', 'list files': 'file_list',
    'lock screen': 'lock', 'sleep device': 'sleep', 'shut down device': 'shutdown',
  };
  if (simple[text.toLowerCase()]) return {action: simple[text.toLowerCase()], args: {}};
  const patterns = [
    [/^(?:set )?volume(?: to)? (\d{1,3})%?$/i, 'volume', 'percent'],
    [/^(?:launch|start) app (.+)$/i, 'app_launch', 'app'],
    [/^focus app (.+)$/i, 'app_focus', 'title'],
    [/^close app (.+)$/i, 'app_close', 'title'],
    [/^(?:find|search) files? (.+)$/i, 'file_search', 'query'],
    [/^(?:list files in|list folder) (.+)$/i, 'file_list', 'path'],
    [/^read file (.+)$/i, 'file_read', 'path'],
    [/^(?:copy to clipboard|set clipboard) (.+)$/i, 'clipboard_set', 'text'],
    [/^type text (.+)$/i, 'type_text', 'text'],
    [/^press (.+)$/i, 'keypress', 'key'],
    [/^(?:run shell|shell:)\s*(.+)$/i, 'shell', 'command'],
    [/^kill process (\d+)$/i, 'kill_process', 'pid'],
    [/^media (play|pause|play-pause|next|previous|stop)$/i, 'media', 'command'],
  ];
  for (const [pattern, action, key] of patterns) {
    const match = text.match(pattern);
    if (!match) continue;
    const value = ['percent', 'pid'].includes(key) ? Number(match[1]) : match[1];
    if (key === 'percent' && value > 100) throw new Error('Volume must be between 0 and 100.');
    return {action, args: {[key]: value}};
  }
  const raw = text.match(/^([a-z_]+)(?:\s+(\{[\s\S]*\}))?$/);
  if (raw && PALETTE_ACTIONS.has(raw[1])) {
    const args = raw[2] ? JSON.parse(raw[2]) : {};
    if (!args || Array.isArray(args) || typeof args !== 'object') throw new Error('Action arguments must be a JSON object.');
    return {action: raw[1], args};
  }
  throw new Error('Try “take a screenshot”, “find files report”, “volume 30”, “open tasks”, or an action name with JSON arguments.');
}

let paletteBusy = false;
function paletteEl() {
  let el = document.getElementById('shadow-command-palette');
  if (el) return el;
  el = document.createElement('dialog');
  el.id = 'shadow-command-palette';
  el.setAttribute('aria-labelledby', 'shadow-palette-title');
  el.innerHTML = `<form method="dialog" class="sp-head"><strong id="shadow-palette-title">Command Shadow</strong><button aria-label="Close command palette" class="cc-btn" value="close">✕</button></form>
    <form id="shadow-palette-form">
      <label for="shadow-palette-device">Target device</label><select id="shadow-palette-device"></select>
      <label for="shadow-palette-input">What would you like to do?</label>
      <input id="shadow-palette-input" autocomplete="off" spellcheck="false" placeholder="Take a screenshot, find files report, open tasks…">
      <div id="shadow-palette-preview" role="status" aria-live="polite"></div>
      <button class="cc-btn primary" id="shadow-palette-run" type="submit" disabled>Run command</button>
    </form>
    <div class="sp-examples" aria-label="Example commands">${['device status', 'take a screenshot', 'list processes', 'find files report', 'open tasks', 'open missions'].map(t => `<button type="button" class="cc-btn sm" data-sp-example="${esc(t)}">${esc(t)}</button>`).join('')}</div>
    <pre id="shadow-palette-result" role="status" aria-live="polite"></pre>`;
  document.body.appendChild(el);
  const input = el.querySelector('#shadow-palette-input');
  const device = el.querySelector('#shadow-palette-device');
  const preview = el.querySelector('#shadow-palette-preview');
  const run = el.querySelector('#shadow-palette-run');
  const output = el.querySelector('#shadow-palette-result');
  function update() {
    if (paletteBusy) return;
    try {
      const intent = parseCommandIntent(input.value);
      if (intent.action && !device.value) throw new Error('Select an online device first.');
      preview.textContent = intent.tab ? `Open ${intent.tab}` : `${device.selectedOptions[0].textContent} → ${intent.action} ${JSON.stringify(intent.args)}${['sleep', 'shutdown'].includes(intent.action) ? ' · Requires approval in Overview' : ''}`;
      run.disabled = false;
      run.textContent = intent.tab ? 'Open' : 'Run command';
    } catch (e) {
      preview.textContent = input.value ? String(e.message || e) : 'Preview the exact action and target here before running.';
      run.disabled = true;
    }
  }
  input.addEventListener('input', update);
  device.addEventListener('change', update);
  el.querySelectorAll('[data-sp-example]').forEach(b => b.addEventListener('click', () => { input.value = b.dataset.spExample; update(); input.focus(); }));
  el.addEventListener('keydown', e => { if (e.key === 'Escape') e.stopPropagation(); });
  el.querySelector('#shadow-palette-form').addEventListener('submit', async e => {
    e.preventDefault();
    if (paletteBusy || run.disabled) return;
    let intent;
    try { intent = parseCommandIntent(input.value); } catch (err) { output.textContent = err.message; return; }
    if (intent.tab) {
      el.close();
      if (intent.tab === 'search') { close(); document.getElementById('rail-search-btn')?.click(); return; }
      if (['missions', 'tasks'].includes(intent.tab)) { location.assign('/' + intent.tab); return; }
      if (!S.open) open();
      S.tab = intent.tab; render();
      return;
    }
    const target = device.value;
    if (!target) return;
    paletteBusy = true;
    run.disabled = true; input.disabled = true; device.disabled = true;
    output.textContent = 'Running…';
    try {
      const result = await runAction(intent.action, intent.args, {id: target});
      output.textContent = result && result.pending ? 'Waiting for approval. Open Overview → Waiting on you to approve or deny.' : JSON.stringify(result, null, 2);
      if (intent.action === 'screenshot' && result && result.image_b64) {
        output.textContent = 'Screenshot captured. Open Screen on this device to view it.';
        if (S.deviceId === target) S._shot = Object.assign({}, result, {ts: Date.now() / 1000});
      }
      refresh();
    } catch (err) { output.textContent = 'Command failed: ' + String(err.message || err); }
    finally { paletteBusy = false; input.disabled = false; device.disabled = false; update(); if (el.open) input.focus(); }
  });
  el._update = update;
  return el;
}
function paletteEnabled() {
  try { return localStorage.getItem('shadow.commandPalette.enabled') !== 'false'; } catch (_) { return true; }
}
async function openPalette() {
  if (!paletteEnabled()) return;
  const el = paletteEl();
  if (!el.open) el.showModal();
  const select = el.querySelector('#shadow-palette-device');
  const input = el.querySelector('#shadow-palette-input');
  input.focus();
  if (paletteBusy) return;
  select.disabled = true;
  el.querySelector('#shadow-palette-preview').textContent = 'Loading devices…';
  el.querySelector('#shadow-palette-run').disabled = true;
  await refresh();
  if (!el.open || paletteBusy) return;
  if (S._refreshError) {
    el.querySelector('#shadow-palette-preview').textContent = 'Could not load devices: ' + S._refreshError + '. Close and reopen to retry.';
    return;
  }
  const previous = select.value || S.deviceId;
  select.innerHTML = '<option value="">Choose an online device</option>' + ((S.overview && S.overview.devices) || []).map(d => `<option value="${esc(d.id)}" ${d.online ? '' : 'disabled'}>${esc(d.name)}${d.online ? '' : ' (offline)'}</option>`).join('');
  if ([...select.options].some(o => o.value === previous && !o.disabled)) select.value = previous;
  select.disabled = false;
  el._update();
}

document.addEventListener?.('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k' && !e.altKey && !e.shiftKey && !e.isComposing && paletteEnabled()) {
    e.preventDefault(); e.stopImmediatePropagation(); openPalette();
  }
}, true);

window.shadowCommandConsole = { open, close, refresh, openPalette };

export default { open, close };
