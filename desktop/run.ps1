# Shadow Desktop - Windows launcher.
# Creates an isolated venv for the pywebview shell and starts the app.
# Requires Python 3 and Docker Desktop. On Windows, pywebview uses the built-in
# Edge WebView2 runtime (preinstalled on Windows 10/11).
$ErrorActionPreference = "Stop"

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Here ".venv-desktop"
$Py   = if ($env:PYTHON) { $env:PYTHON } else { "python" }

if (-not (Get-Command $Py -ErrorAction SilentlyContinue)) {
  Write-Error "Python 3 is required (set `$env:PYTHON to override)."
}

if (-not (Test-Path $Venv)) {
  Write-Host "[shadow-desktop] creating launcher venv..."
  & $Py -m venv $Venv
  & (Join-Path $Venv "Scripts\pip.exe") install --quiet --upgrade pip
  & (Join-Path $Venv "Scripts\pip.exe") install --quiet -r (Join-Path $Here "requirements-desktop.txt")
}

& (Join-Path $Venv "Scripts\python.exe") (Join-Path $Here "launcher.py") @args
