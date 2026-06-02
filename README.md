# Local AI API Gateway

A private, lightweight OpenAI-compatible gateway for [Ollama](https://ollama.com), designed to be exposed privately over [Tailscale Serve](https://tailscale.com/kb/1242/tailscale-serve). Coding agents on your Tailscale tailnet can use the standard OpenAI chat-completions API against your local models — without Ollama ever touching the network.

---

## What this gateway does

- Accepts `POST /v1/chat/completions` requests in OpenAI format
- Normalises model aliases (`main` → `qwen3.5:9b`, `small` → `qwen3.5:4b`, `dev` → `qwen3.5:0.8b`)
- Proxies requests to Ollama running on `127.0.0.1:11434`
- Translates Ollama's response format back to the OpenAI envelope
- Supports both streaming (`stream: true`) and non-streaming responses
- Accepts `POST /v1/audio/transcriptions` requests using local Whisper models
- Accepts `POST /v1/audio/speech` requests using local Chatterbox TTS
- Provides health endpoints at `GET /health` and `GET /health/ollama`

---

## Why Ollama must stay on localhost

Ollama's HTTP API has **no built-in authentication**. If you bind it to `0.0.0.0` or expose port `11434` via a router or firewall rule, anyone who can reach that port can run arbitrary models and consume your GPU — or worse, exfiltrate data.

The correct network layout is:

```
[Tailscale tailnet client]
    → HTTPS via Tailscale Serve (access-controlled)
        → gateway on 127.0.0.1:8080
            → Ollama on 127.0.0.1:11434 (never reachable from outside)
```

Tailscale Serve acts as the TLS terminator and access-control layer. The gateway adds model-name gating and optional API-key auth on top. Ollama sees only local loopback traffic.

`OLLAMA_BASE_URL` is validated at startup and must point to a loopback host such as `127.0.0.1`, `localhost`, or `::1`. Never open port 11434 in your firewall or router.

---

## Requirements

- Docker Engine or Docker Desktop with the Docker Compose plugin for the recommended container setup
- [Tailscale](https://tailscale.com/download) installed for private remote access
- Python 3.11 or later only if you run the gateway outside Docker
- [Ollama](https://ollama.com/download), FFmpeg, and audio runtime libraries only if you run outside Docker

---

## Simple Docker setup

Use these scripts when you want the gateway, Ollama, Python dependencies, and model
setup managed by Docker without touching local Python packages:

```powershell
# Windows / Docker Desktop
powershell -ExecutionPolicy Bypass -File .\scripts\setup-docker.ps1
```

```bash
# Linux, macOS, or WSL with Docker already running
bash scripts/setup-docker.sh
```

The setup scripts:

- create `.env` from `.env.example` if it does not exist
- build the gateway image with pinned Python dependencies inside the container
- install optional Whisper and Chatterbox dependencies inside the image by default
- start private Ollama and gateway containers with a shared network namespace
- pull `OLLAMA_MODELS` into the persistent `ollama-data` Docker volume
- mount `gateway-model-cache` at `/models/cache` for Whisper, Hugging Face, and Torch model caches
- run the test suite inside the gateway image unless `--skip-tests` / `-SkipTests` is used
- verify `/health`, `/health/ollama`, and `/status/check`

Useful options:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-docker.ps1 -Accelerator cpu
powershell -ExecutionPolicy Bypass -File .\scripts\setup-docker.ps1 -NoAudio -SkipTests
```

```bash
bash scripts/setup-docker.sh --accelerator cpu
bash scripts/setup-docker.sh --no-audio --skip-tests
```

The only host port published by Compose is `127.0.0.1:8080`. Raw Ollama remains
private inside Docker on `127.0.0.1:11434`.

---

## Automated Docker deployment on Linux Mint

For a Linux Mint host, the recommended production path is the self-updating Docker installer:

```bash
git clone https://github.com/MarcusFunt/Local-AI-API.git
cd Local-AI-API
./scripts/install-or-update.sh
```

The script:

- fetches `origin/main`, hard-resets tracked files to GitHub, and cleans untracked files while preserving runtime env files such as `.env`
- installs Docker Engine and the Compose plugin if needed
- runs Ollama in Docker with the gateway sharing Ollama's network namespace
- keeps raw Ollama private on `127.0.0.1:11434` inside Docker and publishes only `127.0.0.1:8080`
- binds the gateway to `0.0.0.0` only inside the private Docker network namespace so the host loopback publish works
- auto-selects NVIDIA, AMD/ROCm, or CPU compose overrides
- builds the gateway image and runs `python -m pytest tests -v` inside it before restarting the live gateway
- pulls the space-separated `OLLAMA_MODELS` list from `.env`, defaulting to `qwen3.5:9b`, `qwen3.5:4b`, and `qwen3.5:0.8b`
- installs Tailscale if needed, runs `tailscale up` interactively when unauthenticated, and configures `tailscale serve --bg http://127.0.0.1:8080`
- installs a systemd timer using the selected update schedule, defaulting to boot and daily using the same script

Important: the script intentionally forces the deployment checkout to match `origin/main`. Commit and push any tracked local work before running it on a machine where you care about those changes.

Useful installer options:

```bash
./scripts/install-or-update.sh --accelerator cpu --update-schedule daily --update-time 03:00
./scripts/install-or-update.sh --update-schedule every-hours --every-hours 6 --update-time 03:00
./scripts/install-or-update.sh --update-schedule none
```

Useful verification commands after install:

```bash
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/health/ollama
docker ps --format 'table {{.Names}}\t{{.Ports}}'
systemctl list-timers local-ai-api-update.timer
tailscale serve status
```

To run a manual update later:

```bash
./scripts/install-or-update.sh
```

## Ubuntu 26.04 server bootstrap

For an Ubuntu 26.04 bare-metal AI server with NVIDIA acceleration, Tailscale
Serve, host hardening, agent containers, and config/work backups, use the
server bootstrap guide:

```bash
git clone https://github.com/MarcusFunt/Local-AI-API.git /opt/local-ai-api
cd /opt/local-ai-api
bash scripts/bootstrap-ubuntu26-ai-server.sh
```

See [`docs/ubuntu26-server.md`](docs/ubuntu26-server.md) for the full operating
plan, verification commands, agent-container launcher, and backup workflow.

## Automated Docker deployment on Windows

For a Windows host, use the PowerShell Docker installer. It expects Docker Desktop to be installed and able to run Linux containers.

```powershell
git clone https://github.com/MarcusFunt/Local-AI-API.git
cd Local-AI-API
powershell -ExecutionPolicy Bypass -File .\scripts\install-or-update.ps1
```

The Windows script:

- fetches `origin/main`, hard-resets tracked files to GitHub, and cleans untracked files while preserving runtime env files such as `.env`
- starts Docker Desktop if it is installed but not already running
- runs Ollama in Docker with the gateway sharing Ollama's network namespace
- keeps raw Ollama private on `127.0.0.1:11434` inside Docker and publishes only `127.0.0.1:8080`
- binds the gateway to `0.0.0.0` only inside the private Docker network namespace so the host loopback publish works
- auto-selects NVIDIA when Docker GPU access works, otherwise uses the CPU compose override
- builds the gateway image and runs `python -m pytest tests -v` inside it before restarting the live gateway
- pulls the space-separated `OLLAMA_MODELS` list from `.env`, defaulting to `qwen3.5:9b`, `qwen3.5:4b`, and `qwen3.5:0.8b`
- configures `tailscale serve --bg http://127.0.0.1:8080` when Tailscale is installed and authenticated
- installs a per-user Scheduled Task using the selected update schedule, defaulting to logon and daily using the same script

AMD/ROCm Docker acceleration is Linux-only in this project; Windows hosts use CPU unless NVIDIA Docker GPU access is available.

Useful installer options:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-or-update.ps1 -Accelerator cpu -UpdateSchedule daily -UpdateTime 03:00
powershell -ExecutionPolicy Bypass -File .\scripts\install-or-update.ps1 -UpdateSchedule every-hours -EveryHours 6 -UpdateTime 03:00
powershell -ExecutionPolicy Bypass -File .\scripts\install-or-update.ps1 -UpdateSchedule none
```

Useful verification commands after install:

```powershell
Invoke-WebRequest http://127.0.0.1:8080/health -UseBasicParsing
Invoke-WebRequest http://127.0.0.1:8080/health/ollama -UseBasicParsing
docker ps --format "table {{.Names}}\t{{.Ports}}"
Get-ScheduledTask -TaskName "Local AI API Update"
tailscale serve status
```

To run a manual update later:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-or-update.ps1
```

---

## Graphical installer

Run the Tkinter GUI when you want to choose models, the default profile, optional bearer-token auth, Tailscale setup, accelerator profile, and repository auto-update cadence:

```powershell
python .\scripts\install_gui.py
```

On Linux, use `python3 scripts/install_gui.py` and install your distribution's Tkinter package if needed. The GUI writes `.env`, stores its own UI state in `.local/install-gui.json`, and then runs the same Docker installer scripts described above.

The model selector writes `OLLAMA_MODELS` using the approved model tags only. The gateway still rejects arbitrary model names unless `ENABLE_ARBITRARY_MODELS=true` is set manually.

---

## Installation

```bash
git clone https://github.com/MarcusFunt/Local-AI-API.git
cd Local-AI-API

pip install -r requirements.txt

# Optional: install the local Whisper and Chatterbox runtime stacks.
pip install -r requirements-audio.txt

cp .env.example .env
# Edit .env if you want to change any defaults (the defaults work for local dev)
```

---

## Running Ollama

Start Ollama (it binds to `127.0.0.1:11434` by default — leave it that way):

```bash
ollama serve
```

Pull the three supported models:

```bash
ollama pull qwen3.5:9b
ollama pull qwen3.5:4b
ollama pull qwen3.5:0.8b
```

Verify they are available:

```bash
ollama list
```

---

## Running the gateway

```bash
uvicorn gateway.main:app --host 127.0.0.1 --port 8080
```

The gateway now listens on `http://127.0.0.1:8080`.
Open `http://127.0.0.1:8080/` or `http://127.0.0.1:8080/status` for the built-in status GUI.

For development with auto-reload:

```bash
uvicorn gateway.main:app --host 127.0.0.1 --port 8080 --reload
```

---

## Status web GUI

The gateway container serves a small status GUI from the same FastAPI process:

| Route | Purpose |
|---|---|
| `/` | Status GUI |
| `/status` | Status GUI alias |
| `/status.json` | JSON status feed for gateway, Ollama, model readiness, and runtime settings |
| `/status/check` | Runs a non-streaming end-to-end check against the `dev` profile (`qwen3.5:0.8b`) |

The status page shows gateway runtime health, Ollama connectivity, whether `main`, `small`, and `dev` are pulled, and the latest explicit dev-model inference check. The check uses a fixed tiny prompt and does not log prompt content.

If optional API-key auth is enabled, the status GUI is protected like other non-health routes. Health endpoints remain available without auth.

---

## Smoke tests with curl

Health check (gateway only):

```bash
curl http://localhost:8080/health
```

Health check (including Ollama connectivity):

```bash
curl http://localhost:8080/health/ollama
```

Non-streaming chat (using the `main` alias):

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "main",
    "messages": [{"role": "user", "content": "Say hello in one sentence."}]
  }'
```

Streaming chat:

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "main",
    "messages": [{"role": "user", "content": "Count to five."}],
    "stream": true
}'
```

Audio transcription with Whisper:

```bash
curl http://localhost:8080/v1/audio/transcriptions \
  -F "model=tiny" \
  -F "file=@sample.wav"
```

If the `model` form field is omitted, the gateway uses `DEFAULT_WHISPER_MODEL`.
Set it to `none` to make omitted-model transcription requests fail closed, or send
`tiny`, `base`, or `small` per request.

Text-to-speech with Chatterbox:

```bash
curl http://localhost:8080/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "model": "chatterbox",
    "input": "Hello from the local gateway.",
    "response_format": "wav"
  }' \
  --output speech.wav
```

End-to-end dev-model check through the status endpoint:

```bash
curl -X POST http://localhost:8080/status/check
```

Using a direct model tag instead of an alias:

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.5:4b",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

---

## Chat compatibility notes

The chat endpoint accepts the core OpenAI chat-completions request shape plus a
small set of compatibility fields commonly sent by coding agents:

- text messages and OpenAI content-part lists, including text parts and base64
  `data:` image URLs
- `max_tokens` or `max_completion_tokens`, forwarded to Ollama as
  `num_predict`
- `tools` and `tool_choice`; `tool_choice: "none"` suppresses tool forwarding
- `response_format` with `text`, `json_object`, or `json_schema`
- `stream_options.include_usage`, which emits a final usage chunk before
  `[DONE]`

The gateway still supports only one completion per request. Requests with
`n > 1` return HTTP 422 instead of silently returning fewer choices than the
client requested.

---

## Exposing the gateway privately with Tailscale Serve

Tailscale Serve makes the gateway available to your tailnet devices over HTTPS — no public internet exposure, no router configuration needed.

```bash
# Expose the local gateway on your tailnet at https://<your-machine>.ts.net/
tailscale serve --bg http://127.0.0.1:8080
```

Verify the serve configuration:

```bash
tailscale serve status
```

Your tailnet URL will look like `https://my-machine.tail12345.ts.net`. Only devices on your Tailscale tailnet can reach it. TLS is handled automatically by Tailscale.

To stop serving:

```bash
tailscale serve reset
```

---

## Configuring a coding agent to use the gateway

Set the agent's OpenAI base URL to your Tailscale Serve URL (or `http://127.0.0.1:8080` for local use):

```
base_url = "https://my-machine.tail12345.ts.net/v1"
model    = "main"      # or "small", "dev", "qwen3.5:9b", "qwen3.5:4b", "qwen3.5:0.8b"
api_key  = "unused"    # required by most clients; the gateway ignores it by default
```

### Example: Python with openai library

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://my-machine.tail12345.ts.net/v1",
    api_key="unused",  # required field; ignored by gateway unless auth is enabled
)

response = client.chat.completions.create(
    model="main",
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.choices[0].message.content)
```

### Example: Continue.dev (VS Code extension)

In `~/.continue/config.json`:

```json
{
  "models": [
    {
      "title": "Qwen 3.5 9B (local)",
      "provider": "openai",
      "model": "main",
      "apiBase": "https://my-machine.tail12345.ts.net/v1",
      "apiKey": "unused"
    }
  ]
}
```

### Example: Aider

```bash
aider --openai-api-base https://my-machine.tail12345.ts.net/v1 \
      --openai-api-key unused \
      --model main
```

---

## Configuration reference

Copy `.env.example` to `.env` and adjust as needed.

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama address. Must point to a loopback host. |
| `HOST` | `127.0.0.1` | Gateway listen address. Keep as localhost; Tailscale Serve forwards to it. |
| `PORT` | `8080` | Gateway listen port. |
| `DEFAULT_MODEL_PROFILE` | `main` | Profile used when the client omits the `model` field. |
| `DEFAULT_WHISPER_MODEL` | `none` | Whisper profile used when transcription requests omit `model`; allowed values are `none`, `tiny`, `base`, and `small`. |
| `WHISPER_DEVICE` | `auto` | Device for Whisper model loading (`auto`, `cpu`, `cuda`, or `mps`). |
| `WHISPER_CACHE_DIR` | `/models/cache/whisper` in Docker | Whisper model cache directory. |
| `CHATTERBOX_MODEL` | `chatterbox` | Chatterbox model used when speech requests omit `model`; allowed values are `chatterbox` and `chatterbox-multilingual`. |
| `CHATTERBOX_DEVICE` | `auto` | Device for Chatterbox model loading (`auto`, `cpu`, `cuda`, or `mps`). |
| `ENABLE_ARBITRARY_MODELS` | `false` | If `true`, any model name is forwarded to Ollama. |
| `ENABLE_API_KEY_AUTH` | `false` | If `true`, requires a `Bearer` token on all non-health requests. |
| `API_KEY` | *(empty)* | The required non-empty token when `ENABLE_API_KEY_AUTH=true`. |
| `REQUEST_TIMEOUT_SECONDS` | `600` | Max seconds to wait for Ollama. Large models can be slow on first load. |
| `MAX_REQUEST_BODY_BYTES` | `10485760` | Max allowed request body (10 MiB). |

Docker-only variables used by `compose.yaml`:

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_IMAGE_TAG` | `latest` | Ollama Docker image tag. |
| `OLLAMA_KEEP_ALIVE` | `5m` | How long Ollama keeps models loaded after use. |
| `OLLAMA_MODELS` | `qwen3.5:9b qwen3.5:4b qwen3.5:0.8b` | Space-separated model tags pulled by the Docker `model-init` service. |
| `INSTALL_AUDIO` | `true` | Build the gateway image with Whisper and Chatterbox runtime dependencies. Set `false` for chat-only images. |

In Docker, `compose.yaml` overrides `HOST=0.0.0.0` inside the shared Ollama/gateway network namespace, while the only published host port remains `127.0.0.1:8080`.
Ollama model files live in the `ollama-data` volume, while gateway-side audio
model caches live in the `gateway-model-cache` volume.

### Supported model values

The `dev` profile is intended for faster local development and uses [Qwen/Qwen3.5-0.8B](https://huggingface.co/Qwen/Qwen3.5-0.8B) through Ollama's `qwen3.5:0.8b` tag.

| Client sends | Gateway forwards |
|---|---|
| `main` | `qwen3.5:9b` |
| `small` | `qwen3.5:4b` |
| `dev` | `qwen3.5:0.8b` |
| `qwen3.5:9b` | `qwen3.5:9b` |
| `qwen3.5:4b` | `qwen3.5:4b` |
| `qwen3.5:0.8b` | `qwen3.5:0.8b` |
| anything else | HTTP 422 (unless `ENABLE_ARBITRARY_MODELS=true`) |

### Supported audio model values

Whisper transcription model values:

| Client sends | Gateway uses |
|---|---|
| `none` | disabled; request returns HTTP 422 |
| `tiny` | Whisper `tiny` |
| `base` | Whisper `base` |
| `small` | Whisper `small` |

Chatterbox speech model values:

| Client sends | Gateway uses |
|---|---|
| `chatterbox` | English Chatterbox TTS |
| `chatterbox-multilingual` | Multilingual Chatterbox TTS; optional `language` defaults to `en` |

The local speech endpoint currently returns WAV audio only. If a client sends
`response_format=mp3`, the gateway returns HTTP 422 instead of silently changing
the requested format. Chatterbox voice selection is not exposed by this gateway;
requests with a `voice` value also return HTTP 422.

---

## Enabling optional API-key auth

Tailscale controls who can reach the endpoint at the network level. API-key auth is an optional additional layer — useful if you share a tailnet with people you do not fully trust, or if you want audit-log style rejection at the application layer.

To enable it:

1. In `.env`, set:
   ```
   ENABLE_API_KEY_AUTH=true
   API_KEY=your-strong-random-key-here
   ```

2. Restart the gateway.

3. All clients must now include the header:
   ```
   Authorization: Bearer your-strong-random-key-here
   ```

Health endpoints (`/health`, `/health/ollama`) always bypass auth.

To generate a strong key:
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

---

## Running tests

Tests use `respx` to mock all Ollama HTTP calls — no live Ollama is needed.

```bash
pytest tests/ -v --cov=gateway
```

To see coverage report:

```bash
pytest tests/ -v --cov=gateway --cov-report=term-missing
```

---

## Error responses

All errors use an OpenAI-compatible envelope so clients that parse OpenAI errors work correctly:

```json
{
  "error": {
    "message": "Human-readable description",
    "type": "invalid_request_error",
    "code": "model_not_found"
  }
}
```

| Scenario | HTTP status |
|---|---|
| Unknown model name | 422 |
| Missing or wrong API key | 401 |
| Request body too large | 413 |
| Ollama returned an error | 502 |
| Ollama unreachable | 502 |
| Ollama timed out | 504 |
| Ollama returned malformed JSON | 502 |
| Audio model disabled, unknown, or unsupported format | 422 |
| Whisper or Chatterbox dependency missing | 503 |
| Whisper or Chatterbox runtime failure | 502 |
