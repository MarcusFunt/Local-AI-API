# Ubuntu 26.04 Local AI Gateway + Agent Server

This is the concrete deployment path for an old desktop already installed with
Ubuntu 26.04 Server. It keeps the first version simple: bare-metal Ubuntu,
Docker, NVIDIA acceleration, the private Local AI API gateway, and isolated
per-project agent containers.

## What This Installs

- Docker Engine and Compose through the existing Linux installer.
- Ollama in Docker, private to the gateway network namespace.
- The FastAPI gateway on host loopback only: `127.0.0.1:8080`.
- Agent Zero on host loopback only: `127.0.0.1:50080`.
- Tailscale Serve for trusted tailnet HTTPS access.
- A low-privilege `agent` user with workspaces under `/srv/agent-workspaces`.
- A dedicated `local-ai-agents` Docker bridge plus a gateway-only proxy for
  agent containers at `http://172.30.50.1:18080/v1`.
- Config/work backups that intentionally exclude Ollama model volumes.

Raw Ollama is never published. The only Docker-published host ports should be
the gateway on `127.0.0.1:8080` and Agent Zero on `127.0.0.1:50080`.

## First Run

Run this from any admin account that already has an SSH public key in
`~/.ssh/authorized_keys`:

```bash
git clone https://github.com/MarcusFunt/Local-AI-API.git /opt/local-ai-api
cd /opt/local-ai-api
bash scripts/bootstrap-ubuntu26-ai-server.sh
```

If the NVIDIA driver is not installed yet, either install it first using the
Ubuntu recommended driver flow or run:

```bash
bash scripts/bootstrap-ubuntu26-ai-server.sh --install-nvidia-driver
sudo reboot
cd /opt/local-ai-api
bash scripts/bootstrap-ubuntu26-ai-server.sh
```

The bootstrap script refuses to disable SSH password login unless the admin user
has an authorized key. It also refuses firewall lockdown until Tailscale is
authenticated and the `tailscale0` interface exists.

## Runtime Defaults

The bootstrap writes these important `.env` defaults:

```dotenv
OLLAMA_BASE_URL=http://127.0.0.1:11434
HOST=127.0.0.1
PORT=8080
DEFAULT_MODEL_PROFILE=main
OLLAMA_MODELS=qwen3.5:9b qwen3.5:4b qwen3.5:0.8b qwen3:14b
DEFAULT_WHISPER_MODEL=none
ENABLE_ARBITRARY_MODELS=false
AGENT_ZERO_ENABLED=true
AGENT_ZERO_PORT=50080
AGENT_ZERO_TAILSCALE_HTTPS_PORT=8443
ENABLE_API_KEY_AUTH=false
API_KEY=
```

Tailscale is the access-control layer for v1. If the tailnet becomes less
trusted later, enable bearer-token auth in `.env` and rerun
`scripts/install-or-update.sh`.

## Agent Containers

Create or enter an agent workspace with:

```bash
cd /opt/local-ai-api
bash scripts/run-agent-container.sh --project my-project
```

Run one command:

```bash
bash scripts/run-agent-container.sh --project my-project --command 'python --version'
```

The launcher:

- mounts only `/srv/agent-workspaces/<project>` at `/workspace`;
- mounts only package caches under `/srv/agent-caches/<project>`;
- sets `OPENAI_API_BASE`, `OPENAI_API_KEY`, and `OPENAI_MODEL`;
- does not mount `/var/run/docker.sock`, host root, SSH keys, or `/var/run`;
- uses Docker CPU, memory, PID, capability, and `no-new-privileges` limits.

Use a custom image when a project needs preinstalled system packages:

```bash
bash scripts/run-agent-container.sh --project my-project --image ghcr.io/acme/agent-python:latest
```

## Verification

After install:

```bash
cd /opt/local-ai-api
bash scripts/verify-server-plan.sh --tailscale-url https://<server-name>.<tailnet>.ts.net
```

The verifier checks OS, disk/RAM snapshot, `nvidia-smi`, Docker GPU access,
gateway health, `/status/check`, a `dev` chat request, Docker port exposure,
Tailscale Serve, and an agent-container smoke test.

Useful manual checks:

```bash
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/health/ollama
docker ps --format 'table {{.Names}}\t{{.Ports}}'
tailscale serve status
```

## Backups

Create a config/work backup:

```bash
cd /opt/local-ai-api
bash scripts/backup-server-state.sh
```

The backup includes `.env`, `.local` installer state, agent workspaces, and the
selected SSH, unattended-upgrades, and systemd files created by this deployment.
It excludes Ollama model volumes and package caches. Restore by reinstalling the
repo, extracting the archive onto `/`, rerunning the bootstrap or installer, and
letting Ollama re-pull the approved models.

## Operational Boundaries

- Do not expose `11434`.
- Do not use Tailscale Funnel, Caddy, LiteLLM, public tunnels, or router
  port-forwarding for v1.
- Do not add the `agent` user to the Docker group.
- Do not mount host secrets or Docker control sockets into agent containers.
- Move agents into VMs or Proxmox later if they will run hostile multi-tenant
  workloads.
