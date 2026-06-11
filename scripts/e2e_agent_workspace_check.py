#!/usr/bin/env python3
"""Live end-to-end check: real server, real relay agent, real temp workspace.

Boots the actual FastAPI app (uvicorn subprocess, isolated data directory),
enrolls the actual Linux relay agent (subprocess, isolated config, with
SHADOW_ALLOWED_ROOTS pinned to a dedicated temporary workspace), then drives
the workspace + agent-session APIs exactly like the browser does:

  1.  tree / read / write / patch on real files
  2.  command execution with exit codes
  3.  checkpoint + rollback restoring the file on disk
  4.  path-escape rejection (server policy AND agent realpath containment)
  5.  cross-account rejection (second real account gets 404/denied)
  6.  unattended execution: installer-class command runs with no approval,
      and the same command in `auto` mode returns an approval card
  7.  a full agent session against a scripted OpenAI-compatible endpoint:
      the model writes a file on disk through the relay, runs a command,
      finishes with a report — then rollback removes the created file

Usage: .venv/bin/python scripts/e2e_agent_workspace_check.py
Exit code 0 = every check passed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTHON = str(REPO / ".venv" / "bin" / "python")
SERVER_PORT = 7917
STUB_PORT = 7918
BASE = f"http://127.0.0.1:{SERVER_PORT}"

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  OK   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {('— ' + detail) if detail else ''}")


# ── tiny HTTP client with a cookie jar per account ───────────────────────


class Client:
    def __init__(self) -> None:
        self.cookie = ""

    def request(self, method: str, path: str, body: dict | None = None,
                *, form: dict | None = None, raw: bool = False):
        headers = {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                set_cookie = resp.headers.get("Set-Cookie")
                if set_cookie and "shadow_session=" in set_cookie:
                    self.cookie = set_cookie.split(";")[0]
                payload = resp.read().decode()
                return resp.status, (payload if raw else json.loads(payload or "{}"))
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode()
            try:
                return exc.code, json.loads(payload or "{}")
            except ValueError:
                return exc.code, {"detail": payload[:200]}


# ── scripted OpenAI-compatible model endpoint ────────────────────────────

STUB_REPLIES = [
    {"tool": "ws_write", "args": {"path": "agent_artifact.txt",
                                  "text": "written by a live agent session\n"}},
    {"tool": "ws_run", "args": {"command": "cat agent_artifact.txt"}},
    {"done": True, "report": "Created agent_artifact.txt and verified its contents with cat."},
]
_stub_state = {"i": 0}


class StubModel(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._send({"data": [{"id": "e2e-scripted-model"}]})
        else:
            self._send({"ok": True})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        index = min(_stub_state["i"], len(STUB_REPLIES) - 1)
        _stub_state["i"] += 1
        reply = json.dumps(STUB_REPLIES[index])
        self._send({
            "id": "stub", "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                         "finish_reason": "stop"}],
        })


def wait_for(predicate, timeout: float, what: str):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(1)
    raise TimeoutError(f"timed out waiting for {what}")


def main() -> int:
    sandbox = Path(tempfile.mkdtemp(prefix="shadow-e2e-"))
    server_dir = sandbox / "server"
    workspace = sandbox / "workspace"
    outside = sandbox / "outside-secret"
    agent_cfg = sandbox / "agent-config"
    for d in (server_dir, workspace, outside, agent_cfg):
        d.mkdir(parents=True)
    (server_dir / "data").mkdir()  # sqlite + JSON state live under ./data
    (server_dir / "static").symlink_to(REPO / "static")
    for optional in ("config", "licenses", "mcp_servers"):
        if (REPO / optional).exists():
            (server_dir / optional).symlink_to(REPO / optional)

    # Seed the dedicated temp workspace (a real git repo with real files).
    (workspace / "utils.py").write_text("def add(a, b):\n    return a + b\n")
    (workspace / "README.md").write_text("# E2E workspace\n")
    (outside / "secret.txt").write_text("must never be readable\n")
    (workspace / "link-out").symlink_to(outside)  # symlink escape attempt
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "-c", "user.email=e2e@local", "-c", "user.name=e2e",
                    "add", "-A"], cwd=workspace, check=True)
    subprocess.run(["git", "-c", "user.email=e2e@local", "-c", "user.name=e2e",
                    "commit", "-qm", "seed"], cwd=workspace, check=True)

    stub = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), StubModel)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    env = {
        **os.environ,
        "PYTHONPATH": str(REPO),
        "AUTH_ENABLED": "true",
        "LOCALHOST_BYPASS": "false",
        # Fully isolated account store — never the repo's real data/auth.json.
        "SHADOW_AUTH_PATH": str(server_dir / "data" / "auth.json"),
        "SHADOW_DISABLE_BUILTIN_MCP": "1",
        "TTS_ENABLED": "false",
        "STT_ENABLED": "false",
    }
    server = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(SERVER_PORT), "--log-level", "warning"],
        cwd=server_dir, env=env,
        stdout=open(sandbox / "server.log", "w"), stderr=subprocess.STDOUT,
    )
    agent = None
    try:
        admin = Client()
        wait_for(lambda: admin.request("GET", "/api/health")[0] == 200, 90, "server health")
        print(f"server up at {BASE} (sandbox {sandbox})")

        status, _ = admin.request("POST", "/api/auth/setup",
                                  {"username": "e2eadmin", "password": "e2e-password-123"})
        check("admin account setup", status == 200)
        status, _ = admin.request("POST", "/api/auth/login",
                                  {"username": "e2eadmin", "password": "e2e-password-123"})
        check("admin login (cookie session)", status == 200 and bool(admin.cookie))

        # Second real account for cross-account checks.
        admin.request("PUT", "/api/auth/open-signup", {"enabled": True})
        intruder = Client()
        status, _ = intruder.request("POST", "/api/auth/signup",
                                     {"username": "mallory", "password": "mallory-pass-123"})
        check("second account created", status == 200)
        status, _ = intruder.request("POST", "/api/auth/login",
                                     {"username": "mallory", "password": "mallory-pass-123"})
        check("second account login", status == 200 and bool(intruder.cookie))

        # Enroll the real relay agent against the temp workspace only.
        status, enrollment = admin.request("POST", "/api/shadow/devices/enrollment", {})
        check("enrollment code issued", status == 200 and bool(enrollment.get("code")))
        agent_env = {**os.environ, "PYTHONPATH": str(REPO),
                     "XDG_CONFIG_HOME": str(agent_cfg),
                     "SHADOW_ALLOWED_ROOTS": str(workspace)}
        enroll = subprocess.run(
            [PYTHON, str(REPO / "companion" / "relay_agent.py"), "--server", BASE,
             "--enroll", enrollment["code"], "--name", "e2e-device", "--once"],
            env=agent_env, capture_output=True, text=True)
        check("relay agent enrolled", enroll.returncode == 0, enroll.stderr[-200:])
        agent = subprocess.Popen(
            [PYTHON, str(REPO / "companion" / "relay_agent.py")],
            env=agent_env,
            stdout=open(sandbox / "agent.log", "w"), stderr=subprocess.STDOUT)

        def device_online():
            _, data = admin.request("GET", "/api/shadow/devices")
            rows = data.get("devices") or []
            return any(d.get("online") and "ws_tree" in (d.get("capabilities") or []) for d in rows)
        wait_for(device_online, 45, "device online with workspace capabilities")
        _, data = admin.request("GET", "/api/shadow/devices")
        device_id = next(d["id"] for d in data["devices"] if d.get("online"))
        check("device online with ws_* capabilities", True)

        # Authorize the workspace in UNATTENDED mode (persisted server-side).
        status, ws = admin.request("POST", "/api/missions/workspaces", {
            "device_id": device_id, "root": str(workspace),
            "name": "e2e", "mode": "unattended"})
        check("workspace authorized (unattended)", status == 200 and ws.get("mode") == "unattended")
        ws_id = ws["id"]

        def action(client: Client, act: str, args: dict, mode: str | None = None):
            payload = {"action": act, "args": args}
            if mode:
                payload["mode"] = mode
            return client.request("POST", f"/api/missions/workspaces/{ws_id}/action", payload)

        # 1. tree
        status, out = action(admin, "ws_tree", {"depth": 2})
        names = [e["name"] for e in out.get("result", {}).get("entries", [])]
        check("ws_tree lists real files", status == 200 and "utils.py" in names)

        # 2. read
        status, out = action(admin, "ws_read", {"path": "utils.py"})
        check("ws_read returns real content",
              status == 200 and "def add" in out.get("result", {}).get("text", ""))

        # 3. write — verify on the real disk
        status, out = action(admin, "ws_write", {"path": "notes/created.txt", "text": "hello e2e\n"})
        check("ws_write creates file on disk",
              status == 200 and (workspace / "notes" / "created.txt").read_text() == "hello e2e\n")

        # 4. patch — exact old/new, verify on disk
        status, out = action(admin, "ws_patch", {"edits": [
            {"path": "utils.py", "old": "return a + b", "new": "return a + b  # patched"}]})
        check("ws_patch edits file on disk",
              status == 200 and "# patched" in (workspace / "utils.py").read_text())

        # 5. command with exit code
        status, out = action(admin, "ws_run", {"command": "echo from-the-device && exit 0"})
        result = out.get("result", {})
        check("ws_run executes with exit code",
              status == 200 and result.get("returncode") == 0
              and "from-the-device" in result.get("stdout", ""))

        # 6+7. checkpoint, mutate, rollback restores disk state
        status, out = action(admin, "ws_checkpoint", {"checkpoint_id": "e2e-check"})
        check("checkpoint opened", status == 200 and out.get("result", {}).get("ok"))
        action(admin, "ws_write", {"path": "utils.py", "text": "RUINED\n",
                                   "checkpoint_id": "e2e-check"})
        action(admin, "ws_write", {"path": "brand-new.txt", "text": "temp\n",
                                   "checkpoint_id": "e2e-check"})
        status, out = action(admin, "ws_restore", {"checkpoint_id": "e2e-check"})
        restored_ok = ("# patched" in (workspace / "utils.py").read_text()
                       and not (workspace / "brand-new.txt").exists())
        check("rollback restores pre-images and removes created files",
              status == 200 and restored_ok)

        # 8a. path escape: server-side syntactic rejection (DENY in unattended)
        status, out = action(admin, "ws_read", {"path": "../outside-secret/secret.txt"})
        check("relative path escape rejected", status == 403, str(out)[:120])
        status, out = action(admin, "ws_read", {"path": "/etc/passwd"})
        check("absolute path escape rejected", status == 403, str(out)[:120])

        # 8b. symlink escape: passes syntax checks, MUST die on agent realpath
        status, out = action(admin, "ws_read", {"path": "link-out/secret.txt"})
        body = json.dumps(out)
        check("symlink escape rejected by the device agent",
              status != 200 or "outside" in body.lower(), body[:150])

        # 9. cross-account rejection: second real account, same workspace id
        status, out = intruder.request(
            "POST", f"/api/missions/workspaces/{ws_id}/action",
            {"action": "ws_read", "args": {"path": "utils.py"}})
        check("cross-account workspace access rejected", status in (400, 403, 404),
              f"status={status}")
        status, out = intruder.request("POST", "/api/missions/workspaces", {
            "device_id": device_id, "root": str(workspace), "name": "steal"})
        check("cross-account device authorization rejected", status in (400, 403, 404),
              f"status={status}")

        # 10. unattended executes installer-class commands with no approval...
        status, out = action(admin, "ws_run", {"command": "pip install --help"})
        check("unattended: installer-class command runs without approval",
              status == 200 and out.get("result", {}).get("returncode") == 0
              and not out.get("approval_required"), str(out)[:120])
        # ...the SAME command under `auto` returns an approval card instead
        status, out = action(admin, "ws_run", {"command": "pip install --help"}, mode="auto")
        check("auto mode still asks for the same command",
              status == 200 and out.get("approval_required") is True, str(out)[:120])
        # ...and unattended cleanly denies what stays forbidden
        status, out = action(admin, "ws_run", {"command": "sudo rm -rf /"})
        check("unattended: privileged destructive command denied (no prompt)",
              status == 403, str(out)[:120])

        # 11. live agent session against the scripted model endpoint
        status, ep = admin.request("POST", "/api/model-endpoints",
                                   form={"base_url": f"http://127.0.0.1:{STUB_PORT}/v1",
                                         "name": "e2e-stub", "skip_probe": "false"})
        check("scripted model endpoint registered", status == 200 and bool(ep.get("id")), str(ep)[:150])
        status, session = admin.request("POST", "/api/missions/sessions", {
            "workspace_id": ws_id, "task": "Create agent_artifact.txt with a marker line",
            "endpoint_id": ep["id"], "model": "e2e-scripted-model"})
        check("agent session created + started", status == 200 and session.get("id"), str(session)[:200])
        sid = session.get("id")

        def session_done():
            _, s = admin.request("GET", f"/api/missions/sessions/{sid}")
            return s.get("status") in ("completed", "failed")
        wait_for(session_done, 90, "agent session completion")
        _, final = admin.request("GET", f"/api/missions/sessions/{sid}")
        artifact = workspace / "agent_artifact.txt"
        check("session completed with a report",
              final.get("status") == "completed" and bool(final.get("report")),
              f"status={final.get('status')} err={final.get('error')}")
        check("session wrote the file on the real disk",
              artifact.exists() and "live agent session" in artifact.read_text())
        check("session recorded terminal output",
              any("live agent session" in (t.get("stdout") or "")
                  and t.get("command", "").startswith("cat ")
                  for t in final.get("terminal", [])))
        check("session recorded changed files",
              "agent_artifact.txt" in (final.get("files_changed") or []))
        check("session checkpoint opened before mutation", bool(final.get("checkpoint")))
        check("no approvals were requested (unattended)",
              not final.get("approvals"))

        # 12. one-click rollback of the whole session
        status, out = admin.request("POST", f"/api/missions/sessions/{sid}/rollback", {})
        check("session rollback removes the created file",
              status == 200 and not artifact.exists(), str(out)[:150])
        _, final = admin.request("GET", f"/api/missions/sessions/{sid}")
        check("session marked rolled_back", final.get("status") == "rolled_back")

    finally:
        if agent:
            agent.terminate()
        server.terminate()
        stub.shutdown()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
        print(f"\nlogs kept in {sandbox} (server.log / agent.log)")

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
        return 1
    shutil.rmtree(sandbox, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
