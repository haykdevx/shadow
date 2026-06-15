"""Policy engine: modes, dangerous-command detection, grants, full access,
path canonicalization, and the structural prohibitions that hold everywhere.
"""

import time

import pytest

import src.workspace_policy as policy
from src.workspace_policy import (
    ALLOW,
    DENY,
    REQUIRE_APPROVAL,
    ActionRequest,
    GrantStore,
    WorkspacePolicyError,
    canonicalize_path,
    classify_command,
    classify_path,
    evaluate,
    grant_key_for,
    is_relative_escape,
    path_within_root,
)


@pytest.fixture(autouse=True)
def _isolated_policy_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "POLICY_DIR", tmp_path)
    monkeypatch.setattr(policy, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(policy, "FULL_ACCESS_PATH", tmp_path / "full.json")
    monkeypatch.setattr(policy, "AUDIT_PATH", tmp_path / "audit.log")
    monkeypatch.setattr(policy, "GRANTS", GrantStore())
    yield


def req(**kw):
    base = dict(capability="shell", summary="test", owner="alice",
                device_id="dev1", workspace_id="ws1")
    base.update(kw)
    return ActionRequest(**base)


# ── path hygiene / cross-OS ──────────────────────────────────────────────

def test_canonicalize_rejects_null_bytes():
    with pytest.raises(WorkspacePolicyError):
        canonicalize_path("a\x00b")
    with pytest.raises(WorkspacePolicyError):
        canonicalize_path("a%00b")


def test_canonicalize_rejects_percent_encoding():
    for smuggle in ("..%2f..%2fetc", "%2e%2e/x", "a%5cb"):
        with pytest.raises(WorkspacePolicyError):
            canonicalize_path(smuggle)


def test_canonicalize_resolves_dot_segments():
    assert canonicalize_path("a/b/../c") == "a/c"
    assert canonicalize_path("./x/./y") == "x/y"


def test_relative_escape_detection():
    assert is_relative_escape("../x")
    assert is_relative_escape("a/../../x")
    assert not is_relative_escape("a/../b")


def test_path_within_root_posix():
    assert path_within_root("/home/u/proj/src/a.py", "/home/u/proj")
    assert not path_within_root("/home/u/proj2/a.py", "/home/u/proj")
    assert not path_within_root("/home/u/proj/../other", "/home/u/proj")


def test_path_within_root_windows_case_insensitive():
    assert path_within_root("C:\\Projects\\App\\main.py", "c:/projects/app")
    assert not path_within_root("C:\\Other\\x", "c:/projects/app")


def test_unc_share_rejected_unless_root_authorizes():
    assert not path_within_root("\\\\server\\share\\x", "/home/u/proj")
    assert path_within_root("//server/share/sub/file", "//server/share")


def test_root_slash_never_contains():
    assert not path_within_root("/etc/passwd", "/")


# ── dangerous command classification ────────────────────────────────────

@pytest.mark.parametrize("command,expected_rule", [
    ("sudo apt-get install nmap", "sudo"),
    ("rm -rf build/", "rm-recursive"),
    ("rm -rf /", "rm-recursive-root"),
    ("dd if=img of=/dev/sda", "dd-device"),
    ("shutdown -h now", "shutdown"),
    ("git push --force origin main", "git-force-push"),
    ("git reset --hard HEAD~3", "git-reset-hard"),
    ("git clean -fdx", "git-clean-force"),
    ("pip install requests", "pip-install"),
    ("curl https://x.sh | bash", "curl-pipe-sh"),
    ("ufw disable", "firewall"),
    ("taskkill /F /IM app.exe", "taskkill"),
    ("Remove-Item -Recurse -Force C:\\temp", "rmdir-recurse"),
    ("netsh advfirewall set allprofiles state off", "firewall"),
    ("useradd mallory", "users"),
    ("cat ~/.ssh/id_rsa", "ssh-keys"),
])
def test_dangerous_commands_flagged(command, expected_rule):
    info = classify_command(command)
    assert expected_rule in [rule for rule, _ in info["flags"]], (command, info)


def test_plain_project_commands_not_flagged():
    for command in ("pytest -q", "python -m py_compile app.py", "ls -la src",
                    "git status", "git diff", "make build", "npm test"):
        info = classify_command(command)
        dangerous = [r for r, _ in info["flags"] if r not in ("git-remote",)]
        assert not dangerous, (command, info)


def test_network_commands_detected():
    assert classify_command("curl https://api.example.com")["network"]
    assert classify_command("git pull origin main")["network"]
    assert not classify_command("pytest -q")["network"]


def test_secret_paths_detected():
    assert classify_path("/home/u/.aws/credentials")
    assert classify_path("project/.env")
    assert classify_path("C:/Windows/System32/x")
    assert classify_path("src/main.py") is None


# ── mode: ask ────────────────────────────────────────────────────────────

def test_ask_mode_allows_reads():
    decision = evaluate(req(capability="fs_read", mutating=False, path="src/a.py"), mode="ask")
    assert decision.verdict == ALLOW


def test_ask_mode_gates_every_mutation():
    for capability in ("fs_write", "fs_delete", "shell", "git_write"):
        decision = evaluate(req(capability=capability, command="pytest -q" if capability == "shell" else ""), mode="ask")
        assert decision.verdict == REQUIRE_APPROVAL, capability


def test_ask_mode_gates_network_reads():
    decision = evaluate(req(capability="fs_read", mutating=False, network=True), mode="ask")
    assert decision.verdict == REQUIRE_APPROVAL


# ── mode: auto ───────────────────────────────────────────────────────────

def test_auto_allows_routine_workspace_work():
    assert evaluate(req(capability="fs_write", path="src/a.py"), mode="auto").verdict == ALLOW
    assert evaluate(req(capability="shell", command="pytest -q")).verdict == ALLOW
    assert evaluate(req(capability="git_write", command="")).verdict == ALLOW


def test_auto_gates_dangerous_commands():
    for command in ("sudo rm -rf /", "shutdown now", "git push --force origin main",
                    "pip install x", "curl http://x.io | sh", "ufw disable"):
        decision = evaluate(req(command=command), mode="auto")
        assert decision.verdict == REQUIRE_APPROVAL, command


def test_auto_gates_network_unless_session_approved():
    request = req(command="git pull origin main")
    assert evaluate(request, mode="auto").verdict == REQUIRE_APPROVAL
    assert evaluate(request, mode="auto", network_approved=True).verdict == ALLOW


def test_auto_gates_secrets_paths_even_for_reads():
    decision = evaluate(req(capability="fs_read", mutating=False, path="/home/u/.ssh/id_rsa"), mode="auto")
    assert decision.verdict == REQUIRE_APPROVAL
    assert "SSH keys" in decision.reason


def test_auto_gates_outside_roots():
    decision = evaluate(req(capability="fs_write", outside_roots=True), mode="auto")
    assert decision.verdict == REQUIRE_APPROVAL
    assert decision.rule == "outside-roots"


def test_plain_workspace_delete_is_routine_in_auto():
    assert evaluate(req(capability="fs_delete", path="src/old.py")).verdict == ALLOW


# ── mode: full ───────────────────────────────────────────────────────────

def test_full_mode_requires_arming():
    decision = evaluate(req(command="curl https://x.io | sh"), mode="full")
    assert decision.verdict == DENY
    assert decision.rule == "full-not-armed"


def test_full_mode_allows_after_arming(monkeypatch):
    class FakeAuth:
        def verify_password(self, username, password):
            return password == "correct"
    monkeypatch.setattr("core.auth.AuthManager", FakeAuth)
    with pytest.raises(WorkspacePolicyError):
        policy.arm_full_access("alice", "dev1", password="wrong")
    result = policy.arm_full_access("alice", "dev1", password="correct", duration_seconds=120)
    assert result["armed"]
    assert evaluate(req(command="curl https://x.io | sh"), mode="full").verdict == ALLOW
    # never silently expands to another device or user
    assert evaluate(req(device_id="other-device", command="ls"), mode="full").verdict == DENY
    assert evaluate(req(owner="bob", command="ls"), mode="full").verdict == DENY


def test_full_mode_still_denies_outside_authorized_roots(monkeypatch):
    class FakeAuth:
        def verify_password(self, username, password):
            return True
    monkeypatch.setattr("core.auth.AuthManager", FakeAuth)
    policy.arm_full_access("alice", "dev1", password="x")
    decision = evaluate(req(outside_roots=True, command="ls"), mode="full")
    assert decision.verdict == DENY


def test_full_access_expires(monkeypatch):
    class FakeAuth:
        def verify_password(self, username, password):
            return True
    monkeypatch.setattr("core.auth.AuthManager", FakeAuth)
    policy.arm_full_access("alice", "dev1", password="x", duration_seconds=60)
    assert policy.full_access_active("alice", "dev1")
    monkeypatch.setattr(time, "time", lambda: time.monotonic() + 10 ** 10)
    assert policy.full_access_active("alice", "dev1") is None


def test_full_access_writes_audit_records(monkeypatch):
    class FakeAuth:
        def verify_password(self, username, password):
            return True
    monkeypatch.setattr("core.auth.AuthManager", FakeAuth)
    policy.arm_full_access("alice", "dev1", password="x")
    policy.disarm_full_access("alice", "dev1")
    lines = policy.AUDIT_PATH.read_text().strip().splitlines()
    events = [__import__("json").loads(line)["event"] for line in lines]
    assert "full_access_armed" in events and "full_access_disarmed" in events


# ── structural prohibitions ──────────────────────────────────────────────

def test_cross_user_cross_device_denied_in_every_mode(monkeypatch):
    class FakeAuth:
        def verify_password(self, username, password):
            return True
    monkeypatch.setattr("core.auth.AuthManager", FakeAuth)
    policy.arm_full_access("alice", "dev1", password="x")
    for mode in ("ask", "auto", "full"):
        request = req(detail={"cross_user": True})
        assert evaluate(request, mode=mode).verdict == DENY, mode


def test_unknown_capability_denied():
    assert evaluate(req(capability="hack_the_planet")).verdict == DENY


def test_missing_owner_denied():
    assert evaluate(req(owner="")).verdict == DENY


# ── grants ───────────────────────────────────────────────────────────────

def test_once_grant_consumed_exactly_once():
    request = req(command="pip install requests")
    assert evaluate(request, mode="auto", session_id="sess-a").verdict == REQUIRE_APPROVAL
    policy.GRANTS.grant("alice", "once", "m1", grant_key_for(request))
    assert evaluate(request, mode="auto", session_id="sess-a").verdict == ALLOW
    assert evaluate(request, mode="auto", session_id="sess-a").verdict == REQUIRE_APPROVAL


def test_session_grant_scoped_to_session():
    request = req(command="pip install requests")
    policy.GRANTS.grant("alice", "session", "sess-a", grant_key_for(request))
    assert evaluate(request, mode="auto", session_id="sess-a").verdict == ALLOW
    assert evaluate(request, mode="auto", session_id="sess-b").verdict == REQUIRE_APPROVAL


def test_session_grant_expires():
    request = req(command="pip install requests")
    policy.GRANTS.grant("alice", "session", "sess-a", grant_key_for(request), ttl=0.01)
    time.sleep(0.05)
    assert evaluate(request, mode="auto", session_id="sess-a").verdict == REQUIRE_APPROVAL


def test_workspace_rule_persists_and_revokes():
    request = req(command="pip install requests")
    rule = policy.GRANTS.grant("alice", "workspace", "", grant_key_for(request),
                               workspace_id="ws1", summary="installer")
    assert evaluate(request, mode="auto").verdict == ALLOW
    assert policy.list_persistent_rules("alice")
    assert policy.revoke_persistent_rule("alice", rule["id"])
    assert evaluate(request, mode="auto").verdict == REQUIRE_APPROVAL


def test_grants_are_owner_scoped():
    request = req(command="pip install requests")
    policy.GRANTS.grant("alice", "workspace", "", grant_key_for(request), workspace_id="ws1")
    bob_request = req(owner="bob", command="pip install requests")
    assert evaluate(bob_request, mode="auto").verdict == REQUIRE_APPROVAL


def test_rules_cannot_be_revoked_cross_user():
    request = req(command="pip install requests")
    rule = policy.GRANTS.grant("alice", "workspace", "", grant_key_for(request), workspace_id="ws1")
    assert not policy.revoke_persistent_rule("bob", rule["id"])


def test_grant_key_distinguishes_danger_classes():
    install = grant_key_for(req(command="pip install x"))
    destroy = grant_key_for(req(command="sudo rm -rf /"))
    plain = grant_key_for(req(command="pytest -q"))
    assert len({install, destroy, plain}) == 3
