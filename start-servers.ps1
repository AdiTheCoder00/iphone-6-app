# Starts the Companion backend and frontend. Runs at logon via the
# "Companion Servers" scheduled task. Each Start-Process is detached, so the
# servers keep running after this script exits. If a server is already bound
# to its port, the new process fails silently and exits — safe to re-run.
$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

Start-Process -FilePath "$root\backend\.venv\Scripts\python.exe" `
  -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8000' `
  -WorkingDirectory "$root\backend" -WindowStyle Hidden

Start-Process -FilePath 'python' `
  -ArgumentList '-m', 'http.server', '8080', '--bind', '0.0.0.0' `
  -WorkingDirectory $root -WindowStyle Hidden