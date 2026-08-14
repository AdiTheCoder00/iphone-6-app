# Starts the Companion backend and frontend. Runs at logon via the
# "Companion Servers" scheduled task. Each Start-Process is detached, so the
# servers keep running after this script exits. If a server is already bound
# to its port, the new process fails silently and exits — safe to re-run.
#
# The frontend is served through serve_frontend.py, which only serves the
# PWA's own files (never certs/ or backend/). HTTPS on 8443 is used when the
# local cert chain exists — that is what unlocks tap-to-talk and the service
# worker on iOS — and plain HTTP on 8080 otherwise. Logs are written next to
# this script so a crash is never silent.
$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$backendOut = Join-Path $root 'backend\uvicorn.log'
$backendErr = Join-Path $root 'backend\uvicorn.err.log'
$frontOut   = Join-Path $root 'frontend.log'
$frontErr   = Join-Path $root 'frontend.err.log'

Start-Process -FilePath "$root\backend\.venv\Scripts\python.exe" `
  -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8000' `
  -WorkingDirectory "$root\backend" -WindowStyle Hidden `
  -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr

$cert = Join-Path $root 'certs\companion-server.crt'
if (Test-Path $cert) {
  $frontArgs = @('serve_frontend.py', '--port', '8443', '--cert-dir', 'certs')
} else {
  $frontArgs = @('serve_frontend.py', '--port', '8080')
}
Start-Process -FilePath 'python' `
  -ArgumentList $frontArgs `
  -WorkingDirectory $root -WindowStyle Hidden `
  -RedirectStandardOutput $frontOut -RedirectStandardError $frontErr