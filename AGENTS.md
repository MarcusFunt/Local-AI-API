# AGENTS.md - Project guidance for AI coding agents

This file is automatically read by Codex and similar agents. Follow these
conventions when working in this repository.

---

## What this project is

A private local HTTP gateway that sits between Tailscale Serve and Ollama. It
exposes OpenAI-compatible chat and audio endpoints so coding agents on a
Tailscale tailnet can call local models without exposing raw Ollama to the
network.

---

## Core principles

- Keep Ollama private. `OLLAMA_BASE_URL` must point to a loopback host:
  `127.0.0.1`, `localhost`, or `::1`.
- `OLLAMA_BASE_URL` must not include credentials, a path, params, query string,
  or fragment. `http://127.0.0.1:11434/foo` is intentionally invalid.
- Never expose raw Ollama to the network. The gateway is the only network-facing
  component that talks to Ollama.
- Tailscale Serve is the access-control layer. Do not add public internet
  exposure, router port-forwarding, ngrok, Cloudflare tunnels, or public SSH
  forwarding.
- API-key auth is optional and disabled by default. `ENABLE_API_KEY_AUTH=false`
  is the expected default.
- Arbitrary model names are disabled by default. Keep
  `ENABLE_ARBITRARY_MODELS=false` unless the user explicitly requests otherwise.

---

## Model aliases

| Alias | Resolves to |
|---|---|
| `main` | `qwen3.5:9b` |
| `small` | `qwen3.5:4b` |
| `dev` | `qwen3.5:0.8b` |

Direct tags for those same models are accepted. Everything else is rejected with
HTTP 422 unless `ENABLE_ARBITRARY_MODELS=true`.

The mapping lives in `gateway/normalize.py`. If you add a model profile, update
that mapping and add/adjust tests in `tests/test_normalize.py`.

---

## OpenAI compatibility

The primary endpoint is `POST /v1/chat/completions`.

Supported chat request features include:

- text messages and text content parts
- base64 `data:` image URL content parts, forwarded to Ollama as `images`
- `temperature`, `top_p`, `max_tokens`, `max_completion_tokens`, `stop`, and
  `seed`
- `tools`, `tool_choice`, and tool-call response passthrough
- `response_format` values `text`, `json_object`, and `json_schema`
- streaming responses and `stream_options.include_usage`

The gateway supports only one completion per request. Requests with `n > 1`
must return HTTP 422 rather than silently returning fewer choices than requested.

All error responses should use the OpenAI-compatible envelope:

```json
{"error": {"message": "...", "type": "...", "code": "..."}}
```

---

## Audio endpoints

The gateway also exposes:

- `POST /v1/audio/transcriptions` backed by local Whisper
- `POST /v1/audio/speech` backed by local Chatterbox TTS

Allowed Whisper aliases are `none`, `tiny`, `base`, and `small`. `none`
disables omitted-model transcription requests. Allowed Chatterbox aliases are
`chatterbox` and `chatterbox-multilingual`.

Heavy audio dependencies are imported lazily in `gateway/audio.py`. Keep
chat-only startup lightweight, and protect model caches from concurrent
first-load races.

---

## Stack

| Layer | Library |
|---|---|
| Web framework | FastAPI |
| Data validation | Pydantic v2 + pydantic-settings |
| HTTP client | httpx (async) |
| Server | uvicorn |
| Tests | pytest + pytest-asyncio + respx |
| Audio | openai-whisper + chatterbox-tts |

---

## Project layout

```text
gateway/
  config.py       - Settings class and startup validation
  models.py       - Pydantic request/response schemas
  normalize.py    - Model alias resolution
  client.py       - Async httpx client and OpenAI/Ollama translation
  audio.py        - Whisper and Chatterbox helpers
  main.py         - FastAPI app, middleware, lifespan, route registration
  routes/
    chat.py       - POST /v1/chat/completions
    audio.py      - POST /v1/audio/transcriptions, POST /v1/audio/speech
    health.py     - GET /health, GET /health/ollama
    status.py     - status GUI and status JSON/check endpoints
tests/
  conftest.py
  test_normalize.py
  test_auth.py
  test_health.py
  test_chat.py
  test_audio.py
  test_status_ui.py
  test_config.py
  test_deployment.py
  test_install_gui.py
scripts/
  setup-docker.*
  install-or-update.*
  install_gui.py
  bootstrap-ubuntu26-ai-server.sh
docs/
  ubuntu26-server.md
```

Docker deployment files are part of the project: `Dockerfile`, `compose.yaml`,
and accelerator-specific Compose overrides.

---

## How to run locally

```bash
pip install -r requirements.txt
cp .env.example .env
ollama serve
ollama pull qwen3.5:9b
ollama pull qwen3.5:4b
ollama pull qwen3.5:0.8b
uvicorn gateway.main:app --host 127.0.0.1 --port 8080
```

Use `requirements-audio.txt` only when local audio endpoints are needed outside
Docker.

---

## How to run tests

```bash
python -m pytest tests/ -v --cov=gateway
python -m compileall gateway
```

Tests use `respx` to mock Ollama HTTP calls. No live Ollama is required for the
unit test suite.

For Docker validation:

```bash
docker compose config
```

---

## Code quality expectations

- Use type hints on all function signatures.
- Keep files small and focused, with one clear responsibility per file.
- Add tests for behavior affecting security, compatibility, model gating, auth,
  request validation, error codes, or deployment exposure.
- Do not log full prompt content by default. Log model alias, resolved model,
  stream flag, and message count only.
- Prefer boring, reliable code over clever abstractions.
- Keep middleware registration order intentional. If it changes, update this
  file and tests.
- Keep docs, `.env.example`, install GUI behavior, and tests aligned when adding
  configuration variables or model aliases.

---

## Prohibited actions

- Do not hardcode API keys, bearer tokens, or other secrets.
- Do not bind Ollama to `0.0.0.0` or any routable address.
- Do not publish or document raw Ollama port `11434` for network access.
- Do not add public internet exposure or router port-forwarding instructions.
- Do not add a database, user accounts, another web UI, or new deployment stack
  unless explicitly requested and the extra scope is understood.
- Do not amend previous commits unless the user explicitly asks.

---

## Making changes

1. Check `git status --short` before editing.
2. Preserve unrelated user changes; do not revert work you did not make.
3. For behavior changes, add or update focused tests.
4. Run `python -m pytest tests/ -v --cov=gateway` before finishing when feasible.
5. If changing Docker/deployment behavior, run `docker compose config` when
   Docker is available.
6. If adding a model profile, update `gateway/normalize.py`, tests, README, and
   installer mappings if needed.
7. If adding configuration, update `gateway/config.py`, `.env.example`, README,
   and tests.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **Local-AI-API** (1389 symbols, 3027 relationships, 119 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/Local-AI-API/context` | Codebase overview, check index freshness |
| `gitnexus://repo/Local-AI-API/clusters` | All functional areas |
| `gitnexus://repo/Local-AI-API/processes` | All execution flows |
| `gitnexus://repo/Local-AI-API/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
