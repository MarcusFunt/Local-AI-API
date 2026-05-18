#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_NAME="local-ai-api"
REMOTE_NAME="${REMOTE_NAME:-origin}"
REMOTE_BRANCH="${REMOTE_BRANCH:-main}"
GATEWAY_HEALTH_URL="${GATEWAY_HEALTH_URL:-http://127.0.0.1:8080/health}"
OLLAMA_HEALTH_URL="${OLLAMA_HEALTH_URL:-http://127.0.0.1:8080/health/ollama}"
LOCK_FILE="${LOCK_FILE:-/tmp/local-ai-api-install.lock}"

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

require_linux() {
  [[ "$(uname -s)" == "Linux" ]] || die "This installer targets Linux Mint/Ubuntu-style Linux hosts."
}

sudo_cmd() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

acquire_lock() {
  if [[ "${LOCAL_AI_API_LOCKED:-}" == "1" ]]; then
    return
  fi

  exec 9>"${LOCK_FILE}"
  if ! flock -n 9; then
    die "Another ${PROJECT_NAME} install/update run is already active."
  fi
  export LOCAL_AI_API_LOCKED=1
}

repo_root() {
  git rev-parse --show-toplevel 2>/dev/null || die "Run this script from inside the ${PROJECT_NAME} Git repository."
}

script_hash() {
  sha256sum "$1" | awk '{print $1}'
}

sync_repo_to_github() {
  local root="$1"
  local script_path="$2"
  local before_hash after_hash
  shift 2

  before_hash="$(script_hash "${script_path}")"

  log "Fetching ${REMOTE_NAME}/${REMOTE_BRANCH}."
  git -C "${root}" fetch --prune "${REMOTE_NAME}" "${REMOTE_BRANCH}"

  log "Forcing tracked files to ${REMOTE_NAME}/${REMOTE_BRANCH}."
  git -C "${root}" reset --hard "${REMOTE_NAME}/${REMOTE_BRANCH}"

  log "Cleaning untracked files while preserving runtime env files."
  git -C "${root}" clean -fdx \
    -e .env \
    -e .env.local \
    -e '.env.*.local' \
    -e compose.local.yaml \
    -e '.local/'

  after_hash="$(script_hash "${script_path}")"
  if [[ "${before_hash}" != "${after_hash}" && "${LOCAL_AI_API_REEXECED:-}" != "1" ]]; then
    log "Installer changed during update; re-executing the updated script."
    export LOCAL_AI_API_REEXECED=1
    exec "${script_path}" "$@"
  fi
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

  [[ -n "${codename}" ]] || die "Could not determine Ubuntu/Debian codename for Docker apt repository."
  printf '%s\n' "${codename}"
}

install_docker_engine() {
  local codename arch

  if have docker && docker compose version >/dev/null 2>&1; then
    log "Docker and Docker Compose are already installed."
    return
  fi

  if [[ "${LOCAL_AI_API_SYSTEMD:-}" == "1" ]]; then
    die "Docker is not installed; run ${PROJECT_NAME} installer manually once before scheduled updates."
  fi

  log "Installing Docker Engine and Compose plugin."
  codename="$(detect_ubuntu_codename)"
  arch="$(dpkg --print-architecture)"

  sudo_cmd apt-get update
  sudo_cmd apt-get install -y ca-certificates curl gnupg
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

  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    sudo_cmd usermod -aG docker "${SUDO_USER}"
  elif [[ "${EUID}" -ne 0 ]]; then
    sudo_cmd usermod -aG docker "${USER}"
  fi
}

docker_cmd() {
  if docker info >/dev/null 2>&1; then
    docker "$@"
  else
    sudo_cmd docker "$@"
  fi
}

compose_cmd() {
  docker_cmd compose "$@"
}

nvidia_docker_works() {
  docker_cmd run --rm --gpus all hello-world >/dev/null 2>&1
}

install_nvidia_container_toolkit() {
  if [[ "${LOCAL_AI_API_SYSTEMD:-}" == "1" ]]; then
    log "NVIDIA Docker runtime is not configured; skipping toolkit install during scheduled run."
    return 1
  fi

  log "Installing NVIDIA Container Toolkit for Docker GPU access."

  sudo_cmd apt-get update
  sudo_cmd apt-get install -y curl gnupg
  sudo_cmd rm -f /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
    sudo_cmd gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo_cmd tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null

  sudo_cmd apt-get update
  sudo_cmd apt-get install -y nvidia-container-toolkit
  sudo_cmd nvidia-ctk runtime configure --runtime=docker
  sudo_cmd systemctl restart docker
}

detect_accelerator() {
  if have nvidia-smi; then
    if nvidia_docker_works; then
      printf '%s\n' "nvidia"
      return
    fi

    install_nvidia_container_toolkit || true
    if nvidia_docker_works; then
      printf '%s\n' "nvidia"
      return
    fi

    log "NVIDIA GPU detected, but Docker GPU access still failed; falling back to CPU."
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
}

build_and_test_gateway_image() {
  local root="$1"
  shift
  local compose_args=("$@")

  log "Validating Docker Compose configuration."
  compose_cmd "${compose_args[@]}" config >/dev/null

  log "Building gateway image."
  compose_cmd "${compose_args[@]}" build gateway

  log "Running tests inside the freshly built gateway image."
  docker_cmd run --rm \
    --entrypoint python \
    --workdir /app \
    local-ai-api-gateway:latest \
    -m pytest tests -v
}

start_stack() {
  shift
  local compose_args=("$@")

  log "Starting Ollama."
  compose_cmd "${compose_args[@]}" up -d ollama

  log "Pulling required Ollama models."
  compose_cmd "${compose_args[@]}" run --rm model-init

  log "Starting gateway."
  compose_cmd "${compose_args[@]}" up -d gateway
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

install_tailscale() {
  if have tailscale; then
    log "Tailscale is already installed."
  else
    if [[ "${LOCAL_AI_API_SYSTEMD:-}" == "1" ]]; then
      log "Tailscale is not installed; skipping install during scheduled run."
      return 1
    fi
    log "Installing Tailscale."
    curl -fsSL https://tailscale.com/install.sh | sudo_cmd sh
  fi

  if have systemctl; then
    if [[ "${LOCAL_AI_API_SYSTEMD:-}" != "1" ]]; then
      sudo_cmd systemctl enable --now tailscaled || true
    fi
  fi
}

tailscale_is_authenticated() {
  tailscale status >/dev/null 2>&1
}

configure_tailscale_serve() {
  install_tailscale || return

  if ! tailscale_is_authenticated; then
    if [[ -t 0 && "${LOCAL_AI_API_SYSTEMD:-}" != "1" ]]; then
      log "Tailscale is not authenticated; running tailscale up."
      sudo_cmd tailscale up
    else
      log "Tailscale is installed but not authenticated; skipping serve configuration in non-interactive mode."
      return
    fi
  fi

  log "Configuring Tailscale Serve for ${GATEWAY_HEALTH_URL%/health}."
  if ! tailscale serve --bg http://127.0.0.1:8080; then
    if [[ "${LOCAL_AI_API_SYSTEMD:-}" == "1" ]]; then
      log "Tailscale Serve command failed during scheduled run; leaving current serve config unchanged."
      return
    fi
    sudo_cmd tailscale serve --bg http://127.0.0.1:8080
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

install_systemd_units() {
  local root="$1"
  local user group

  if [[ "${LOCAL_AI_API_SYSTEMD:-}" == "1" ]]; then
    log "Running under systemd; skipping systemd unit installation."
    return
  fi

  have systemctl || {
    log "systemd is not available; skipping scheduled update timer."
    return
  }

  user="$(service_user)"
  group="$(service_group)"

  log "Installing systemd update service and timer."
  sudo_cmd tee /etc/systemd/system/local-ai-api-update.service >/dev/null <<EOF
[Unit]
Description=Update and restart Local AI API Docker stack
Wants=network-online.target docker.service
After=network-online.target docker.service

[Service]
Type=oneshot
User=${user}
Group=${group}
SupplementaryGroups=docker
WorkingDirectory=${root}
Environment=LOCAL_AI_API_SYSTEMD=1
ExecStart=${root}/scripts/install-or-update.sh
EOF

  sudo_cmd tee /etc/systemd/system/local-ai-api-update.timer >/dev/null <<'EOF'
[Unit]
Description=Run Local AI API updater at boot and daily

[Timer]
OnBootSec=5min
OnCalendar=daily
Persistent=true
Unit=local-ai-api-update.service

[Install]
WantedBy=timers.target
EOF

  sudo_cmd systemctl daemon-reload
  sudo_cmd systemctl enable --now local-ai-api-update.timer
}

main() {
  local root script_path accelerator
  local -a compose_args

  require_linux
  acquire_lock

  root="$(repo_root)"
  cd "${root}"
  script_path="${root}/scripts/install-or-update.sh"

  sync_repo_to_github "${root}" "${script_path}" "$@"

  install_docker_engine

  accelerator="$(detect_accelerator)"
  log "Selected accelerator profile: ${accelerator}."
  mapfile -t compose_args < <(compose_files_for_accelerator "${root}" "${accelerator}")

  build_and_test_gateway_image "${root}" "${compose_args[@]}"
  start_stack "${root}" "${compose_args[@]}"

  wait_for_url "${GATEWAY_HEALTH_URL}" "Gateway health"
  wait_for_url "${OLLAMA_HEALTH_URL}" "Ollama health"

  configure_tailscale_serve
  install_systemd_units "${root}"

  log "${PROJECT_NAME} install/update complete."
}

main "$@"
