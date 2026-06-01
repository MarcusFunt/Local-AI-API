#!/usr/bin/env bash
set -Eeuo pipefail

GATEWAY_URL="${LOCAL_AI_API_GATEWAY_URL:-http://127.0.0.1:8080}"
TAILSCALE_URL="${LOCAL_AI_API_TAILSCALE_URL:-}"
SKIP_AGENT=0
SKIP_GPU_DOCKER=0

pass() {
  printf '[PASS] %s\n' "$*"
}

fail() {
  printf '[FAIL] %s\n' "$*" >&2
  exit 1
}

warn() {
  printf '[WARN] %s\n' "$*" >&2
}

have() {
  command -v "$1" >/dev/null 2>&1
}

usage() {
  cat <<'EOF'
Usage: scripts/verify-server-plan.sh [options]

Runs the deployment checks from the Ubuntu 26.04 server plan.

Options:
  --gateway-url URL          Local gateway URL (default: http://127.0.0.1:8080)
  --tailscale-url URL        Tailscale Serve URL to test, e.g. https://host.ts.net
  --skip-agent               Skip the agent-container smoke test
  --skip-gpu-docker          Skip docker --gpus smoke test
  -h, --help                 Show this help
EOF
}

parse_args() {
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --gateway-url)
        [[ "$#" -ge 2 ]] || fail "--gateway-url requires a URL."
        GATEWAY_URL="${2%/}"
        shift 2
        ;;
      --tailscale-url)
        [[ "$#" -ge 2 ]] || fail "--tailscale-url requires a URL."
        TAILSCALE_URL="${2%/}"
        shift 2
        ;;
      --skip-agent)
        SKIP_AGENT=1
        shift
        ;;
      --skip-gpu-docker)
        SKIP_GPU_DOCKER=1
        shift
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        fail "Unknown argument: $1"
        ;;
    esac
  done
}

check_os() {
  [[ -r /etc/os-release ]] || fail "/etc/os-release is missing."
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || fail "Expected Ubuntu, found ${PRETTY_NAME:-unknown}."
  if [[ "${VERSION_ID:-}" == "26.04" ]]; then
    pass "Ubuntu 26.04 detected."
  else
    warn "Expected Ubuntu 26.04, found ${PRETTY_NAME:-Ubuntu ${VERSION_ID:-unknown}}."
  fi
}

check_resources() {
  free -h || true
  df -h / /srv /opt 2>/dev/null || df -h /
  pass "Resource snapshot printed."
}

check_gpu() {
  have nvidia-smi || fail "nvidia-smi is not installed."
  nvidia-smi
  pass "nvidia-smi works."
}

check_docker() {
  have docker || fail "docker is not installed."
  docker compose version >/dev/null || fail "docker compose plugin is not installed."
  docker info >/dev/null || fail "docker daemon is not reachable."
  pass "Docker and Compose are reachable."

  if [[ "${SKIP_GPU_DOCKER}" != "1" ]]; then
    docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu24.04 nvidia-smi >/dev/null
    pass "Docker can access the NVIDIA GPU."
  fi
}

curl_json() {
  local method="$1" url="$2" data="${3:-}"
  if [[ -n "${data}" ]]; then
    curl -fsS -X "${method}" "${url}" -H 'Content-Type: application/json' -d "${data}" >/dev/null
  else
    curl -fsS -X "${method}" "${url}" >/dev/null
  fi
}

check_gateway() {
  curl_json GET "${GATEWAY_URL}/health"
  pass "Gateway /health is healthy."

  curl_json GET "${GATEWAY_URL}/health/ollama"
  pass "Gateway /health/ollama is healthy."

  curl_json POST "${GATEWAY_URL}/status/check"
  pass "Gateway /status/check works."

  curl_json POST "${GATEWAY_URL}/v1/chat/completions" \
    '{"model":"dev","messages":[{"role":"user","content":"Reply with ok."}],"stream":false}'
  pass "Chat completions request works."
}

check_security_posture() {
  local ports
  ports="$(docker ps --format '{{.Names}} {{.Ports}}')"
  printf '%s\n' "${ports}"

  if printf '%s\n' "${ports}" | grep -Eq '0\.0\.0\.0|:::'; then
    fail "A container publishes on a non-loopback address."
  fi

  if printf '%s\n' "${ports}" | grep -q '11434'; then
    fail "Raw Ollama port 11434 is published."
  fi

  printf '%s\n' "${ports}" | grep -q '127.0.0.1:8080->8080/tcp' || \
    fail "Expected only gateway port 127.0.0.1:8080 to be published."

  if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    fail "Raw Ollama is reachable on host loopback."
  fi

  pass "Docker port exposure matches the private gateway plan."
}

check_tailscale() {
  if have tailscale; then
    tailscale status >/dev/null && pass "Tailscale is authenticated." || warn "Tailscale is not authenticated."
    tailscale serve status || warn "Tailscale Serve status failed."
  else
    warn "tailscale CLI is not installed."
  fi

  if [[ -n "${TAILSCALE_URL}" ]]; then
    curl_json GET "${TAILSCALE_URL}/health"
    pass "Tailscale Serve URL is reachable."
  fi
}

check_agent() {
  local script_dir

  if [[ "${SKIP_AGENT}" == "1" ]]; then
    warn "Skipping agent smoke test."
    return
  fi

  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  "${script_dir}/run-agent-container.sh" --project smoke --command \
    'python - <<'"'"'PY'"'"'
import os
import urllib.request

base = os.environ["OPENAI_API_BASE"].rstrip("/")
urllib.request.urlopen(base.removesuffix("/v1") + "/health", timeout=10).read()
assert not os.path.exists("/var/run/docker.sock")
print("agent smoke ok")
PY'
  pass "Agent container can reach the gateway and has no Docker socket."
}

main() {
  parse_args "$@"
  [[ "$(uname -s)" == "Linux" ]] || fail "This verification script must run on Linux."
  GATEWAY_URL="${GATEWAY_URL%/}"
  check_os
  check_resources
  check_gpu
  check_docker
  check_gateway
  check_security_posture
  check_tailscale
  check_agent
  pass "Server plan verification complete."
}

main "$@"
