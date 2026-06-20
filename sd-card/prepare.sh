#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_NAME="local-ai-api"
INSTALL_ROOT="${LOCAL_AI_API_INSTALL_ROOT:-/opt/local-ai-api}"
REPO_URL="${LOCAL_AI_API_REPO_URL:-https://github.com/MarcusFunt/Local-AI-API.git}"
TAILSCALE_HOSTNAME="${LOCAL_AI_API_TAILSCALE_HOSTNAME:-local-ai-api}"
TAILNET_NAME="${LOCAL_AI_API_TAILNET_NAME:-marcusfunt.github}"
TAILNET_DOMAIN="${LOCAL_AI_API_TAILNET_DOMAIN:-taile97c31.ts.net}"
TAILNET_MAGICDNS_NAMESERVER="${LOCAL_AI_API_TAILNET_MAGICDNS_NAMESERVER:-100.100.100.100}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTHORIZED_KEYS_FILE="${LOCAL_AI_API_AUTHORIZED_KEYS_FILE:-${SCRIPT_DIR}/authorized_keys}"
TAILSCALE_AUTH_KEY_FILE="${LOCAL_AI_API_TAILSCALE_AUTH_KEY_FILE:-${SCRIPT_DIR}/tailscale-auth-key.txt}"

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

admin_user() {
  if [[ -n "${LOCAL_AI_API_ADMIN_USER:-}" ]]; then
    printf '%s\n' "${LOCAL_AI_API_ADMIN_USER}"
  elif [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    printf '%s\n' "${SUDO_USER}"
  elif [[ "${EUID}" -ne 0 ]]; then
    printf '%s\n' "${USER}"
  else
    awk -F: '$3 >= 1000 && $1 != "nobody" { print $1; exit }' /etc/passwd
  fi
}

admin_home() {
  getent passwd "$1" | awk -F: '{print $6}'
}

require_linux_apt() {
  [[ "$(uname -s)" == "Linux" ]] || die "This script must run on Linux."
  have apt-get || die "This script expects an apt-based Linux host."
}

install_base_packages() {
  log "Installing base packages."
  sudo_cmd apt-get update
  sudo_cmd apt-get install -y \
    ca-certificates \
    curl \
    git \
    gnupg \
    iproute2 \
    lsb-release \
    openssh-server \
    rsync \
    sudo \
    ufw \
    unattended-upgrades
}

detect_ubuntu_codename() {
  local codename=""

  # shellcheck disable=SC1091
  source /etc/os-release
  if [[ "${ID:-}" == "linuxmint" && -n "${UBUNTU_CODENAME:-}" ]]; then
    codename="${UBUNTU_CODENAME}"
  elif [[ -n "${VERSION_CODENAME:-}" ]]; then
    codename="${VERSION_CODENAME}"
  elif have lsb_release; then
    codename="$(lsb_release -cs)"
  fi

  [[ -n "${codename}" ]] || die "Could not determine Ubuntu/Debian codename."
  printf '%s\n' "${codename}"
}

install_docker_engine() {
  local codename arch user

  if have docker && docker compose version >/dev/null 2>&1; then
    log "Docker and Docker Compose are already installed."
  else
    log "Installing Docker Engine and Compose plugin."
    codename="$(detect_ubuntu_codename)"
    arch="$(dpkg --print-architecture)"

    sudo_cmd install -m 0755 -d /etc/apt/keyrings
    sudo_cmd rm -f /etc/apt/keyrings/docker.gpg
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
      sudo_cmd gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo_cmd chmod a+r /etc/apt/keyrings/docker.gpg

    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu %s stable\n' \
      "${arch}" "${codename}" | \
      sudo_cmd tee /etc/apt/sources.list.d/docker.list >/dev/null

    sudo_cmd apt-get update
    sudo_cmd apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    sudo_cmd systemctl enable --now docker
  fi

  user="$(admin_user)"
  if [[ -n "${user}" ]]; then
    sudo_cmd usermod -aG docker "${user}" || true
  fi
}

find_sd_repo() {
  local candidate
  for candidate in "${SCRIPT_DIR}" "${SCRIPT_DIR}/.." "${SCRIPT_DIR}/../.."; do
    candidate="$(cd "${candidate}" && pwd)"
    if [[ -d "${candidate}/.git" && -f "${candidate}/compose.yaml" && -d "${candidate}/gateway" ]]; then
      printf '%s\n' "${candidate}"
      return
    fi
  done
}

install_repo() {
  local source_repo user group
  user="$(admin_user)"
  [[ -n "${user}" ]] || die "Could not determine admin user. Set LOCAL_AI_API_ADMIN_USER."
  group="$(id -gn "${user}")"

  source_repo="$(find_sd_repo || true)"
  sudo_cmd mkdir -p "$(dirname "${INSTALL_ROOT}")"

  if [[ -n "${source_repo}" && "${source_repo}" != "${INSTALL_ROOT}" ]]; then
    log "Copying repository from SD card source ${source_repo} to ${INSTALL_ROOT}."
    sudo_cmd mkdir -p "${INSTALL_ROOT}"
    sudo_cmd rsync -a --delete \
      --exclude .env \
      --exclude .env.local \
      --exclude .local/ \
      --exclude .pytest_cache/ \
      --exclude __pycache__/ \
      "${source_repo}/" "${INSTALL_ROOT}/"
  elif [[ -d "${INSTALL_ROOT}/.git" ]]; then
    log "Using existing repository at ${INSTALL_ROOT}."
  else
    log "Cloning ${REPO_URL} to ${INSTALL_ROOT}."
    sudo_cmd git clone "${REPO_URL}" "${INSTALL_ROOT}"
  fi

  sudo_cmd chown -R "${user}:${group}" "${INSTALL_ROOT}"
}

set_env_var() {
  local file="$1" key="$2" value="$3" tmp
  tmp="$(mktemp)"

  if [[ -f "${file}" ]]; then
    awk -v key="${key}" -v value="${value}" '
      BEGIN { updated = 0 }
      $0 ~ "^" key "=" {
        print key "=" value
        updated = 1
        next
      }
      { print }
      END {
        if (!updated) print key "=" value
      }
    ' "${file}" >"${tmp}"
  else
    printf '%s=%s\n' "${key}" "${value}" >"${tmp}"
  fi

  sudo_cmd install -m 0640 "${tmp}" "${file}"
  rm -f "${tmp}"
}

configure_env() {
  local env_file="${INSTALL_ROOT}/.env" user group
  user="$(admin_user)"
  group="$(id -gn "${user}")"

  if [[ ! -f "${env_file}" ]]; then
    log "Creating ${env_file} from .env.example."
    sudo_cmd cp "${INSTALL_ROOT}/.env.example" "${env_file}"
  fi

  log "Writing private gateway defaults."
  set_env_var "${env_file}" "OLLAMA_BASE_URL" "http://127.0.0.1:11434"
  set_env_var "${env_file}" "HOST" "127.0.0.1"
  set_env_var "${env_file}" "PORT" "8080"
  set_env_var "${env_file}" "DEFAULT_MODEL_PROFILE" "main"
  set_env_var "${env_file}" "OLLAMA_MODELS" "qwen3.5:9b qwen3.5:4b qwen3.5:0.8b qwen3:14b qwen3:8b"
  set_env_var "${env_file}" "DEFAULT_WHISPER_MODEL" "none"
  set_env_var "${env_file}" "ENABLE_ARBITRARY_MODELS" "false"
  set_env_var "${env_file}" "AGENT_ZERO_ENABLED" "true"
  set_env_var "${env_file}" "AGENT_ZERO_PORT" "50080"
  set_env_var "${env_file}" "AGENT_ZERO_TAILSCALE_HTTPS_PORT" "8443"
  set_env_var "${env_file}" "ENABLE_API_KEY_AUTH" "false"
  set_env_var "${env_file}" "API_KEY" ""
  sudo_cmd chown "${user}:${group}" "${env_file}"
}

configure_ssh() {
  local user home ssh_dir key_file installed_keys=0
  user="$(admin_user)"
  home="$(admin_home "${user}")"
  [[ -n "${home}" ]] || die "Could not determine home directory for ${user}."
  ssh_dir="${home}/.ssh"
  key_file="${ssh_dir}/authorized_keys"

  log "Enabling OpenSSH server."
  sudo_cmd systemctl enable --now ssh || sudo_cmd systemctl enable --now sshd
  sudo_cmd install -d -m 0700 -o "${user}" -g "$(id -gn "${user}")" "${ssh_dir}"

  if [[ -s "${AUTHORIZED_KEYS_FILE}" ]]; then
    log "Installing SSH public keys from ${AUTHORIZED_KEYS_FILE}."
    sudo_cmd touch "${key_file}"
    sudo_cmd chown "${user}:$(id -gn "${user}")" "${key_file}"
    sudo_cmd chmod 0600 "${key_file}"
    cat "${AUTHORIZED_KEYS_FILE}" | sudo_cmd tee -a "${key_file}" >/dev/null
    sudo_cmd awk '!seen[$0]++' "${key_file}" | sudo_cmd tee "${key_file}.tmp" >/dev/null
    sudo_cmd mv "${key_file}.tmp" "${key_file}"
    sudo_cmd chown "${user}:$(id -gn "${user}")" "${key_file}"
    sudo_cmd chmod 0600 "${key_file}"
  fi

  if [[ -s "${key_file}" ]]; then
    installed_keys=1
  fi

  if [[ "${installed_keys}" == "1" ]]; then
    log "Configuring SSH key-only login."
    sudo_cmd tee /etc/ssh/sshd_config.d/99-local-ai-api.conf >/dev/null <<'EOF'
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
  else
    log "No SSH authorized_keys found; leaving password login unchanged to avoid lockout."
    sudo_cmd tee /etc/ssh/sshd_config.d/99-local-ai-api.conf >/dev/null <<'EOF'
PubkeyAuthentication yes
PermitRootLogin prohibit-password
EOF
  fi

  sudo_cmd systemctl reload ssh || sudo_cmd systemctl reload sshd
}

install_tailscale() {
  local auth_key=""

  if have tailscale; then
    log "Tailscale is already installed."
  else
    log "Installing Tailscale."
    curl -fsSL https://tailscale.com/install.sh | sudo_cmd sh
  fi

  sudo_cmd systemctl enable --now tailscaled || true

  if tailscale status >/dev/null 2>&1; then
    log "Tailscale is authenticated."
    return
  fi

  if [[ -n "${TAILSCALE_AUTH_KEY:-}" ]]; then
    auth_key="${TAILSCALE_AUTH_KEY}"
  elif [[ -s "${TAILSCALE_AUTH_KEY_FILE}" ]]; then
    auth_key="$(tr -d '[:space:]' <"${TAILSCALE_AUTH_KEY_FILE}")"
  fi

  if [[ -n "${auth_key}" ]]; then
    log "Authenticating Tailscale using auth key from environment or SD card file."
    sudo_cmd tailscale up --auth-key="${auth_key}" --hostname="${TAILSCALE_HOSTNAME}" --ssh
  elif [[ -t 0 ]]; then
    log "Tailscale is not authenticated; opening interactive tailscale up."
    sudo_cmd tailscale up --hostname="${TAILSCALE_HOSTNAME}" --ssh
  else
    die "Tailscale is not authenticated. Put an auth key in tailscale-auth-key.txt or run interactively."
  fi
}

configure_firewall() {
  if [[ "${LOCAL_AI_API_SKIP_FIREWALL:-0}" == "1" ]]; then
    log "Skipping UFW configuration."
    return
  fi

  ip link show tailscale0 >/dev/null 2>&1 || die "tailscale0 is missing; refusing to enable firewall lockdown."

  log "Configuring UFW: SSH only on tailscale0; no raw Ollama exposure."
  sudo_cmd ufw default deny incoming
  sudo_cmd ufw default allow outgoing
  sudo_cmd ufw allow in on tailscale0 to any port 22 proto tcp comment "Local AI API SSH over Tailscale"
  sudo_cmd ufw allow 41641/udp comment "Tailscale direct connections"
  sudo_cmd ufw delete allow OpenSSH >/dev/null 2>&1 || true
  sudo_cmd ufw delete allow 22/tcp >/dev/null 2>&1 || true
  sudo_cmd ufw --force enable
}

install_start_command() {
  if [[ -f "${SCRIPT_DIR}/start.sh" ]]; then
    log "Installing local-ai-start command."
    sudo_cmd install -m 0755 "${SCRIPT_DIR}/start.sh" /usr/local/bin/local-ai-start
  fi
}

print_summary() {
  local user ip4="" expected_dns=""
  user="$(admin_user)"
  ip4="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
  [[ -n "${TAILNET_DOMAIN}" ]] && expected_dns="${TAILSCALE_HOSTNAME}.${TAILNET_DOMAIN}"

  log "Prepare complete."
  [[ -n "${TAILNET_NAME}" ]] && log "Tailnet: ${TAILNET_NAME}"
  [[ -n "${expected_dns}" ]] && log "Expected MagicDNS name: ${expected_dns}"
  [[ -n "${TAILNET_MAGICDNS_NAMESERVER}" ]] && log "MagicDNS resolver: ${TAILNET_MAGICDNS_NAMESERVER}"
  [[ -n "${ip4}" ]] && log "SSH target: ssh ${user}@${ip4}"
  log "Start command: sudo local-ai-start"
}

main() {
  require_linux_apt
  install_base_packages
  install_docker_engine
  install_repo
  configure_env
  configure_ssh
  install_tailscale
  configure_firewall
  install_start_command
  print_summary
}

main "$@"
