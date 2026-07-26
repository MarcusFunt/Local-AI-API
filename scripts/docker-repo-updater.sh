#!/bin/sh
# Poll a checked-out Git repository and rebuild/recreate the gateway after a
# clean fast-forward update. This script runs in the repo-updater Compose
# service, which deliberately has access to the Docker socket.

set -eu

REPO_DIR="${REPO_DIR:-/repo}"
REMOTE="${REPO_UPDATE_REMOTE:-origin}"
BRANCH="${REPO_UPDATE_BRANCH:-main}"
INTERVAL_SECONDS="${REPO_UPDATE_INTERVAL_SECONDS:-300}"
COMPOSE_FILES="${REPO_UPDATE_COMPOSE_FILES:-}"
export GIT_TERMINAL_PROMPT=0

log() {
  printf '%s [repo-updater] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

require_valid_interval() {
  case "${INTERVAL_SECONDS}" in
    ''|*[!0-9]*)
      log "REPO_UPDATE_INTERVAL_SECONDS must be a whole number of seconds."
      exit 1
      ;;
  esac

  if [ "${INTERVAL_SECONDS}" -lt 60 ]; then
    log "REPO_UPDATE_INTERVAL_SECONDS must be at least 60 seconds."
    exit 1
  fi
}

compose_files_from_running_stack() {
  project="$1"
  ollama_id="$(docker ps -q \
    --filter "label=com.docker.compose.project=${project}" \
    --filter 'label=com.docker.compose.service=ollama' | head -n 1)"

  printf '%s\n' compose.yaml
  if [ -n "${ollama_id}" ]; then
    accelerator="$(docker inspect -f '{{ index .Config.Labels "local-ai-api.accelerator" }}' "${ollama_id}" 2>/dev/null || true)"
    case "${accelerator}" in
      cpu) printf '%s\n' compose.cpu.yaml ;;
      nvidia) printf '%s\n' compose.gpu-nvidia.yaml ;;
      amd) printf '%s\n' compose.gpu-amd.yaml ;;
    esac
  fi

  if docker ps -q --filter "label=com.docker.compose.project=${project}" \
      --filter 'label=com.docker.compose.service=qdrant' | grep -q .; then
    printf '%s\n' compose.qdrant.yaml
  fi
  if docker ps -q --filter "label=com.docker.compose.project=${project}" \
      --filter 'label=com.docker.compose.service=agent-zero' | grep -q .; then
    printf '%s\n' compose.agent-zero.yaml
  fi
}

compose_args() {
  project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "${HOSTNAME}" 2>/dev/null || true)"
  if [ -z "${project}" ] || [ "${project}" = "<no value>" ]; then
    project="local-ai-api"
  fi

  set -- --project-name "${project}" --project-directory "${REPO_DIR}"
  if [ -n "${COMPOSE_FILES}" ]; then
    files="${COMPOSE_FILES}"
  else
    files="$(compose_files_from_running_stack "${project}")"
  fi

  for file in ${files}; do
    case "${file}" in
      /*|../*|*/../*|*' '*|"")
        log "Invalid REPO_UPDATE_COMPOSE_FILES entry: ${file}"
        return 1
        ;;
    esac
    set -- "$@" -f "${REPO_DIR}/${file}"
  done
  printf '%s\n' "$@"
}

rebuild_services() {
  compose_arguments="$(compose_args)" || return 1
  project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "${HOSTNAME}" 2>/dev/null || true)"
  if [ -z "${project}" ] || [ "${project}" = "<no value>" ]; then
    project="local-ai-api"
  fi
  # shellcheck disable=SC2086 # compose_args emits individually quoted-safe paths.
  set -- ${compose_arguments}

  log "Validating updated Compose configuration."
  docker compose "$@" config -q
  log "Building updated gateway image."
  docker compose "$@" build --pull gateway
  if docker ps -q --filter "label=com.docker.compose.project=${project}" \
      --filter 'label=com.docker.compose.service=agent-zero' | grep -q .; then
    log "Building and smoke-validating the Agent Zero cockpit candidate."
    docker compose "$@" build --pull agent-zero
  fi
  log "Recreating gateway with the updated image."
  docker compose "$@" up -d --no-deps --force-recreate gateway
  if docker ps -q --filter "label=com.docker.compose.project=${project}" \
      --filter 'label=com.docker.compose.service=agent-zero' | grep -q .; then
    docker compose "$@" up -d --no-deps --force-recreate agent-zero
  fi
}

update_once() {
  if ! git -C "${REPO_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log "${REPO_DIR} is not a Git checkout; retrying after the next interval."
    return
  fi

  active_branch="$(git -C "${REPO_DIR}" branch --show-current)"
  if [ "${active_branch}" != "${BRANCH}" ]; then
    log "Checkout is on ${active_branch:-detached HEAD}, not ${BRANCH}; leaving it unchanged."
    return
  fi

  if ! git -C "${REPO_DIR}" diff --quiet || ! git -C "${REPO_DIR}" diff --cached --quiet; then
    log "Tracked local changes are present; refusing to overwrite them."
    return
  fi

  if ! git -C "${REPO_DIR}" fetch --quiet "${REMOTE}" \
      "refs/heads/${BRANCH}:refs/remotes/${REMOTE}/${BRANCH}"; then
    log "Could not fetch ${REMOTE}/${BRANCH}; leaving the current deployment running."
    return
  fi

  local_head="$(git -C "${REPO_DIR}" rev-parse HEAD)"
  remote_head="$(git -C "${REPO_DIR}" rev-parse "${REMOTE}/${BRANCH}")"
  if [ "${local_head}" = "${remote_head}" ]; then
    return
  fi

  log "Updating ${local_head} to ${remote_head}."
  if ! git -C "${REPO_DIR}" pull --ff-only "${REMOTE}" "${BRANCH}"; then
    log "Fast-forward update failed; leaving the current deployment running."
    return
  fi

  if rebuild_services; then
    log "Gateway updated and recreated at ${remote_head}."
  else
    log "Source was updated to ${remote_head}, but the gateway rebuild failed; inspect repo-updater logs."
  fi
}

require_valid_interval
log "Monitoring ${REMOTE}/${BRANCH} every ${INTERVAL_SECONDS} seconds."
while true; do
  update_once
  sleep "${INTERVAL_SECONDS}"
done
