# AGENTS.md — Project guidance for AI coding agents

This file is automatically read by Codex and similar agents. Follow these conventions when working in this repository.

---

## What this project is

A private, local HTTP gateway that sits between [Tailscale Serve](https://tailscale.com/kb/1242/tailscale-serve) and [Ollama](https://ollama.com). It exposes an OpenAI-compatible `/v1/chat/completions` endpoint so coding agents on your Tailscale tailnet can call a local LLM without exposing Ollama directly to the network.

---

## Core principles

- **Keep Ollama private.** `OLLAMA_BASE_URL` must always point to `127.0.0.1`. Never change it to `0.0.0.0` or a routable address.
- **Never expose raw Ollama to the network.** The gateway is the only thing that talks to Ollama.
- **Tailscale Serve is the access-control layer.** No public internet endpoint. No router port-forwarding.
- **No API-key auth by default.** `ENABLE_API_KEY_AUTH=false` is correct. Optional bearer-token auth exists only as a disabled-by-default extra layer.
- **No arbitrary model names by default.** `ENABLE_ARBITRARY_MODELS=false`. Only `main`, `small`, and `dev` aliases (and their resolved tags) are accepted unless explicitly unlocked.

---

## Model aliases

| Alias | Resolves to |
|-------|-------------|
| `main` | `qwen3.5:9b` |
| `small` | `qwen3.5:4b` |
| `dev` | `qwen3.5:0.8b` |

Direct model tags (`qwen3.5:9b`, `qwen3.5:4b`, `qwen3.5:0.8b`) are also accepted. Everything else is rejected with HTTP 422 unless `ENABLE_ARBITRARY_MODELS=true`.

The mapping lives in `gateway/normalize.py`. Update it there if you add new profiles.

---

## Stack

| Layer | Library |
|-------|---------|
| Web framework | FastAPI |
| Data validation | Pydantic v2 + pydantic-settings |
| HTTP client | httpx (async) |
| Server | uvicorn |
| Tests | pytest + pytest-asyncio + respx |

---

## Project layout

```
gateway/
  config.py       — Settings class (pydantic-settings)
  models.py       — Pydantic request/response schemas
  normalize.py    — Model alias resolution
  client.py       — Async httpx client for Ollama; streaming and non-streaming proxy
  main.py         — FastAPI app, middleware (body-size + auth), lifespan
  routes/
    chat.py       — POST /v1/chat/completions
    health.py     — GET /health, GET /health/ollama
tests/
  conftest.py     — Shared fixtures (settings overrides, async test client)
  test_normalize.py
  test_auth.py
  test_health.py
  test_chat.py
```

---

## How to run

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and edit env (defaults work for local dev)
cp .env.example .env

# 3. Start Ollama (must be running separately)
ollama serve

# 4. Pull the models
ollama pull qwen3.5:9b
ollama pull qwen3.5:4b
ollama pull qwen3.5:0.8b

# 5. Run the gateway
uvicorn gateway.main:app --host 127.0.0.1 --port 8080
```

---

## How to run tests

```bash
pytest tests/ -v --cov=gateway
```

Tests use `respx` to mock all Ollama HTTP calls — no live Ollama needed.

---

## Code quality expectations

- Use **type hints** on all function signatures.
- Keep files **small and focused** — one clear responsibility per file.
- Add **tests for any behaviour that affects security or compatibility** (model gating, auth, error codes).
- **Do not log full prompt content** by default. Log model alias, resolved model, and message count only.
- Prefer **boring, reliable code** over clever abstractions.
- All error responses use the OpenAI error envelope: `{"error": {"message": "...", "type": "...", "code": "..."}}`.

---

## Prohibited actions

- Do not hardcode API keys or secrets anywhere in source files.
- Do not change the middleware registration order in `main.py` without updating this file.
- Do not add public-internet exposure (no ngrok, no Cloudflare tunnels, no public SSH port-forward).
- Do not add router port-forwarding instructions. If asked, explain that Tailscale Serve is the intended path.
- Do not add a database, user accounts, a web UI, or Docker Compose unless the user explicitly requests it and understands it adds scope.

---

## Making changes

1. Run `pytest tests/ -v` before and after your change to confirm no regressions.
2. If you add a new model profile, update `MODEL_MAP` in `gateway/normalize.py` and add a test in `tests/test_normalize.py`.
3. If you add a new configuration variable, add it to `gateway/config.py`, `.env.example`, and the README.
4. Do not amend the previous commit — create a new one.
