# Fix: backend HTTPS start (start-servers.ps1 quoting) + finish verification + commit

## Bug 1 (fixed, code committed pending)
`start-servers.ps1` passed `--ssl-keyfile`/`--ssl-certfile` paths as array
elements with embedded quotes; PS 5.1's Start-Process strips those quotes, so
paths containing spaces (`D:\iphone 6 app\certs\...`) arrived split at uvicorn
(verified empirically with an argv-dump test). Result: `load_cert_chain` ->
`OSError: [Errno 22]` -> backend crash on startup. FIXED: `-ArgumentList` is
now a single string with embedded quotes (proven: argv-dump shows the paths
intact). PS parse OK. Backend now starts and listens on :8000 (https).

## Bug 2 (NEW, needs the one-line fix)
Every TLS request to the running backend dies right after the handshake
(`RemoteProtocolError: Server disconnected without sending a response`; curl
schannel: "failed to receive handshake"; plain HTTP fails as expected on a
TLS-only listener; zero access-log lines; backend process alive and healthy).
Root cause: `app/main.py:20-22` calls `truststore.inject_into_ssl()` (initial
commit e374dad, for huggingface weight downloads). truststore's SSLContext
overrides wrap_socket/wrap_bio/SSLObject.do_handshake to run
`_verify_peercerts(...)` on EVERY handshake — a client-side design. uvicorn
uses `wrap_bio(server_side=True)`, so truststore tries to verify the CLIENT,
which has no cert -> `SSLCertVerificationError: Peer sent no certificates to
verify` -> connection dropped. Isolated reproduction: a truststore-context
test server fails the same way; the plain-context test server works; the
frontend (serve_frontend.py, plain context) works on :8443.
Evidence the global patch is unnecessary: without injection, httpx verifies
https://huggingface.co and https://github.com with certifi (200 OK).
services/tools.py keeps its OWN scoped truststore context for the browser
tool (no server sockets involved) - unaffected.

## Step 1 - Remove the global injection from app/main.py
Delete the comment block + `import truststore` + `truststore.inject_into_ssl()`
(lines 14-22) from `backend/app/main.py`.

## Step 2 - Restart + live verification
1. Stop backend pid 31108 (kill via port 8000 owning process, python only);
   truncate backend/uvicorn.log + uvicorn.err.log.
2. Run start-servers.ps1 -> "Started backend on :8000 (https)",
   frontend skipped (already on :8443).
3. `httpx.get('https://localhost:8000/health', verify='certs/companion-ca.crt')`
   -> 200.
4. `GET /events?token=<real-token>` -> grep backend/uvicorn.log: token count
   0, `[REDACTED]` present (re-check uvicorn access-formatter args index if
   not clean).
5. Frontend :8443: nosniff header, CSP on /dashboard/, traversal probe 404,
   missing asset (e.g. /dashboard/assets/nope.js) 404.
6. Rerun start-servers.ps1 -> both "already listening ... skipped".
7. pytest -q (expect 65 passed) - one import line removed.

## Step 3 - Commit (two commits)
1. Tier 1+2 (backend correctness/security + iOS 12 robustness): tts.py import,
   token filter, es2018 target, serve_frontend throttle + asset 404, tools
   async, /vision size gate, tplink negative TTL, store unmark_fired + re-arm,
   SSE ping, slider/overflow-wrap CSS, dashboard hooks/App/StatusBar/types,
   sw.js shell repair, proactive detach, test db isolation + new tests.
2. Tier 3: start-servers.ps1 (quoting fix + markers), main.py truststore
   removal, README HTTPS backend docs, .env.example note.
Report commit hashes.