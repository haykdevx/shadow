// ============================================================
// voiceMode.js — "Talk to Shadow" voice overlay.
//
// A full-screen particle-cloud HUD for holding a spoken conversation
// with Shadow, wired end-to-end to the app's REAL on-device stack:
//
//   mic (MediaRecorder)
//     → POST /api/stt/transcribe        (faster-whisper)
//     → inject into the chat + send     (the app's normal pipeline)
//     → wait for the reply to finish     (chatModule.hasActiveStream)
//     → speak it via window.aiTTSManager (Kokoro-82M)
//
// The particle canvas reacts to the conversation state
// (idle / listening / thinking / speaking). Nothing here is a mock —
// if STT or TTS is unavailable the UI says so plainly.
// ============================================================

const V = {
  veil: null, canvas: null, ctx: null,
  stateLbl: null, liveLine: null, mic: null, clock: null,
  transcript: null, turnsEl: null,
  open: false,
  state: 'idle',            // idle | listening | thinking | speaking
  turns: 0,
  running: false,
  t: 0,
  reduceMotion: false,
  lite: false,               // desktop app (WebKitGTK, often software-rendered) — cheaper draw
  particles: [],
  W: 0, H: 0, cx: 0, cy: 0, dpr: 1,
  // recording
  mediaRecorder: null, chunks: [], stream: null, recording: false,
  clockTimer: null,
};

function $(id) { return document.getElementById(id); }

// The desktop app's WebKitGTK view often has no GPU compositor (software
// rendering), where per-frame Canvas work is far pricier than in Chrome —
// see the same signal used for the CSS perf overrides in style.css
// (html.desktop-app). Re-checked on every open since browserLauncher.js
// adds the class asynchronously (pywebviewready), possibly after this
// module's own init() already ran.
function detectLite() {
  return document.documentElement.classList.contains('desktop-app');
}

// ── Particle system (ported from the Shadow Console design) ──
function initParticles() {
  const N = V.lite ? 55 : 170;
  V.particles = [];
  for (let i = 0; i < N; i++) {
    V.particles.push({
      a: Math.random() * Math.PI * 2,
      r: 40 + Math.random() * 95,
      speed: 0.15 + Math.random() * 0.35,
      phase: Math.random() * Math.PI * 2,
      z: Math.random(),
    });
  }
}

function resizeCanvas() {
  if (!V.canvas) return;
  // Software rendering pays for every extra device pixel — stay at 1x there.
  V.dpr = V.lite ? 1 : Math.min(window.devicePixelRatio || 1, 2);
  const rect = V.canvas.getBoundingClientRect();
  V.W = rect.width; V.H = rect.height;
  V.canvas.width = V.W * V.dpr; V.canvas.height = V.H * V.dpr;
  V.ctx.setTransform(V.dpr, 0, 0, V.dpr, 0, 0);
  V.cx = V.W / 2; V.cy = V.H / 2;
}

function radiusScale() {
  if (V.state === 'listening') return 0.6;
  if (V.state === 'speaking') return 1.12 + Math.sin(V.t * 3) * 0.1;
  if (V.state === 'thinking') return 0.9 + Math.sin(V.t * 1.6) * 0.05;
  return 1;
}
function speedScale() {
  if (V.state === 'listening') return 2.3;
  if (V.state === 'speaking') return 1.5;
  if (V.state === 'thinking') return 1.1;
  return 1;
}
function hexA(hex, a) {
  let h = (hex || '#e8737f').trim().replace('#', '');
  if (h.length === 3) h = h.split('').map(c => c + c).join('');
  const r = parseInt(h.substring(0, 2), 16) || 232;
  const g = parseInt(h.substring(2, 4), 16) || 115;
  const b = parseInt(h.substring(4, 6), 16) || 127;
  return `rgba(${r},${g},${b},${a})`;
}

function frame() {
  if (!V.running) return;
  V.t += 0.016;
  const ctx = V.ctx;
  ctx.clearRect(0, 0, V.W, V.H);
  const scale = radiusScale();
  const spd = speedScale();
  const accent = getComputedStyle(document.documentElement).getPropertyValue('--red').trim() || '#e8737f';

  const pts = V.particles.map(p => {
    const ang = p.a + V.t * p.speed * spd * 0.4;
    const wobble = Math.sin(V.t * 1.3 + p.phase) * 8;
    const rad = (p.r + wobble) * scale;
    return { x: V.cx + Math.cos(ang) * rad, y: V.cy + Math.sin(ang) * rad * 0.72, z: p.z };
  });

  // The connecting-line pass is O(n^2) (~14k checks/frame at 170 particles) —
  // fine for a GPU compositor, expensive for WebKitGTK's software rendering.
  // Skip it in lite mode; the dots + glow alone still read as a cloud.
  if (!V.lite) {
    ctx.lineWidth = 0.6;
    for (let i = 0; i < pts.length; i++) {
      for (let j = i + 1; j < pts.length; j++) {
        const dx = pts[i].x - pts[j].x, dy = pts[i].y - pts[j].y;
        const d2 = dx * dx + dy * dy;
        if (d2 < 1400) {
          ctx.strokeStyle = hexA(accent, (1 - d2 / 1400) * 0.12);
          ctx.beginPath(); ctx.moveTo(pts[i].x, pts[i].y); ctx.lineTo(pts[j].x, pts[j].y); ctx.stroke();
        }
      }
    }
  }
  for (const p of pts) {
    ctx.beginPath();
    ctx.fillStyle = hexA(accent, 0.35 + p.z * 0.5);
    ctx.arc(p.x, p.y, 1 + p.z * 1.8, 0, Math.PI * 2);
    ctx.fill();
  }
  const glow = ctx.createRadialGradient(V.cx, V.cy, 0, V.cx, V.cy, 130 * scale);
  glow.addColorStop(0, hexA(accent, V.state === 'idle' ? 0.05 : 0.12));
  glow.addColorStop(1, hexA(accent, 0));
  ctx.fillStyle = glow;
  ctx.fillRect(0, 0, V.W, V.H);

  if (!V.reduceMotion) requestAnimationFrame(frame);
}
function startAnim() { if (!V.running) { V.running = true; requestAnimationFrame(frame); if (V.reduceMotion) frame(); } }
function stopAnim() { V.running = false; }

// ── State + transcript ──
function setState(s, label, line, dim) {
  V.state = s;
  if (V.stateLbl) {
    V.stateLbl.textContent = label;
    V.stateLbl.className = 'voice-state' + (s !== 'idle' ? ' ' + s : '');
  }
  if (V.liveLine) {
    V.liveLine.textContent = line;
    V.liveLine.className = 'voice-live-line' + (dim ? ' dim' : '');
  }
  if (V.mic) V.mic.classList.toggle('on', s === 'listening');
  if (V.reduceMotion) frame();
}
function appendTurn(who, text) {
  if (!V.transcript) return;
  const row = document.createElement('div');
  row.className = 'voice-turn' + (who === 'Shadow' ? ' shadow' : '');
  const whoEl = document.createElement('div'); whoEl.className = 'who'; whoEl.textContent = who;
  const txtEl = document.createElement('div'); txtEl.className = 'txt'; txtEl.textContent = text;
  row.appendChild(whoEl); row.appendChild(txtEl);
  V.transcript.appendChild(row);
  V.transcript.scrollTop = V.transcript.scrollHeight;
}

// ── The real conversation turn ──
async function startListening() {
  if (V.recording) { stopListening(); return; }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    setState('idle', 'Idle', 'Microphone not available in this browser.', true);
    return;
  }
  try {
    V.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    setState('idle', 'Idle', 'Microphone permission denied.', true);
    return;
  }
  V.chunks = [];
  let mime = '';
  if (window.MediaRecorder) {
    if (MediaRecorder.isTypeSupported('audio/webm')) mime = 'audio/webm';
    else if (MediaRecorder.isTypeSupported('audio/mp4')) mime = 'audio/mp4';
  }
  try {
    V.mediaRecorder = mime ? new MediaRecorder(V.stream, { mimeType: mime }) : new MediaRecorder(V.stream);
  } catch (e) {
    V.mediaRecorder = new MediaRecorder(V.stream);
  }
  V.mediaRecorder.ondataavailable = (ev) => { if (ev.data && ev.data.size > 0) V.chunks.push(ev.data); };
  V.mediaRecorder.onstop = () => onRecordingStopped(mime || 'audio/webm');
  V.mediaRecorder.start();
  V.recording = true;
  setState('listening', 'Listening', 'Listening… click again when you’re done.', false);
}

function stopListening() {
  if (V.mediaRecorder && V.recording) {
    V.recording = false;
    try { V.mediaRecorder.stop(); } catch (e) {}
  }
  if (V.stream) { V.stream.getTracks().forEach(t => t.stop()); V.stream = null; }
}

async function onRecordingStopped(mime) {
  setState('thinking', 'Transcribing', 'Transcribing what you said…', true);
  const blob = new Blob(V.chunks, { type: mime });
  V.chunks = [];
  if (blob.size < 800) {
    setState('idle', 'Idle', 'Didn’t catch that — tap the mic and try again.', true);
    return;
  }
  let userText = '';
  try {
    const fd = new FormData();
    fd.append('file', blob, 'voice.webm');
    const res = await fetch('/api/stt/transcribe', { method: 'POST', body: fd });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      const msg = (detail && detail.detail && detail.detail.message) || ('STT error ' + res.status);
      setState('idle', 'Idle', msg, true);
      return;
    }
    const data = await res.json();
    userText = (data.text || '').trim();
  } catch (e) {
    setState('idle', 'Idle', 'Transcription failed — check the STT service.', true);
    return;
  }
  if (!userText) {
    setState('idle', 'Idle', 'Silence — tap the mic and try again.', true);
    return;
  }
  appendTurn('You', userText);
  V.turns++; if (V.turnsEl) V.turnsEl.textContent = V.turns;
  await sendToShadow(userText);
}

// Send the transcribed text through the app's normal chat pipeline and
// wait for the assistant reply to finish, then speak it.
async function sendToShadow(text) {
  setState('thinking', 'Thinking', text, false);
  const input = $('message');
  // Canonical send path: the chat form's submit button — the exact element
  // the Enter-to-send handler clicks (falls back to .send-btn).
  const form = $('chat-form');
  const sendBtn = (form && form.querySelector('button[type="submit"]')) || document.querySelector('.send-btn');
  const sessionModule = window.sessionModule;
  const chatModule = window.chatModule;
  if (!input || !sendBtn) {
    setState('idle', 'Idle', 'Chat input unavailable.', true);
    return;
  }

  // Snapshot the assistant messages already present so we can spot the new one.
  const historyEl = $('chat-history');
  const beforeAiCount = historyEl ? historyEl.querySelectorAll('.msg-ai').length : 0;

  // Inject + send exactly as a user would (value → input event flips the
  // button out of mic-mode → click submits).
  input.value = text;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  await sleep(30);
  sendBtn.click();

  const sid = sessionModule && sessionModule.getCurrentSessionId
    ? sessionModule.getCurrentSessionId() : null;

  // Wait for the stream to start, then finish (bounded).
  const startedAt = Date.now();
  const streamActive = () => {
    try {
      if (chatModule && chatModule.hasActiveStream && sid) return chatModule.hasActiveStream(sid);
    } catch (e) {}
    return false;
  };
  // give it up to ~4s to begin streaming
  while (Date.now() - startedAt < 4000 && !streamActive()) {
    const nowCount = historyEl ? historyEl.querySelectorAll('.msg-ai').length : 0;
    if (nowCount > beforeAiCount) break;
    await sleep(120);
  }
  // then wait for it to finish (up to 90s)
  const waitStart = Date.now();
  while (Date.now() - waitStart < 90000 && streamActive()) {
    await sleep(200);
  }
  // small settle so the final text/footer lands
  await sleep(400);

  const replyText = extractLatestReply(historyEl, beforeAiCount);
  if (!replyText) {
    setState('idle', 'Idle', 'Tap the mic and talk to Shadow.', true);
    return;
  }
  appendTurn('Shadow', replyText);
  await speak(replyText);
}

function extractLatestReply(historyEl, beforeAiCount) {
  if (!historyEl) return '';
  const aiMsgs = historyEl.querySelectorAll('.msg-ai');
  if (aiMsgs.length <= beforeAiCount) return '';
  const last = aiMsgs[aiMsgs.length - 1];
  // Prefer the rendered markdown body; fall back to the message clone minus UI chrome.
  let node = last.querySelector('.markdown-body, .msg-content, .msg-text');
  if (!node) node = last;
  const clone = node.cloneNode(true);
  // Strip interactive chrome, code-run output, footers, thinking blocks.
  clone.querySelectorAll(
    '.msg-footer, .msg-actions, button, .agent-thread-node, .code-run-output, ' +
    '.copy-btn, .tok-per-sec, script, style, svg'
  ).forEach(el => el.remove());
  let txt = (clone.textContent || '').replace(/\s+/g, ' ').trim();
  // Cap what we send to TTS so a very long answer doesn't monopolize synthesis.
  if (txt.length > 700) txt = txt.slice(0, 700).replace(/\s+\S*$/, '') + '…';
  return txt;
}

async function speak(text) {
  const mgr = window.aiTTSManager;
  setState('speaking', 'Speaking', text, false);
  if (!mgr || !mgr.available || mgr._provider === 'disabled') {
    // No TTS — still show the reply, just don't voice it.
    await sleep(1200);
    setState('idle', 'Idle', 'Tap the mic and talk to Shadow.', true);
    return;
  }
  try {
    await mgr.play(text);
    // mgr.play resolves for browser TTS; for audio playback it starts playback
    // and sets isPlaying — poll until it finishes so we stay in "speaking".
    const startedAt = Date.now();
    while (mgr.isPlaying && Date.now() - startedAt < 120000) {
      await sleep(200);
    }
  } catch (e) {
    // fall through to idle
  }
  setState('idle', 'Idle', 'Tap the mic and talk to Shadow.', true);
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ── Open / close ──
async function refreshStackLabels() {
  try {
    const [stt, tts] = await Promise.all([
      fetch('/api/stt/stats').then(r => r.json()).catch(() => null),
      fetch('/api/tts/stats').then(r => r.json()).catch(() => null),
    ]);
    const sttEl = $('voice-stt-model'), ttsEl = $('voice-tts-model');
    if (sttEl && stt) {
      sttEl.textContent = stt.available
        ? `faster-whisper · ${stt.model || 'base'}`
        : 'unavailable';
    }
    if (ttsEl && tts) {
      ttsEl.textContent = tts.available
        ? `${(tts.model || 'Kokoro-82M').split(' ')[0]} · ${tts.voice || 'af_heart'}`
        : 'unavailable';
    }
  } catch (e) {}
}

function openVoice() {
  if (!V.veil) return;
  V.open = true;
  V.veil.classList.add('open');
  V.veil.setAttribute('aria-hidden', 'false');
  // Re-check every open: the desktop app adds .desktop-app asynchronously
  // (pywebviewready), so this module's init() may have run before it landed.
  const lite = detectLite();
  if (lite !== V.lite) { V.lite = lite; initParticles(); }
  resizeCanvas();
  startAnim();
  setState('idle', 'Idle', 'Tap the mic and talk to Shadow.', true);
  refreshStackLabels();
  if (!V.clockTimer) { tickClock(); V.clockTimer = setInterval(tickClock, 1000); }
}
function closeVoice() {
  if (!V.veil) return;
  V.open = false;
  V.veil.classList.remove('open');
  V.veil.setAttribute('aria-hidden', 'true');
  stopAnim();
  stopListening();
  if (window.aiTTSManager) { try { window.aiTTSManager.stop(); } catch (e) {} }
  if (V.clockTimer) { clearInterval(V.clockTimer); V.clockTimer = null; }
  setState('idle', 'Idle', 'Tap the mic and talk to Shadow.', true);
}
function tickClock() {
  if (V.clock) V.clock.textContent = new Date().toLocaleTimeString([], { hour12: false });
}

// ── Wire up ──
function init() {
  V.veil = $('voice-veil');
  if (!V.veil) return;
  V.canvas = $('voice-canvas');
  V.ctx = V.canvas ? V.canvas.getContext('2d') : null;
  V.stateLbl = $('voice-state-lbl');
  V.liveLine = $('voice-live-line');
  V.mic = $('voice-mic');
  V.clock = $('voice-clock');
  V.transcript = $('voice-transcript');
  V.turnsEl = $('voice-turns');
  V.reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!V.ctx) return;

  initParticles();
  window.addEventListener('resize', () => { if (V.open) resizeCanvas(); });

  if (V.mic) V.mic.addEventListener('click', () => startListening());
  const closeBtn = $('voice-close');
  if (closeBtn) closeBtn.addEventListener('click', closeVoice);
  V.veil.addEventListener('click', (e) => { if (e.target === V.veil) closeVoice(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && V.open) { e.stopPropagation(); closeVoice(); }
  });

  // The top-bar mic opens this overlay via app.js, which calls
  // window.shadowVoice.open() at click time. Exposing it here (rather than
  // attaching our own listener to that button) avoids any listener-ordering
  // race between the two modules.
  window.shadowVoice = { open: openVoice, close: closeVoice };
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}

export default { open: openVoice, close: closeVoice };
