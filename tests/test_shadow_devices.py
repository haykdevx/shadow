import threading

import pytest

from src import shadow_devices


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(shadow_devices, "DEVICES_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(shadow_devices, "JOBS_PATH", tmp_path / "jobs.json")
    monkeypatch.delenv("SHADOW_PC_OWNER", raising=False)
    monkeypatch.delenv("SHADOW_HOME_AGENT_URL", raising=False)
    monkeypatch.delenv("SHADOW_HOME_AGENT_SOCKET", raising=False)
    monkeypatch.delenv("SHADOW_HOME_AGENT_TOKEN", raising=False)


def _enroll(owner, name="PC"):
    code = shadow_devices.create_enrollment(owner)["code"]
    return shadow_devices.enroll_device(code, {"name": name, "platform": "Test OS", "agent_version": "2.0.0"})


def test_devices_are_owned_and_cross_account_lookup_is_rejected():
    alice = _enroll("alice", "Alice PC")
    bob = _enroll("bob", "Bob PC")

    assert [row["name"] for row in shadow_devices.list_devices("alice")] == ["Alice PC"]
    assert [row["name"] for row in shadow_devices.list_devices("bob")] == ["Bob PC"]
    with pytest.raises(shadow_devices.ShadowDeviceError, match="does not belong"):
        shadow_devices.get_device("alice", bob["device"]["id"])
    assert shadow_devices.authenticate_device(alice["token"])["owner"] == "alice"
    assert alice["device"]["agent_version"] == "2.0.0"
    shadow_devices.touch_device(alice["device"]["id"], {"agent_version": "2.0.1"})
    assert shadow_devices.get_device("alice", alice["device"]["id"])["agent_version"] == "2.0.1"


def test_enrollment_code_is_single_use():
    pair = shadow_devices.create_enrollment("alice")
    shadow_devices.enroll_device(pair["code"], {"name": "One"})
    with pytest.raises(shadow_devices.ShadowDeviceError, match="invalid or expired"):
        shadow_devices.enroll_device(pair["code"], {"name": "Two"})


def test_legacy_device_is_visible_only_to_configured_owner(monkeypatch):
    monkeypatch.setenv("SHADOW_PC_OWNER", "haykdevx")
    monkeypatch.setenv("SHADOW_HOME_AGENT_URL", "http://100.64.1.2:8765")
    monkeypatch.setenv("SHADOW_HOME_AGENT_TOKEN", "a" * 64)

    assert shadow_devices.list_devices("haykdevx")[0]["id"] == "legacy-home"
    assert shadow_devices.list_devices("other") == []


def test_durable_relay_job_round_trip():
    enrolled = _enroll("alice")
    device = shadow_devices.authenticate_device(enrolled["token"])
    output = {}

    def dispatch():
        output.update(shadow_devices.dispatch_action("alice", device["id"], "status", {}, confirmed=False, timeout=5))

    thread = threading.Thread(target=dispatch)
    thread.start()
    job = shadow_devices.poll_job(device, timeout=2)
    assert job["action"] == "status"
    shadow_devices.complete_job(device, job["id"], {"result": {"ok": True, "hostname": "alice-pc"}})
    thread.join(timeout=5)
    assert output["hostname"] == "alice-pc"


def test_relay_agent_advertises_workspace_capabilities_and_version():
    from companion import relay_agent

    metadata = relay_agent._metadata("Workstation")
    assert metadata["agent_version"] == relay_agent.AGENT_VERSION
    assert "ws_tree" in metadata["capabilities"]
    assert "ws_write" in metadata["capabilities"]
