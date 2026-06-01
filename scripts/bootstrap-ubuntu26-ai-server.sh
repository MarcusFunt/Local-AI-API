#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_NAME="local-ai-api"
INSTALL_ROOT="${LOCAL_AI_API_INSTALL_ROOT:-/opt/local-ai-api}"
REPO_URL="${LOCAL_AI_API_REPO_URL:-https://github.com/MarcusFunt/Local-AI-API.git}"
UPDATE_TIME="${LOCAL_AI_API_UPDATE_TIME:-03:00}"
SKIP_SSH_HARDENING=0
SKIP_FIREWALL=0
SKIP_GPU_CHECK=0
INSTALL_NVIDIA_DRIVER=0
SKIP_GATEWAY_INSTALL=0
SKIP_AGENT_RUNTIME=0

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
Usage: scripts/bootstrap-ubuntu26-ai-server.sh [options]

Bootstraps an Ubuntu 26.04 bare-metal Local AI API server:
  - installs host prerequisites, Tailscale, UFW, and unattended upgrades
  - verifies NVIDIA driver readiness
  - configures repo .env for private gateway defaults
  - installs the agent runtime bridge
  - runs scripts/install-or-update.sh with NVIDIA acceleration

Options:
  --install-root PATH        Repo checkout path (default: /opt/local-ai-api)
  --repo-url URL             Git URL to clone when install root is missing
  --update-time HH:MM        Daily update timer time (default: 03:00)
  --install-nvidia-driver    Run ubuntu-drivers install if nvidia-smi is missing
  --skip-ssh-hardening       Do not enforce SSH key-only login
  --skip-firewall            Do not configure UFW
  --skip-gpu-check           Do not require nvidia-smi before install
  --skip-gateway-install     Prepare host only; do not run install-or-update.sh
  --skip-agent-runtime       Do not create agent user/network/proxy
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
      --repo-url)
        [[ "$#" -ge 2 ]] || die "--repo-url requires a URL."
        REPO_URL="$2"
        shift 2
        ;;
      --update-time)
        [[ "$#" -ge 2 ]] || die "--update-time requires HH:MM."
        UPDATE_TIME="$2"
        shift 2
        ;;
      --install-nvidia-driver)
        INSTALL_NVIDIA_DRIVER=1
        shift
        ;;
      --skip-ssh-hardening)
        SKIP_SSH_HARDENING=1
        shift
        ;;
      --skip-firewall)
        SKIP_FIREWALL=1
        shift
        ;;
      --skip-gpu-check)
        SKIP_GPU_CHECK=1
        shift
        ;;
      --skip-gateway-install)
        SKIP_GATEWAY_INSTALL=1
        shift
        ;;
      --skip-agent-runtime)
        SKIP_AGENT_RUNTIME=1
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

  [[ "${UPDATE_TIME}" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || die "Update time must use HH:MM."
}

sudo_cmd() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

service_user() {
  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    printf '%s\n' "${SUDO_USER}"
  else
    printf '%s\n' "${USER}"
  fi
}

service_group() {
  id -gn "$(service_user)"
}

require_ubuntu_server() {
  [[ "$(uname -s)" == "Linux" ]] || die "This bootstrap script must run on Linux."
  [[ -r /etc/os-release ]] || die "Could not read /etc/os-release."

  # shellcheck disable=SC1091
  source /etc/os-release
  if [[ "${ID:-}" != "ubuntu" ]]; then
    die "Expected Ubuntu; found ${PRETTY_NAME:-unknown OS}."
  fi

  if [[ "${VERSION_ID:-}" != "26.04" ]]; then
    log "WARNING: expected Ubuntu 26.04; found ${PRETTY_NAME:-Ubuntu ${VERSION_ID:-unknown}}."
  else
    log "Detected ${PRETTY_NAME}."
  fi
}

install_base_packages() {
  log "Installing host prerequisites."
  sudo_cmd apt-get update
  sudo_cmd apt-get install -y \
    ca-certificates \
    curl \
    git \
    iproute2 \
    openssh-server \
    socat \
    ufw \
    unattended-upgrades
}

ensure_repo_checkout() {
  local admin group
  admin="$(service_user)"
  group="$(service_group)"

  if [[ -d "${INSTALL_ROOT}/.git" ]]; then
    log "Using existing checkout at ${INSTALL_ROOT}."
  else
    log "Cloning ${REPO_URL} to ${INSTALL_ROOT}."
    sudo_cmd mkdir -p "$(dirname "${INSTALL_ROOT}")"
    sudo_cmd git clone "${REPO_URL}" "${INSTALL_ROOT}"
  fi

  sudo_cmd chown -R "${admin}:${group}" "${INSTALL_ROOT}"
}

set_env_var() {
  local file="$1" key="$2" value="$3" admin group tmp
  admin="$(service_user)"
  group="$(service_group)"
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
        if (!updated) {
          print key "=" value
        }
      }
    ' "${file}" >"${tmp}"
  else
    printf '%s=%s\n' "${key}" "${value}" >"${tmp}"
  fi

  sudo_cmd install -m 0640 -o "${admin}" -g "${group}" "${tmp}" "${file}"
  rm -f "${tmp}"
}

configure_gateway_env() {
  local env_file="${INSTALL_ROOT}/.env"

  if [[ ! -f "${env_file}" ]]; then
    log "Creating ${env_file} from .env.example."
    cp "${INSTALL_ROOT}/.env.example" "${env_file}"
  fi

  log "Applying private gateway defaults to ${env_file}."
  set_env_var "${env_file}" "OLLAMA_BASE_URL" "http://127.0.0.1:11434"
  set_env_var "${env_file}" "HOST" "127.0.0.1"
  set_env_var "${env_file}" "PORT" "8080"
  set_env_var "${env_file}" "DEFAULT_MODEL_PROFILE" "main"
  set_env_var "${env_file}" "OLLAMA_MODELS" "qwen3.5:9b qwen3.5:4b qwen3.5:0.8b"
  set_env_var "${env_file}" "DEFAULT_WHISPER_MODEL" "none"
  set_env_var "${env_file}" "ENABLE_ARBITRARY_MODELS" "false"
  set_env_var "${env_file}" "ENABLE_API_KEY_AUTH" "false"
  set_env_var "${env_file}" "API_KEY" ""
}

authorized_keys_file() {
  local admin home_dir
  admin="$(service_user)"
  home_dir="$(getent passwd "${admin}" | awk -F: '{print $6}')"
  [[ -n "${home_dir}" ]] || die "Could not determine home directory for ${admin}."
  printf '%s/.ssh/authorized_keys\n' "${home_dir}"
}

configure_ssh_hardening() {
  local key_file

  if [[ "${SKIP_SSH_HARDENING}" == "1" ]]; then
    log "Skipping SSH hardening."
    return
  fi

  key_file="$(authorized_keys_file)"
  if [[ ! -s "${key_file}" ]]; then
    die "Refusing to disable SSH passwords because ${key_file} is missing or empty."
  fi

  log "Configuring SSH key-only login."
  sudo_cmd tee /etc/ssh/sshd_config.d/99-local-ai-api.conf >/dev/null <<'EOF'
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF

  sudo_cmd systemctl reload ssh || sudo_cmd systemctl reload sshd
}

install_tailscale() {
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

  if [[ -t 0 ]]; then
    log "Tailscale is not authenticated; running tailscale up."
    sudo_cmd tailscale up
  else
    die "Tailscale is not authenticated. Run 'sudo tailscale up', then rerun this script."
  fi
}

configure_firewall() {
  if [[ "${SKIP_FIREWALL}" == "1" ]]; then
    log "Skipping UFW firewall configuration."
    return
  fi

  ip link show tailscale0 >/dev/null 2>&1 || die "tailscale0 is not present; refusing firewall lockdown."

  log "Configuring UFW default-deny inbound with SSH allowed on tailscale0 only."
  sudo_cmd ufw default deny incoming
  sudo_cmd ufw default allow outgoing
  sudo_cmd ufw allow in on tailscale0 to any port 22 proto tcp comment "Local AI API SSH over Tailscale"
  sudo_cmd ufw allow 41641/udp comment "Tailscale direct connections"
  sudo_cmd ufw delete allow OpenSSH >/dev/null 2>&1 || true
  sudo_cmd ufw delete allow 22/tcp >/dev/null 2>&1 || true
  sudo_cmd ufw --force enable
}

configure_unattended_upgrades() {
  log "Configuring unattended security upgrades."
  sudo_cmd tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
  sudo_cmd systemctl enable --now unattended-upgrades.service >/dev/null 2>&1 || true
}

version_major() {
  printf '%s\n' "${1%%.*}"
}

check_nvidia() {
  local line driver compute_cap driver_major

  if [[ "${SKIP_GPU_CHECK}" == "1" ]]; then
    log "Skipping NVIDIA preflight."
    return
  fi

  if ! have nvidia-smi; then
    if [[ "${INSTALL_NVIDIA_DRIVER}" == "1" ]]; then
      have ubuntu-drivers || sudo_cmd apt-get install -y ubuntu-drivers-common
      log "Installing Ubuntu-recommended NVIDIA driver."
      sudo_cmd ubuntu-drivers install
      die "NVIDIA driver installed. Reboot the server, then rerun this bootstrap script."
    fi
    die "nvidia-smi was not found. Install an NVIDIA driver first, or rerun with --install-nvidia-driver."
  fi

  line="$(nvidia-smi --query-gpu=driver_version,compute_cap --format=csv,noheader 2>/dev/null | head -n 1 || true)"
  if [[ -z "${line}" ]]; then
    log "nvidia-smi is present, but compute capability query failed; continuing."
    nvidia-smi
    return
  fi

  driver="$(printf '%s\n' "${line}" | awk -F, '{gsub(/ /, "", $1); print $1}')"
  compute_cap="$(printf '%s\n' "${line}" | awk -F, '{gsub(/ /, "", $2); print $2}')"
  driver_major="$(version_major "${driver}")"

  if [[ "${driver_major}" =~ ^[0-9]+$ ]] && (( driver_major < 531 )); then
    die "NVIDIA driver ${driver} is too old for the planned Ollama GPU path; use 531+."
  fi

  awk -v cap="${compute_cap}" 'BEGIN { exit (cap + 0 >= 5.0) ? 0 : 1 }' || \
    die "NVIDIA compute capability ${compute_cap} is below the required 5.0."

  log "NVIDIA preflight passed (driver=${driver}, compute_capability=${compute_cap})."
}

install_agent_runtime() {
  if [[ "${SKIP_AGENT_RUNTIME}" == "1" ]]; then
    log "Skipping agent runtime setup."
    return
  fi

  log "Installing agent runtime."
  bash "${INSTALL_ROOT}/scripts/setup-agent-runtime.sh"
}

install_gateway_stack() {
  if [[ "${SKIP_GATEWAY_INSTALL}" == "1" ]]; then
    log "Skipping gateway Docker stack install."
    return
  fi

  log "Installing Local AI API gateway stack."
  cd "${INSTALL_ROOT}"
  bash ./scripts/install-or-update.sh \
    --accelerator nvidia \
    --update-schedule daily \
    --update-time "${UPDATE_TIME}"
}

main() {
  parse_args "$@"
  require_ubuntu_server
  install_base_packages
  ensure_repo_checkout
  configure_gateway_env
  configure_unattended_upgrades
  install_tailscale
  configure_ssh_hardening
  configure_firewall
  check_nvidia
  install_gateway_stack
  install_agent_runtime

  log "Bootstrap complete. Verify with: bash ${INSTALL_ROOT}/scripts/verify-server-plan.sh"
}

main "$@"
