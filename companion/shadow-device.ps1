[CmdletBinding()]
param(
    [string]$Server = "",
    [string]$Enroll = "",
    [string]$Name = "",
    [switch]$Once
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.Security

$script:AgentVersion = "2.1.0"
$script:ConfigDir = Join-Path $env:APPDATA "Shadow"
$script:ConfigPath = Join-Path $script:ConfigDir "device.json"
$script:Capabilities = @(
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
$script:WriteActions = @(
    "clipboard_set", "media", "volume", "app_launch", "app_focus",
    "app_close", "kill_process", "shell", "file_write", "lock", "sleep",
    "shutdown", "type_text", "keypress", "mouse_move", "mouse_click",
    "ws_write", "ws_mkdir", "ws_rename", "ws_delete", "ws_patch", "ws_run",
    "git_commit", "git_checkout", "ws_checkpoint", "ws_restore"
)

function Get-ShadowArg {
    param($Arguments, [string]$Key, $Default = $null)
    if ($null -ne $Arguments -and $null -ne $Arguments.PSObject.Properties[$Key]) {
        return $Arguments.$Key
    }
    return $Default
}

function ConvertTo-ShadowJson {
    param($Value)
    return ($Value | ConvertTo-Json -Depth 12 -Compress)
}

function Get-ShadowErrorDetail {
    param($ErrorRecord)
    $detail = ""
    try {
        $payload = $ErrorRecord.ErrorDetails.Message | ConvertFrom-Json
        if ($payload.detail) { $detail = [string]$payload.detail }
    } catch {}
    if (-not $detail) { $detail = [string]$ErrorRecord.Exception.Message }
    return $detail
}

function Invoke-ShadowApi {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Payload,
        [string]$Token = "",
        [int]$TimeoutSec = 40
    )
    $curl = Join-Path $env:SystemRoot "System32\curl.exe"
    if (-not (Test-Path -LiteralPath $curl -PathType Leaf)) { throw "Windows curl.exe is required." }
    $requestFile = Join-Path $env:TEMP ("shadow-request-" + [Guid]::NewGuid().ToString("N") + ".json")
    $responseFile = Join-Path $env:TEMP ("shadow-response-" + [Guid]::NewGuid().ToString("N") + ".json")
    try {
        $utf8 = New-Object Text.UTF8Encoding($false)
        [IO.File]::WriteAllText($requestFile, (ConvertTo-ShadowJson $Payload), $utf8)
        $curlArgs = @("--silent", "--show-error", "--fail", "--connect-timeout", "10", "--max-time", [string]$TimeoutSec, "--request", "POST", "--header", "Content-Type: application/json", "--header", "Accept: application/json", "--header", "User-Agent: ShadowDevice-Windows/$script:AgentVersion")
        if ($Token) { $curlArgs += @("--header", "Authorization: Bearer $Token") }
        $curlArgs += @("--data-binary", "@$requestFile", "--output", $responseFile, ($BaseUrl.TrimEnd("/") + $Path))
        $output = @(& $curl @curlArgs 2>&1)
        if ($LASTEXITCODE -ne 0) { throw "curl exited with code $LASTEXITCODE`: $($output -join ' ')" }
        $response = Get-Content -LiteralPath $responseFile -Raw -Encoding UTF8
        if (-not $response) { return $null }
        return ($response | ConvertFrom-Json)
    } catch {
        throw "Shadow request failed: $($_.Exception.Message)"
    } finally {
        Remove-Item -LiteralPath $requestFile, $responseFile -Force -ErrorAction SilentlyContinue
    }
}

function Protect-ShadowToken {
    param([Parameter(Mandatory = $true)][string]$Token)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Token)
    $protected = [Security.Cryptography.ProtectedData]::Protect(
        $bytes,
        $null,
        [Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    return [Convert]::ToBase64String($protected)
}

function Unprotect-ShadowToken {
    param([Parameter(Mandatory = $true)][string]$ProtectedToken)
    $bytes = [Convert]::FromBase64String($ProtectedToken)
    $plain = [Security.Cryptography.ProtectedData]::Unprotect(
        $bytes,
        $null,
        [Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    return [Text.Encoding]::UTF8.GetString($plain)
}

function Protect-ShadowConfigAcl {
    param([Parameter(Mandatory = $true)][string]$Path)
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $acl = New-Object Security.AccessControl.FileSecurity
        $acl.SetAccessRuleProtection($true, $false)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule(
            $identity,
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow
        )
        $acl.AddAccessRule($rule)
        Set-Acl -LiteralPath $Path -AclObject $acl
    } catch {
        Write-Warning "Could not restrict the config ACL: $($_.Exception.Message)"
    }
}

function Get-ShadowMetadata {
    param([string]$DeviceName = "")
    $resolvedName = $DeviceName.Trim()
    if (-not $resolvedName) { $resolvedName = $env:COMPUTERNAME }
    return [ordered]@{
        name = $resolvedName
        hostname = $env:COMPUTERNAME
        platform = "Windows $([Environment]::OSVersion.Version)"
        agent_version = $script:AgentVersion
        capabilities = $script:Capabilities
    }
}

function Save-ShadowEnrollment {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][string]$Code,
        [string]$DeviceName = ""
    )
    if ($BaseUrl -notmatch "^https://" -and $BaseUrl -notmatch "^http://(localhost|127\.0\.0\.1)(:\d+)?$") {
        throw "Shadow server must use HTTPS (HTTP is allowed only for localhost)."
    }
    $payload = Get-ShadowMetadata $DeviceName
    $payload["code"] = $Code.Trim()
    $result = Invoke-ShadowApi $BaseUrl "/api/shadow/device/enroll" $payload
    if (-not $result.token -or -not $result.device.id) {
        throw "Shadow returned an incomplete enrollment response."
    }
    New-Item -ItemType Directory -Force -Path $script:ConfigDir | Out-Null
    $config = [ordered]@{
        server = $BaseUrl.TrimEnd("/")
        device_id = [string]$result.device.id
        name = [string]$result.device.name
        token_protected = Protect-ShadowToken ([string]$result.token)
        installed_at = [DateTime]::UtcNow.ToString("o")
        agent_version = $script:AgentVersion
    }
    $config | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $script:ConfigPath -Encoding UTF8
    Protect-ShadowConfigAcl $script:ConfigPath
    return $config
}

function Read-ShadowConfig {
    if (-not (Test-Path -LiteralPath $script:ConfigPath -PathType Leaf)) {
        throw "Shadow device is not enrolled. Run the setup command from the Command page."
    }
    $config = Get-Content -LiteralPath $script:ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $config.server -or -not $config.device_id) {
        throw "Shadow device configuration is incomplete."
    }
    if ($config.token_protected) {
        $token = Unprotect-ShadowToken ([string]$config.token_protected)
    } elseif ($config.token) {
        $token = [string]$config.token
        $config | Add-Member -NotePropertyName token_protected -NotePropertyValue (Protect-ShadowToken $token) -Force
        $config.PSObject.Properties.Remove("token")
        $config | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $script:ConfigPath -Encoding UTF8
        Protect-ShadowConfigAcl $script:ConfigPath
    } else {
        throw "Shadow device token is missing."
    }
    return [ordered]@{
        server = [string]$config.server
        device_id = [string]$config.device_id
        name = [string]$config.name
        token = $token
    }
}

function Get-ShadowAllowedRoots {
    $raw = [Environment]::GetEnvironmentVariable("SHADOW_ALLOWED_ROOTS", "User")
    if (-not $raw) { $raw = $env:USERPROFILE }
    $roots = New-Object Collections.Generic.List[string]
    foreach ($item in ($raw -split ";")) {
        if (-not $item.Trim()) { continue }
        try {
            $expanded = [Environment]::ExpandEnvironmentVariables($item.Trim())
            $full = [IO.Path]::GetFullPath($expanded).TrimEnd("\")
            if (Test-Path -LiteralPath $full -PathType Container) { $roots.Add($full) }
        } catch {}
    }
    if ($roots.Count -eq 0) { $roots.Add([IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd("\")) }
    return $roots.ToArray()
}

function Normalize-ShadowPath {
    param([Parameter(Mandatory = $true)][string]$Value)
    $full = [IO.Path]::GetFullPath($Value)
    $root = [IO.Path]::GetPathRoot($full)
    if ($root -and $full.Length -gt $root.Length) { return $full.TrimEnd("\") }
    return $full
}

function Resolve-ShadowPath {
    param([string]$Value = "", [switch]$AllowMissing)
    # PowerShell unwraps a one-item array into a scalar string. Without @(),
    # $roots[0] becomes the first character ("C") instead of "C:\Users\name".
    $roots = @(Get-ShadowAllowedRoots)
    if (-not $Value) { $Value = $roots[0] }
    $expanded = [Environment]::ExpandEnvironmentVariables($Value)
    if (-not [IO.Path]::IsPathRooted($expanded)) { $expanded = Join-Path $roots[0] $expanded }
    $full = Normalize-ShadowPath $expanded
    $allowed = $false
    foreach ($root in $roots) {
        if ($full.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
            $full.StartsWith($root + "\", [StringComparison]::OrdinalIgnoreCase)) {
            $allowed = $true
            break
        }
    }
    if (-not $allowed) { throw "Path is outside SHADOW_ALLOWED_ROOTS." }
    if (-not $AllowMissing -and -not (Test-Path -LiteralPath $full)) { throw "Path does not exist: $full" }
    return $full
}

function Get-ShadowFileEntry {
    param([Parameter(Mandatory = $true)][string]$Path)
    $item = Get-Item -LiteralPath $Path -Force
    $stamp = New-Object DateTimeOffset($item.LastWriteTimeUtc)
    return [ordered]@{
        name = $item.Name
        path = $item.FullName
        type = $(if ($item.PSIsContainer) { "dir" } else { "file" })
        size = $(if ($item.PSIsContainer) { 0 } else { [int64]$item.Length })
        modified = $stamp.ToUnixTimeSeconds()
    }
}

function Get-ShadowStatus {
    $os = Get-CimInstance Win32_OperatingSystem
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($env:SystemDrive)'" | Select-Object -First 1
    $memoryTotal = [int64]$os.TotalVisibleMemorySize * 1024
    $memoryFree = [int64]$os.FreePhysicalMemory * 1024
    $interfaces = @()
    try {
        $interfaces = @(Get-NetAdapterStatistics -ErrorAction Stop | ForEach-Object {
            [ordered]@{
                name = [string]$_.Name
                rx_bytes = [int64]$_.ReceivedBytes
                tx_bytes = [int64]$_.SentBytes
            }
        })
    } catch {}
    $rx = [int64]0
    $tx = [int64]0
    foreach ($interface in $interfaces) {
        $rx += [int64]$interface.rx_bytes
        $tx += [int64]$interface.tx_bytes
    }
    $gpus = @()
    try {
        $nvidia = Get-Command nvidia-smi.exe -ErrorAction Stop
        $rows = & $nvidia.Source "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu" "--format=csv,noheader,nounits"
        foreach ($row in $rows) {
            $parts = @($row -split "," | ForEach-Object { $_.Trim() })
            if ($parts.Count -ge 5) {
                $gpus += [ordered]@{
                    name = $parts[0]
                    util_percent = [double]$parts[1]
                    memory_used_mib = [double]$parts[2]
                    memory_total_mib = [double]$parts[3]
                    temp_c = [double]$parts[4]
                }
            }
        }
    } catch {
        try {
            $gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object {
                [ordered]@{
                    name = [string]$_.Name
                    util_percent = $null
                    memory_used_mib = $null
                    memory_total_mib = $(if ($_.AdapterRAM) { [math]::Round([double]$_.AdapterRAM / 1MB, 1) } else { $null })
                    temp_c = $null
                }
            })
        } catch {}
    }
    $boot = [DateTime]$os.LastBootUpTime
    $uptime = [math]::Max(0, ([DateTime]::Now - $boot).TotalSeconds)
    return [ordered]@{
        ok = $true
        hostname = $env:COMPUTERNAME
        platform = "Microsoft Windows $($os.Version)"
        os = [ordered]@{ system = "Windows"; release = [string]$os.Caption }
        uptime_seconds = [int64]$uptime
        load = @()
        cpu = [ordered]@{ percent = [double]$cpu.LoadPercentage; count = [Environment]::ProcessorCount }
        memory = [ordered]@{ total = $memoryTotal; used = $memoryTotal - $memoryFree; available = $memoryFree }
        disk = [ordered]@{ total = [int64]$disk.Size; used = [int64]$disk.Size - [int64]$disk.FreeSpace; free = [int64]$disk.FreeSpace }
        network = [ordered]@{ interfaces = $interfaces; rx_bytes = $rx; tx_bytes = $tx }
        gpu = $gpus
    }
}

function Get-ShadowProcesses {
    param($Arguments)
    $limit = [math]::Max(1, [math]::Min([int](Get-ShadowArg $Arguments "limit" 15), 50))
    $totalMemory = [double](Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory
    $cores = [math]::Max(1, [Environment]::ProcessorCount)
    $now = Get-Date
    $rows = @(Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $age = [math]::Max(1, ($now - $_.StartTime).TotalSeconds)
            $cpuPercent = [math]::Min(999, [math]::Round((([double]$_.CPU / $age) / $cores) * 100, 1))
            $memoryPercent = [math]::Round(([double]$_.WorkingSet64 / $totalMemory) * 100, 1)
            [pscustomobject]@{
                Id = $_.Id
                Name = ($_.ProcessName -replace "\s+", "_")
                Cpu = $cpuPercent
                Memory = $memoryPercent
            }
        } catch {}
    } | Sort-Object Cpu -Descending | Select-Object -First $limit)
    $lines = New-Object Collections.Generic.List[string]
    $lines.Add("PID COMMAND %CPU %MEM")
    foreach ($row in $rows) {
        $lines.Add(("{0} {1} {2:N1} {3:N1}" -f $row.Id, $row.Name, $row.Cpu, $row.Memory))
    }
    return [ordered]@{ ok = $true; processes = $lines.ToArray() }
}

function Get-ShadowWindows {
    $rows = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle } | ForEach-Object {
        [ordered]@{
            id = [string]$_.Id
            pid = [string]$_.Id
            class = [string]$_.ProcessName
            title = [string]$_.MainWindowTitle
            host = ""
        }
    })
    return [ordered]@{ ok = $true; windows = $rows }
}

function Get-ShadowScreenshot {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $bounds = [Windows.Forms.SystemInformation]::VirtualScreen
    $bitmap = New-Object Drawing.Bitmap $bounds.Width, $bounds.Height
    $graphics = [Drawing.Graphics]::FromImage($bitmap)
    $stream = New-Object IO.MemoryStream
    try {
        $graphics.CopyFromScreen($bounds.Location, [Drawing.Point]::Empty, $bounds.Size)
        $bitmap.Save($stream, [Drawing.Imaging.ImageFormat]::Png)
        return [ordered]@{
            ok = $true
            mime = "image/png"
            width = $bounds.Width
            height = $bounds.Height
            image_b64 = [Convert]::ToBase64String($stream.ToArray())
        }
    } finally {
        $stream.Dispose()
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

function Get-ShadowFileList {
    param($Arguments)
    $path = Resolve-ShadowPath ([string](Get-ShadowArg $Arguments "path" ""))
    if (-not (Test-Path -LiteralPath $path -PathType Container)) { throw "Path is not a directory." }
    $limit = [math]::Max(1, [math]::Min([int](Get-ShadowArg $Arguments "limit" 160), 400))
    $entries = @(Get-ChildItem -LiteralPath $path -Force -ErrorAction Stop |
        Sort-Object @{ Expression = { -not $_.PSIsContainer } }, Name |
        Select-Object -First $limit |
        ForEach-Object { Get-ShadowFileEntry $_.FullName })
    return [ordered]@{ ok = $true; path = $path; roots = @(Get-ShadowAllowedRoots); entries = $entries }
}

function Get-ShadowFile {
    param($Arguments)
    $path = Resolve-ShadowPath ([string](Get-ShadowArg $Arguments "path" ""))
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Path is not a file." }
    $maxBytes = [math]::Max(1000, [math]::Min([int](Get-ShadowArg $Arguments "max_bytes" 200000), 1000000))
    $all = [IO.File]::ReadAllBytes($path)
    $length = [math]::Min($all.Length, $maxBytes)
    $bytes = New-Object byte[] $length
    [Array]::Copy($all, $bytes, $length)
    $binary = [Array]::IndexOf($bytes, [byte]0) -ge 0
    $text = ""
    if (-not $binary) { $text = [Text.Encoding]::UTF8.GetString($bytes) }
    return [ordered]@{
        ok = $true
        path = $path
        name = [IO.Path]::GetFileName($path)
        size = [int64]$all.Length
        truncated = $all.Length -gt $length
        binary = $binary
        text = $text
        mime = "application/octet-stream"
    }
}

function Search-ShadowFiles {
    param($Arguments)
    $base = Resolve-ShadowPath ([string](Get-ShadowArg $Arguments "path" ""))
    if (-not (Test-Path -LiteralPath $base -PathType Container)) { throw "Search path is not a directory." }
    $query = ([string](Get-ShadowArg $Arguments "query" "")).Trim()
    if (-not $query) { throw "query is required." }
    $limit = [math]::Max(1, [math]::Min([int](Get-ShadowArg $Arguments "limit" 50), 100))
    $results = New-Object Collections.Generic.List[object]
    $visited = 0
    $truncated = $false
    $queue = New-Object Collections.Generic.Queue[string]
    $queue.Enqueue($base)
    while ($queue.Count -gt 0 -and $results.Count -lt $limit -and $visited -lt 6000) {
        $dir = $queue.Dequeue()
        try { $children = Get-ChildItem -LiteralPath $dir -Force -ErrorAction Stop } catch { continue }
        foreach ($child in $children) {
            $visited++
            if ($child.Name.IndexOf($query, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $results.Add((Get-ShadowFileEntry $child.FullName))
                if ($results.Count -ge $limit) { break }
            }
            if ($child.PSIsContainer -and -not ($child.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                $queue.Enqueue($child.FullName)
            }
            if ($visited -ge 6000) { $truncated = $true; break }
        }
    }
    return [ordered]@{
        ok = $true
        path = $base
        query = $query
        truncated = $truncated
        results = $results.ToArray()
    }
}

function Write-ShadowFile {
    param($Arguments)
    $path = Resolve-ShadowPath ([string](Get-ShadowArg $Arguments "path" "")) -AllowMissing
    $text = [string](Get-ShadowArg $Arguments "text" "")
    $bytes = [Text.Encoding]::UTF8.GetByteCount($text)
    if ($bytes -gt 1000000) { throw "File write is limited to 1 MB." }
    $parent = Split-Path -Parent $path
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($path, $text, $utf8)
    return [ordered]@{ ok = $true; path = $path; bytes = $bytes }
}

# Workspace actions use the same contract as companion/workspace_agent.py.
# The server applies the Ask/Auto/Full policy first; this agent independently
# intersects the requested workspace with SHADOW_ALLOWED_ROOTS before touching disk.
function Get-ShadowWorkspaceRoots {
    param($Arguments)
    $requested = @(Get-ShadowArg $Arguments "roots" @())
    if ($requested.Count -eq 0) { throw "Workspace jobs must name their authorized roots." }
    $localRoots = @(Get-ShadowAllowedRoots)
    $roots = New-Object Collections.Generic.List[string]
    foreach ($raw in $requested) {
        if (-not ([string]$raw).Trim()) { continue }
        $candidate = Normalize-ShadowPath ([Environment]::ExpandEnvironmentVariables(([string]$raw).Trim()))
        $allowed = $false
        foreach ($local in $localRoots) {
            if ($candidate.Equals($local, [StringComparison]::OrdinalIgnoreCase) -or
                $candidate.StartsWith($local + "\", [StringComparison]::OrdinalIgnoreCase)) {
                $allowed = $true
                break
            }
        }
        if (-not $allowed) { throw "Workspace root is outside this device's SHADOW_ALLOWED_ROOTS." }
        if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
            throw "Workspace root does not exist: $candidate"
        }
        $roots.Add($candidate)
    }
    if ($roots.Count -eq 0) { throw "No valid workspace root was supplied." }
    return $roots.ToArray()
}

function Resolve-ShadowWorkspacePath {
    param($Arguments, [string]$Value = "", [switch]$AllowMissing)
    $roots = @(Get-ShadowWorkspaceRoots $Arguments)
    if (-not $Value) { $Value = $roots[0] }
    $expanded = [Environment]::ExpandEnvironmentVariables($Value)
    if (-not [IO.Path]::IsPathRooted($expanded)) { $expanded = Join-Path $roots[0] $expanded }
    $full = Normalize-ShadowPath $expanded
    $allowed = $false
    foreach ($root in $roots) {
        if ($full.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
            $full.StartsWith($root + "\", [StringComparison]::OrdinalIgnoreCase)) {
            $allowed = $true
            break
        }
    }
    if (-not $allowed) { throw "Path is outside the authorized workspace." }
    if (-not $AllowMissing -and -not (Test-Path -LiteralPath $full)) {
        throw "Path does not exist: $Value"
    }
    return $full
}

function Get-ShadowWorkspaceRelative {
    param($Arguments, [Parameter(Mandatory = $true)][string]$Path)
    $full = Normalize-ShadowPath $Path
    foreach ($root in @(Get-ShadowWorkspaceRoots $Arguments)) {
        if ($full.Equals($root, [StringComparison]::OrdinalIgnoreCase)) { return "." }
        if ($full.StartsWith($root + "\", [StringComparison]::OrdinalIgnoreCase)) {
            return $full.Substring($root.Length + 1).Replace("\", "/")
        }
    }
    return $full
}

function Get-ShadowSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-ShadowWorkspaceEntry {
    param($Arguments, [Parameter(Mandatory = $true)][string]$Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    $stamp = New-Object DateTimeOffset($item.LastWriteTimeUtc)
    $row = [ordered]@{
        name = $item.Name
        path = Get-ShadowWorkspaceRelative $Arguments $item.FullName
        type = $(if ($item.PSIsContainer) { "dir" } else { "file" })
        size = $(if ($item.PSIsContainer) { 0 } else { [int64]$item.Length })
        modified = $stamp.ToUnixTimeSeconds()
    }
    if (-not $item.PSIsContainer) { $row["sha256"] = Get-ShadowSha256 $item.FullName }
    return $row
}

function Get-ShadowWorkspaceTreeChildren {
    param($Arguments, [string]$Directory, [int]$Level, [int]$Depth, [ref]$Count, [int]$Limit)
    $rows = New-Object Collections.Generic.List[object]
    $skip = @(".git", "node_modules", "__pycache__", ".venv", "venv", ".shadow-trash", ".shadow-checkpoints", "dist", "build", ".next")
    $children = @(Get-ChildItem -LiteralPath $Directory -Force -ErrorAction SilentlyContinue |
        Sort-Object @{ Expression = { -not $_.PSIsContainer } }, Name)
    foreach ($child in $children) {
        if ($Count.Value -ge $Limit) { break }
        if ($skip -contains $child.Name) { continue }
        $Count.Value++
        $row = Get-ShadowWorkspaceEntry $Arguments $child.FullName
        if ($child.PSIsContainer -and $Level -lt $Depth -and
            -not ($child.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            $row["children"] = @(Get-ShadowWorkspaceTreeChildren $Arguments $child.FullName ($Level + 1) $Depth $Count $Limit)
        }
        $rows.Add($row)
    }
    return $rows.ToArray()
}

function Get-ShadowWorkspaceTree {
    param($Arguments)
    $base = Resolve-ShadowWorkspacePath $Arguments ([string](Get-ShadowArg $Arguments "path" ""))
    if (-not (Test-Path -LiteralPath $base -PathType Container)) { throw "Tree path is not a directory." }
    $depth = [math]::Max(1, [math]::Min([int](Get-ShadowArg $Arguments "depth" 2), 6))
    $limit = [math]::Max(1, [math]::Min([int](Get-ShadowArg $Arguments "limit" 500), 2000))
    $count = 0
    $entries = @(Get-ShadowWorkspaceTreeChildren $Arguments $base 1 $depth ([ref]$count) $limit)
    return [ordered]@{
        ok = $true
        path = Get-ShadowWorkspaceRelative $Arguments $base
        entries = $entries
        truncated = $count -ge $limit
    }
}

function Get-ShadowWorkspaceStat {
    param($Arguments)
    $path = Resolve-ShadowWorkspacePath $Arguments ([string](Get-ShadowArg $Arguments "path" ""))
    $row = Get-ShadowWorkspaceEntry $Arguments $path
    $row["ok"] = $true
    return $row
}

function Read-ShadowWorkspaceFile {
    param($Arguments)
    $path = Resolve-ShadowWorkspacePath $Arguments ([string](Get-ShadowArg $Arguments "path" ""))
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Path is not a file." }
    $maxBytes = [math]::Max(1000, [math]::Min([int](Get-ShadowArg $Arguments "max_bytes" 1000000), 1000000))
    $all = [IO.File]::ReadAllBytes($path)
    $length = [math]::Min($all.Length, $maxBytes)
    $bytes = New-Object byte[] $length
    [Array]::Copy($all, $bytes, $length)
    $binary = [Array]::IndexOf($bytes, [byte]0) -ge 0
    return [ordered]@{
        ok = $true
        path = Get-ShadowWorkspaceRelative $Arguments $path
        size = [int64]$all.Length
        sha256 = Get-ShadowSha256 $path
        binary = $binary
        truncated = $all.Length -gt $length
        text = $(if ($binary) { "" } else { [Text.Encoding]::UTF8.GetString($bytes) })
    }
}

function Search-ShadowWorkspace {
    param($Arguments)
    $base = Resolve-ShadowWorkspacePath $Arguments ([string](Get-ShadowArg $Arguments "path" ""))
    if (-not (Test-Path -LiteralPath $base -PathType Container)) { throw "Search path is not a directory." }
    $query = ([string](Get-ShadowArg $Arguments "query" "")).Trim()
    if (-not $query) { throw "query is required." }
    $mode = ([string](Get-ShadowArg $Arguments "mode" "content")).ToLowerInvariant()
    $limit = [math]::Max(1, [math]::Min([int](Get-ShadowArg $Arguments "limit" 60), 200))
    $results = New-Object Collections.Generic.List[object]
    $visited = 0
    $skipPattern = "\\(\.git|node_modules|__pycache__|\.venv|venv|\.shadow-trash|\.shadow-checkpoints|dist|build|\.next)\\"
    foreach ($item in @(Get-ChildItem -LiteralPath $base -Recurse -File -Force -ErrorAction SilentlyContinue)) {
        if ($item.FullName -match $skipPattern) { continue }
        $visited++
        if ($visited -gt 20000 -or $results.Count -ge $limit) { break }
        if ($mode -eq "name") {
            if ($item.Name.IndexOf($query, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $results.Add((Get-ShadowWorkspaceEntry $Arguments $item.FullName))
            }
            continue
        }
        if ($item.Length -gt 2000000) { continue }
        try {
            $content = [IO.File]::ReadAllText($item.FullName, [Text.Encoding]::UTF8)
            $lineNumber = 0
            foreach ($line in ($content -split "`r?`n")) {
                $lineNumber++
                if ($line.IndexOf($query, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                    $results.Add([ordered]@{
                        path = Get-ShadowWorkspaceRelative $Arguments $item.FullName
                        line = $lineNumber
                        text = $line.Trim().Substring(0, [math]::Min(300, $line.Trim().Length))
                    })
                    if ($results.Count -ge $limit) { break }
                }
            }
        } catch {}
    }
    return [ordered]@{
        ok = $true
        query = $query
        mode = $mode
        truncated = $visited -gt 20000 -or $results.Count -ge $limit
        results = $results.ToArray()
    }
}

function Get-ShadowWorkspaceHashes {
    param($Arguments)
    $files = New-Object Collections.Generic.List[object]
    foreach ($raw in @((Get-ShadowArg $Arguments "paths" @())) | Select-Object -First 200) {
        try {
            $path = Resolve-ShadowWorkspacePath $Arguments ([string]$raw)
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                $item = Get-Item -LiteralPath $path
                $files.Add([ordered]@{
                    path = Get-ShadowWorkspaceRelative $Arguments $path
                    sha256 = Get-ShadowSha256 $path
                    modified = (New-Object DateTimeOffset($item.LastWriteTimeUtc)).ToUnixTimeSeconds()
                })
            } else {
                $files.Add([ordered]@{ path = [string]$raw; error = "not a file" })
            }
        } catch {
            $files.Add([ordered]@{ path = [string]$raw; error = $_.Exception.Message })
        }
    }
    return [ordered]@{ ok = $true; files = $files.ToArray() }
}

function Get-ShadowWorkspaceDiff {
    param($Arguments)
    $path = Resolve-ShadowWorkspacePath $Arguments ([string](Get-ShadowArg $Arguments "path" "")) -AllowMissing
    $newText = [string](Get-ShadowArg $Arguments "text" "")
    $oldText = $(if (Test-Path -LiteralPath $path -PathType Leaf) { [IO.File]::ReadAllText($path) } else { "" })
    if ($oldText -eq $newText) { $diff = "" }
    else {
        $relative = Get-ShadowWorkspaceRelative $Arguments $path
        $diff = "--- a/$relative`n+++ b/$relative`n@@ content @@`n-" + $oldText + "`n+" + $newText
        if ($diff.Length -gt 200000) { $diff = $diff.Substring(0, 200000) }
    }
    return [ordered]@{ ok = $true; path = Get-ShadowWorkspaceRelative $Arguments $path; diff = $diff; exists = (Test-Path -LiteralPath $path) }
}

function Get-ShadowWorkspaceCheckpointRoot {
    param($Arguments)
    $root = @(Get-ShadowWorkspaceRoots $Arguments)[0]
    return Join-Path $root ".shadow-checkpoints"
}

function Save-ShadowWorkspacePreimage {
    param($Arguments, [Parameter(Mandatory = $true)][string]$Path)
    $checkpointId = ([string](Get-ShadowArg $Arguments "checkpoint_id" "")).Trim()
    if (-not $checkpointId -or $checkpointId -notmatch "^[A-Za-z0-9_-]+$") { return }
    $relative = Get-ShadowWorkspaceRelative $Arguments $Path
    $base = Join-Path (Get-ShadowWorkspaceCheckpointRoot $Arguments) $checkpointId
    $snapshot = Join-Path (Join-Path $base "files") $relative
    $marker = Join-Path (Join-Path $base "created") $relative
    if (Test-Path -LiteralPath $snapshot) { return }
    if (Test-Path -LiteralPath $marker) { return }
    if (Test-Path -LiteralPath $Path) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $snapshot) | Out-Null
        Copy-Item -LiteralPath $Path -Destination $snapshot -Recurse -Force
    } else {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $marker) | Out-Null
        [IO.File]::WriteAllText($marker, "created", (New-Object Text.UTF8Encoding($false)))
    }
}

function Write-ShadowWorkspaceFile {
    param($Arguments)
    $path = Resolve-ShadowWorkspacePath $Arguments ([string](Get-ShadowArg $Arguments "path" "")) -AllowMissing
    $text = [string](Get-ShadowArg $Arguments "text" "")
    if ([Text.Encoding]::UTF8.GetByteCount($text) -gt 2000000) { throw "Write is limited to 2 MB." }
    $expected = ([string](Get-ShadowArg $Arguments "expect_sha256" "")).Trim()
    if ($expected -and (Test-Path -LiteralPath $path -PathType Leaf) -and (Get-ShadowSha256 $path) -ne $expected.ToLowerInvariant()) {
        throw "Conflict: file changed on disk since it was read."
    }
    Save-ShadowWorkspacePreimage $Arguments $path
    $parent = Split-Path -Parent $path
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($path, $text, $utf8)
    return [ordered]@{
        ok = $true
        path = Get-ShadowWorkspaceRelative $Arguments $path
        sha256 = Get-ShadowSha256 $path
        bytes = [Text.Encoding]::UTF8.GetByteCount($text)
    }
}

function New-ShadowWorkspaceDirectory {
    param($Arguments)
    $path = Resolve-ShadowWorkspacePath $Arguments ([string](Get-ShadowArg $Arguments "path" "")) -AllowMissing
    Save-ShadowWorkspacePreimage $Arguments $path
    New-Item -ItemType Directory -Force -Path $path | Out-Null
    return [ordered]@{ ok = $true; path = Get-ShadowWorkspaceRelative $Arguments $path }
}

function Rename-ShadowWorkspacePath {
    param($Arguments)
    $source = Resolve-ShadowWorkspacePath $Arguments ([string](Get-ShadowArg $Arguments "path" ""))
    $destination = Resolve-ShadowWorkspacePath $Arguments ([string](Get-ShadowArg $Arguments "to" "")) -AllowMissing
    if (Test-Path -LiteralPath $destination) { throw "Destination already exists." }
    Save-ShadowWorkspacePreimage $Arguments $source
    Save-ShadowWorkspacePreimage $Arguments $destination
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
    Move-Item -LiteralPath $source -Destination $destination
    return [ordered]@{
        ok = $true
        path = Get-ShadowWorkspaceRelative $Arguments $source
        to = Get-ShadowWorkspaceRelative $Arguments $destination
    }
}

function Remove-ShadowWorkspacePath {
    param($Arguments)
    $path = Resolve-ShadowWorkspacePath $Arguments ([string](Get-ShadowArg $Arguments "path" ""))
    $relative = Get-ShadowWorkspaceRelative $Arguments $path
    if ($relative -eq ".") { throw "Refusing to delete the workspace root." }
    Save-ShadowWorkspacePreimage $Arguments $path
    $root = @(Get-ShadowWorkspaceRoots $Arguments)[0]
    $trash = Join-Path (Join-Path $root ".shadow-trash") ((Get-Date -UFormat %s) + "-" + [IO.Path]::GetFileName($path))
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $trash) | Out-Null
    Move-Item -LiteralPath $path -Destination $trash
    return [ordered]@{ ok = $true; path = $relative; trash = Get-ShadowWorkspaceRelative $Arguments $trash }
}

function Patch-ShadowWorkspaceFiles {
    param($Arguments)
    $edits = @(Get-ShadowArg $Arguments "edits" @())
    if ($edits.Count -lt 1 -or $edits.Count -gt 50) { throw "ws_patch needs 1-50 edits." }
    $plan = New-Object Collections.Generic.List[object]
    foreach ($edit in $edits) {
        $path = Resolve-ShadowWorkspacePath $Arguments ([string](Get-ShadowArg $edit "path" "")) -AllowMissing
        if ($null -ne $edit.PSObject.Properties["old"]) {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Cannot patch missing file." }
            $current = [IO.File]::ReadAllText($path)
            $old = [string](Get-ShadowArg $edit "old" "")
            $new = [string](Get-ShadowArg $edit "new" "")
            if (-not $old) { throw "Edit old-text must not be empty." }
            $first = $current.IndexOf($old, [StringComparison]::Ordinal)
            if ($first -lt 0) { throw "Patch text not found in $(Get-ShadowWorkspaceRelative $Arguments $path)." }
            $second = $current.IndexOf($old, $first + $old.Length, [StringComparison]::Ordinal)
            $replaceAll = [bool](Get-ShadowArg $edit "replace_all" $false)
            if ($second -ge 0 -and -not $replaceAll) { throw "Patch text is ambiguous." }
            if ($replaceAll) { $updated = $current.Replace($old, $new) }
            else { $updated = $current.Substring(0, $first) + $new + $current.Substring($first + $old.Length) }
        } else {
            $updated = [string](Get-ShadowArg $edit "text" "")
        }
        if ([Text.Encoding]::UTF8.GetByteCount($updated) -gt 2000000) { throw "Patched file exceeds 2 MB." }
        $plan.Add([pscustomobject]@{ Path = $path; Text = $updated })
    }
    $changed = New-Object Collections.Generic.List[object]
    $utf8 = New-Object Text.UTF8Encoding($false)
    foreach ($entry in $plan) {
        Save-ShadowWorkspacePreimage $Arguments $entry.Path
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $entry.Path) | Out-Null
        [IO.File]::WriteAllText($entry.Path, $entry.Text, $utf8)
        $changed.Add([ordered]@{
            path = Get-ShadowWorkspaceRelative $Arguments $entry.Path
            sha256 = Get-ShadowSha256 $entry.Path
        })
    }
    return [ordered]@{ ok = $true; changed = $changed.ToArray() }
}

function Invoke-ShadowWorkspaceShell {
    param($Arguments)
    $cwd = Resolve-ShadowWorkspacePath $Arguments ([string](Get-ShadowArg $Arguments "cwd" ""))
    $shellArgs = [pscustomobject]@{
        command = [string](Get-ShadowArg $Arguments "command" "")
        cwd = $cwd
        timeout = [int](Get-ShadowArg $Arguments "timeout" 120)
    }
    $result = Invoke-ShadowShell $shellArgs
    $result["cwd"] = Get-ShadowWorkspaceRelative $Arguments $cwd
    return $result
}

function Invoke-ShadowWorkspaceGit {
    param($Arguments, [string[]]$GitArgs)
    $root = @(Get-ShadowWorkspaceRoots $Arguments)[0]
    $output = @(& git.exe -C $root @GitArgs 2>&1)
    $code = $(if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE })
    return [ordered]@{ returncode = $code; output = ($output | Out-String).Trim() }
}

function Get-ShadowWorkspaceGitInfo {
    param($Arguments)
    try { $inside = Invoke-ShadowWorkspaceGit $Arguments @("rev-parse", "--is-inside-work-tree") }
    catch { return [ordered]@{ ok = $true; is_repo = $false } }
    if ($inside.returncode -ne 0 -or $inside.output -ne "true") { return [ordered]@{ ok = $true; is_repo = $false } }
    $branch = Invoke-ShadowWorkspaceGit $Arguments @("rev-parse", "--abbrev-ref", "HEAD")
    $head = Invoke-ShadowWorkspaceGit $Arguments @("rev-parse", "HEAD")
    $status = Invoke-ShadowWorkspaceGit $Arguments @("status", "--porcelain")
    return [ordered]@{
        ok = $true
        is_repo = $true
        branch = $branch.output
        head = $head.output
        dirty = [bool]$status.output
        status = @($status.output -split "`r?`n" | Where-Object { $_ })
    }
}

function Invoke-ShadowWorkspaceGitAction {
    param([string]$Action, $Arguments)
    switch ($Action) {
        "git_info" { return Get-ShadowWorkspaceGitInfo $Arguments }
        "git_diff" {
            $gitArgs = @("diff", "--no-ext-diff")
            $target = ([string](Get-ShadowArg $Arguments "target" "")).Trim()
            if ($target) { $gitArgs += $target }
            $path = ([string](Get-ShadowArg $Arguments "path" "")).Trim()
            if ($path) { $gitArgs += @("--", $path) }
            $result = Invoke-ShadowWorkspaceGit $Arguments $gitArgs
            return [ordered]@{ ok = $result.returncode -eq 0; diff = $result.output; returncode = $result.returncode }
        }
        "git_log" {
            $limit = [math]::Max(1, [math]::Min([int](Get-ShadowArg $Arguments "limit" 20), 100))
            $result = Invoke-ShadowWorkspaceGit $Arguments @("log", "--oneline", "-n", [string]$limit)
            return [ordered]@{ ok = $result.returncode -eq 0; log = $result.output; returncode = $result.returncode }
        }
        "git_commit" {
            $message = ([string](Get-ShadowArg $Arguments "message" "Shadow mission changes")).Trim()
            $add = Invoke-ShadowWorkspaceGit $Arguments @("add", "--", ".")
            if ($add.returncode -ne 0) { throw $add.output }
            $result = Invoke-ShadowWorkspaceGit $Arguments @("-c", "user.name=Shadow Mission", "-c", "user.email=mission@shadow.local", "commit", "-m", $message)
            if ($result.returncode -ne 0) { throw $result.output }
            $head = Invoke-ShadowWorkspaceGit $Arguments @("rev-parse", "HEAD")
            return [ordered]@{ ok = $true; head = $head.output; message = $message }
        }
        "git_checkout" {
            $branch = ([string](Get-ShadowArg $Arguments "branch" "")).Trim()
            if (-not $branch -or $branch -notmatch "^[A-Za-z0-9._/-]+$") { throw "Invalid branch name." }
            $gitArgs = @("checkout")
            if ([bool](Get-ShadowArg $Arguments "create" $false)) { $gitArgs += "-b" }
            $gitArgs += $branch
            $result = Invoke-ShadowWorkspaceGit $Arguments $gitArgs
            if ($result.returncode -ne 0) { throw $result.output }
            return [ordered]@{ ok = $true; branch = $branch }
        }
    }
    throw "Unsupported git action: $Action"
}

function New-ShadowWorkspaceCheckpoint {
    param($Arguments)
    $checkpointId = ([string](Get-ShadowArg $Arguments "checkpoint_id" "")).Trim()
    if (-not $checkpointId -or $checkpointId -notmatch "^[A-Za-z0-9_-]+$") { throw "A valid checkpoint_id is required." }
    $base = Join-Path (Get-ShadowWorkspaceCheckpointRoot $Arguments) $checkpointId
    New-Item -ItemType Directory -Force -Path $base | Out-Null
    $git = Get-ShadowWorkspaceGitInfo $Arguments
    $meta = [ordered]@{ created_at = [DateTime]::UtcNow.ToString("o"); git = $(if ($git.is_repo) { $git } else { $null }) }
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText((Join-Path $base "meta.json"), (ConvertTo-ShadowJson $meta), $utf8)
    return [ordered]@{
        ok = $true
        checkpoint_id = $checkpointId
        git = $meta.git
        uncommitted_user_work = [bool]($git.is_repo -and $git.dirty)
    }
}

function Restore-ShadowWorkspaceCheckpoint {
    param($Arguments)
    $checkpointId = ([string](Get-ShadowArg $Arguments "checkpoint_id" "")).Trim()
    if (-not $checkpointId -or $checkpointId -notmatch "^[A-Za-z0-9_-]+$") { throw "A valid checkpoint_id is required." }
    $base = Join-Path (Get-ShadowWorkspaceCheckpointRoot $Arguments) $checkpointId
    if (-not (Test-Path -LiteralPath $base -PathType Container)) { throw "Checkpoint not found on this device." }
    $root = @(Get-ShadowWorkspaceRoots $Arguments)[0]
    $only = @{}
    foreach ($item in @(Get-ShadowArg $Arguments "paths" @())) { if ([string]$item) { $only[([string]$item).Replace("\", "/")] = $true } }
    $restored = New-Object Collections.Generic.List[string]
    $removed = New-Object Collections.Generic.List[string]
    $filesRoot = Join-Path $base "files"
    if (Test-Path -LiteralPath $filesRoot -PathType Container) {
        foreach ($snapshot in @(Get-ChildItem -LiteralPath $filesRoot -Recurse -File -Force -ErrorAction SilentlyContinue)) {
            $relative = $snapshot.FullName.Substring($filesRoot.Length + 1).Replace("\", "/")
            if ($only.Count -gt 0 -and -not $only.ContainsKey($relative)) { continue }
            $target = Resolve-ShadowWorkspacePath $Arguments $relative -AllowMissing
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
            Copy-Item -LiteralPath $snapshot.FullName -Destination $target -Force
            $restored.Add($relative)
        }
    }
    $createdRoot = Join-Path $base "created"
    if (Test-Path -LiteralPath $createdRoot -PathType Container) {
        foreach ($marker in @(Get-ChildItem -LiteralPath $createdRoot -Recurse -File -Force -ErrorAction SilentlyContinue)) {
            $relative = $marker.FullName.Substring($createdRoot.Length + 1).Replace("\", "/")
            if ($only.Count -gt 0 -and -not $only.ContainsKey($relative)) { continue }
            $target = Resolve-ShadowWorkspacePath $Arguments $relative -AllowMissing
            if (Test-Path -LiteralPath $target) {
                $trash = Join-Path (Join-Path $root ".shadow-trash") ("rollback-" + (Get-Date -UFormat %s) + "\" + $relative)
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $trash) | Out-Null
                Move-Item -LiteralPath $target -Destination $trash -Force
                $removed.Add($relative)
            }
        }
    }
    return [ordered]@{ ok = $true; checkpoint_id = $checkpointId; restored = $restored.ToArray(); removed_created = $removed.ToArray() }
}

function Invoke-ShadowWorkspaceAction {
    param([string]$Action, $Arguments)
    switch ($Action) {
        "ws_tree" { return Get-ShadowWorkspaceTree $Arguments }
        "ws_stat" { return Get-ShadowWorkspaceStat $Arguments }
        "ws_read" { return Read-ShadowWorkspaceFile $Arguments }
        "ws_search" { return Search-ShadowWorkspace $Arguments }
        "ws_hash" { return Get-ShadowWorkspaceHashes $Arguments }
        "ws_diff" { return Get-ShadowWorkspaceDiff $Arguments }
        "ws_write" { return Write-ShadowWorkspaceFile $Arguments }
        "ws_mkdir" { return New-ShadowWorkspaceDirectory $Arguments }
        "ws_rename" { return Rename-ShadowWorkspacePath $Arguments }
        "ws_delete" { return Remove-ShadowWorkspacePath $Arguments }
        "ws_patch" { return Patch-ShadowWorkspaceFiles $Arguments }
        "ws_run" { return Invoke-ShadowWorkspaceShell $Arguments }
        "ws_checkpoint" { return New-ShadowWorkspaceCheckpoint $Arguments }
        "ws_restore" { return Restore-ShadowWorkspaceCheckpoint $Arguments }
        "git_info" { return Invoke-ShadowWorkspaceGitAction $Action $Arguments }
        "git_diff" { return Invoke-ShadowWorkspaceGitAction $Action $Arguments }
        "git_log" { return Invoke-ShadowWorkspaceGitAction $Action $Arguments }
        "git_commit" { return Invoke-ShadowWorkspaceGitAction $Action $Arguments }
        "git_checkout" { return Invoke-ShadowWorkspaceGitAction $Action $Arguments }
    }
    throw "Unsupported workspace action: $Action"
}

function Invoke-ShadowShell {
    param($Arguments)
    $command = ([string](Get-ShadowArg $Arguments "command" "")).Trim()
    if (-not $command) { throw "command is required." }
    if ($command.Length -gt 4000) { throw "command is too long." }
    $cwd = Resolve-ShadowPath ([string](Get-ShadowArg $Arguments "cwd" ""))
    if (-not (Test-Path -LiteralPath $cwd -PathType Container)) { throw "Shell cwd is not a directory." }
    $timeout = [math]::Max(1, [math]::Min([int](Get-ShadowArg $Arguments "timeout" 20), 600))
    $job = Start-Job -ScriptBlock {
        param($Command, $WorkingDirectory)
        Set-Location -LiteralPath $WorkingDirectory
        $lines = @(& powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command $Command 2>&1)
        [pscustomobject]@{
            output = ($lines | Out-String)
            returncode = $(if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE })
        }
    } -ArgumentList $command, $cwd
    try {
        $completed = Wait-Job -Job $job -Timeout $timeout
        if (-not $completed) {
            Stop-Job -Job $job -ErrorAction SilentlyContinue
            throw "Command timed out after $timeout seconds."
        }
        $received = @(Receive-Job -Job $job)
        $result = $received | Select-Object -Last 1
        $output = [string]$result.output
        if ($output.Length -gt 12000) { $output = $output.Substring($output.Length - 12000) }
        return [ordered]@{
            ok = [int]$result.returncode -eq 0
            returncode = [int]$result.returncode
            stdout = $output
            stderr = ""
            cwd = $cwd
        }
    } finally {
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
    }
}

function Ensure-ShadowNative {
    if ("Shadow.Windows.Native" -as [type]) { return }
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
namespace Shadow.Windows {
    public static class Native {
        [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
        [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extraInfo);
        [DllImport("user32.dll")] public static extern void keybd_event(byte key, byte scan, uint flags, UIntPtr extraInfo);
    }
}
"@
}

function Ensure-ShadowAudio {
    if ("Shadow.Windows.Audio" -as [type]) { return }
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
namespace Shadow.Windows {
    enum EDataFlow { eRender, eCapture, eAll }
    enum ERole { eConsole, eMultimedia, eCommunications }
    [Flags] enum CLSCTX : uint { ALL = 23 }
    [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
    class MMDeviceEnumerator {}
    [Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceEnumerator {
        int NotImpl1();
        [PreserveSig] int GetDefaultAudioEndpoint(EDataFlow dataFlow, ERole role, out IMMDevice device);
    }
    [Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDevice {
        [PreserveSig] int Activate(ref Guid iid, CLSCTX context, IntPtr activationParams, [MarshalAs(UnmanagedType.IUnknown)] out object instance);
    }
    [Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioEndpointVolume {
        int RegisterControlChangeNotify(IntPtr notify);
        int UnregisterControlChangeNotify(IntPtr notify);
        int GetChannelCount(out uint count);
        int SetMasterVolumeLevel(float levelDb, Guid eventContext);
        int SetMasterVolumeLevelScalar(float level, Guid eventContext);
    }
    public static class Audio {
        public static void SetVolume(float level) {
            IMMDeviceEnumerator enumerator = (IMMDeviceEnumerator)(new MMDeviceEnumerator());
            IMMDevice device;
            Marshal.ThrowExceptionForHR(enumerator.GetDefaultAudioEndpoint(EDataFlow.eRender, ERole.eMultimedia, out device));
            Guid iid = typeof(IAudioEndpointVolume).GUID;
            object endpointObject;
            Marshal.ThrowExceptionForHR(device.Activate(ref iid, CLSCTX.ALL, IntPtr.Zero, out endpointObject));
            IAudioEndpointVolume endpoint = (IAudioEndpointVolume)endpointObject;
            Marshal.ThrowExceptionForHR(endpoint.SetMasterVolumeLevelScalar(Math.Max(0f, Math.Min(1f, level)), Guid.Empty));
            Marshal.ReleaseComObject(endpoint);
            Marshal.ReleaseComObject(device);
            Marshal.ReleaseComObject(enumerator);
        }
    }
}
"@
}

function ConvertTo-ShadowSendKeys {
    param([string]$Text)
    $builder = New-Object Text.StringBuilder
    foreach ($char in $Text.ToCharArray()) {
        if ("+^%~(){}[]" -contains [string]$char) {
            [void]$builder.Append("{" + $char + "}")
        } elseif ($char -eq "`r") {
            continue
        } elseif ($char -eq "`n") {
            [void]$builder.Append("{ENTER}")
        } elseif ($char -eq "`t") {
            [void]$builder.Append("{TAB}")
        } else {
            [void]$builder.Append($char)
        }
    }
    return $builder.ToString()
}

function Send-ShadowKey {
    param([string]$Combination)
    Add-Type -AssemblyName System.Windows.Forms
    $tokens = @($Combination.ToLowerInvariant() -split "\+| " | Where-Object { $_ })
    if ($tokens.Count -eq 0) { throw "key is required." }
    $modifiers = @{
        ctrl = "^"; control = "^"; alt = "%"; option = "%"; opt = "%"
        shift = "+"; cmd = "^"; command = "^"; super = "^"; win = "^"
    }
    $keys = @{
        return = "{ENTER}"; enter = "{ENTER}"; tab = "{TAB}"; space = " "
        escape = "{ESC}"; esc = "{ESC}"; delete = "{DEL}"; backspace = "{BACKSPACE}"
        left = "{LEFT}"; right = "{RIGHT}"; up = "{UP}"; down = "{DOWN}"
        home = "{HOME}"; end = "{END}"; pageup = "{PGUP}"; pagedown = "{PGDN}"
        f1 = "{F1}"; f2 = "{F2}"; f3 = "{F3}"; f4 = "{F4}"; f5 = "{F5}"; f6 = "{F6}"
        f7 = "{F7}"; f8 = "{F8}"; f9 = "{F9}"; f10 = "{F10}"; f11 = "{F11}"; f12 = "{F12}"
    }
    $prefix = ""
    for ($i = 0; $i -lt $tokens.Count - 1; $i++) {
        if (-not $modifiers.ContainsKey($tokens[$i])) { throw "Unsupported modifier: $($tokens[$i])" }
        $prefix += $modifiers[$tokens[$i]]
    }
    $key = $tokens[-1]
    if ($keys.ContainsKey($key)) {
        $keyToken = $keys[$key]
    } elseif ($key.Length -eq 1 -and $key -match "^[a-z0-9]$") {
        $keyToken = $key
    } else {
        throw "Unsupported key: $key"
    }
    [Windows.Forms.SendKeys]::SendWait($prefix + $keyToken)
}

function Get-ShadowAllowedApps {
    $apps = @{}
    $raw = [Environment]::GetEnvironmentVariable("SHADOW_ALLOWED_APPS", "User")
    foreach ($item in ([string]$raw -split ",")) {
        $parts = $item -split "=", 2
        if ($parts.Count -eq 2 -and $parts[0].Trim() -and $parts[1].Trim()) {
            $apps[$parts[0].Trim().ToLowerInvariant()] = $parts[1].Trim()
        }
    }
    return $apps
}

function Invoke-ShadowAction {
    param(
        [Parameter(Mandatory = $true)][string]$Action,
        $Arguments,
        [bool]$Confirmed = $false
    )
    $actionName = $Action.Trim().ToLowerInvariant()
    if ($script:Capabilities -notcontains $actionName) { throw "Unsupported action: $actionName" }
    if ($script:WriteActions -contains $actionName -and -not $Confirmed) {
        throw "State-changing actions require confirmed=true."
    }
    if ($actionName.StartsWith("ws_") -or $actionName.StartsWith("git_")) {
        return Invoke-ShadowWorkspaceAction $actionName $Arguments
    }
    switch ($actionName) {
        "status" { return Get-ShadowStatus }
        "processes" { return Get-ShadowProcesses $Arguments }
        "windows" { return Get-ShadowWindows }
        "screenshot" { return Get-ShadowScreenshot }
        "file_list" { return Get-ShadowFileList $Arguments }
        "file_read" { return Get-ShadowFile $Arguments }
        "file_search" { return Search-ShadowFiles $Arguments }
        "file_write" { return Write-ShadowFile $Arguments }
        "clipboard_get" {
            return [ordered]@{ ok = $true; text = [string](Get-Clipboard -Raw) }
        }
        "clipboard_set" {
            Set-Clipboard -Value ([string](Get-ShadowArg $Arguments "text" ""))
            return [ordered]@{ ok = $true; provider = "powershell"; resident = $false }
        }
        "kill_process" {
            $pidToKill = [int](Get-ShadowArg $Arguments "pid" 0)
            if ($pidToKill -le 4 -or $pidToKill -eq $PID) { throw "Refusing to kill a protected process." }
            $force = ([string](Get-ShadowArg $Arguments "signal" "TERM")).ToUpperInvariant() -eq "KILL"
            Stop-Process -Id $pidToKill -Force:$force
            return [ordered]@{ ok = $true; pid = $pidToKill }
        }
        "shell" { return Invoke-ShadowShell $Arguments }
        "lock" {
            Start-Process -FilePath "$env:SystemRoot\System32\rundll32.exe" -ArgumentList "user32.dll,LockWorkStation" -WindowStyle Hidden
            return [ordered]@{ ok = $true }
        }
        "sleep" {
            Start-Process -FilePath "$env:SystemRoot\System32\rundll32.exe" -ArgumentList "powrprof.dll,SetSuspendState 0,1,0" -WindowStyle Hidden
            return [ordered]@{ ok = $true }
        }
        "shutdown" {
            Start-Process -FilePath "$env:SystemRoot\System32\shutdown.exe" -ArgumentList "/s /t 0" -WindowStyle Hidden
            return [ordered]@{ ok = $true }
        }
        "type_text" {
            Add-Type -AssemblyName System.Windows.Forms
            [Windows.Forms.SendKeys]::SendWait((ConvertTo-ShadowSendKeys ([string](Get-ShadowArg $Arguments "text" ""))))
            return [ordered]@{ ok = $true }
        }
        "keypress" {
            Send-ShadowKey ([string](Get-ShadowArg $Arguments "key" ""))
            return [ordered]@{ ok = $true }
        }
        "mouse_move" {
            $x = [int](Get-ShadowArg $Arguments "x" -1)
            $y = [int](Get-ShadowArg $Arguments "y" -1)
            if ($x -lt 0 -or $x -gt 10000 -or $y -lt 0 -or $y -gt 10000) { throw "Mouse coordinates are outside safety bounds." }
            Ensure-ShadowNative
            [void][Shadow.Windows.Native]::SetCursorPos($x, $y)
            return [ordered]@{ ok = $true; x = $x; y = $y }
        }
        "mouse_click" {
            $button = [math]::Max(1, [math]::Min([int](Get-ShadowArg $Arguments "button" 1), 3))
            $xValue = Get-ShadowArg $Arguments "x" $null
            $yValue = Get-ShadowArg $Arguments "y" $null
            Ensure-ShadowNative
            if ($null -ne $xValue -and $null -ne $yValue) {
                [void][Shadow.Windows.Native]::SetCursorPos([int]$xValue, [int]$yValue)
            }
            if ($button -eq 1) { $down = 0x0002; $up = 0x0004 }
            elseif ($button -eq 2) { $down = 0x0020; $up = 0x0040 }
            else { $down = 0x0008; $up = 0x0010 }
            [Shadow.Windows.Native]::mouse_event($down, 0, 0, 0, [UIntPtr]::Zero)
            [Shadow.Windows.Native]::mouse_event($up, 0, 0, 0, [UIntPtr]::Zero)
            return [ordered]@{ ok = $true; button = $button; x = $xValue; y = $yValue }
        }
        "media" {
            $command = ([string](Get-ShadowArg $Arguments "command" "")).ToLowerInvariant()
            $keys = @{ play = 0xB3; pause = 0xB3; "play-pause" = 0xB3; next = 0xB0; previous = 0xB1; stop = 0xB2 }
            if (-not $keys.ContainsKey($command)) { throw "Unsupported media command." }
            Ensure-ShadowNative
            [Shadow.Windows.Native]::keybd_event([byte]$keys[$command], 0, 0, [UIntPtr]::Zero)
            [Shadow.Windows.Native]::keybd_event([byte]$keys[$command], 0, 2, [UIntPtr]::Zero)
            return [ordered]@{ ok = $true; command = $command }
        }
        "volume" {
            $value = [math]::Max(0, [math]::Min([int](Get-ShadowArg $Arguments "percent" 50), 100))
            Ensure-ShadowAudio
            [Shadow.Windows.Audio]::SetVolume([single]($value / 100.0))
            return [ordered]@{ ok = $true; percent = $value }
        }
        "app_launch" {
            $app = ([string](Get-ShadowArg $Arguments "app" "")).Trim().ToLowerInvariant()
            $apps = Get-ShadowAllowedApps
            if (-not $apps.ContainsKey($app)) { throw "App '$app' is not in SHADOW_ALLOWED_APPS." }
            Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-Command", $apps[$app])
            return [ordered]@{ ok = $true; app = $app }
        }
        "app_focus" {
            $title = [string](Get-ShadowArg $Arguments "title" "")
            if (-not $title) { throw "title is required." }
            $activated = (New-Object -ComObject WScript.Shell).AppActivate($title)
            return [ordered]@{ ok = [bool]$activated; title = $title }
        }
        "app_close" {
            $windowPid = [int](Get-ShadowArg $Arguments "id" 0)
            if ($windowPid -le 4 -or $windowPid -eq $PID) { throw "Refusing to close a protected process." }
            $process = Get-Process -Id $windowPid -ErrorAction Stop
            if (-not $process.CloseMainWindow()) { Stop-Process -Id $windowPid }
            return [ordered]@{ ok = $true; pid = $windowPid }
        }
    }
    throw "Unsupported action: $actionName"
}

function Start-ShadowRelay {
    param($Config)
    $delay = 2.0
    Write-Host "Shadow device online: $($Config.name) -> $($Config.server)"
    while ($true) {
        try {
            $poll = Get-ShadowMetadata ([string]$Config.name)
            $poll["timeout"] = 25
            $response = Invoke-ShadowApi $Config.server "/api/shadow/device/poll" $poll $Config.token 35
            if ($null -eq $response.job) {
                $delay = 2.0
                continue
            }
            $job = $response.job
            try {
                $confirmed = $false
                if ($null -ne $job.PSObject.Properties["confirmed"]) { $confirmed = $job.confirmed -eq $true }
                $result = Invoke-ShadowAction ([string]$job.action) $job.args $confirmed
                $completion = [ordered]@{ job_id = [string]$job.id; result = $result }
            } catch {
                $message = [string]$_.Exception.Message
                if ($message.Length -gt 1000) { $message = $message.Substring(0, 1000) }
                $completion = [ordered]@{ job_id = [string]$job.id; error = $message }
            }
            [void](Invoke-ShadowApi $Config.server "/api/shadow/device/result" $completion $Config.token 20)
            $delay = 2.0
        } catch {
            Write-Warning "[shadow-device] $($_.Exception.Message); retrying in $([math]::Round($delay))s"
            Start-Sleep -Seconds ([int][math]::Round($delay))
            $delay = [math]::Min($delay * 1.7, 30.0)
        }
    }
}

try {
    if ($Enroll) {
        if (-not $Server) { throw "-Server is required with -Enroll." }
        $saved = Save-ShadowEnrollment $Server $Enroll $Name
        Write-Host "Enrolled $($saved.name) ($($saved.device_id))"
        if ($Once) { exit 0 }
    }
    $config = Read-ShadowConfig
    Start-ShadowRelay $config
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
