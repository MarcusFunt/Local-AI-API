#!/usr/bin/env bash
set -Eeuo pipefail

AGENT_USER="${LOCAL_AI_API_AGENT_USER:-agent}"
WORKSPACE_ROOT="${LOCAL_AI_API_AGENT_WORKSPACE_ROOT:-/srv/agent-workspaces}"
CACHE_ROOT="${LOCAL_AI_API_AGENT_CACHE_ROOT:-/srv/agent-caches}"
AGENT_NETWORK="${LOCAL_AI_API_AGENT_NETWORK:-local-ai-agents}"
NETWORK_SUBNET="${LOCAL_AI_API_AGENT_SUBNET:-172.30.50.0/24}"
PROXY_PORT="${LOCAL_AI_API_AGENT_PROXY_PORT:-18080}"
SKIP_PROXY=0

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

sudo_cmd() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

usage() {
  cat <<'EOF'
Usage: scripts/setup-agent-runtime.sh [options]

Creates the low-privilege agent user, agent workspace/cache roots, a dedicated
Docker bridge network, and an optional gateway-only proxy for agent containers.

Options:
  --agent-user USER          Low-privilege host user (default: agent)
  --workspace-root PATH      Agent workspaces root (default: /srv/agent-workspaces)
  --cache-root PATH          Agent package cache root (default: /srv/agent-caches)
  --network NAME             Docker network name (default: local-ai-agents)
  --subnet CIDR              Docker network subnet (default: 172.30.50.0/24)
  --proxy-port PORT          Gateway proxy port on the agent bridge (default: 18080)
  --skip-proxy               Do not install the systemd socat proxy
  -h, --help                 Show this help
EOF
}

parse_args() {
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --agent-user)
        [[ "$#" -ge 2 ]] || die "--agent-user requires a value."
        AGENT_USER="$2"
        shift 2
        ;;
      --workspace-root)
        [[ "$#" -ge 2 ]] || die "--workspace-root requires a path."
        WORKSPACE_ROOT="$2"
        shift 2
        ;;
      --cache-root)
        [[ "$#" -ge 2 ]] || die "--cache-root requires a path."
        CACHE_ROOT="$2"
        shift 2
        ;;
      --network)
        [[ "$#" -ge 2 ]] || die "--network requires a name."
        AGENT_NETWORK="$2"
        shift 2
        ;;
      --subnet)
        [[ "$#" -ge 2 ]] || die "--subnet requires a CIDR."
        NETWORK_SUBNET="$2"
        shift 2
        ;;
      --proxy-port)
        [[ "$#" -ge 2 ]] || die "--proxy-port requires a port."
        PROXY_PORT="$2"
        shift 2
        ;;
      --skip-proxy)
        SKIP_PROXY=1
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

  [[ "${PROXY_PORT}" =~ ^[0-9]+$ ]] || die "Proxy port must be numeric."
}

ensure_agent_user() {
  if id "${AGENT_USER}" >/dev/null 2>&1; then
    log "Agent user ${AGENT_USER} already exists."
  else
    log "Creating low-privilege agent user ${AGENT_USER}."
    sudo_cmd useradd \
      --create-home \
      --home-dir "${WORKSPACE_ROOT}" \
      --shell /usr/sbin/nologin \
      --user-group \
      "${AGENT_USER}"
  fi

  sudo_cmd mkdir -p "${WORKSPACE_ROOT}" "${CACHE_ROOT}"
  sudo_cmd chown -R "${AGENT_USER}:${AGENT_USER}" "${WORKSPACE_ROOT}" "${CACHE_ROOT}"
  sudo_cmd chmod 0750 "${WORKSPACE_ROOT}" "${CACHE_ROOT}"
}

ensure_docker_network() {
  have docker || die "Docker is required before setting up agent runtime."

  if docker network inspect "${AGENT_NETWORK}" >/dev/null 2>&1; then
    log "Docker network ${AGENT_NETWORK} already exists."
  else
    log "Creating Docker network ${AGENT_NETWORK} (${NETWORK_SUBNET})."
    docker network create --subnet "${NETWORK_SUBNET}" "${AGENT_NETWORK}" >/dev/null
  fi
}

network_gateway_ip() {
  docker network inspect \
    --format '{{(index .IPAM.Config 0).Gateway}}' \
    "${AGENT_NETWORK}"
}

install_proxy() {
  local gateway_ip script_path unit_path

  if [[ "${SKIP_PROXY}" == "1" ]]; then
    log "Skipping agent gateway proxy installation."
    return
  fi

  have socat || sudo_cmd apt-get install -y socat

  gateway_ip="$(network_gateway_ip)"
  script_path="/usr/local/lib/local-ai-api/agent-gateway-proxy.sh"
  unit_path="/etc/systemd/system/local-ai-api-agent-gateway-proxy.service"

  log "Installing agent gateway proxy on ${gateway_ip}:${PROXY_PORT}."
  sudo_cmd mkdir -p "$(dirname "${script_path}")"
  sudo_cmd tee "${script_path}" >/dev/null <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail

AGENT_NETWORK="${AGENT_NETWORK}"
PROXY_PORT="${PROXY_PORT}"

gateway_ip="\$(docker network inspect --format '{{(index .IPAM.Config 0).Gateway}}' "\${AGENT_NETWORK}")"
exec socat "TCP-LISTEN:\${PROXY_PORT},bind=\${gateway_ip},fork,reuseaddr" TCP:127.0.0.1:8080
EOF
  sudo_cmd chmod 0755 "${script_path}"

  if have systemctl; then
    sudo_cmd tee "${unit_path}" >/dev/null <<EOF
[Unit]
Description=Local AI API gateway proxy for agent containers
After=docker.service
Requires=docker.service

[Service]
Type=simple
ExecStart=${script_path}
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
    sudo_cmd systemctl daemon-reload
    sudo_cmd systemctl enable --now local-ai-api-agent-gateway-proxy.service
  else
    log "systemd not found; start ${script_path} manually if agent containers need local gateway access."
  fi

  log "Agent containers can use OPENAI_API_BASE=http://${gateway_ip}:${PROXY_PORT}/v1"
}

main() {
  parse_args "$@"
  [[ "$(uname -s)" == "Linux" ]] || die "This script must run on Linux."
  ensure_agent_user
  ensure_docker_network
  install_proxy
}

main "$@"
