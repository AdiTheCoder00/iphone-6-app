# Audit pass 3: fixes for scan findings (Tier 1 + Tier 2)

Scope: 8 CRITICAL/MAJOR findings + MINOR bundle. Committed in 2-3 commits.

## Tier 1 - broken / risky now

### 1. ESP32 firmware: HTTPS to the backend (CRITICAL, wake is 100% dead)
`firmware/wake_esp32s3/wake_esp32s3.ino:269` builds `http://%s:%u/wake` into an
HTTPS-only port. User chose: embed the CA.
- Use `WiFiClientSecure`; `setCACert()` with the CA PEM string; URL becomes
  `https://...`.
- `config.h` (gitignored, real values) gets the CA string (read from
  `certs/companion-ca.crt` during execution). `config.h.example` gets a
  placeholder comment ("paste contents of certs/companion-ca.crt here").
- Update the header comment (:7) + README note: regenerating the CA requires
  reflashing the board.
- Bundle firmware minors: distinct 401 log message; keep WDT note in README.

### 2. SSE lost-wakeup drops claimed reminders (MAJOR)
`backend/app/main.py:610-618`: claim runs in a worker thread; a disconnect
during the only await cancels the generator before publish -> claimed rows
never published, never re-armed. Wrap claim+publish in
`try/except asyncio.CancelledError` that publishes (or re-arms) before
re-raising. Same shape in `reminders.py` poll loop (shutdown-only today, keep
consistent).

### 3. Dashboard CSP leaks onto PWA on kept-alive connections (MAJOR)
`serve_frontend.py:194,240-251`: `_dashboard_csp` is handler-instance state,
never reset. Reset it at the top of `do_GET`/`do_HEAD` (per-request decision).

### 4. Unbounded log growth (MAJOR)
`start-servers.ps1:22-25,80-81,119`: before each Start-Process, if
uvicorn.log/uvicorn.err.log/frontend.log/frontend.err.log exceeds ~5 MB,
rotate to `<name>.1` (Move-Item, keep one old copy).

### 5. TTS preload ignores TTS_ENABLED (MAJOR)
`main.py:166` + `tts.py:118-128`: skip the preload task when
`not settings.tts_enabled`. Bundle T2 item: gate `_TTS_EXECUTOR` creation on
tts_enabled and `await executor.shutdown()` in lifespan teardown.

### 6. PWA health monitor stops after first success (MAJOR)
`companion.html:2978-2986`: on the ready path, re-arm a slower periodic probe
(armHealthRetry with ~15s interval via a param) so a backend that dies later
surfaces. Bundle T2: in-flight guard at the top of checkHealth.

### 7. Dashboard first-run + request timeout (MAJOR)
`dashboard/src/api.ts:25`: default backendUrl derived from the page origin
(`location.protocol + '//' + location.hostname + ':8000'`, fallback https),
placeholder text in SettingsBar.tsx:38 updated. Timeout: create the
AbortController inside request(), abort on timeout, cover the `json()` read.

### 8. Test suite does real downloads/network (MAJOR)
`backend/tests/conftest.py`: before the TestClient enters lifespan,
monkeypatch `transcription_service.preload` / `tts_service.preload` to no-ops,
disable proactive/llm-prewarm via settings, point tts dir at tmp.
`backend/requirements.txt:63-67`: move pytest(+asyncio) into
`backend/requirements-dev.txt`.

## Tier 2 - MINOR bundle

9. sw.js: skip `cache.put` for `text/event-stream` responses (one line).
10. companion.html: EventSource constructor error logs only `e.name` (token
    was in the URL in the message); "Download conversation" feature-detects
    the `download` attr and falls back to clipboard copy on iOS 12.
11. `timers.py` loop: add the exception guard its sibling loops have.
12. `companion.py:633-656`: normalize `tool_calls` (non-list -> []), skip
    non-dict entries.
13. DST-safe daily reminders: compute next occurrence from calendar date
    (`tools.py _next_occurrence`, `reminders.py`, `store.py:184-197`) instead
    of fixed 86400-second steps.
14. `main.py:544-557`: unlink the screenshot temp file in try/finally (not
    only BackgroundTask, which is skipped on cancellation).
15. `proactive.stop()`: cancel + gather `_pending_tasks` before returning.
16. `pc_control.py:358-380`: apply the `_PROGRAM_SUFFIXES` rejection to the
    scheme branch's hostname too; add a small table-driven test for open_url
    accept/reject (security-critical, cheap).
17. `tplink.is_available()`: return fresh cache value while valid; treat
    discovery as a background refresh instead of a synchronous 6s probe.
18. Whisper silence-artefact check: normalize punctuation before the
    exact-match set lookup.
19. `fresh_store` fixture: re-init the singleton after teardown so later
    endpoint tests can't hit "Store.init() has not been called".
20. Prompt injection: prefix web/tool content fed to the model with an
    explicit "untrusted data" marker (tools.py RSS/snippets, companion.py
    tool-message results).
21. Dashboard minors: tsconfig `target: ES2018` (match vite), touch targets to
    ~40-44px via padding, `nextId` computed before setEvents, ref-write out of
    render, `aria-live` only on newly inserted rows.
22. start-servers.ps1 stale-kill: verify the owning process CommandLine
    matches uvicorn/serve_frontend before Stop-Process.

## Verification
- `pytest -q` (suite must run offline: no downloads, no ollama calls)
- `node --check` companion.html inline JS; `npm run build` + grep dist for
  `??`/`catch{` -> zero; py_compile all changed files; PS parse check
- Live: restart via start-servers.ps1; same-connection check that
  `/dashboard/` then `/companion.html` yields no dashboard CSP on the PWA;
  log rotation behavior on re-run; health re-arm is client-side (phone)
- Firmware: cannot compile here (Arduino IDE) - hand over exact code + config
  embedding instructions; user flashes
- Commits: (1) backend+serving Tier 1&2, (2) PWA + dashboard, (3) firmware
  (or fold 3 into 2 if cleaner)