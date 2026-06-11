param(
  [string]$Server = "",
  [string]$Code = "",
  [string]$Name = "",
  [switch]$Repair,
  [switch]$Uninstall,
  [switch]$Purge
)

$ErrorActionPreference = "Stop"
$Base = Join-Path $env:LOCALAPPDATA "Shadow\device-agent"
$Agent = Join-Path $Base "shadow-device.ps1"
$Config = Join-Path $env:APPDATA "Shadow\device.json"
$LegacyTaskName = "Shadow Device"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunValueName = "ShadowDevice"
$Startup = [Environment]::GetFolderPath("Startup")
$StartupFile = Join-Path $Startup "shadow-device.cmd"
$PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$AgentArguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Agent`""
$RunCommand = "`"$PowerShell`" $AgentArguments"

function Stop-ShadowDevice {
  # Clean up the legacy Scheduled Task used by older installers. Its removal is
  # best-effort because standard Windows accounts may not own that task.
  try { Stop-ScheduledTask -TaskName $LegacyTaskName -ErrorAction SilentlyContinue } catch {}
  try { Unregister-ScheduledTask -TaskName $LegacyTaskName -Confirm:$false -ErrorAction SilentlyContinue } catch {}
  Remove-ItemProperty -Path $RunKey -Name $RunValueName -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $StartupFile -Force -ErrorAction SilentlyContinue
  try {
    Get-CimInstance Win32_Process |
      Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($Agent, [StringComparison]::OrdinalIgnoreCase) -ge 0 } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  } catch {}
}

function Install-ShadowStartup {
  $method = "Current-user Run key"
  try {
    New-Item -Path $RunKey -Force -ErrorAction Stop | Out-Null
    New-ItemProperty `
      -Path $RunKey `
      -Name $RunValueName `
      -Value $RunCommand `
      -PropertyType String `
      -Force `
      -ErrorAction Stop | Out-Null
    $saved = (Get-ItemProperty -Path $RunKey -Name $RunValueName -ErrorAction Stop).$RunValueName
    if ($saved -ne $RunCommand) { throw "Windows did not persist the startup command." }
  } catch {
    $method = "Startup folder"
    $command = "@start `"`" `"$PowerShell`" $AgentArguments"
    Set-Content -LiteralPath $StartupFile -Value $command -Encoding ASCII -ErrorAction Stop
    if (-not (Test-Path -LiteralPath $StartupFile -PathType Leaf)) {
      throw "Could not create current-user startup registration."
    }
  }
  Start-Process -FilePath $PowerShell -ArgumentList $AgentArguments -WindowStyle Hidden -ErrorAction Stop
  return $method
}

function Update-ShadowAgent {
  param([Parameter(Mandatory = $true)][string]$BaseUrl)
  if ($BaseUrl -notmatch "^https://" -and $BaseUrl -notmatch "^http://(localhost|127\.0\.0\.1)(:\d+)?$") {
    throw "Shadow server must use HTTPS (HTTP is allowed only for localhost)."
  }
  $Curl = Join-Path $env:SystemRoot "System32\curl.exe"
  if (-not (Test-Path -LiteralPath $Curl -PathType Leaf)) { throw "Windows curl.exe is required." }
  New-Item -ItemType Directory -Force -Path $Base | Out-Null
  $TempGzip = Join-Path $env:TEMP ("shadow-device-" + [Guid]::NewGuid().ToString("N") + ".ps1.gz")
  $TempAgent = Join-Path $env:TEMP ("shadow-device-" + [Guid]::NewGuid().ToString("N") + ".ps1")
  try {
    $CurlOutput = @(& $Curl "--silent" "--show-error" "--fail" "--connect-timeout" "15" "--max-time" "300" "--retry" "4" "--retry-delay" "2" "--output" $TempGzip ($BaseUrl.TrimEnd("/") + "/api/shadow/device/source/shadow-device.ps1.gz") 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "Agent download failed: $($CurlOutput -join ' ')" }
    $InputStream = [IO.File]::OpenRead($TempGzip)
    $OutputStream = [IO.File]::Create($TempAgent)
    $GzipStream = New-Object IO.Compression.GzipStream($InputStream, [IO.Compression.CompressionMode]::Decompress)
    try { $GzipStream.CopyTo($OutputStream) } finally {
      $GzipStream.Dispose()
      $OutputStream.Dispose()
      $InputStream.Dispose()
    }
    if ((Get-Item -LiteralPath $TempAgent).Length -lt 10000) { throw "The downloaded Windows companion is incomplete." }
    Move-Item -LiteralPath $TempAgent -Destination $Agent -Force
  } finally {
    Remove-Item -LiteralPath $TempGzip, $TempAgent -Force -ErrorAction SilentlyContinue
  }
}

function Save-ShadowEnrollmentDirect {
  param(
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [Parameter(Mandatory = $true)][string]$EnrollmentCode,
    [string]$DeviceName = ""
  )
  $Curl = Join-Path $env:SystemRoot "System32\curl.exe"
  if (-not (Test-Path -LiteralPath $Curl -PathType Leaf)) { throw "Windows curl.exe is required." }
  $ResolvedName = $DeviceName.Trim()
  if (-not $ResolvedName) { $ResolvedName = $env:COMPUTERNAME }
  $Payload = [ordered]@{
    code = $EnrollmentCode.Trim()
    name = $ResolvedName
    hostname = $env:COMPUTERNAME
    platform = "Windows $([Environment]::OSVersion.Version)"
    agent_version = "2.1.0"
    capabilities = @(
      "status", "processes", "screenshot", "clipboard_get", "windows",
      "file_list", "file_read", "file_search", "clipboard_set", "media",
      "volume", "app_launch", "app_focus", "app_close", "kill_process",
      "shell", "file_write", "lock", "sleep", "shutdown", "type_text",
      "keypress", "mouse_move", "mouse_click",
      "ws_tree", "ws_stat", "ws_read", "ws_search", "ws_hash", "ws_diff",
      "ws_write", "ws_mkdir", "ws_rename", "ws_delete", "ws_patch", "ws_run",
      "git_info", "git_diff", "git_log", "git_commit", "git_checkout",
      "ws_checkpoint", "ws_restore"
    )
  }
  $RequestFile = Join-Path $env:TEMP ("shadow-enroll-request-" + [Guid]::NewGuid().ToString("N") + ".json")
  $ResponseFile = Join-Path $env:TEMP ("shadow-enroll-response-" + [Guid]::NewGuid().ToString("N") + ".json")
  try {
    $Utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($RequestFile, ($Payload | ConvertTo-Json -Depth 6 -Compress), $Utf8)
    Write-Host "[1/4] Enrolling this PC..."
    $CurlOutput = @(& $Curl "--silent" "--show-error" "--fail" "--connect-timeout" "10" "--max-time" "30" "--request" "POST" "--header" "Content-Type: application/json" "--data-binary" "@$RequestFile" "--output" $ResponseFile ($BaseUrl.TrimEnd("/") + "/api/shadow/device/enroll") 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "Enrollment request failed: $($CurlOutput -join ' ')" }
    $Result = Get-Content -LiteralPath $ResponseFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $Result.token -or -not $Result.device.id) { throw "Shadow returned an incomplete enrollment response." }
    Add-Type -AssemblyName System.Security
    $Bytes = [Text.Encoding]::UTF8.GetBytes([string]$Result.token)
    $Protected = [Security.Cryptography.ProtectedData]::Protect($Bytes, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Config) | Out-Null
    $Saved = [ordered]@{
      server = $BaseUrl.TrimEnd("/")
      device_id = [string]$Result.device.id
      name = [string]$Result.device.name
      token_protected = [Convert]::ToBase64String($Protected)
      installed_at = [DateTime]::UtcNow.ToString("o")
      agent_version = "2.1.0"
    }
    [IO.File]::WriteAllText($Config, ($Saved | ConvertTo-Json -Depth 6), $Utf8)
    return $Saved
  } finally {
    Remove-Item -LiteralPath $RequestFile, $ResponseFile -Force -ErrorAction SilentlyContinue
  }
}

if ($Uninstall) {
  Stop-ShadowDevice
  Remove-Item -LiteralPath $Base -Recurse -Force -ErrorAction SilentlyContinue
  if ($Purge) { Remove-Item -LiteralPath $Config -Force -ErrorAction SilentlyContinue }
  Write-Host "Shadow device removed."
  exit 0
}

if ($Repair) {
  if (-not (Test-Path -LiteralPath $Agent -PathType Leaf)) {
    throw "Shadow agent is missing. Run a fresh setup command from Shadow Command."
  }
  if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) {
    throw "Shadow enrollment is missing. Run a fresh setup command from Shadow Command."
  }
  $SavedConfig = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
  if (-not $SavedConfig.server) { throw "Shadow enrollment does not contain a server URL." }
  Update-ShadowAgent ([string]$SavedConfig.server)
  Stop-ShadowDevice
  $InstalledWith = Install-ShadowStartup
  Write-Host ""
  Write-Host "Shadow device startup repaired and agent started."
  Write-Host "Startup: $InstalledWith"
  exit 0
}

if (-not $Server -or -not $Code) {
  throw "Server and Code are required. Create a fresh setup code in Shadow Command."
}
if ($Server -notmatch "^https://" -and $Server -notmatch "^http://(localhost|127\.0\.0\.1)(:\d+)?$") {
  throw "Shadow server must use HTTPS (HTTP is allowed only for localhost)."
}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Enrollment = Save-ShadowEnrollmentDirect $Server $Code $Name
Write-Host "[2/4] Downloading the device agent..."
Update-ShadowAgent $Server
Write-Host "[3/4] Installing current-user startup..."

Stop-ShadowDevice
$InstalledWith = Install-ShadowStartup
Write-Host "[4/4] Connected as $($Enrollment.name)."

Write-Host ""
Write-Host "Shadow device installed and started."
Write-Host "Runtime: built-in Windows PowerShell/.NET (no Python, pip, Node, or admin install)."
Write-Host "Startup: $InstalledWith"
