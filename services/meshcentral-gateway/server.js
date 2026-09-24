'use strict';

const http = require('http');
const { spawn } = require('child_process');
const crypto = require('crypto');

const PORT = Number(process.env.PORT || 8099);
const KEY = String(process.env.SHADOW_MESH_GATEWAY_KEY || '');
const ADMIN_USER = String(process.env.SHADOW_MESH_ADMIN_USER || 'shadow_mesh_admin');
const ADMIN_PASS = String(process.env.SHADOW_MESH_ADMIN_PASSWORD || '');
const MESH_URL = String(process.env.SHADOW_MESH_INTERNAL_URL || 'wss://meshcentral:443/remote');
const MESHCTRL = '/opt/meshcentral/meshcentral/meshctrl.js';
const MAX_BODY = 32 * 1024;

if (KEY.length < 32 || ADMIN_PASS.length < 24) {
  console.error('MeshCentral gateway requires strong SHADOW_MESH_GATEWAY_KEY and SHADOW_MESH_ADMIN_PASSWORD values.');
  process.exit(1);
}

// MeshCentral runs on a named domain (the last path segment of MESH_URL,
// e.g. wss://meshcentral:443/remote -> "remote"). Several meshctrl commands
// silently do nothing when handed a bare user name on such a server.
const MESH_DOMAIN = (() => {
  try {
    const path = new URL(MESH_URL.replace(/^ws/, 'http')).pathname || '';
    return path.replace(/^\/+|\/+$/g, '').split('/')[0] || '';
  } catch (_) {
    return '';
  }
})();

function qualifiedUserId(username) {
  if (String(username).startsWith('user/')) return String(username);
  return MESH_DOMAIN ? `user/${MESH_DOMAIN}/${username}` : String(username);
}

function validName(value, max = 80) {
  return typeof value === 'string' && value.length >= 3 && value.length <= max && /^[A-Za-z0-9_. -]+$/.test(value);
}

function authorized(req) {
  const supplied = Buffer.from(String(req.headers['x-shadow-gateway-key'] || ''));
  const expected = Buffer.from(KEY);
  return supplied.length === expected.length && crypto.timingSafeEqual(supplied, expected);
}

function send(res, status, body) {
  const data = Buffer.from(JSON.stringify(body));
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': data.length,
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
  });
  res.end(data);
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY) {
        reject(new Error('Request body is too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      try {
        resolve(chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : {});
      } catch (_) {
        reject(new Error('Invalid JSON body'));
      }
    });
    req.on('error', reject);
  });
}

function meshctrl(command, args = [], credentials = null, timeoutMs = 30000) {
  const auth = credentials || { username: ADMIN_USER, password: ADMIN_PASS };
  const argv = [
    MESHCTRL,
    command,
    '--url', MESH_URL,
    '--loginuser', auth.username,
    '--loginpass', auth.password,
    ...args,
  ];
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, argv, {
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, NODE_ENV: 'production' },
    });
    let stdout = '';
    let stderr = '';
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      reject(new Error(`MeshCentral ${command} timed out`));
    }, timeoutMs);
    child.stdout.on('data', (chunk) => { stdout += chunk.toString('utf8'); });
    child.stderr.on('data', (chunk) => { stderr += chunk.toString('utf8'); });
    child.on('error', (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      const output = stdout.trim();
      const errorText = stderr.trim();
      const combined = `${output}\n${errorText}`;
      // "Nothing done" / "Mismatch domains" come back with exit code 0, so
      // without this a failed grant looks like success.
      if (code !== 0 || /invalid login|authentication token required|url key is invalid|server disconnected|nothing done|mismatch domains|not found/i.test(combined)) {
        reject(new Error(errorText || output || `MeshCentral ${command} failed`));
        return;
      }
      resolve(output);
    });
  });
}

function parseJson(output, fallback = []) {
  try {
    return JSON.parse(output);
  } catch (_) {
    return fallback;
  }
}

async function provision(body) {
  const username = String(body.username || '');
  const password = String(body.password || '');
  const group = String(body.group || '');
  if (!validName(username, 64) || !validName(group, 100) || password.length < 24) {
    throw new Error('Invalid remote account provisioning request');
  }

  const userExists = (await meshctrl('ListUsers', ['--idexists', username])).trim() === '1';
  if (!userExists) {
    await meshctrl('AddUser', [
      '--user', username,
      '--pass', password,
      '--realname', 'Shadow Remote User',
      '--rights', 'nonewgroups,locksettings',
    ]);
  }

  // Re-apply the account restrictions for existing users too. MeshCentral's
  // `notools` site flag also disables login-token creation, so desktop-only
  // access is enforced with device-group rights below instead.
  await meshctrl('EditUser', [
    '--userid', qualifiedUserId(username),
    '--rights', 'nonewgroups,locksettings',
  ]);

  const groupId = (await meshctrl('ListDeviceGroups', ['--nameexists', group])).trim();
  if (!groupId) {
    await meshctrl('AddDeviceGroup', [
      '--name', group,
      '--desc', 'Account-owned Shadow Remote Desktop devices',
      '--consent', '65',
    ]);
  }

  // Desktop control only. Terminal and files remain behind Shadow's own
  // confirmation-gated APIs.
  await meshctrl('AddUserToDeviceGroup', [
    '--group', group,
    '--userid', qualifiedUserId(username),
    '--remotecontrol',
    '--noterminal',
    '--nofiles',
    '--noamt',
    '--limitedevents',
  ]);
  return { ok: true };
}

// The agent installer needs the bare mesh id (no "mesh/<domain>/" prefix) —
// that is what /meshsettings?id= and meshinstall.sh both take.
async function groupId(body) {
  const group = String(body.group || '');
  if (!validName(group, 100)) throw new Error('Invalid device group');
  const raw = (await meshctrl('ListDeviceGroups', ['--nameexists', group])).trim();
  if (!raw) throw new Error('Device group does not exist');
  const line = raw.split(/\r?\n/).map((v) => v.trim()).filter(Boolean).pop() || '';
  // meshctrl returns the full id, e.g. "mesh/remote/AbC...@" — strip the
  // domain prefix, leaving the token the agent endpoints expect.
  const parts = line.split('/');
  const meshId = parts.length >= 3 ? parts.slice(2).join('/') : line;
  if (!meshId) throw new Error('MeshCentral did not return a device group id');
  return { ok: true, group_id: meshId, full_id: line };
}

async function invite(body) {
  const group = String(body.group || '');
  const hours = Math.max(1, Math.min(Number(body.hours || 24), 168));
  if (!validName(group, 100)) throw new Error('Invalid device group');
  const output = await meshctrl('GenerateInviteLink', [
    '--group', group,
    '--hours', String(hours),
    '--flags', '2',
  ]);
  const line = output.split(/\r?\n/).find((value) => /^https?:\/\//i.test(value.trim()));
  if (!line) throw new Error(output || 'MeshCentral did not return an invitation URL');
  return { ok: true, invite_url: line.trim() };
}

async function session(body) {
  const username = String(body.username || '');
  const password = String(body.password || '');
  const minutes = Math.max(1, Math.min(Number(body.minutes || 3), 10));
  if (!validName(username, 64) || password.length < 24) throw new Error('Invalid remote account');
  const output = await meshctrl('LoginTokens', [
    '--add', `shadow-${Date.now()}`,
    '--expire', String(minutes),
  ], { username, password });
  const userMatch = output.match(/^Username:\s*(.+)$/mi);
  const passMatch = output.match(/^Password:\s*(.+)$/mi);
  if (!userMatch || !passMatch) throw new Error(output || 'MeshCentral did not create a login token');
  return {
    ok: true,
    token_user: userMatch[1].trim(),
    token_pass: passMatch[1].trim(),
  };
}

async function devices(body) {
  const username = String(body.username || '');
  const password = String(body.password || '');
  if (!validName(username, 64) || password.length < 24) throw new Error('Invalid remote account');
  const output = await meshctrl('ListDevices', ['--json'], { username, password });
  return { ok: true, devices: parseJson(output, []) };
}

const server = http.createServer(async (req, res) => {
  if (req.method === 'GET' && req.url === '/health') {
    send(res, 200, { ok: true });
    return;
  }
  if (req.method !== 'POST' || !authorized(req)) {
    send(res, 404, { ok: false, error: 'Not found' });
    return;
  }
  try {
    const body = await readJson(req);
    let result;
    if (req.url === '/provision') result = await provision(body);
    else if (req.url === '/invite') result = await invite(body);
    else if (req.url === '/groupid') result = await groupId(body);
    else if (req.url === '/session') result = await session(body);
    else if (req.url === '/devices') result = await devices(body);
    else {
      send(res, 404, { ok: false, error: 'Not found' });
      return;
    }
    send(res, 200, result);
  } catch (error) {
    console.error(`MeshCentral gateway request failed: ${error.message}`);
    send(res, 502, { ok: false, error: error.message });
  }
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`Shadow MeshCentral gateway listening on ${PORT}`);
});
