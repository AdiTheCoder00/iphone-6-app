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
python -m http.server 8080 --bind 0.0.0.0
```

Open `http://localhost:8080/companion.html` on the computer. On the phone,
open `http://<computer-LAN-IP>:8080/companion.html`, then long-press the face
to open Settings and change **Backend URL** to
`http://<computer-LAN-IP>:8000`. `localhost` on the phone refers to the phone,
not the computer.

The first use of voice transcription and TTS downloads their local model files;
the initial response may therefore take longer than later ones.

## Configuration

Create `backend/.env` to override settings without changing code:

```dotenv
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3:8b
TTS_ENABLED=true
WHISPER_MODEL_SIZE=base
CONVERSATION_STORE_LIMIT=500
```

See `backend/app/config.py` for all available settings. Runtime data is stored
under `backend/data/` and is ignored by Git.

## Notes

- The backend defaults to permissive CORS for local development. Restrict
  `CORS_ORIGINS` before exposing it outside a trusted network.
- The service worker requires HTTPS (or localhost), so it is intentionally
  unavailable when the PWA is served over plain HTTP to a phone on the LAN.
