"""Central permission policy for Autonomous Missions and the Desktop Workspace.

Every workspace/mission tool call is described as an :class:`ActionRequest`
and submitted to :func:`evaluate`, which returns ``ALLOW``,
``REQUIRE_APPROVAL`` or ``DENY``. Models never decide their own permissions:
the engine is pure (no I/O in the decision path), deterministic, and
unit-tested, and the device agent independently re-enforces filesystem roots
with ``os.path.realpath`` so even a compromised server cannot escape them.

Permission modes (Codex-style):

- ``ask``  — read-only autonomy. Reads and non-mutating inspection inside the
  authorized workspace run freely; every edit, state-changing command,
  network access, or out-of-workspace touch requires approval.
- ``auto`` — the recommended default. Reads, workspace edits, and ordinary
  project commands (tests, builds, formatters, git inspection) run freely;
  actions detected as potentially unsafe (destructive filesystem operations,
  privilege escalation, power control, force pushes, secrets paths, network
  access not yet approved for the mission, installers, security settings)
  still require approval.
- ``full`` — explicitly armed access. Requires password reauthentication,
  shows a persistent indicator, supports an optional expiry, and writes an
  immutable audit record. Even in full mode, structurally prohibited
  operations (cross-user/cross-device access, paths outside the device
  agent's allowed roots) remain DENY.

Approval grants are scoped: ``once`` (consumed by the next matching action),
``mission``, ``session``, or ``workspace`` (a persistent, revocable rule).
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.atomic_io import atomic_write_json

ALLOW = "ALLOW"
REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
DENY = "DENY"

MODES = ("ask", "auto", "full")
DEFAULT_MODE = "auto"

GRANT_SCOPES = ("once", "mission", "session", "workspace")

# Capabilities a tool call may declare. Keep this list closed: unknown
# capabilities are treated as the most dangerous thing they could be.
CAPABILITIES = (
    "fs_read",        # read file/tree/search/hash inside the workspace
    "fs_write",       # create/edit/rename/move files inside the workspace
    "fs_delete",      # delete inside the workspace (soft-delete via trash)
    "shell",          # run a command inside the workspace
    "git_read",       # status/log/diff/branch listing
    "git_write",      # commit, branch create, checkout
    "git_destructive",  # force push, reset --hard, clean -fd, branch -D
    "network",        # any internet access
    "install",        # dependency installation
    "system",         # power, services, firewall, users — system level
    "process",        # kill/terminate processes
    "credentials",    # secrets, keys, tokens, browser profiles
)

_RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

POLICY_DIR = Path(os.getenv("SHADOW_MISSIONS_DATA", "data/missions"))
RULES_PATH = POLICY_DIR / "policy-rules.json"
FULL_ACCESS_PATH = POLICY_DIR / "full-access.json"
AUDIT_PATH = POLICY_DIR / "audit.log"

_LOCK = threading.RLock()

FULL_ACCESS_MAX_SECONDS = 8 * 3600
FULL_ACCESS_DEFAULT_SECONDS = 3600


class MissionPolicyError(RuntimeError):
    """A clean policy/configuration failure."""


# ── action request ──────────────────────────────────────────────────────


@dataclass
class ActionRequest:
    """Everything a tool call must declare before it can run.

    ``outside_roots`` is computed by the caller from canonical paths (and
    re-checked on the device agent); the engine treats it as ground truth.
    """

    capability: str
    summary: str                      # human-readable, shown on approval cards
    owner: str = ""
    device_id: str = ""
    workspace_id: str = ""
    mutating: bool = True
    risk: str = "medium"
    network: bool = False
    outside_roots: bool = False
    command: str = ""                 # raw command line for shell actions
    path: str = ""                    # primary target path, if any
    mission_id: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    verdict: str
    reason: str
    rule: str = ""                    # machine id of the rule that fired
    grant_key: str = ""               # key a grant must match to allow this

    @property
    def allowed(self) -> bool:
        return self.verdict == ALLOW


# ── path hygiene (server-side syntactic checks) ─────────────────────────

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_UNC_RE = re.compile(r"^[\\/]{2}")


def canonicalize_path(raw: str) -> str:
    """Normalize a user/model-supplied path for containment checks.

    Pure string normalization (the filesystem lives on the device, not the
    server): unicode NFC, percent-decoding, backslash unification, ``.``/``..``
    segment resolution. Raises on null bytes and other smuggling attempts.
    """
    value = str(raw or "")
    if "\x00" in value or "%00" in value:
        raise MissionPolicyError("Path contains a null byte")
    value = unicodedata.normalize("NFC", value)
    # Collapse percent-encoded traversal attempts before normalization.
    lowered = value.lower()
    for encoded in ("%2e", "%2f", "%5c"):
        if encoded in lowered:
            raise MissionPolicyError("Percent-encoded path characters are not allowed")
    value = value.replace("\\", "/")
    # posixpath.normpath resolves '.' and '..' textually.
    normalized = posixpath.normpath(value)
    return normalized


def path_within_root(path: str, root: str) -> bool:
    """Syntactic containment check on canonicalized paths.

    Case-insensitive when the root looks like a Windows path. The device
    agent repeats this check against ``os.path.realpath`` so symlink escapes
    are caught where the real filesystem is.
    """
    try:
        c_path = canonicalize_path(path)
        c_root = canonicalize_path(root)
    except MissionPolicyError:
        return False
    if c_root in ("", ".", "/"):
        return False
    windows = bool(_WINDOWS_DRIVE_RE.match(c_root)) or bool(_UNC_RE.match(c_root))
    if windows:
        c_path, c_root = c_path.lower(), c_root.lower()
    if _UNC_RE.match(c_path) and not _UNC_RE.match(c_root):
        return False  # UNC/network share that the root does not authorize
    if c_path == c_root:
        return True
    return c_path.startswith(c_root.rstrip("/") + "/")


def is_relative_escape(raw: str) -> bool:
    """True when a *relative* path tries to climb out (``..`` after norm)."""
    try:
        normalized = canonicalize_path(raw)
    except MissionPolicyError:
        return True
    return normalized == ".." or normalized.startswith("../")


# ── dangerous-pattern classification ────────────────────────────────────

# Each entry: (machine id, compiled regex, human reason). Matched against a
# whitespace-normalized lowercase command line.
def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


_DESTRUCTIVE_FS = [
    ("rm-recursive-root", _rx(r"\brm\s+(-[a-z]*[rf][a-z]*\s+)+(/|~|\$home|[a-z]:[\\/])\s*$"), "recursive delete of a root directory"),
    ("rm-recursive", _rx(r"\brm\s+-[a-z]*r[a-z]*\b"), "recursive delete"),
    ("del-force", _rx(r"\b(del|erase)\b.*\s/[sq]\b"), "forced/recursive Windows delete"),
    ("rmdir-recurse", _rx(r"\bremove-item\b.*-recurse|\brmdir\s+/s\b"), "recursive directory removal"),
    ("mkfs", _rx(r"\bmkfs(\.| )"), "filesystem format"),
    ("dd-device", _rx(r"\bdd\b.*\bof=/dev/"), "raw write to a block device"),
    ("format-cmd", _rx(r"^format(\.com)?\s+[a-z]:"), "drive format"),
    ("diskpart", _rx(r"\bdiskpart\b"), "disk partitioning"),
    ("shred", _rx(r"\bshred\b"), "secure file destruction"),
    ("truncate-dev", _rx(r">\s*/dev/sd[a-z]"), "overwrite of a block device"),
]

_PRIVILEGE = [
    ("sudo", _rx(r"(^|[;&|]\s*)sudo\b"), "privilege escalation via sudo"),
    ("doas", _rx(r"(^|[;&|]\s*)doas\b"), "privilege escalation via doas"),
    ("su-root", _rx(r"(^|[;&|]\s*)su\b(\s|$)"), "switch to another user"),
    ("runas", _rx(r"\brunas\b"), "Windows privilege escalation"),
    ("uac-start", _rx(r"start-process\b.*-verb\s+runas"), "Windows UAC elevation"),
    ("pkexec", _rx(r"\bpkexec\b"), "privilege escalation via pkexec"),
]

_POWER = [
    ("shutdown", _rx(r"\bshutdown\b"), "system shutdown"),
    ("reboot", _rx(r"\b(reboot|restart-computer)\b"), "system reboot"),
    ("halt-poweroff", _rx(r"\b(halt|poweroff)\b"), "system halt"),
    ("systemctl-power", _rx(r"\bsystemctl\s+(poweroff|reboot|halt|suspend|hibernate)\b"), "systemd power control"),
    ("pmset", _rx(r"\bpmset\b.*\bsleepnow\b"), "macOS sleep"),
]

_PROCESS = [
    ("kill", _rx(r"\b(kill|pkill|killall)\b"), "process termination"),
    ("taskkill", _rx(r"\btaskkill\b"), "Windows process termination"),
    ("stop-process", _rx(r"\bstop-process\b"), "PowerShell process termination"),
]

_GIT_DESTRUCTIVE = [
    ("git-force-push", _rx(r"\bgit\s+push\b.*(\s--force\b|\s-f\b|\s--force-with-lease\b)"), "git force push"),
    ("git-reset-hard", _rx(r"\bgit\s+reset\b.*--hard"), "git hard reset"),
    ("git-clean-force", _rx(r"\bgit\s+clean\b.*-[a-z]*f"), "git clean -f"),
    ("git-branch-delete-force", _rx(r"\bgit\s+branch\b.*\s-D\b"), "git force branch delete"),
    ("git-push-delete", _rx(r"\bgit\s+push\b.*(--delete|:\S+)"), "git remote delete push"),
    ("git-filter", _rx(r"\bgit\s+(filter-branch|filter-repo)\b"), "git history rewrite"),
]

_INSTALL = [
    ("pkg-system", _rx(r"\b(apt|apt-get|yum|dnf|pacman|zypper|apk)\b\s+(install|add|remove|upgrade|dist-upgrade)"), "system package management"),
    ("brew", _rx(r"\bbrew\s+(install|uninstall|upgrade)\b"), "Homebrew package management"),
    ("choco-winget", _rx(r"\b(choco|winget|scoop)\s+(install|uninstall|upgrade)\b"), "Windows package management"),
    ("pip-install", _rx(r"\bpip3?\s+(install|uninstall)\b"), "Python dependency installation"),
    ("npm-global", _rx(r"\b(npm|pnpm|yarn)\s+(install|add|remove|i)\b"), "Node dependency installation"),
    ("cargo-install", _rx(r"\bcargo\s+install\b"), "Rust binary installation"),
    ("gem-install", _rx(r"\bgem\s+install\b"), "Ruby dependency installation"),
    ("curl-pipe-sh", _rx(r"\b(curl|wget)\b[^|;&]*\|\s*(ba)?sh\b"), "piping a download into a shell"),
]

_SECURITY_SETTINGS = [
    ("firewall", _rx(r"\b(ufw|firewall-cmd|iptables|nft|netsh\s+advfirewall)\b"), "firewall change"),
    ("users", _rx(r"\b(useradd|userdel|usermod|adduser|net\s+user|dscl)\b"), "user account change"),
    ("passwd", _rx(r"\b(passwd|chpasswd)\b"), "password change"),
    ("auth-config", _rx(r"\b(visudo|/etc/sudoers|/etc/pam\.d)\b"), "authentication configuration"),
    ("service-mgmt", _rx(r"\b(systemctl\s+(enable|disable|mask)|sc\.exe\s+(create|delete|config))\b"), "service configuration"),
    ("registry", _rx(r"\b(reg(\.exe)?\s+(add|delete)|set-itemproperty\s+.*hk(lm|cu))\b"), "Windows registry change"),
    ("launchctl", _rx(r"\blaunchctl\s+(load|unload|bootstrap)\b"), "macOS service configuration"),
    ("csrutil", _rx(r"\b(csrutil|spctl)\b"), "macOS security policy"),
]

_NETWORK_CMD = [
    ("curl-wget", _rx(r"\b(curl|wget|invoke-webrequest|invoke-restmethod|iwr)\b"), "network download"),
    ("ssh-scp", _rx(r"\b(ssh|scp|sftp|rsync\s+[^ ]*:)"), "remote connection"),
    ("nc", _rx(r"\b(nc|ncat|netcat|telnet)\b"), "raw network connection"),
    ("git-remote", _rx(r"\bgit\s+(push|pull|fetch|clone|remote)\b"), "git network operation"),
    ("pkg-fetch", _rx(r"\b(pip3?|npm|pnpm|yarn|cargo|gem|go)\s+(install|add|get|download)\b"), "package download"),
]

_SECRET_PATHS = [
    ("ssh-keys", _rx(r"(^|/|\\)\.ssh(/|\\|$)|id_(rsa|ed25519|ecdsa)"), "SSH keys"),
    ("cloud-creds", _rx(r"(^|/|\\)\.(aws|azure|gcloud|kube)(/|\\|$)"), "cloud credentials"),
    ("env-files", _rx(r"(^|/|\\)\.env(\.[a-z0-9.]+)?$"), "environment secrets file"),
    ("netrc", _rx(r"(^|/|\\)\.netrc$|_netrc$"), "stored network credentials"),
    ("gnupg", _rx(r"(^|/|\\)\.gnupg(/|\\|$)"), "GPG keyring"),
    ("browser-profiles", _rx(r"(mozilla/firefox|google[/\\]chrome|chromium|microsoft[/\\]edge|appdata[/\\]local[/\\]google|library[/\\]application support[/\\]google)", ), "browser profile data"),
    ("system-dirs", _rx(r"^(/etc|/boot|/root|/sys|/proc|c:/windows|c:/program files)"), "system directory"),
    ("password-stores", _rx(r"(^|/|\\)(\.password-store|keepass.*\.kdbx|login\.keychain)"), "password store"),
    ("shadow-data", _rx(r"(^|/|\\)data[/\\](auth\.json|\.app_key|shadow-devices\.json)"), "Shadow server credentials"),
]


def _normalize_command(command: str) -> str:
    return re.sub(r"\s+", " ", str(command or "")).strip().lower()


def _scan(table: list[tuple[str, re.Pattern, str]], text: str) -> tuple[str, str] | None:
    for rule_id, rx, reason in table:
        if rx.search(text):
            return rule_id, reason
    return None


def classify_command(command: str) -> dict[str, Any]:
    """Classify a shell command line. Pure; used server-side and in tests.

    Returns ``{capability, risk, flags: [(rule, reason)...], network}``.
    The *most dangerous* classification wins.
    """
    text = _normalize_command(command)
    flags: list[tuple[str, str]] = []
    capability = "shell"
    risk = "medium"
    network = False

    for table, cap, table_risk in (
        (_DESTRUCTIVE_FS, "fs_delete", "critical"),
        (_PRIVILEGE, "system", "critical"),
        (_POWER, "system", "critical"),
        (_SECURITY_SETTINGS, "system", "critical"),
        (_GIT_DESTRUCTIVE, "git_destructive", "high"),
        (_PROCESS, "process", "high"),
        (_INSTALL, "install", "high"),
    ):
        hit = _scan(table, text)
        if hit:
            flags.append(hit)
            if _RISK_ORDER[table_risk] > _RISK_ORDER[risk]:
                risk = table_risk
            if capability == "shell":
                capability = cap

    net_hit = _scan(_NETWORK_CMD, text)
    if net_hit:
        network = True
        flags.append(net_hit)
        if _RISK_ORDER[risk] < _RISK_ORDER["medium"]:
            risk = "medium"

    secret_hit = _scan(_SECRET_PATHS, text)
    if secret_hit:
        flags.append(secret_hit)
        capability = "credentials" if capability == "shell" else capability
        risk = "critical" if _RISK_ORDER[risk] < _RISK_ORDER["critical"] else risk

    return {"capability": capability, "risk": risk, "flags": flags, "network": network}


def classify_path(path: str) -> tuple[str, str] | None:
    """Return (rule, reason) when a path touches secrets/system locations."""
    try:
        normalized = canonicalize_path(path).lower()
    except MissionPolicyError:
        return ("invalid-path", "path could not be canonicalized")
    return _scan(_SECRET_PATHS, normalized)


# ── grant storage ────────────────────────────────────────────────────────


def grant_key_for(request: ActionRequest) -> str:
    """Stable key an approval grant must match to cover an action.

    Shell grants key on the *classification* (rule ids), not the exact text,
    so "always allow running tests in this workspace" works while a newly
    dangerous command never inherits an old grant.
    """
    if request.capability == "shell" and request.command:
        info = classify_command(request.command)
        flag_ids = ",".join(sorted(rule for rule, _ in info["flags"])) or "plain"
        return f"shell:{flag_ids}"
    if request.command:
        return f"{request.capability}:{_normalize_command(request.command)[:120]}"
    if request.path:
        try:
            return f"{request.capability}:{canonicalize_path(request.path).lower()[:160]}"
        except MissionPolicyError:
            return f"{request.capability}:invalid"
    return f"{request.capability}:*"


class GrantStore:
    """Scoped approval grants. Session/mission/once grants are in memory by
    design (a restart drops them — fail closed); workspace rules persist."""

    def __init__(self) -> None:
        self._volatile: dict[str, dict[str, float]] = {}  # scope_key -> grant_key -> expiry

    @staticmethod
    def _scope_key(owner: str, scope: str, scope_id: str) -> str:
        return f"{str(owner or '').strip().lower()}|{scope}|{scope_id}"

    def grant(self, owner: str, scope: str, scope_id: str, grant_key: str,
              *, workspace_id: str = "", summary: str = "", ttl: float = 12 * 3600) -> dict[str, Any]:
        if scope not in GRANT_SCOPES:
            raise MissionPolicyError(f"Unknown grant scope: {scope}")
        owner = str(owner or "").strip().lower()
        if not owner:
            raise MissionPolicyError("Grants require a real account owner")
        if scope == "workspace":
            if not workspace_id:
                raise MissionPolicyError("Workspace grants need a workspace id")
            return _add_persistent_rule(owner, workspace_id, grant_key, summary)
        with _LOCK:
            bucket = self._volatile.setdefault(self._scope_key(owner, scope, scope_id), {})
            bucket[grant_key] = time.time() + (0 if scope == "once" else ttl)
            if scope == "once":
                bucket[grant_key] = time.time() + 600  # once-grants stay claimable for 10 min
        return {"scope": scope, "grant_key": grant_key}

    def consume(self, owner: str, request: ActionRequest, *,
                mission_id: str = "", session_id: str = "") -> bool:
        """True when a grant covers this action. Once-grants are consumed."""
        key = grant_key_for(request)
        owner = str(owner or "").strip().lower()
        now = time.time()
        with _LOCK:
            for scope, scope_id in (("once", mission_id or session_id),
                                    ("mission", mission_id),
                                    ("session", session_id)):
                if not scope_id:
                    continue
                bucket = self._volatile.get(self._scope_key(owner, scope, scope_id)) or {}
                expiry = bucket.get(key)
                if expiry and expiry > now:
                    if scope == "once":
                        bucket.pop(key, None)
                    return True
                if expiry and expiry <= now:
                    bucket.pop(key, None)
        if request.workspace_id and _has_persistent_rule(owner, request.workspace_id, key):
            return True
        return False

    def drop_scope(self, owner: str, scope: str, scope_id: str) -> None:
        with _LOCK:
            self._volatile.pop(self._scope_key(str(owner or "").strip().lower(), scope, scope_id), None)


def _rules_state() -> dict[str, Any]:
    try:
        raw = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "rules": []}
    if not isinstance(raw, dict) or not isinstance(raw.get("rules"), list):
        return {"version": 1, "rules": []}
    return raw


def _save_rules(state: dict[str, Any]) -> None:
    POLICY_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(RULES_PATH), state, indent=2)


def _add_persistent_rule(owner: str, workspace_id: str, grant_key: str, summary: str) -> dict[str, Any]:
    rule = {
        "id": secrets.token_urlsafe(8),
        "owner": owner,
        "workspace_id": str(workspace_id),
        "grant_key": grant_key,
        "summary": str(summary or "")[:300],
        "created_at": time.time(),
    }
    with _LOCK:
        state = _rules_state()
        exists = any(
            r.get("owner") == owner and r.get("workspace_id") == rule["workspace_id"]
            and r.get("grant_key") == grant_key
            for r in state["rules"] if isinstance(r, dict)
        )
        if not exists:
            state["rules"].append(rule)
            _save_rules(state)
    return rule


def _has_persistent_rule(owner: str, workspace_id: str, grant_key: str) -> bool:
    with _LOCK:
        state = _rules_state()
    return any(
        isinstance(r, dict) and r.get("owner") == owner
        and r.get("workspace_id") == str(workspace_id) and r.get("grant_key") == grant_key
        for r in state["rules"]
    )


def list_persistent_rules(owner: str) -> list[dict[str, Any]]:
    owner = str(owner or "").strip().lower()
    with _LOCK:
        state = _rules_state()
    return [r for r in state["rules"] if isinstance(r, dict) and r.get("owner") == owner]


def revoke_persistent_rule(owner: str, rule_id: str) -> bool:
    owner = str(owner or "").strip().lower()
    with _LOCK:
        state = _rules_state()
        before = len(state["rules"])
        state["rules"] = [
            r for r in state["rules"]
            if not (isinstance(r, dict) and r.get("owner") == owner and r.get("id") == rule_id)
        ]
        if len(state["rules"]) != before:
            _save_rules(state)
            return True
    return False


GRANTS = GrantStore()


# ── full-access state ────────────────────────────────────────────────────


def _full_state() -> dict[str, Any]:
    try:
        raw = json.loads(FULL_ACCESS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "sessions": {}}
    if not isinstance(raw, dict) or not isinstance(raw.get("sessions"), dict):
        return {"version": 1, "sessions": {}}
    return raw


def _save_full(state: dict[str, Any]) -> None:
    POLICY_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(FULL_ACCESS_PATH), state, indent=2)
    try:
        os.chmod(FULL_ACCESS_PATH, 0o600)
    except OSError:
        pass


def arm_full_access(owner: str, device_id: str, *, password: str,
                    duration_seconds: int | None = None) -> dict[str, Any]:
    """Arm full access for owner+device after password reauthentication.

    Never silently expands to another user or device: the armed record is
    keyed on both, and :func:`evaluate` only honors an exact match.
    """
    owner = str(owner or "").strip().lower()
    if not owner or not device_id:
        raise MissionPolicyError("Full access needs an owner and a device")
    from core.auth import AuthManager  # late import to keep the engine pure for tests

    if not AuthManager().verify_password(owner, str(password or "")):
        audit(owner, "full_access_denied", {"device_id": device_id, "reason": "bad password"})
        raise MissionPolicyError("Password verification failed")
    duration = int(duration_seconds or FULL_ACCESS_DEFAULT_SECONDS)
    duration = max(60, min(duration, FULL_ACCESS_MAX_SECONDS))
    expires = time.time() + duration
    key = f"{owner}|{device_id}"
    with _LOCK:
        state = _full_state()
        state["sessions"][key] = {"owner": owner, "device_id": device_id,
                                  "armed_at": time.time(), "expires_at": expires}
        _save_full(state)
    audit(owner, "full_access_armed", {"device_id": device_id, "expires_at": expires})
    return {"armed": True, "expires_at": expires}


def disarm_full_access(owner: str, device_id: str) -> None:
    owner = str(owner or "").strip().lower()
    with _LOCK:
        state = _full_state()
        if state["sessions"].pop(f"{owner}|{device_id}", None) is not None:
            _save_full(state)
    audit(owner, "full_access_disarmed", {"device_id": device_id})


def full_access_active(owner: str, device_id: str) -> dict[str, Any] | None:
    owner = str(owner or "").strip().lower()
    with _LOCK:
        state = _full_state()
    row = state["sessions"].get(f"{owner}|{device_id}")
    if isinstance(row, dict) and float(row.get("expires_at") or 0) > time.time():
        return dict(row)
    return None


# ── immutable audit log ─────────────────────────────────────────────────


def audit(owner: str, event: str, detail: dict[str, Any] | None = None) -> None:
    """Append-only audit record. Never raises into the caller."""
    try:
        POLICY_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.time(),
            "owner": str(owner or "").strip().lower(),
            "event": str(event)[:80],
            "detail": detail or {},
        }
        with _LOCK, AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        try:
            os.chmod(AUDIT_PATH, 0o600)
        except OSError:
            pass
    except OSError:
        pass


# ── the decision function ───────────────────────────────────────────────

_READ_CAPS = {"fs_read", "git_read"}
# Capabilities mode `auto` runs without asking, inside the workspace.
_AUTO_CAPS = _READ_CAPS | {"fs_write", "shell", "git_write"}


def evaluate(
    request: ActionRequest,
    *,
    mode: str = DEFAULT_MODE,
    mission_id: str = "",
    session_id: str = "",
    mission_network_approved: bool = False,
) -> Decision:
    """Decide ALLOW / REQUIRE_APPROVAL / DENY for one declared action."""
    if mode not in MODES:
        return Decision(DENY, f"Unknown permission mode: {mode}", rule="bad-mode")
    if request.capability not in CAPABILITIES:
        return Decision(DENY, f"Undeclared capability: {request.capability}", rule="bad-capability")
    if not str(request.owner or "").strip():
        return Decision(DENY, "Actions require a real account owner", rule="no-owner")

    # Structural prohibitions hold in every mode, including full access.
    if request.detail.get("cross_user") or request.detail.get("cross_device"):
        return Decision(DENY, "Cross-user / cross-device access is prohibited", rule="cross-tenant")

    command_info = classify_command(request.command) if request.command else None
    effective_risk = request.risk
    flags: list[tuple[str, str]] = []
    network = request.network
    capability = request.capability
    if command_info:
        flags = command_info["flags"]
        network = network or command_info["network"]
        if _RISK_ORDER.get(command_info["risk"], 1) > _RISK_ORDER.get(effective_risk, 1):
            effective_risk = command_info["risk"]
        if command_info["capability"] != "shell" and capability == "shell":
            capability = command_info["capability"]
    if request.path:
        path_hit = classify_path(request.path)
        if path_hit:
            flags.append(path_hit)
            if capability in _READ_CAPS | {"fs_write", "fs_delete"}:
                capability = "credentials"
            effective_risk = "critical"

    grant_key = grant_key_for(request)
    flag_text = "; ".join(reason for _, reason in flags)

    def needs_approval(reason: str, rule: str) -> Decision:
        if GRANTS.consume(request.owner, request,
                          mission_id=mission_id, session_id=session_id):
            return Decision(ALLOW, f"approved by grant ({reason})", rule=f"grant:{rule}", grant_key=grant_key)
        return Decision(REQUIRE_APPROVAL, reason, rule=rule, grant_key=grant_key)

    # ── full access ──
    if mode == "full":
        if request.outside_roots:
            # Full access spans *explicitly authorized device roots*, never beyond.
            return Decision(DENY, "Path is outside the device's authorized roots", rule="outside-roots")
        if not full_access_active(request.owner, request.device_id):
            return Decision(DENY, "Full access is not armed for this device (reauthenticate)", rule="full-not-armed")
        return Decision(ALLOW, "full access armed", rule="full-access", grant_key=grant_key)

    # ── shared hard gates for ask/auto ──
    if request.outside_roots:
        return needs_approval("Touches a path outside the authorized workspace", "outside-roots")

    # ── ask mode: only reads run freely ──
    if mode == "ask":
        if capability in _READ_CAPS and not request.mutating and not network:
            return Decision(ALLOW, "read-only inside workspace", rule="ask-read", grant_key=grant_key)
        reason = flag_text or ("network access" if network else "state-changing action")
        return needs_approval(f"Ask mode: {reason} requires approval", "ask-mutate")

    # ── auto mode ──
    if capability == "credentials":
        return needs_approval(f"Touches sensitive data: {flag_text or 'credentials'}", "auto-credentials")
    if capability in {"system", "fs_delete"} and flags:
        return needs_approval(f"Potentially unsafe: {flag_text}", "auto-dangerous")
    if capability == "git_destructive":
        return needs_approval(f"Destructive git operation: {flag_text or 'history rewrite'}", "auto-git-destructive")
    if capability == "process" and flags:
        return needs_approval(f"Process control: {flag_text}", "auto-process")
    if capability == "install":
        return needs_approval(f"Dependency installation: {flag_text or 'installer'}", "auto-install")
    if network and not mission_network_approved:
        return needs_approval(f"Network access: {flag_text or 'internet access'}", "auto-network")
    if capability == "fs_delete":
        # Plain in-workspace delete (soft, recoverable) is routine in auto mode.
        return Decision(ALLOW, "workspace delete (recoverable)", rule="auto-delete", grant_key=grant_key)
    if capability in _AUTO_CAPS:
        if _RISK_ORDER.get(effective_risk, 1) >= _RISK_ORDER["critical"]:
            return needs_approval(f"Critical-risk action: {flag_text or request.summary}", "auto-critical")
        return Decision(ALLOW, "routine workspace action", rule="auto-routine", grant_key=grant_key)
    return needs_approval(f"Unrecognized action class: {capability}", "auto-unknown")
