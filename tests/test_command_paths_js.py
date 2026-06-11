import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_command_paths_handle_windows_and_posix():
    if shutil.which("node") is None:
        pytest.skip("node binary not on PATH")
    script = textwrap.dedent(
        """
        const paths = await import('./static/js/commandPaths.js');
        console.log(JSON.stringify({
          windowsParent: paths.parentPath('C:\\\\Users\\\\Max\\\\Documents'),
          windowsRoot: paths.parentPath('C:\\\\Users'),
          windowsJoin: paths.joinPath('C:\\\\Users\\\\Max', 'note.txt'),
          posixParent: paths.parentPath('/home/hayk/projects'),
          posixRoot: paths.parentPath('/home'),
          posixJoin: paths.joinPath('/home/hayk', 'note.txt'),
        }));
        """
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "windowsParent": r"C:\Users\Max",
        "windowsRoot": "C:\\",
        "windowsJoin": r"C:\Users\Max\note.txt",
        "posixParent": "/home/hayk",
        "posixRoot": "/",
        "posixJoin": "/home/hayk/note.txt",
    }
