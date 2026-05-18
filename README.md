# Local AI API Gateway

A private, lightweight OpenAI-compatible gateway for [Ollama](https://ollama.com), designed to be exposed privately over [Tailscale Serve](https://tailscale.com/kb/1242/tailscale-serve). Coding agents on your Tailscale tailnet can use the standard OpenAI chat-completions API against your local models — without Ollama ever touching the network.

---

## What this gateway does

- Accepts `POST /v1/chat/completions` requests in OpenAI format
- Normalises model aliases (`main` → `qwen3.5:9b`, `small` → `qwen3.5:4b`)
- Proxies requests to Ollama running on `127.0.0.1:11434`
- Translates Ollama's response format back to the OpenAI envelope
- Supports both streaming (`stream: true`) and non-streaming responses
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

- Python 3.11 or later
- [Ollama](https://ollama.com/download) installed and running
- [Tailscale](https://tailscale.com/download) installed (for private remote access)

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
- auto-selects NVIDIA, AMD/ROCm, or CPU compose overrides
- builds the gateway image and runs `python -m pytest tests -v` inside it before restarting the live gateway
- pulls `qwen3.5:9b` and `qwen3.5:4b`
- installs Tailscale if needed, runs `tailscale up` interactively when unauthenticated, and configures `tailscale serve --bg http://127.0.0.1:8080`
- installs a systemd timer that runs on boot and daily using the same script

Important: the script intentionally forces the deployment checkout to match `origin/main`. Commit and push any tracked local work before running it on a machine where you care about those changes.

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

---

## Installation

```bash
git clone https://github.com/MarcusFunt/Local-AI-API.git
cd Local-AI-API

pip install -r requirements.txt

cp .env.example .env
# Edit .env if you want to change any defaults (the defaults work for local dev)
```

---

## Running Ollama

Start Ollama (it binds to `127.0.0.1:11434` by default — leave it that way):

```bash
ollama serve
```

Pull the two supported models:

```bash
ollama pull qwen3.5:9b
ollama pull qwen3.5:4b
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

For development with auto-reload:

```bash
uvicorn gateway.main:app --host 127.0.0.1 --port 8080 --reload
```

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
model    = "main"      # or "small", "qwen3.5:9b", "qwen3.5:4b"
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

### Supported model values

| Client sends | Gateway forwards |
|---|---|
| `main` | `qwen3.5:9b` |
| `small` | `qwen3.5:4b` |
| `qwen3.5:9b` | `qwen3.5:9b` |
| `qwen3.5:4b` | `qwen3.5:4b` |
| anything else | HTTP 422 (unless `ENABLE_ARBITRARY_MODELS=true`) |

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
