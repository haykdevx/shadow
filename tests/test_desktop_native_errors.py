import subprocess

import pytest

from companion import home_agent


@pytest.mark.parametrize("system", ["Windows", "Darwin", "Linux"])
def test_native_permission_failure_is_actionable(monkeypatch, system):
    monkeypatch.setattr(home_agent.platform, "system", lambda: system)
    def denied(*args, **kwargs):
        raise PermissionError("permission denied")
    monkeypatch.setattr(subprocess, "run", denied)
    with pytest.raises(home_agent.HomeAgentError, match="permission"):
        home_agent._run(["native-helper"])


def test_native_fallback_survives_permission_failure(monkeypatch):
    monkeypatch.setattr(home_agent.shutil, "which", lambda name: name)
    def execute(argv, **kwargs):
        if argv[0] == "first":
            raise PermissionError("permission denied")
        return subprocess.CompletedProcess(argv, 0, "ready", "")
    monkeypatch.setattr(subprocess, "run", execute)
    assert home_agent._run_first([["first"], ["second"]]) == "ready"
