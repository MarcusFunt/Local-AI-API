#!/usr/bin/env bash
set -Eeuo pipefail

ACCELERATOR="${LOCAL_AI_API_ACCELERATOR:-auto}"
SKIP_TESTS=0
INSTALL_AUDIO="${INSTALL_AUDIO:-true}"
GATEWAY_HEALTH_URL="${GATEWAY_HEALTH_URL:-http://127.0.0.1:8080/health}"
OLLAMA_HEALTH_URL="${OLLAMA_HEALTH_URL:-http://127.0.0.1:8080/health/ollama}"
AGENT_ZERO_PORT="${AGENT_ZERO_PORT:-50080}"

log() {
  printf '[%s] %s\n' "$(date -Iseconds)" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

have() {
  command -v "$1" >/dev/null 2>&1
}

usage() {
  cat <<'EOF'
Usage: scripts/setup-docker.sh [options]

Options:
  --accelerator auto|cpu|nvidia|amd  Select Compose accelerator profile.
  --skip-tests                       Build and start without running tests in the image.
  --no-audio                         Build chat-only gateway image without Whisper/Chatterbox dependencies.
  -h, --help                         Show this help.
EOF
}

parse_args() {
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --accelerator)
        [[ "$#" -ge 2 ]] || die "--accelerator requires a value."
        ACCELERATOR="$2"
        shift 2
        ;;
      --skip-tests)
        SKIP_TESTS=1
        shift
        ;;
      --no-audio)
        INSTALL_AUDIO=false
        shift
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        die "Unknown argument: $1"
        ;;
    esac
  done

  case "${ACCELERATOR}" in
    auto | cpu | nvidia | amd) ;;
    *) die "Unknown accelerator: ${ACCELERATOR}" ;;
  esac
}

repo_root() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "${script_dir}/.." && pwd
}

ensure_env_file() {
  local root="$1"
  if [[ ! -f "${root}/.env" ]]; then
    cp "${root}/.env.example" "${root}/.env"
    log "Created .env from .env.example."
  fi
}

require_docker() {
  have docker || die "Docker CLI was not found. Install Docker Engine or Docker Desktop, then rerun this script."
  docker info >/dev/null 2>&1 || die "Docker is not running. Start Docker and rerun this script."
  docker compose version >/dev/null 2>&1 || die "Docker Compose plugin was not found. Install the Compose plugin, then rerun this script."
}

nvidia_docker_works() {
  have nvidia-smi && docker run --rm --gpus all hello-world >/dev/null 2>&1
}

detect_accelerator() {
  if [[ "${ACCELERATOR}" != "auto" ]]; then
    printf '%s\n' "${ACCELERATOR}"
    return
  fi

  if nvidia_docker_works; then
    printf '%s\n' "nvidia"
    return
  fi

  if [[ -e /dev/kfd && -d /dev/dri ]]; then
    printf '%s\n' "amd"
    return
  fi

  printf '%s\n' "cpu"
}

compose_files_for_accelerator() {
  local root="$1"
  local accelerator="$2"

  case "${accelerator}" in
    nvidia)
      printf '%s\n' -f "${root}/compose.yaml" -f "${root}/compose.gpu-nvidia.yaml"
      ;;
    amd)
      printf '%s\n' -f "${root}/compose.yaml" -f "${root}/compose.gpu-amd.yaml"
      ;;
    cpu)
      printf '%s\n' -f "${root}/compose.yaml" -f "${root}/compose.cpu.yaml"
      ;;
    *)
      die "Unknown accelerator: ${accelerator}"
      ;;
  esac
  printf '%s\n' -f "${root}/compose.agent-zero.yaml"
}

compose_cmd() {
  docker compose "$@"
}

build_gateway_image() {
  local -a compose_args=("$@")

  export INSTALL_AUDIO

  log "Validating Docker Compose configuration."
  compose_cmd "${compose_args[@]}" config >/dev/null

  log "Building gateway image with container-owned Python dependencies."
  compose_cmd "${compose_args[@]}" build gateway

  if [[ "${SKIP_TESTS}" != "1" ]]; then
    log "Running tests inside the gateway image."
    docker run --rm \
      --entrypoint python \
      --workdir /app \
      local-ai-api-gateway:latest \
      -m pytest tests -v
  fi
}

start_stack() {
  local -a compose_args=("$@")

  log "Starting private Ollama container."
  compose_cmd "${compose_args[@]}" up -d ollama

  log "Pulling configured Ollama models into the Docker volume."
  compose_cmd "${compose_args[@]}" run --rm model-init

  log "Starting gateway container."
  compose_cmd "${compose_args[@]}" up -d gateway

  log "Starting Agent Zero."
  compose_cmd "${compose_args[@]}" up -d agent-zero
}

wait_for_url() {
  local url="$1"
  local label="$2"
  local attempts="${3:-60}"

  for _ in $(seq 1 "${attempts}"); do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      log "${label} is healthy."
      return
    fi
    sleep 2
  done

  die "${label} did not become healthy at ${url}."
}

main() {
  local root accelerator
  local -a compose_args

  parse_args "$@"

  root="$(repo_root)"
  cd "${root}"

  ensure_env_file "${root}"
  require_docker

  accelerator="$(detect_accelerator)"
  log "Selected accelerator profile: ${accelerator}."
  mapfile -t compose_args < <(compose_files_for_accelerator "${root}" "${accelerator}")

  build_gateway_image "${compose_args[@]}"
  start_stack "${compose_args[@]}"

  wait_for_url "${GATEWAY_HEALTH_URL}" "Gateway health"
  wait_for_url "${OLLAMA_HEALTH_URL}" "Ollama health"
  wait_for_url "http://127.0.0.1:${AGENT_ZERO_PORT}" "Agent Zero UI" 90

  log "Running dev model smoke check."
  curl -fsS -X POST --max-time 120 "http://127.0.0.1:8080/status/check" >/dev/null

  log "Docker setup complete. Gateway: http://127.0.0.1:8080/ Agent Zero: http://127.0.0.1:${AGENT_ZERO_PORT}/"
}

main "$@"
