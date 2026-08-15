# Starts the Companion backend and frontend. Runs at logon via the
# "Companion Servers" scheduled task. Each Start-Process is detached, so the
# servers keep running after this script exits. If a server is already bound
# to its port, this script skips it - safe to re-run at any time.
#
# The frontend is served through serve_frontend.py, which only serves the
# PWA's own files (never certs/ or backend/). HTTPS on 8443 is used when the
# local cert chain exists - that is what unlocks tap-to-talk and the service
# worker on iOS - and plain HTTP on 8080 otherwise. Logs are written next to
# this script so a crash is never silent.
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

# Port pre-checks: the scheduled task can fire again after a crash, and a
# second server would fail to bind anyway - skip cleanly instead.
function Test-PortListening([int]$port) {
  $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  return ($null -ne $conn)
}

if (-not (Test-PortListening 8000)) {
  Start-Process -FilePath $python `
    -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8000' `
    -WorkingDirectory "$root\backend" -WindowStyle Hidden `
    -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr
  Write-Host "Started backend on :8000"
} else {
  Write-Host "Backend already listening on :8000 - skipped"
}

$cert = Join-Path $root 'certs\companion-server.crt'
$frontPort = 8443
if (Test-Path $cert) {
  $frontArgs = @('serve_frontend.py', '--port', '8443', '--cert-dir', 'certs')
} else {
  $frontPort = 8080
  $frontArgs = @('serve_frontend.py', '--port', '8080')
}
if (-not (Test-PortListening $frontPort)) {
  Start-Process -FilePath $python `
    -ArgumentList $frontArgs `
    -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput $frontOut -RedirectStandardError $frontErr
  Write-Host "Started frontend on :$frontPort"
} else {
  Write-Host "Frontend already listening on :$frontPort - skipped"
}