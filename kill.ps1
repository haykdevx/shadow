# Shadow - complete uninstaller (Windows).
#
#   ./kill.ps1                       # asks for confirmation
#   ./kill.ps1 -Force                # no prompt
#   ./kill.ps1 -Force -PurgeRepo     # also delete the repo directory itself
#
# Removes everything Shadow put on this machine - Docker containers/volumes/
# networks/locally-built images, the launcher venv, webview storage, local app
# data, and (optionally) the repo folder. Scoped to Shadow only.
param([switch]$Force, [switch]$PurgeRepo, [switch]$RmiAll)
$ErrorActionPreference = "SilentlyContinue"

$Here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$Compose = Join-Path $Here "docker-compose.yml"
$Rmi     = if ($RmiAll) { "all" } else { "local" }

Write-Host "This PERMANENTLY removes Shadow from this machine:"
Write-Host "  - Docker containers, volumes, networks, images (--rmi $Rmi) for this project"
Write-Host "  - launcher venv, webview storage (~\.shadow-desktop), first-run marker"
Write-Host "  - local app data + secrets: $Here\{data,logs,.env}"
if ($PurgeRepo) { Write-Host "  - the ENTIRE repo directory: $Here" }
Write-Host ""
if (-not $Force) {
  $ans = Read-Host "Type DELETE to confirm"
  if ($ans -ne "DELETE") { Write-Host "Aborted - nothing was removed."; exit 1 }
}

Write-Host "[kill] stopping & removing Docker stack..."
if ((Get-Command docker -EA SilentlyContinue) -and (Test-Path $Compose)) {
  docker compose -f $Compose --profile remote --profile guacamole down -v --rmi $Rmi --remove-orphans 2>$null
}

Write-Host "[kill] removing launcher env, storage, data & secrets..."
$paths = @(
  (Join-Path $Here "desktop\.venv-desktop"),
  (Join-Path $Here "desktop\.installed"),
  (Join-Path $Here "desktop\dist"),
  (Join-Path $Here "desktop\build"),
  (Join-Path $Here "data"),
  (Join-Path $Here "logs"),
  (Join-Path $Here ".env"),
  (Join-Path $env:USERPROFILE ".shadow-desktop")
)
foreach ($p in $paths) { Remove-Item -Recurse -Force $p -EA SilentlyContinue }

# Start Menu shortcut, if one was created.
Remove-Item -Force (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Shadow.lnk") -EA SilentlyContinue

Write-Host "[kill] Shadow removed."
if ($PurgeRepo) {
  Write-Host "[kill] deleting repo directory: $Here"
  Set-Location $env:TEMP
  Remove-Item -Recurse -Force $Here -EA SilentlyContinue
  Write-Host "[kill] done - no trace left."
} else {
  Write-Host "Repo files remain at: $Here"
  Write-Host "Run './kill.ps1 -Force -PurgeRepo' to delete the directory too."
}
