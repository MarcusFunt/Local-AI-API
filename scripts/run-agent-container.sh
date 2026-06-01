#!/usr/bin/env bash
set -Eeuo pipefail

AGENT_USER="${LOCAL_AI_API_AGENT_USER:-agent}"
WORKSPACE_ROOT="${LOCAL_AI_API_AGENT_WORKSPACE_ROOT:-/srv/agent-workspaces}"
CACHE_ROOT="${LOCAL_AI_API_AGENT_CACHE_ROOT:-/srv/agent-caches}"
AGENT_NETWORK="${LOCAL_AI_API_AGENT_NETWORK:-local-ai-agents}"
PROXY_PORT="${LOCAL_AI_API_AGENT_PROXY_PORT:-18080}"
IMAGE="${LOCAL_AI_API_AGENT_IMAGE:-python:3.12-bookworm}"
PROJECT=""
COMMAND=""
MODEL="${OPENAI_MODEL:-main}"
API_KEY="${OPENAI_API_KEY:-unused}"
API_BASE="${OPENAI_API_BASE:-}"
MEMORY_LIMIT="${LOCAL_AI_API_AGENT_MEMORY:-8g}"
CPU_LIMIT="${LOCAL_AI_API_AGENT_CPUS:-4}"
CONTAINER_NAME=""

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: scripts/run-agent-container.sh --project NAME [options] [-- command...]

Runs a per-project coding-agent container with only that workspace and package
caches mounted. It does not mount the Docker socket, host root, SSH keys, or
/var/run.

Options:
  --project NAME             Required workspace name
  --image IMAGE              Container image (default: python:3.12-bookworm)
  --workspace-root PATH      Host workspace root (default: /srv/agent-workspaces)
  --cache-root PATH          Host package cache root (default: /srv/agent-caches)
  --network NAME             Docker network (default: local-ai-agents)
  --gateway-url URL          OpenAI-compatible base URL for the agent
  --model MODEL              Default model env value (default: main)
  --api-key KEY              Default API key env value (default: unused)
  --memory LIMIT             Docker memory limit (default: 8g)
  --cpus COUNT               Docker CPU limit (default: 4)
  --name NAME                Container name
  --command COMMAND          Run COMMAND through bash -lc
  -h, --help                 Show this help

Examples:
  scripts/run-agent-container.sh --project demo
  scripts/run-agent-container.sh --project demo --command 'python --version'
EOF
}

parse_args() {
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --project)
        [[ "$#" -ge 2 ]] || die "--project requires a name."
        PROJECT="$2"
        shift 2
        ;;
      --image)
        [[ "$#" -ge 2 ]] || die "--image requires a value."
        IMAGE="$2"
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
      --gateway-url)
        [[ "$#" -ge 2 ]] || die "--gateway-url requires a URL."
        API_BASE="$2"
        shift 2
        ;;
      --model)
        [[ "$#" -ge 2 ]] || die "--model requires a value."
        MODEL="$2"
        shift 2
        ;;
      --api-key)
        [[ "$#" -ge 2 ]] || die "--api-key requires a value."
        API_KEY="$2"
        shift 2
        ;;
      --memory)
        [[ "$#" -ge 2 ]] || die "--memory requires a Docker memory limit."
        MEMORY_LIMIT="$2"
        shift 2
        ;;
      --cpus)
        [[ "$#" -ge 2 ]] || die "--cpus requires a count."
        CPU_LIMIT="$2"
        shift 2
        ;;
      --name)
        [[ "$#" -ge 2 ]] || die "--name requires a value."
        CONTAINER_NAME="$2"
        shift 2
        ;;
      --command)
        [[ "$#" -ge 2 ]] || die "--command requires a command string."
        COMMAND="$2"
        shift 2
        ;;
      --)
        shift
        COMMAND="$*"
        break
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        if [[ -z "${PROJECT}" ]]; then
          PROJECT="$1"
          shift
        else
          die "Unknown argument: $1"
        fi
        ;;
    esac
  done

  [[ -n "${PROJECT}" ]] || die "--project is required."
  [[ "${PROJECT}" =~ ^[A-Za-z0-9._-]+$ ]] || die "Project name must be alphanumeric plus '.', '_' or '-'."
  [[ "${PROJECT}" != "." && "${PROJECT}" != ".." && "${PROJECT}" != *".."* ]] || die "Project name cannot contain '..'."
}

network_gateway_ip() {
  docker network inspect \
    --format '{{(index .IPAM.Config 0).Gateway}}' \
    "${AGENT_NETWORK}"
}

default_api_base() {
  local gateway_ip
  gateway_ip="$(network_gateway_ip)"
  printf 'http://%s:%s/v1\n' "${gateway_ip}" "${PROXY_PORT}"
}

prepare_paths() {
  local project_dir cache_dir
  project_dir="${WORKSPACE_ROOT}/${PROJECT}"
  cache_dir="${CACHE_ROOT}/${PROJECT}"

  sudo mkdir -p "${project_dir}" "${cache_dir}/pip" "${cache_dir}/npm" "${cache_dir}/cargo" "${cache_dir}/uv"
  sudo chown -R "${AGENT_USER}:${AGENT_USER}" "${project_dir}" "${cache_dir}"
  sudo chmod 0750 "${project_dir}" "${cache_dir}"
}

main() {
  local uid gid project_dir cache_dir tty_args cmd_args name_arg

  parse_args "$@"
  command -v docker >/dev/null 2>&1 || die "docker is required."
  id "${AGENT_USER}" >/dev/null 2>&1 || die "Agent user ${AGENT_USER} does not exist. Run scripts/setup-agent-runtime.sh first."
  docker network inspect "${AGENT_NETWORK}" >/dev/null 2>&1 || die "Docker network ${AGENT_NETWORK} does not exist. Run scripts/setup-agent-runtime.sh first."

  if [[ -z "${API_BASE}" ]]; then
    API_BASE="$(default_api_base)"
  fi

  prepare_paths
  uid="$(id -u "${AGENT_USER}")"
  gid="$(id -g "${AGENT_USER}")"
  project_dir="${WORKSPACE_ROOT}/${PROJECT}"
  cache_dir="${CACHE_ROOT}/${PROJECT}"

  tty_args=()
  if [[ -t 0 && -t 1 ]]; then
    tty_args=(-it)
  fi

  name_arg=()
  if [[ -n "${CONTAINER_NAME}" ]]; then
    name_arg=(--name "${CONTAINER_NAME}")
  else
    name_arg=(--name "local-ai-agent-${PROJECT}")
  fi

  if [[ -n "${COMMAND}" ]]; then
    cmd_args=(bash -lc "${COMMAND}")
  else
    cmd_args=(bash)
  fi

  exec docker run --rm "${tty_args[@]}" \
    "${name_arg[@]}" \
    --pull=missing \
    --network "${AGENT_NETWORK}" \
    --user "${uid}:${gid}" \
    --workdir /workspace \
    --env HOME=/workspace \
    --env OPENAI_API_BASE="${API_BASE}" \
    --env OPENAI_BASE_URL="${API_BASE}" \
    --env OPENAI_API_KEY="${API_KEY}" \
    --env OPENAI_MODEL="${MODEL}" \
    --env PIP_CACHE_DIR=/cache/pip \
    --env npm_config_cache=/cache/npm \
    --env CARGO_HOME=/cache/cargo \
    --env UV_CACHE_DIR=/cache/uv \
    --mount "type=bind,src=${project_dir},dst=/workspace" \
    --mount "type=bind,src=${cache_dir},dst=/cache" \
    --tmpfs /tmp:rw,nosuid,nodev,size=1g \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --pids-limit 512 \
    --memory "${MEMORY_LIMIT}" \
    --cpus "${CPU_LIMIT}" \
    "${IMAGE}" \
    "${cmd_args[@]}"
}

main "$@"
