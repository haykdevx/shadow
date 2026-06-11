from pathlib import Path

from routes.shadow_routes import _setup_commands


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-shadow-device.ps1"
AGENT = ROOT / "companion" / "shadow-device.ps1"


def test_windows_setup_command_is_one_shot_and_quotes_server_and_code():
    command = _setup_commands("https://shadow.example", "ABCD-EF12-3456")["windows"]

    assert command.startswith('powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "')
    assert "curl.exe" in command
    assert "--connect-timeout 15" in command
    assert "https://shadow.example/api/shadow/device/install/windows" in command
    assert "EncodedCommand" not in command
    assert "-Server 'https://shadow.example'" in command
    assert "-Code 'ABCD-EF12-3456'" in command


def test_windows_installer_has_no_external_runtime_dependency():
    text = INSTALLER.read_text(encoding="utf-8").lower()

    assert "get-command python" not in text
    assert "relay_agent.py" not in text
    assert "pip install" not in text
    assert "get-command node" not in text
    assert "shadow-device.ps1" in text
    assert '-name $name' not in text
    assert "new-scheduledtaskaction" not in text
    assert "start-scheduledtask" not in text
    assert "current-user run key" in text
    assert "hkcu:\\software\\microsoft\\windows\\currentversion\\run" in text
    assert "[switch]$repair" in text
    assert "startup folder" in text
    assert "update-shadowagent" in text
    assert "/api/shadow/device/source/shadow-device.ps1" in text
    assert "curl.exe" in text
    assert "invoke-webrequest" not in text
    assert "system.management.automation.language.parser" not in text


def test_windows_agent_uses_dpapi_and_matches_relay_protocol():
    text = AGENT.read_text(encoding="utf-8")

    assert "ProtectedData]::Protect" in text
    assert "DataProtectionScope]::CurrentUser" in text
    assert "/api/shadow/device/enroll" in text
    assert "/api/shadow/device/poll" in text
    assert "/api/shadow/device/result" in text
    assert '$roots = @(Get-ShadowAllowedRoots)' in text
    assert '$script:AgentVersion = "2.1.0"' in text


def test_windows_agent_advertises_every_command_capability():
    text = AGENT.read_text(encoding="utf-8")
    actions = {
        "status",
        "processes",
        "screenshot",
        "clipboard_get",
        "clipboard_set",
        "windows",
        "file_list",
        "file_read",
        "file_search",
        "file_write",
        "shell",
        "kill_process",
        "lock",
        "sleep",
        "shutdown",
        "type_text",
        "keypress",
        "mouse_move",
        "mouse_click",
        "media",
        "volume",
        "app_launch",
        "app_focus",
        "app_close",
    }
    for action in actions:
        assert f'"{action}"' in text


def test_windows_agent_advertises_codex_style_workspace_actions():
    text = AGENT.read_text(encoding="utf-8")
    actions = {
        "ws_tree", "ws_stat", "ws_read", "ws_search", "ws_hash", "ws_diff",
        "ws_write", "ws_mkdir", "ws_rename", "ws_delete", "ws_patch", "ws_run",
        "git_info", "git_diff", "git_log", "git_commit", "git_checkout",
        "ws_checkpoint", "ws_restore",
    }
    for action in actions:
        assert f'"{action}"' in text
    assert "Get-ShadowWorkspaceRoots" in text
    assert "Resolve-ShadowWorkspacePath" in text
    assert "Save-ShadowWorkspacePreimage" in text
