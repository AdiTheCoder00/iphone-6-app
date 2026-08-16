# Companion

A local AI desk companion designed for an iPhone 6/6s-sized display. The UI is
a small PWA shell (`companion.html`); the FastAPI backend supplies chat,
reminders, memory, speech-to-text and optional local text-to-speech.

## Run it locally

Requirements: Python 3.11+, [Ollama](https://ollama.com/) running locally, and
the configured model (by default, `qwen3:8b`).

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
ollama pull qwen3:8b
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

In a second terminal, serve the frontend from the repository root:

```powershell
python serve_frontend.py --port 8080
```

On the phone, open `http://<computer-LAN-IP>:8080/companion.html`, then
long-press the face to open Settings and change **Backend URL** to
`http://<computer-LAN-IP>:8000`. `localhost` on the phone refers to the phone,
not the computer.

`serve_frontend.py` serves **only** the PWA's own files (`companion.html`,
`qrcode.js`, `sw.js`, `manifest.json`, `icons/`) and the built dashboard
(`dashboard/dist/` at `/dashboard/`, with a fallback to `index.html`) —
everything else in the repo answers 404. The repository root also holds
`certs/` (private keys), `backend/data/` (the conversation database) and
`backend/venv`, and a plain `python -m http.server` would have exposed all of
them to anyone on the LAN, so don't fall back to one.

To build the dashboard (needed once, or after editing `dashboard/src/`):

```powershell
npm run build   # in dashboard/
```

Then "open dashboard" opens it on the phone: the dashboard URL lives in
`backend/.env` as `PC_URL_SHORTCUTS.dashboard` (the shipped default is
`http://localhost:8080/dashboard/` — edit it to the computer's LAN IP if the
phone is on a different network).

The first use of voice transcription and TTS downloads their local model files;
the initial response may therefore take longer than later ones.

## Tap-to-talk and the service worker over HTTPS

Tap-to-talk (getUserMedia) and the service worker only work in a secure
context, so for the phone they need HTTPS:

1. Generate a local CA and a server certificate (see below), then serve:

   ```powershell
   python serve_frontend.py --port 8443 --cert-dir certs
   ```

   (`python frontend_https.py` does the same thing on 8443.)

2. Install `certs/companion-ca.crt` on the phone as a trusted root (Settings >
   General > VPN & Device Management, or install the profile from a page
   served over the LAN).
3. Open `https://<computer-LAN-IP>:8443/companion.html` on the phone. `start-servers.ps1` picks HTTPS automatically when `certs/companion-server.crt` exists.
4. The backend must use the same scheme: iOS blocks fetch/EventSource to a
   plain-HTTP backend from an HTTPS page (mixed content). `start-servers.ps1`
   runs uvicorn with the same cert chain when it exists — set the phone's
   **Backend URL** to `https://<computer-LAN-IP>:8000`. Manually:

   ```powershell
   uvicorn app.main:app --host 0.0.0.0 --port 8000 `
     --ssl-keyfile certs\companion-server.key --ssl-certfile certs\companion-server.crt
   ```

### Local HTTPS certs

`certs/` is gitignored — never commit TLS material. Regenerate a fresh chain
any time you need one, with your computer's LAN IP substituted everywhere
`192.168.31.139` appears:

```powershell
cd certs
openssl req -x509 -newkey rsa:2048 -nodes -keyout companion-ca.key `
  -out companion-ca.crt -days 3650 -subj "/CN=Companion Local CA"
openssl req -newkey rsa:2048 -nodes -keyout companion-server.key `
  -out companion-server.csr -subj "/CN=companion"
echo "subjectAltName = IP:<your-LAN-IP>, IP:127.0.0.1, DNS:localhost" | Out-File -Encoding ascii san.cnf
openssl x509 -req -in companion-server.csr -CA companion-ca.crt `
  -CAkey companion-ca.key -CAcreateserial -out companion-server.crt `
  -days 825 -extfile san.cnf
```

A previous commit published the CA key to a public repo; history was rewritten
and the CA regenerated. Because the CA is installed as a trusted root on the
phone, anyone holding `companion-ca.key` could mint a trusted certificate for
any domain — keep it off the network entirely.

## Configuration

Create `backend/.env` to override settings without changing code:

```dotenv
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3:8b
COMPANION_TOKEN=change-me-to-a-long-random-string
TTS_ENABLED=true
WHISPER_MODEL_SIZE=base
CONVERSATION_STORE_LIMIT=500
MAX_AUDIO_MB=10
MAX_IMAGE_MB=10
```

`COMPANION_TOKEN` gates every endpoint (except `/health`). If it is unset the
server logs a warning and runs open — fine on a trusted LAN, dangerous
anywhere else. See `backend/app/config.py` for all available settings. Runtime
data is stored under `backend/data/` and is ignored by Git.

## ESP32-S3 wake board

Machine-specific values live in `firmware/wake_esp32s3/config.h` (gitignored):

1. Copy `config.h.example` to `config.h` and fill in `WIFI_SSID`,
   `WIFI_PASSWORD`, `BACKEND_HOST` (your computer's LAN IP),
   `BACKEND_TOKEN` (same value as `COMPANION_TOKEN` above) and
   `BACKEND_CA_CERT` (the contents of `certs/companion-ca.crt`, verbatim).
2. Open `firmware/wake_esp32s3/wake_esp32s3.ino` in the Arduino IDE, select the
   ESP32-S3 board, and flash. The sketch refuses to compile until `config.h`
   exists, so a half-configured board cannot be flashed silently.

The board wakes the backend over HTTPS, trusting the CA cert embedded in
`config.h`. Regenerating the CA chain (see the certs section above) therefore
requires updating `BACKEND_CA_CERT` and reflashing the board — the sketch
cannot otherwise trust the new certificate.

## Notes

- The backend defaults to permissive CORS for local development. Restrict
  `CORS_ORIGINS` before exposing it outside a trusted network.
- The service worker requires HTTPS (or localhost), so it is intentionally
  unavailable when the PWA is served over plain HTTP to a phone on the LAN.
