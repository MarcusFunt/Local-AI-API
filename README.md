# Local AI API — a local AI lab

Local AI API is a private, local-first AI lab. Its current runtime is an
[Ollama](https://ollama.com) gateway plus [Agent Zero](https://github.com/frdel/agent-zero),
retrieval, and isolated repository-operation workers. It is designed to be
reached privately through [Tailscale Serve](https://tailscale.com/kb/1242/tailscale-serve),
without exposing raw Ollama to the network.

The product goal is twofold: keep a dependable Agent Zero workspace available
for normal work, while running bounded local experiments that can improve
prompts, policies, skills, routing, retrieval configuration, or code candidates
over time. An experiment never edits the active runtime in place. It must create
an isolated candidate, produce reproducible evaluation evidence, and receive an
explicit local promotion decision.

The concrete controller design for that next layer is in
[Lab controller v0.1](docs/lab-controller-v0.1.md). Phases 1–2 now provide a
loopback-only SQLite control plane with validated jobs, status APIs, durable
leases/heartbeats, and one isolated repo-ops workspace-preparation adapter.
Evaluation, artifacts, releases, and promotion remain deliberately
unimplemented.

## Deployment modes

| Mode | What it starts | Intended use |
|---|---|---|
| `compose.yaml` with an accelerator overlay | Ollama, model initialization, and the OpenAI-compatible gateway | Small, core local runtime |
| `scripts/setup-docker.sh` / `.ps1` | Core runtime plus Agent Zero | Standard interactive local setup |
| `scripts/install-or-update.sh` / `.ps1` | Managed full stack: Agent Zero (unless opted out), Qdrant/RAG, repo-ops, lifecycle/preview workers, and a separate sandbox Agent Zero instance | Private lab host and scheduled maintenance |
| `uvicorn lab_controller.main:app --host 127.0.0.1 --port 8091` | Controller API: migrations, job registry, status APIs, lease reaper, and token-disabled worker endpoints | Local controller development; it is not part of the installer |
| `compose.lab-controller.yaml` | Optional controller plus one internal-network repo-ops workspace adapter | Explicit local lab setup; it does not run models, edit code, evaluate, or promote |

MCP support remains opt-in at image-build time. The repo-ops worker is an
internal Agent Zero MCP service, not a public gateway endpoint.

---

## Current runtime capabilities

- Accepts `POST /v1/chat/completions` requests in OpenAI format
- Accepts `GET /v1/models` requests for the gateway allowlist
- Normalises model aliases (`main` → `qwen3.5:9b`, `small` → `qwen3.5:4b`, `dev` → `qwen3.5:0.8b`)
- Agent Zero aliases `agent` and `agent-utility` both resolve to `qwen3:14b`
- Proxies requests to Ollama running on `127.0.0.1:11434`
- Translates Ollama's response format back to the OpenAI envelope
- Supports both streaming (`stream: true`) and non-streaming responses
- Accepts `POST /v1/embeddings` for the local `embedding` (`nomic-embed-text`) profile
- Offers opt-in, quality-first multi-call agents at `POST /v1/agent/completions`
- Accepts `POST /v1/audio/transcriptions` requests using local Whisper models
- Accepts `POST /v1/audio/speech` requests using local Chatterbox TTS
- Provides health endpoints at `GET /health`, `GET /health/ollama`, and `GET /health/qdrant`
- Optionally indexes documents with Qdrant and retrieves them with hybrid semantic + keyword search and a local reranker (RAG)
- Optionally exposes the local AI and audio operations as MCP tools at `/mcp`
- Runs Agent Zero as a separate Docker UI that uses this gateway as an
  OpenAI-compatible provider

## Current boundary and configuration note

The repository already contains redacted learning records, isolated repo-ops
workspaces, evaluation manifests, and a human-operated candidate promotion
script. The controller now provides a durable SQLite job registry, schema
migrations, strict `lab.job/v1` validation, idempotency protection, read-only
status APIs, pull leases, fenced heartbeats, expiry recovery, and one adapter
that creates a disposable `code_patch` workspace. It does **not** yet contain an
artifact registry, candidate records, evaluator, release records, or an
automatic/promotion API. A completed phase-2 job is not an evaluated or
promotable result; see [Lab controller v0.1](docs/lab-controller-v0.1.md).

`.env.example` and the installers set `ENABLE_ARBITRARY_MODELS=false` and
`WARM_AUDIO_ON_START=false`. The raw fallback values in `gateway/config.py` and
`compose.yaml` are currently `true`, so operators must create and retain a
`.env` file rather than relying on Compose fallbacks. Treat resolving that
fallback mismatch as a hardening prerequisite before any shared or multi-user
deployment.

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

## Quality-first agents

`POST /v1/agent/completions` is a deliberate, non-streaming surface for tasks
where a better answer is worth several model calls. It is stateless and uses
the same Tailscale, optional bearer authentication, body-size limit, request
timeout, and model allow-list as normal chat completions. It does not execute
client-supplied tools or enable arbitrary model names.

- `mode: "adaptive"` (the default) performs task intake, three independent
  specialist passes, evidence verification, and final writing. It selects a
  conservative quality profile from `balanced`, `research`, `rag`, `coding`,
  `tool_planning`, or `personal`; callers may set `quality_profile` explicitly.
  Its metadata reports only bounded verification labels and never intermediate
  reasoning or ledgers.
- `mode: "graph"` is a fixed state machine: planner → drafter → critic → verifier →
  writer. It uses the `quality` (`qwen3.5:9b`) profile by default; `agent`
  (`qwen3:14b`) remains available for comparison and Agent Zero.
- `mode: "mixture_of_experts"` obtains independent specialist opinions, then
  lets the selected `model` synthesize a final answer. By default, it uses
  two independent `quality` (`qwen3.5:9b`) passes and one `agent`
  (`qwen3:14b`) critic pass, each with distinct roles and temperatures. Supply
  two to four approved `quality` or `agent` aliases to override that ensemble;
  repeated aliases are supported for self-critique.

The adaptive path makes six calls; the fixed graph makes five. Planner, drafter, critic, verifier, and
specialists use private reasoning; their hidden reasoning is discarded. Each produces a compact evidence ledger containing requirements,
verified facts, assumptions, alternatives, risks, and a recommendation. The
verifier passes only accepted findings to the non-thinking, user-facing writer.
The default 8,192-token context carries substantially more evidence than the
former 4k profile. Agent requests are still bounded so the prompt, grounding,
evidence ledger, and final answer coexist without silent truncation.

For document-grounded work, set `use_rag: true` (and optionally
`rag_document_id`). The agent retrieves once, labels the resulting source IDs,
and carries the same immutable evidence snapshot through every review stage.
The final answer can cite those IDs, and `metadata.grounding_sources` exposes
their document and chunk identities. Intermediate drafts and private reasoning
are never returned. The response reports aggregate token use and completed
stages in `metadata`.

```bash
curl http://127.0.0.1:8080/v1/agent/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "mode": "adaptive",
    "quality_profile": "rag",
    "use_rag": true,
    "messages": [{"role": "user", "content": "Design a reliable backup plan."}]
  }'
```

```bash
curl http://127.0.0.1:8080/v1/agent/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "mode": "mixture_of_experts",
    "model": "agent",
    "expert_models": ["agent", "agent", "agent"],
    "messages": [{"role": "user", "content": "Compare these architecture options."}]
  }'
```

---

## Requirements

- Docker Engine or Docker Desktop with the Docker Compose plugin for the recommended container setup
- [Tailscale](https://tailscale.com/download) installed for private remote access
- Python 3.11 or later only if you run the gateway outside Docker
- [Ollama](https://ollama.com/download), FFmpeg, and audio runtime libraries only if you run outside Docker
- **Disk:** the default full stack uses ~50–60 GB (Low Compute Mode ~15–20 GB). See [`docs/disk-and-cleanup.md`](docs/disk-and-cleanup.md) for sizing and how to reclaim space — including the Windows/WSL2 disk that grows but never shrinks on its own.

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

### Scheduled updates

The host installer is the only update owner. Its optional systemd timer or
Windows scheduled task fast-forwards a clean checkout, validates the rebuilt
gateway image, and records the outcome in `.local/last-update.json`.

Scheduled runs refuse dirty, non-approved-branch, or non-fast-forward checkouts;
use an explicit manual installer run only when you intend to replace local work.

The status page is read-only and reports the latest installer update marker.

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
- keeps raw Ollama private on `127.0.0.1:11434` inside Docker and publishes only gateway and Agent Zero loopback ports
- binds the gateway to `0.0.0.0` only inside the private Docker network namespace so the host loopback publish works
- auto-selects NVIDIA, AMD/ROCm, or CPU compose overrides
- builds the gateway image and runs `python -m pytest tests -v` inside it before restarting the live gateway
- pulls the space-separated `OLLAMA_MODELS` list from `.env`, defaulting to `qwen3.5:9b`, `qwen3.5:4b`, `qwen3.5:0.8b`, and `qwen3:14b`
- installs Tailscale if needed, runs `tailscale up` interactively when unauthenticated, and configures Tailscale Serve for the gateway and Agent Zero
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
- pulls the space-separated `OLLAMA_MODELS` list from `.env`, defaulting to `qwen3.5:9b`, `qwen3.5:4b`, `qwen3.5:0.8b`, and `qwen3:14b`
- configures `tailscale serve --bg http://127.0.0.1:8080` when Tailscale is installed and authenticated
- starts Agent Zero on `127.0.0.1:50080`, using `agent` and `agent-utility` through this gateway
- configures Agent Zero for Tailscale Serve on HTTPS port `8443`
- installs a per-user Scheduled Task using the selected update schedule, defaulting to logon and daily using the same script

The Windows update task runs as the current user, so its scheduled triggers fire
while that user is logged on (Docker Desktop itself requires an interactive
session anyway). On a server you want to update while logged off, prefer the
Linux systemd path, which runs the updater as a system service independent of any
login.

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

### Agent Zero on Windows

Agent Zero starts alongside the gateway on Windows Docker Desktop. The installer
adds `qwen3:14b` to `OLLAMA_MODELS`, starts Agent Zero on
`http://127.0.0.1:50080`, and configures Tailscale Serve for
`https://<machine>.ts.net:8443/` when Tailscale is available.

Agent Zero is configured to use this gateway's `/v1` OpenAI-compatible API with
the `agent` (`qwen3:14b`) model for both chat and utility work. Both Agent Zero
contexts are aligned to the deployed 8,192-token Ollama window. The
`agent-utility` alias is retained for compatible external clients and resolves
to the same 14B model, never to a weaker substep. Do not use Agent Zero's public
Remote Link, Cloudflare Tunnel, Tailscale Funnel, or Microsoft Dev Tunnels in
this project; keep access private through Tailscale Serve. When the gateway
`API_KEY` is set, the Agent Zero Docker override writes it into Agent Zero's
`API_KEY_OTHER` setting automatically so the configured OpenAI-compatible
provider can authenticate to the gateway. If gateway auth is disabled, Agent
Zero receives the harmless dummy key `unused`.

Its managed memory configuration uses the local
`sentence-transformers/all-MiniLM-L6-v2` embedding model, preserving the
existing Agent Zero memory index without exposing Ollama. The gateway also
offers the separately gated `embedding` alias (`nomic-embed-text`) at
`POST /v1/embeddings` for OpenAI-compatible clients.

The local overlay also seeds three non-destructive Agent Zero profiles and
projects: **Research**, **Code**, and **Personal**. Their scoped instructions
keep document evidence, repository operations, and personal memory separate.
Seeds are copied only when the matching Agent Zero profile or project does not
already exist, so later user edits are preserved. Select the corresponding
profile/project in Agent Zero before starting work. Periodically review its
project memory and remove stale or incorrect entries; memory is evidence, not
an authority.

### Isolated repository MCP for Agent Zero

The optional repository worker gives Agent Zero bounded code search, GitNexus
context/impact analysis, disposable branch editing, named verification checks,
and review reports. It is not part of the gateway's Tailscale-facing `/mcp`
server and publishes no host port. See [the repository MCP guide](docs/repo-ops.md)
for the Compose command, Agent Zero connection URL, and the separate untrusted
skill/plugin quarantine workflow.

---

## One-click launchers (Windows)

If you would rather not touch a terminal, two double-clickable scripts sit at the
repository root:

| File | What it does |
|---|---|
| `Install.cmd` | Opens the graphical configurator (choose models, default profile, auth, Tailscale, accelerator, and the auto-update schedule), then runs the Docker install/update. Run this first, and again whenever you want to change configuration. |
| `Start.cmd` | Starts the already-installed stack (Ollama + gateway + Agent Zero) and opens the status page. It does not rebuild, re-test, or sync the repository, so it is the fast way to bring everything back up after a reboot. |

`Install.cmd` needs Python 3.11+ on `PATH` (it launches the Tkinter GUI); it
prints download instructions if Python is missing. `Start.cmd` only needs Docker
Desktop and starts it automatically if it is installed but not running.

On Linux/macOS the equivalents are `python3 scripts/install_gui.py` for
configuration and `bash scripts/start-stack.sh` for a fast start.

---

## Graphical installer

Run the Tkinter GUI directly when you want to choose models, the default profile, optional bearer-token auth, mandatory Agent Zero support, Tailscale setup, accelerator profile, and repository auto-update cadence:

```powershell
python .\scripts\install_gui.py
```

On Windows you can also just double-click `Install.cmd`, which launches this same GUI.

On Linux, use `python3 scripts/install_gui.py` and install your distribution's Tkinter package if needed. The GUI writes `.env`, stores its own UI state in `.local/install-gui.json`, and then runs the same Docker installer scripts described above.

The model selector writes `OLLAMA_MODELS` using the approved model tags only.
With the installer-created `.env`, the gateway rejects arbitrary model names
unless `ENABLE_ARBITRARY_MODELS=true` is set manually. See the configuration
note above: direct raw Compose/config fallbacks currently differ and must not be
relied upon as a security control.

---

## Low compute mode

For a machine without a usable GPU (or when you just want a small footprint), enable **Low compute mode** — a single toggle that runs the whole stack lean:

- **CPU only** — builds the gateway image with the CPU-only PyTorch wheel, which drops ~2.7 GB of unused CUDA libraries (the audio image goes from ~11.5 GB to ~4.8 GB).
- **Smallest model only** — pulls just `qwen3.5:0.8b` (the `dev` profile) instead of the full set.
- **Agent Zero off** — Agent Zero needs the large `qwen3:14b` model, so it is skipped in this mode.

Speech-to-text (Whisper) and text-to-speech (Chatterbox) still work; they just run on CPU.

Enable it in the graphical installer with the **"Low compute mode"** checkbox, or pass the flag directly:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-or-update.ps1 -LowCompute
```

```bash
./scripts/install-or-update.sh --low-compute
```

To go back to your GPU and the full model set, re-run the installer **without** the flag (or uncheck the box in the GUI).

---

## Installation

Run the gateway outside Docker inside a virtual environment so its pinned
dependencies do not collide with other Python packages on your machine (the
gateway pins exact `fastapi`/`starlette`/`pydantic` versions, and a newer
globally-installed Starlette will break the import):

```bash
git clone https://github.com/MarcusFunt/Local-AI-API.git
cd Local-AI-API

python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell:
#   .\.venv\Scripts\Activate.ps1

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
| `/health/qdrant` | Reports Qdrant status; returns `disabled` when RAG is off |

The status page shows gateway runtime health, Ollama connectivity, whether `main`, `small`, `dev`, `agent`, and `agent-utility` are pulled, the latest explicit dev-model inference check, and the latest installer update result when the gateway is running from a Git checkout. The check uses a fixed tiny prompt and does not log prompt content. The status page is read-only: the configured host installer schedule is the only update owner.

`/status.json` also includes a `last_update_run` field: the installers write a `.local/last-update.json` marker after every scheduled or manual run (`passed`/`failed`, timestamp, and whether it was scheduled), so a nightly auto-update that failed silently — for example because a test flaked during the in-image gate — is visible here instead of only in the systemd journal or Task Scheduler history.

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

Transcription supports `json` (default), `text`, and `verbose_json` response
formats. Speech currently supports only `wav`; non-default `speed` values and
any `voice` value are rejected with HTTP 422.

Realtime speech-to-speech conversation WebSocket:

```text
ws://localhost:8080/v1/audio/conversations
```

For a ready-to-use browser client, open `/live-call` on the gateway (for
example, `https://your-tailnet-host.ts.net/live-call` through Tailscale Serve).
It keeps one private WebSocket session open and provides push-to-talk turns,
live transcript/text updates, and WAV playback. Microphone capture requires
HTTPS or localhost. The page itself contains no gateway data and remains
available when optional API-key auth is enabled; enter the API key in the page
to authenticate the WebSocket connection. The key is sent in a WebSocket
subprotocol rather than a URL and is retained only in the open browser tab.

The first WebSocket message must be a JSON session frame:

```json
{
  "type": "session.start",
  "model": "main",
  "whisper_model": "small",
  "tts_model": "chatterbox",
  "input_audio_format": "wav",
  "language": "en",
  "max_tokens": 512
}
```

After `session.created`, send `input_audio.start`, one or more binary audio
frames, then `input_audio.commit`. The server responds with transcript events,
assistant text deltas, a completed `speech_text`, a binary WAV frame, and final
completion metadata. The conversation text contract is plain UTF-8 speech text:
metadata stays in JSON events, while only cleaned `speech_text` is sent to TTS.

This v1 API is realtime at the transport/session layer, but speech processing is
explicit-turn based: Whisper runs after `input_audio.commit`, and Chatterbox
returns one WAV response after assistant text completes. Lower-latency partial
transcription or sentence-level TTS can be added later without changing the
existing HTTP audio endpoints.

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
- `use_rag: true`, which adds retrieved document context when RAG is enabled

The gateway still supports only one completion per request. Requests with
`n > 1` return HTTP 422 instead of silently returning fewer choices than the
client requested.

## Optional RAG document search

RAG adds document ingestion and quality-first retrieval backed by Qdrant. It is
off by default, and document routes return HTTP 503 until it is enabled. Search
combines dense semantic candidates with a Unicode-aware keyword/BM25 pass,
fuses them, then reranks the small candidate set locally with the multilingual
ColBERT-style `answerdotai/answerai-colbert-small-v1` model. The first rerank
downloads that model into the gateway cache. The Docker setup requires the
Qdrant Compose overlay and the RAG dependencies:

```bash
# The managed installer includes this overlay. For manual Compose use, add it
# explicitly; set RAG_ENABLED=false or INSTALL_RAG=false to opt out.
docker compose -f compose.yaml -f compose.qdrant.yaml up -d --build
docker compose exec ollama ollama pull nomic-embed-text
```

In the Compose deployment, Qdrant is private in Ollama's shared network
namespace; do not publish port 6333. The gateway exposes these authenticated
routes (unless API-key auth is disabled):

| Route | Purpose |
|---|---|
| `POST /v1/documents/ingest` | Upload a document (maximum 10 MiB) and index it. An optional multipart `document_id` overrides the content-derived ID. |
| `GET /v1/documents` | List indexed documents. |
| `DELETE /v1/documents/{document_id}` | Remove all chunks for one document. |
| `POST /v1/search` | Search with JSON `{ "query": "...", "top_k": 4, "document_id": null }`. |

Set `use_rag: true` in a chat-completions or agent-completions request to add
retrieved context to that request. The conversation WebSocket has the same
optional `session.start.use_rag` flag. Agent mode retrieves once and preserves
source IDs through planning, review, and final writing. When RAG is disabled,
standard chat/conversation flags are ignored; the agent endpoint instead
returns an explicit configuration error to avoid an ungrounded answer.

Quality experiments are deliberately separate from production configuration.
Use the ignored private case set and scripts documented in
[`quality/README.md`](quality/README.md) to compare the 4k baseline with 8k,
12k, or an optional `qwen3:30b` challenger. The evaluation gate promotes
nothing automatically: retain a candidate only when it improves the private
benchmark without a regression in factuality, instruction following, source
support, completeness, or safety.

For pinned public regressions, use the opt-in
[`evals/README.md`](evals/README.md) Compose profile. It keeps IFEval,
EvalPlus, and selected LiveBench dependencies out of the gateway image; scores
normal chat and the five-stage agent separately; and executes generated code
only in a network-disabled, disposable Docker child. Its daily scheduler is
serial and never changes production configuration automatically.

`/health/qdrant` is always available without API-key authentication, like the
other health routes. For a direct, non-Docker run, export the RAG variables in
the shell that launches Uvicorn: the RAG module reads process environment
variables rather than the gateway's `.env` settings loader.

The built-in MCP server is also optional. Build with FastMCP installed, then
configure an MCP client to use `https://<machine>.ts.net/mcp/`; see
[`docs/mcp-setup.md`](docs/mcp-setup.md) for the commands and available tools.

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
| `CONTROLLER_HOST` | `127.0.0.1` | Lab-controller listen address. Only loopback hosts are accepted. |
| `CONTROLLER_PORT` | `8091` | Lab-controller listen port when started manually. |
| `CONTROLLER_DATABASE_PATH` | `.local/lab-controller/controller.sqlite3` | Local SQLite path for controller migrations, jobs, events, workers, and attempts. |
| `CONTROLLER_MAX_LIST_LIMIT` | `100` | Maximum jobs returned by one status list request. |
| `CONTROLLER_LEASE_SECONDS` | `60` | Lease duration for the phase-2 repo-ops adapter (30–300 seconds). |
| `CONTROLLER_SCHEDULER_INTERVAL_SECONDS` | `5` | Expired-lease reaper interval (1–60 seconds). |
| `CONTROLLER_WORKER_TOKEN` | *(empty)* | Required before worker endpoints or `compose.lab-controller.yaml` can be used; keep it local and distinct from `API_KEY`. |
| `CONTROLLER_ALLOWED_CANDIDATE_TARGET_PREFIXES` | `agent-zero/,gateway/,rag/,repo-ops/` | Comma-separated target namespaces allowed for `candidate_build` jobs. |
| `CONTROLLER_ALLOWED_CANDIDATE_CHANGE_FIELDS` | approved bounded fields | Comma-separated change fields a candidate may request; each job may name at most two. |
| `REPO_OPS_CONTROLLER_*` | see `.env.example` | Optional adapter ID, image identity, poll interval, and heartbeat interval. The Compose overlay fixes its controller URL to the private `lab-controller` service. |
| `DEFAULT_MODEL_PROFILE` | `main` | Profile used when the client omits the `model` field. |
| `DEFAULT_WHISPER_MODEL` | `none` | Whisper profile used when transcription requests omit `model`; allowed values are `none`, `tiny`, `base`, and `small`. |
| `WHISPER_DEVICE` | `auto` | Device for Whisper model loading (`auto`, `cpu`, `cuda`, or `mps`). |
| `WHISPER_CACHE_DIR` | `/models/cache/whisper` in Docker | Whisper model cache directory. |
| `CHATTERBOX_MODEL` | `chatterbox` | Chatterbox model used when speech requests omit `model`; allowed values are `chatterbox` and `chatterbox-multilingual`. |
| `CHATTERBOX_DEVICE` | `auto` | Device for Chatterbox model loading (`auto`, `cpu`, `cuda`, or `mps`). |
| `WARM_AUDIO_ON_START` | `false` in `.env.example`; `true` raw fallback | If `true`, load the Whisper and Chatterbox models in the background at startup so the first speech request is fast instead of paying a one-time download + load. Use an explicit `.env`; the fallback mismatch is a known hardening issue. |
| `ENABLE_ARBITRARY_MODELS` | `false` in `.env.example`; `true` raw fallback | If `true`, any model name is forwarded to Ollama. Use an explicit `.env`; do not rely on the current fallback as a model-gating control. |
| `AGENT_ZERO_ENABLED` | `true` | Enables the Agent Zero overlay for installer workflows. It can be opted out of for a core gateway deployment; when enabled, installers and status treat `agent` and `agent-utility` as required models. |
| `AGENT_ZERO_PORT` | `50080` | Host loopback port for the Agent Zero UI. |
| `AGENT_ZERO_TAILSCALE_HTTPS_PORT` | `8443` | Tailscale Serve HTTPS port for Agent Zero. |
| `ENABLE_API_KEY_AUTH` | `false` | If `true`, requires a `Bearer` token on all non-health requests. |
| `API_KEY` | *(empty)* | The required non-empty token when `ENABLE_API_KEY_AUTH=true`; also passed to Agent Zero as its `other` provider key. |
| `REQUEST_TIMEOUT_SECONDS` | `0` | Upstream read timeout in seconds. `0` waits indefinitely for continuous quality work. |
| `MAX_REQUEST_BODY_BYTES` | `10485760` | Max allowed request body (10 MiB). |

Docker-only variables used by `compose.yaml`:

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_IMAGE` | pinned digest | Ollama image reference. Upgrade by changing the tested tag and digest together. |
| `AUTOHEAL_IMAGE` | pinned digest | Autoheal image reference. |
| `QDRANT_IMAGE` | pinned digest | Qdrant image reference used by the RAG overlay. |
| `OLLAMA_KEEP_ALIVE` | `24h` | How long Ollama keeps models loaded after use. |
| `OLLAMA_CONTEXT_LENGTH` | `8192` | Default Ollama context. Confirm models remain 100% GPU-resident before raising it. |
| `QUALITY_CONTEXT_TOKENS` | `8192` | Context sent by the advanced quality-agent endpoint; requests may override it from 4k–32k. |
| `OLLAMA_MODELS` | `qwen3.5:9b qwen3.5:4b qwen3.5:0.8b qwen3:14b nomic-embed-text` | Space-separated model tags pulled by the Docker `model-init` service. |
| `INSTALL_AUDIO` | `true` | Build the gateway image with Whisper and Chatterbox runtime dependencies. Set `false` for chat-only images. |
| `INSTALL_RAG` | `true` | Build the gateway image with the RAG Python dependencies. Set `false` for a smaller non-RAG image. |
| `RAG_ENABLED` | `true` | Enable document routes and allow chat/conversation requests that opt into RAG. Set `false` to disable retrieval. |
| `QDRANT_URL` | `http://127.0.0.1:6333` | Private Qdrant address used by the RAG module. |
| `QDRANT_COLLECTION` | `local-ai-api-docs` | Qdrant collection used for document chunks. |
| `RAG_EMBED_MODEL` | `nomic-embed-text` | Ollama embedding model used for indexing and retrieval. |
| `RAG_EMBED_DIM` | `768` | Embedding-vector dimension; it must match the configured embedding model. |
| `RAG_TOP_K` | `4` | Default number of retrieved chunks. |
| `RAG_HYBRID_CANDIDATES` | `16` | Dense and lexical candidates considered before reranking. |
| `RAG_RERANK_CANDIDATES` | `12` | Fused candidates reranked locally by the ColBERT-style model. |
| `RAG_LEXICAL_SCAN_LIMIT` | `5000` | Maximum chunks scanned for the local keyword/BM25 pass. |
| `RAG_RERANK_MODEL` | `answerdotai/answerai-colbert-small-v1` | Local multilingual reranker model. |
| `RAG_RERANK_CACHE_DIR` | `/models/cache/fastembed` | Persistent cache path for the reranker model. |
| `RAG_CHUNK_SIZE` | `512` | Target document chunk size. |
| `RAG_CHUNK_OVERLAP` | `64` | Overlap between consecutive document chunks. |
| `AGENT_ZERO_BASE_IMAGE` | pinned digest | Agent Zero base image used for the local cockpit overlay and skill sandbox. |

In Docker, `compose.yaml` overrides `HOST=0.0.0.0` inside the shared Ollama/gateway network namespace, while the only published host port remains `127.0.0.1:8080`.
Ollama model files live in the `ollama-data` volume, while gateway-side audio
model caches live in the `gateway-model-cache` volume.

### Supported model values

The `dev` profile is intended for faster local development and uses [Qwen/Qwen3.5-0.8B](https://huggingface.co/Qwen/Qwen3.5-0.8B) through Ollama's `qwen3.5:0.8b` tag.

| Client sends | Gateway forwards |
|---|---|
| `main` | `qwen3.5:9b` |
| `quality` | `qwen3.5:9b` — quality-agent candidate |
| `small` | `qwen3.5:4b` |
| `dev` | `qwen3.5:0.8b` |
| `agent` | `qwen3:14b` |
| `agent-utility` | `qwen3:14b` |
| `qwen3.5:9b` | `qwen3.5:9b` |
| `qwen3.5:4b` | `qwen3.5:4b` |
| `qwen3.5:0.8b` | `qwen3.5:0.8b` |
| `qwen3:14b` | `qwen3:14b` |
| `openai/<approved alias or tag>` | The same approved alias or tag (the `openai/` prefix is stripped) |
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

### Speech-to-speech conversation mode

`/v1/audio/conversations` is a WebSocket API for one persistent spoken
conversation. It reuses the same local components as the HTTP endpoints:
Whisper for committed user-audio turns, Ollama chat streaming for assistant text,
and Chatterbox for WAV speech output.

Supported client events:

| Event | Purpose |
|---|---|
| `session.start` | Opens the session and chooses chat/STT/TTS models. |
| `input_audio.start` | Starts collecting binary user-audio frames. |
| binary frames | Carry WAV or WebM audio bytes for the active turn. |
| `input_audio.commit` | Ends the current user turn and starts STT -> chat -> TTS. |
| `input_audio.clear` | Drops the active input buffer. |
| `response.cancel` | Cancels the active response task when possible. |
| `ping` | Returns `pong`. |
| `session.close` | Closes the WebSocket. |

Supported server events include `transcript.completed`, `response.text.delta`,
`response.text.completed`, `response.audio.started`, a binary WAV frame,
`response.audio.completed`, `response.completed`, and `error`.

The bundled `/live-call` client is intentionally push-to-talk. Each released
turn is transcribed, answered, and synthesized before the next turn begins;
this avoids sending microphone audio while the user is not speaking and matches
the WebSocket API's committed-turn design.

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

3. API clients must now include the header:
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

All validation runs locally; this repository deliberately does not use GitHub
Actions or GitHub-hosted Linux runners.

Tests use `respx` to mock all Ollama HTTP calls — no live Ollama is needed.

```bash
python -m pytest tests/ -v --cov=gateway
python -m compileall gateway
```

To see coverage report:

```bash
python -m pytest tests/ -v --cov=gateway --cov-report=term-missing
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
| Model needs more memory than is available | 507 |
| Ollama unreachable | 502 |
| Ollama timed out | 504 |
| Ollama returned malformed JSON | 502 |
| Audio model disabled, unknown, or unsupported format | 422 |
| Whisper or Chatterbox dependency missing | 503 |
| Whisper or Chatterbox runtime failure | 502 |
| RAG route while RAG is disabled | 503 |
| RAG vector-store failure | 502 |
