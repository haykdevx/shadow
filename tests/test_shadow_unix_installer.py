from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-shadow-device.sh"


def test_unix_installer_requires_and_checks_workspace_agent():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "workspace_agent.py" in text
    assert "workspace_agent.py\" || true" not in text
    assert "--check" in text
    assert "SHADOW_ALLOWED_ROOTS" in text
    assert "EnvironmentFile=" in text
    assert "<key>EnvironmentVariables</key>" in text


def test_unix_installer_has_repair_and_bounded_retries():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "--repair" in text
    assert "--retry 4" in text
    assert "--max-time 180" in text
    assert "shadow-device.service" in text
