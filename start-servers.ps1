# Starts the Companion backend and frontend. Runs at logon via the
# "Companion Servers" scheduled task. Each Start-Process is detached, so the
# servers keep running after this script exits. If a server is already bound
# to its port, this script skips it - safe to re-run at any time.
#
# The frontend is served through serve_frontend.py, which only serves the
# PWA's own files (never certs/ or backend/). HTTPS on 8443 is used when the
# local cert chain exists - that is what unlocks tap-to-talk and the service
# worker on iOS - and plain HTTP on 8080 otherwise. The backend follows the
# same rule: HTTPS on 8000 when the cert exists (iOS blocks fetch/EventSource
# to a plain-HTTP backend from an HTTPS page as mixed content), plain HTTP
# otherwise. With certs present, the frontend also runs a plain-HTTP side
# listener on 8081 that serves only the public CA certificate, so a phone can
# install it before it trusts the HTTPS servers.
#
# Cert state can change between runs (certs generated or removed). Instances
# started on the previous scheme/port would keep serving a stale origin, so
# this script records what it started in .frontend-port and .backend-scheme
# and stops the stale instance before starting the new one. Instances it did
# not start (no marker, or a process that is not python) are left alone.
$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$backendOut = Join-Path $root 'backend\uvicorn.log'
$backendErr = Join-Path $root 'backend\uvicorn.err.log'
$frontOut   = Join-Path $root 'frontend.log'
$frontErr   = Join-Path $root 'frontend.err.log'

$python = Join-Path $root 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
  Write-Host "ERROR: $python not found - create the backend venv first (see README)." -ForegroundColor Red
  exit 1
}

# Single-instance guard: the scheduled task can double-fire alongside a manual
# run, and two concurrent invocations each pass Test-PortListening before
# either binds, which starts duplicate server pairs (one of each dies on bind,
# but the survivor's origin is a coin flip). An exclusively-held lock file
# makes the loser exit immediately. A crashed run leaves the file behind, but
# the handle dies with the process, so the next run acquires it cleanly.
$lockPath = Join-Path $root '.start-servers.lock'
$lockStream = $null
try {
  $lockStream = [System.IO.File]::Open(
    $lockPath,
    [System.IO.FileMode]::OpenOrCreate,
    [System.IO.FileAccess]::ReadWrite,
    [System.IO.FileShare]::None
  )
} catch {
  Write-Host 'Another start-servers.ps1 is already running - exiting.' -ForegroundColor Yellow
  exit 0
}

# Port pre-checks: the scheduled task can fire again after a crash, and a
# second server would fail to bind anyway - skip cleanly instead.
function Test-PortListening([int]$port) {
  $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  return ($null -ne $conn)
}

# uvicorn/frontend logs append forever; on a desk machine that runs for
# months that is unbounded disk growth. Rotate at start, keeping one .1
# copy. While the server is running the file is open without FILE_SHARE_
# DELETE, so the move silently fails (SilentlyContinue) and the next
# restart catches it - rotation happens at every boot, which bounds each
# session's growth.
function Rotate-Log([string]$path) {
  if (Test-Path $path) {
    if ((Get-Item $path).Length -gt 5MB) {
      Move-Item -Path $path -Destination "$path.1" -Force
    }
  }
}

# Stale-instance kills must only ever touch OUR servers, not some other
# python the user has running on the same port.
function Is-CompanionProcess([int]$procId) {
  $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $procId" -ErrorAction SilentlyContinue
  if (-not $cim) { return $false }
  return (($cim.Name -match 'python') -and ($cim.CommandLine -match 'uvicorn|serve_frontend|frontend_https'))
}

$cert = Join-Path $root 'certs\companion-server.crt'
$hasCert = Test-Path $cert

# --- backend ---
$scheme = 'http'
if ($hasCert) {
  $scheme = 'https'
  $key = Join-Path $root 'certs\companion-server.key'
}

$backendSchemeMarker = Join-Path $root '.backend-scheme'
$previousScheme = $null
if (Test-Path $backendSchemeMarker) {
  $previousScheme = (Get-Content $backendSchemeMarker).Trim()
}

$backendRunning = Test-PortListening 8000
if ($backendRunning -and $previousScheme -and $previousScheme -ne $scheme) {
  # The cert state changed since we last started the backend; the old
  # instance is still serving the wrong scheme and would keep the phone
  # on the mixed-content failure path until a reboot.
  $conn = Get-NetTCPConnection -LocalPort 8000 -State Listen | Select-Object -First 1
  $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
  if ($proc -and (Is-CompanionProcess $proc.Id)) {
    Stop-Process -Id $proc.Id -Force
    $backendRunning = $false
    Write-Host "Stopped stale backend (was $previousScheme, now $scheme)"
  }
}

if (-not $backendRunning) {
  # A single string: PS 5.1's Start-Process -ArgumentList strips embedded
  # quotes from array elements, which would split the cert paths at spaces.
  $backendArgs = '-m uvicorn app.main:app --host 0.0.0.0 --port 8000'
  if ($hasCert) {
    $backendArgs += ' --ssl-keyfile "' + $key + '" --ssl-certfile "' + $cert + '"'
  }
  Rotate-Log $backendOut; Rotate-Log $backendErr
  Start-Process -FilePath $python `
    -ArgumentList $backendArgs `
    -WorkingDirectory "$root\backend" -WindowStyle Hidden `
    -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr
  Set-Content -Path $backendSchemeMarker -Value $scheme
  Write-Host "Started backend on :8000 ($scheme)"
} else {
  Write-Host "Backend already listening on :8000 - skipped"
}

# --- frontend ---
$frontPort = 8443
$frontArgs = 'serve_frontend.py --port 8443 --cert-dir certs'
if ($hasCert) {
  # Plain-HTTP side listener for the CA certificate download (see
  # serve_frontend.py). A phone that has not yet trusted the CA cannot fetch
  # anything over HTTPS, so the public root cert is served over http:8081.
  $frontArgs += ' --ca-port 8081'
} else {
  $frontPort = 8080
  $frontArgs = 'serve_frontend.py --port 8080'
}

$frontPortMarker = Join-Path $root '.frontend-port'
$previousPort = $null
if (Test-Path $frontPortMarker) {
  $previousPort = (Get-Content $frontPortMarker).Trim()
  if ($previousPort -notmatch '^\d+$') { $previousPort = $null }
}

$frontRunning = Test-PortListening $frontPort
if (-not $frontRunning -and $previousPort -and $previousPort -ne $frontPort -and (Test-PortListening $previousPort)) {
  # Same stale-origin problem as the backend: an instance on the old port
  # would keep serving a plain-HTTP app with no service worker or mic.
  $conn = Get-NetTCPConnection -LocalPort $previousPort -State Listen | Select-Object -First 1
  $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
  if ($proc -and (Is-CompanionProcess $proc.Id)) {
    Stop-Process -Id $proc.Id -Force
    Write-Host "Stopped stale frontend on :$previousPort (port changed to :$frontPort)"
    $frontRunning = $false
  }
}

# An instance started before --ca-port existed serves the PWA but not the
# certificate download; the phone would stall on "install the profile from a
# page served over the LAN". Restart it if the CA port is missing — but only
# an instance this script started (the port marker records that it did).
# A manually launched serve_frontend.py (no marker) is left alone, matching
# the policy above: this script never kills what it did not start.
if ($frontRunning -and $hasCert -and -not (Test-PortListening 8081) -and $previousPort -eq "$frontPort") {
  $conn = Get-NetTCPConnection -LocalPort $frontPort -State Listen | Select-Object -First 1
  $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
  if ($proc -and (Is-CompanionProcess $proc.Id)) {
    Stop-Process -Id $proc.Id -Force
    $frontRunning = $false
    Write-Host "Stopped frontend without CA download (missing :8081) - restarting"
  }
}

if (-not $frontRunning) {
  Rotate-Log $frontOut; Rotate-Log $frontErr
  Start-Process -FilePath $python `
    -ArgumentList $frontArgs `
    -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput $frontOut -RedirectStandardError $frontErr
  Set-Content -Path $frontPortMarker -Value $frontPort
  if ($hasCert) { Set-Content -Path $caPortMarker -Value '8081' }
  Write-Host "Started frontend on :$frontPort"
} else {
  Write-Host "Frontend already listening on :$frontPort - skipped"
}

# Release the single-instance lock. The handle also dies on process exit, so a
# mid-script failure cannot leave a stale lock behind.
$lockStream.Close()
Remove-Item -Path $lockPath -Force -ErrorAction SilentlyContinue