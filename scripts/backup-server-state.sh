#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_ROOT="${LOCAL_AI_API_INSTALL_ROOT:-/opt/local-ai-api}"
WORKSPACE_ROOT="${LOCAL_AI_API_AGENT_WORKSPACE_ROOT:-/srv/agent-workspaces}"
DESTINATION="${LOCAL_AI_API_BACKUP_DESTINATION:-/var/backups/local-ai-api}"

log() {
  printf '[%s] %s\n' "$(date -Iseconds)" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
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
Usage: scripts/backup-server-state.sh [options]

Creates a config/work backup for the Local AI API server. Ollama model volumes
and package caches are intentionally excluded; models should be re-pulled after
restore.

Options:
  --install-root PATH        Repo checkout path (default: /opt/local-ai-api)
  --workspace-root PATH      Agent workspace root (default: /srv/agent-workspaces)
  --destination PATH         Backup directory (default: /var/backups/local-ai-api)
  -h, --help                 Show this help
EOF
}

parse_args() {
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --install-root)
        [[ "$#" -ge 2 ]] || die "--install-root requires a path."
        INSTALL_ROOT="$2"
        shift 2
        ;;
      --workspace-root)
        [[ "$#" -ge 2 ]] || die "--workspace-root requires a path."
        WORKSPACE_ROOT="$2"
        shift 2
        ;;
      --destination)
        [[ "$#" -ge 2 ]] || die "--destination requires a path."
        DESTINATION="$2"
        shift 2
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
}

owner_user() {
  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    printf '%s\n' "${SUDO_USER}"
  else
    printf '%s\n' "${USER}"
  fi
}

copy_if_exists() {
  local source="$1" staging="$2"
  if [[ -e "${source}" ]]; then
    sudo_cmd cp -a --parents "${source}" "${staging}"
  fi
}

main() {
  local timestamp staging archive owner

  parse_args "$@"
  [[ "$(uname -s)" == "Linux" ]] || die "This backup script must run on Linux."

  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  staging="$(mktemp -d)"
  archive="${DESTINATION}/local-ai-api-state-${timestamp}.tar.gz"
  owner="$(owner_user)"

  sudo_cmd mkdir -p "${DESTINATION}"

  sudo_cmd tee "${staging}/MANIFEST.txt" >/dev/null <<EOF
Local AI API server-state backup
Created: ${timestamp}
Included:
- ${INSTALL_ROOT}/.env
- ${INSTALL_ROOT}/.local, if present
- ${WORKSPACE_ROOT}
- selected SSH, UFW, unattended-upgrades, and systemd service files

Excluded:
- Ollama Docker volumes and model files
- ${LOCAL_AI_API_AGENT_CACHE_ROOT:-/srv/agent-caches}
- common dependency/build caches inside workspaces
EOF

  copy_if_exists "${INSTALL_ROOT}/.env" "${staging}"
  copy_if_exists "${INSTALL_ROOT}/.local" "${staging}"
  copy_if_exists "${WORKSPACE_ROOT}" "${staging}"
  copy_if_exists "/etc/ssh/sshd_config.d/99-local-ai-api.conf" "${staging}"
  copy_if_exists "/etc/apt/apt.conf.d/20auto-upgrades" "${staging}"
  copy_if_exists "/etc/systemd/system/local-ai-api-update.service" "${staging}"
  copy_if_exists "/etc/systemd/system/local-ai-api-update.timer" "${staging}"
  copy_if_exists "/etc/systemd/system/local-ai-api-agent-gateway-proxy.service" "${staging}"
  copy_if_exists "/usr/local/lib/local-ai-api/agent-gateway-proxy.sh" "${staging}"

  log "Writing ${archive}."
  sudo_cmd tar -C "${staging}" \
    --exclude='*/node_modules' \
    --exclude='*/.venv' \
    --exclude='*/.pytest_cache' \
    --exclude='*/__pycache__' \
    --exclude='*/target' \
    -czf "${archive}" .

  sudo_cmd chown "${owner}:$(id -gn "${owner}")" "${archive}" || true
  sudo_cmd rm -rf "${staging}"
  log "Backup complete: ${archive}"
}

main "$@"
