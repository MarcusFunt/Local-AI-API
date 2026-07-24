#!/usr/bin/env bash
# ============================================================================
#  Start the already-installed Local AI API Docker stack and open the status
#  page. This is a lightweight "run" launcher: it does NOT sync the repository,
#  rebuild the image, or run the test suite. Use scripts/install-or-update.sh
#  to install, update, or change configuration.
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

ACCELERATOR="${LOCAL_AI_API_ACCELERATOR:-auto}"
OPEN_BROWSER="${OPEN_BROWSER:-1}"
GATEWAY_URL="http://127.0.0.1:8080/"
GATEWAY_HEALTH_URL="http://127.0.0.1:8080/health"
OLLAMA_HEALTH_URL="http://127.0.0.1:8080/health/ollama"
AGENT_ZERO_PORT="${AGENT_ZERO_PORT:-50080}"

log() { printf '[%s] %s\n' "$(date -Is)" "$*"; }
die() { log "ERROR: $*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --accelerator)
      [[ $# -ge 2 ]] || die "--accelerator requires a value (auto, cpu, nvidia, amd)."
      ACCELERATOR="$2"
      shift 2
      ;;
    --no-browser)
      OPEN_BROWSER=0
      shift
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
done

have docker || die "Docker CLI was not found. Install Docker first, or run scripts/install-or-update.sh."
docker info >/dev/null 2>&1 || die "Docker daemon is not running."

cd "${ROOT}"
if [[ ! -f .env ]]; then
  cp .env.example .env
  log "Created .env from .env.example."
fi

detect_accelerator() {
  if [[ "${ACCELERATOR}" != "auto" ]]; then
    printf '%s\n' "${ACCELERATOR}"
    return
  fi
  if have nvidia-smi && docker run --rm --gpus all hello-world >/dev/null 2>&1; then
    printf 'nvidia\n'
    return
  fi
  if have rocminfo; then
    printf 'amd\n'
    return
  fi
  printf 'cpu\n'
}

selected="$(detect_accelerator)"
log "Selected accelerator profile: ${selected}."

case "${selected}" in
  nvidia) compose_files=(-f compose.yaml -f compose.gpu-nvidia.yaml) ;;
  amd)    compose_files=(-f compose.yaml -f compose.gpu-amd.yaml) ;;
  cpu)    compose_files=(-f compose.yaml -f compose.cpu.yaml) ;;
  *)      die "Unknown accelerator profile: ${selected}" ;;
esac
compose_files+=(-f compose.agent-zero.yaml)

wait_for_url() {
  local url="$1" label="$2" attempts="${3:-60}" i
  for ((i = 1; i <= attempts; i++)); do
    if curl -fsS -m 5 "${url}" >/dev/null 2>&1; then
      log "${label} is healthy."
      return 0
    fi
    sleep 2
  done
  die "${label} did not become healthy at ${url}."
}

log "Starting the Local AI API stack (the first run pulls models and can take a while)."
docker compose "${compose_files[@]}" up -d

wait_for_url "${GATEWAY_HEALTH_URL}" "Gateway health"
wait_for_url "${OLLAMA_HEALTH_URL}" "Ollama health"

log "Stack is up."
log "Gateway:    ${GATEWAY_URL}"
log "Agent Zero: http://127.0.0.1:${AGENT_ZERO_PORT}/"

if [[ "${OPEN_BROWSER}" == "1" ]]; then
  if have xdg-open; then
    xdg-open "${GATEWAY_URL}" >/dev/null 2>&1 || true
  elif have open; then
    open "${GATEWAY_URL}" >/dev/null 2>&1 || true
  fi
fi
