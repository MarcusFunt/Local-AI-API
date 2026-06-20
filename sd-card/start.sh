#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_NAME="local-ai-api"
INSTALL_ROOT="${LOCAL_AI_API_INSTALL_ROOT:-/opt/local-ai-api}"
ACCELERATOR="${LOCAL_AI_API_ACCELERATOR:-auto}"
UPDATE_SCHEDULE="${LOCAL_AI_API_UPDATE_SCHEDULE:-daily}"
UPDATE_TIME="${LOCAL_AI_API_UPDATE_TIME:-03:00}"
SYNC_REPO="${LOCAL_AI_API_START_SYNC_REPO:-0}"
TAILSCALE_HOSTNAME="${LOCAL_AI_API_TAILSCALE_HOSTNAME:-local-ai-api}"
TAILNET_DOMAIN="${LOCAL_AI_API_TAILNET_DOMAIN:-taile97c31.ts.net}"
TAILNET_MAGICDNS_NAMESERVER="${LOCAL_AI_API_TAILNET_MAGICDNS_NAMESERVER:-100.100.100.100}"

log() {
  printf '[%s] %s\n' "$(date -Iseconds)" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

require_linux() {
  [[ "$(uname -s)" == "Linux" ]] || die "This script must run on Linux."
}

sudo_cmd() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

installer_args() {
  printf '%s\n' --accelerator "${ACCELERATOR}"
  printf '%s\n' --update-schedule "${UPDATE_SCHEDULE}"
  printf '%s\n' --update-time "${UPDATE_TIME}"
  if [[ "${SYNC_REPO}" != "1" ]]; then
    printf '%s\n' --skip-repo-sync
  fi
}

print_summary() {
  local ts_name ts_ip gateway_url agent_url ssh_user fallback_dns
  ssh_user="${LOCAL_AI_API_ADMIN_USER:-${SUDO_USER:-$(id -un)}}"
  ts_name="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("Self", {}).get("DNSName", "").rstrip("."))' 2>/dev/null || true)"
  ts_ip="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
  fallback_dns=""
  [[ -n "${TAILNET_DOMAIN}" ]] && fallback_dns="${TAILSCALE_HOSTNAME}.${TAILNET_DOMAIN}"
  [[ -z "${ts_name}" && -n "${fallback_dns}" ]] && ts_name="${fallback_dns}"
  gateway_url="http://127.0.0.1:8080/status"
  agent_url="http://127.0.0.1:${AGENT_ZERO_PORT:-50080}"

  log "Start complete."
  log "Local gateway status: ${gateway_url}"
  log "Local Agent Zero UI: ${agent_url}"
  [[ -n "${ts_name}" ]] && log "Tailscale machine: ${ts_name}"
  [[ -n "${ts_name}" ]] && log "Gateway over Tailscale Serve: https://${ts_name}/status"
  [[ -n "${TAILNET_MAGICDNS_NAMESERVER}" ]] && log "MagicDNS resolver: ${TAILNET_MAGICDNS_NAMESERVER}"
  [[ -n "${ts_ip}" ]] && log "SSH over Tailscale: ssh ${ssh_user}@${ts_ip}"
  tailscale serve status 2>/dev/null || true
}

main() {
  local -a args

  require_linux
  [[ -d "${INSTALL_ROOT}/.git" ]] || die "${INSTALL_ROOT} is not prepared. Run prepare.sh first."
  [[ -x "${INSTALL_ROOT}/scripts/install-or-update.sh" ]] || die "Missing installer at ${INSTALL_ROOT}/scripts/install-or-update.sh."

  cd "${INSTALL_ROOT}"
  mapfile -t args < <(installer_args)

  log "Starting ${PROJECT_NAME} with accelerator=${ACCELERATOR}."
  log "Repository sync is $([[ "${SYNC_REPO}" == "1" ]] && printf enabled || printf disabled)."
  bash ./scripts/install-or-update.sh "${args[@]}" "$@"
  print_summary
}

main "$@"
