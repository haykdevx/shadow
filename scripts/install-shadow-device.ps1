param(
  [Parameter(Mandatory=$true)][string]$Server,
  [Parameter(Mandatory=$true)][string]$Code,
  [string]$Name = ""
)

$ErrorActionPreference = "Stop"
$Base = Join-Path $env:LOCALAPPDATA "Shadow\device-agent"
New-Item -ItemType Directory -Force -Path $Base | Out-Null
Invoke-WebRequest "$Server/api/shadow/device/source/relay_agent.py" -OutFile (Join-Path $Base "relay_agent.py")
Invoke-WebRequest "$Server/api/shadow/device/source/home_agent.py" -OutFile (Join-Path $Base "home_agent.py")

$Python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) { $Python = (Get-Command py -ErrorAction Stop).Source }
try { & $Python -m pip install --user -q psutil } catch { Write-Warning "psutil install failed; limited telemetry will be available" }
& $Python (Join-Path $Base "relay_agent.py") --server $Server --enroll $Code --name $Name --once

$TaskName = "Shadow Device"
$Action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Base\relay_agent.py`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Shadow device installed and started."
