#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_NAME="local-ai-api"
REMOTE_NAME="${REMOTE_NAME:-origin}"
REMOTE_BRANCH="${REMOTE_BRANCH:-main}"
GATEWAY_HEALTH_URL="${GATEWAY_HEALTH_URL:-http://127.0.0.1:8080/health}"
OLLAMA_HEALTH_URL="${OLLAMA_HEALTH_URL:-http://127.0.0.1:8080/health/ollama}"
AGENT_ZERO_PORT="${AGENT_ZERO_PORT:-50080}"
AGENT_ZERO_TAILSCALE_HTTPS_PORT="${AGENT_ZERO_TAILSCALE_HTTPS_PORT:-8443}"
LOCK_FILE="${LOCK_FILE:-/tmp/local-ai-api-install.lock}"
ACCELERATOR="${LOCAL_AI_API_ACCELERATOR:-auto}"
SKIP_REPO_SYNC="${LOCAL_AI_API_SKIP_REPO_SYNC:-0}"
SKIP_TAILSCALE_SERVE="${LOCAL_AI_API_SKIP_TAILSCALE_SERVE:-0}"
NO_SCHEDULED_TASK="${LOCAL_AI_API_NO_SCHEDULED_TASK:-0}"
LOW_COMPUTE="${LOCAL_AI_API_LOW_COMPUTE:-0}"
AGENT_ZERO_ENABLED=1
REPO_ROOT_DIR=""
UPDATE_SCHEDULE="${LOCAL_AI_API_UPDATE_SCHEDULE:-default}"
UPDATE_TIME="${LOCAL_AI_API_UPDATE_TIME:-03:00}"
UPDATE_WEEKLY_DAY="${LOCAL_AI_API_UPDATE_WEEKLY_DAY:-Sunday}"
UPDATE_EVERY_HOURS="${LOCAL_AI_API_UPDATE_EVERY_HOURS:-6}"

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

write_update_marker() {
  local status="$1"
  local error="${2:-}"
  [[ -n "${REPO_ROOT_DIR}" ]] || return 0
  local dir="${REPO_ROOT_DIR}/.local"
  mkdir -p "${dir}" 2>/dev/null || return 0
  local scheduled="false"
  if [[ "${LOCAL_AI_API_SYSTEMD:-}" == "1" ]]; then
    scheduled="true"
  fi
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if [[ -n "${error}" ]]; then
    printf '{"status":"%s","scheduled":%s,"finished_at":"%s","error":"%s"}\n' \
      "${status}" "${scheduled}" "${ts}" "${error}" > "${dir}/last-update.json"
  else
    printf '{"status":"%s","scheduled":%s,"finished_at":"%s"}\n' \
      "${status}" "${scheduled}" "${ts}" > "${dir}/last-update.json"
  fi
}

on_exit() {
  local rc=$?
  if [[ "${rc}" == "0" ]]; then
    write_update_marker "passed"
  else
    write_update_marker "failed" "install/update failed (exit ${rc}); see: journalctl -u local-ai-api-update.service"
  fi
}
trap on_exit EXIT

parse_args() {
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --accelerator)
        [[ "$#" -ge 2 ]] || die "--accelerator requires a value."
        ACCELERATOR="$2"
        shift 2
        ;;
      --skip-repo-sync)
        SKIP_REPO_SYNC=1
        shift
        ;;
      --skip-tailscale-serve)
        SKIP_TAILSCALE_SERVE=1
        shift
        ;;
      --low-compute)
        LOW_COMPUTE=1
        shift
        ;;
      --no-scheduled-task)
        NO_SCHEDULED_TASK=1
        shift
        ;;
      --scheduled-run)
        export LOCAL_AI_API_SYSTEMD=1
        shift
        ;;
      --update-schedule)
        [[ "$#" -ge 2 ]] || die "--update-schedule requires a value."
        UPDATE_SCHEDULE="$2"
        shift 2
        ;;
      --update-time)
        [[ "$#" -ge 2 ]] || die "--update-time requires a value."
        UPDATE_TIME="$2"
        shift 2
        ;;
      --weekly-day)
        [[ "$#" -ge 2 ]] || die "--weekly-day requires a value."
        UPDATE_WEEKLY_DAY="$2"
        shift 2
        ;;
      --every-hours)
        [[ "$#" -ge 2 ]] || die "--every-hours requires a value."
        UPDATE_EVERY_HOURS="$2"
        shift 2
        ;;
      *)
        die "Unknown argument: $1"
        ;;
    esac
  done

  validate_options
}

validate_options() {
  case "${ACCELERATOR}" in
    auto | cpu | nvidia | amd) ;;
    *) die "Unknown accelerator: ${ACCELERATOR}" ;;
  esac

  case "${UPDATE_SCHEDULE}" in
    default | none | logon | daily | weekly | every-hours) ;;
    *) die "Unknown update schedule: ${UPDATE_SCHEDULE}" ;;
  esac

  [[ "${UPDATE_TIME}" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || die "Update time must use 24-hour HH:mm format."

  case "${UPDATE_WEEKLY_DAY}" in
    Sunday | Monday | Tuesday | Wednesday | Thursday | Friday | Saturday) ;;
    *) die "Weekly day must be a full English weekday name." ;;
  esac

  [[ "${UPDATE_EVERY_HOURS}" =~ ^[0-9]+$ ]] || die "Every-hours interval must be numeric."
  UPDATE_EVERY_HOURS=$((10#${UPDATE_EVERY_HOURS}))
  if (( UPDATE_EVERY_HOURS < 1 || UPDATE_EVERY_HOURS > 24 || 24 % UPDATE_EVERY_HOURS != 0 )); then
    die "Every-hours update interval must divide evenly into 24."
  fi
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

  if [[ "${SKIP_REPO_SYNC}" == "1" ]]; then
    log "Skipping repository sync because --skip-repo-sync was provided."
    return
  fi

  before_hash="$(script_hash "${script_path}")"

  log "Fetching ${REMOTE_NAME}/${REMOTE_BRANCH}."
  git -C "${root}" fetch --prune "${REMOTE_NAME}" "${REMOTE_BRANCH}"

  if [[ "${LOCAL_AI_API_SYSTEMD:-}" == "1" ]]; then
    if [[ -n "$(git -C "${root}" status --porcelain)" ]]; then
      die "Scheduled updates refuse a dirty checkout; resolve or commit local changes first."
    fi
    current_branch="$(git -C "${root}" branch --show-current)"
    if [[ "${current_branch}" != "${REMOTE_BRANCH}" ]]; then
      die "Scheduled updates require the approved ${REMOTE_BRANCH} branch (current: ${current_branch:-detached})."
    fi
    if ! git -C "${root}" merge-base --is-ancestor HEAD "${REMOTE_NAME}/${REMOTE_BRANCH}"; then
      die "Scheduled updates require a fast-forward path to ${REMOTE_NAME}/${REMOTE_BRANCH}."
    fi
    log "Fast-forwarding scheduled update to ${REMOTE_NAME}/${REMOTE_BRANCH}."
    git -C "${root}" pull --ff-only "${REMOTE_NAME}" "${REMOTE_BRANCH}"
    return
  fi

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
  if [[ "${ACCELERATOR}" != "auto" ]]; then
    printf '%s\n' "${ACCELERATOR}"
    return
  fi

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
  if [[ "${AGENT_ZERO_ENABLED}" == "1" ]]; then
    printf '%s\n' -f "${root}/compose.agent-zero.yaml"
  fi
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
    local-ai-api-gateway:1.0.0 \
    -m pytest tests -v
}

remove_legacy_repo_updater() {
  local container_ids container_id
  local -a ids=()
  if ! container_ids="$(docker_cmd ps -aq \
    --filter "label=com.docker.compose.project=local-ai-api" \
    --filter "label=com.docker.compose.service=repo-updater")"; then
    log "Could not inspect legacy Docker updater containers; leaving them unchanged."
    return
  fi
  if [[ -z "${container_ids}" ]]; then
    return
  fi

  log "Removing obsolete Docker repository updater container(s); updates now run only through the host installer schedule."
  while IFS= read -r container_id; do
    [[ -n "${container_id}" ]] && ids+=("${container_id}")
  done <<< "${container_ids}"
  docker_cmd rm -f "${ids[@]}" >/dev/null
}

start_stack() {
  shift
  local compose_args=("$@")

  remove_legacy_repo_updater

  log "Starting Ollama."
  compose_cmd "${compose_args[@]}" up -d ollama

  log "Pulling required Ollama models."
  compose_cmd "${compose_args[@]}" run --rm model-init

  log "Starting gateway."
  compose_cmd "${compose_args[@]}" up -d gateway

  if [[ "${AGENT_ZERO_ENABLED}" == "1" ]]; then
    log "Starting Agent Zero."
    compose_cmd "${compose_args[@]}" up -d agent-zero
  fi
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
  if [[ "${SKIP_TAILSCALE_SERVE}" == "1" ]]; then
    log "Skipping Tailscale Serve configuration because --skip-tailscale-serve was provided."
    return
  fi

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

  if [[ "${AGENT_ZERO_ENABLED}" == "1" ]]; then
    log "Configuring Tailscale Serve for Agent Zero on HTTPS port ${AGENT_ZERO_TAILSCALE_HTTPS_PORT}."
    if ! tailscale serve --bg "--https=${AGENT_ZERO_TAILSCALE_HTTPS_PORT}" "http://127.0.0.1:${AGENT_ZERO_PORT}"; then
      if [[ "${LOCAL_AI_API_SYSTEMD:-}" == "1" ]]; then
        log "Agent Zero Tailscale Serve command failed during scheduled run; leaving current serve config unchanged."
        return
      fi
      sudo_cmd tailscale serve --bg "--https=${AGENT_ZERO_TAILSCALE_HTTPS_PORT}" "http://127.0.0.1:${AGENT_ZERO_PORT}"
    fi
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

systemd_weekday() {
  case "$1" in
    Sunday) printf '%s\n' "Sun" ;;
    Monday) printf '%s\n' "Mon" ;;
    Tuesday) printf '%s\n' "Tue" ;;
    Wednesday) printf '%s\n' "Wed" ;;
    Thursday) printf '%s\n' "Thu" ;;
    Friday) printf '%s\n' "Fri" ;;
    Saturday) printf '%s\n' "Sat" ;;
    *) die "Unknown weekday: $1" ;;
  esac
}

timer_calendar_lines() {
  local hour minute start_minutes total_minutes trigger_hour trigger_minute count

  case "${UPDATE_SCHEDULE}" in
    default)
      printf '%s\n' "OnBootSec=5min"
      printf '%s\n' "OnCalendar=daily"
      ;;
    logon)
      printf '%s\n' "OnBootSec=5min"
      ;;
    daily)
      printf 'OnCalendar=*-*-* %s:00\n' "${UPDATE_TIME}"
      ;;
    weekly)
      printf 'OnCalendar=%s *-*-* %s:00\n' "$(systemd_weekday "${UPDATE_WEEKLY_DAY}")" "${UPDATE_TIME}"
      ;;
    every-hours)
      hour="${UPDATE_TIME%%:*}"
      minute="${UPDATE_TIME##*:}"
      start_minutes=$((10#${hour} * 60 + 10#${minute}))
      count=$((24 / UPDATE_EVERY_HOURS))
      for ((i = 0; i < count; i++)); do
        total_minutes=$(((start_minutes + (i * UPDATE_EVERY_HOURS * 60)) % 1440))
        trigger_hour=$((total_minutes / 60))
        trigger_minute=$((total_minutes % 60))
        printf '%02d:%02d\n' "${trigger_hour}" "${trigger_minute}"
      done | sort | while read -r trigger_time; do
        printf 'OnCalendar=*-*-* %s:00\n' "${trigger_time}"
      done
      ;;
    *)
      die "Unknown update schedule: ${UPDATE_SCHEDULE}"
      ;;
  esac
}

install_systemd_units() {
  local root="$1"
  local user group timer_lines

  if [[ "${LOCAL_AI_API_SYSTEMD:-}" == "1" ]]; then
    log "Running under systemd; skipping systemd unit installation."
    return
  fi

  have systemctl || {
    log "systemd is not available; skipping scheduled update timer."
    return
  }

  if [[ "${NO_SCHEDULED_TASK}" == "1" || "${UPDATE_SCHEDULE}" == "none" ]]; then
    log "Scheduled updates disabled; removing existing systemd timer if present."
    sudo_cmd systemctl disable --now local-ai-api-update.timer >/dev/null 2>&1 || true
    return
  fi

  user="$(service_user)"
  group="$(service_group)"
  timer_lines="$(timer_calendar_lines)"

  log "Installing systemd update service and timer using schedule '${UPDATE_SCHEDULE}'."
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
Environment=LOCAL_AI_API_ACCELERATOR=${ACCELERATOR}
Environment=LOCAL_AI_API_SKIP_REPO_SYNC=${SKIP_REPO_SYNC}
Environment=LOCAL_AI_API_SKIP_TAILSCALE_SERVE=${SKIP_TAILSCALE_SERVE}
Environment=LOCAL_AI_API_LOW_COMPUTE=${LOW_COMPUTE}
ExecStart=${root}/scripts/install-or-update.sh --scheduled-run --no-scheduled-task
EOF

  sudo_cmd tee /etc/systemd/system/local-ai-api-update.timer >/dev/null <<EOF
[Unit]
Description=Run Local AI API updater

[Timer]
${timer_lines}
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
  local -a original_args=("$@")
  local -a compose_args

  parse_args "$@"
  if [[ "${LOW_COMPUTE}" == "1" ]]; then
    # Low Compute Mode: CPU-only, and Agent Zero (needs the large models) off.
    ACCELERATOR="cpu"
    AGENT_ZERO_ENABLED=0
  fi
  require_linux
  acquire_lock

  root="$(repo_root)"
  REPO_ROOT_DIR="${root}"
  cd "${root}"
  script_path="${root}/scripts/install-or-update.sh"

  sync_repo_to_github "${root}" "${script_path}" "${original_args[@]}"

  install_docker_engine

  accelerator="$(detect_accelerator)"
  log "Selected accelerator profile: ${accelerator}."
  mapfile -t compose_args < <(compose_files_for_accelerator "${root}" "${accelerator}")

  build_and_test_gateway_image "${root}" "${compose_args[@]}"
  if [[ "${AGENT_ZERO_ENABLED}" == "1" ]]; then
    if ! sh "${root}/scripts/update-agent-zero-cockpit.sh"; then
      log "Agent Zero candidate validation failed; retaining the last known-good local image."
    fi
  fi
  start_stack "${root}" "${compose_args[@]}"

  wait_for_url "${GATEWAY_HEALTH_URL}" "Gateway health"
  wait_for_url "${OLLAMA_HEALTH_URL}" "Ollama health"
  if [[ "${AGENT_ZERO_ENABLED}" == "1" ]]; then
    wait_for_url "http://127.0.0.1:${AGENT_ZERO_PORT}" "Agent Zero UI" 90
  fi

  configure_tailscale_serve
  install_systemd_units "${root}"

  log "${PROJECT_NAME} install/update complete."
}

main "$@"
