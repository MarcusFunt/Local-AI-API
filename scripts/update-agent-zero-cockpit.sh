#!/usr/bin/env sh
# Build a candidate against upstream Agent Zero, smoke-test its cockpit files,
# then promote only when every check succeeds. The previous image remains live
# until the final tag update succeeds.
set -eu

base_tag="${AGENT_ZERO_IMAGE_TAG:-latest}"
candidate="local-ai-api-agent-zero-cockpit:candidate"
stable="local-ai-api-agent-zero-cockpit:latest"

docker pull "agent0ai/agent-zero:${base_tag}"
docker build --build-arg "AGENT_ZERO_IMAGE_TAG=${base_tag}" -f Dockerfile.agent-zero-cockpit -t "$candidate" .
docker run --rm --entrypoint /bin/sh "$candidate" -c '
  test -f /a0/plugins/local_ai_api_cockpit/plugin.yaml &&
  test -f /a0/plugins/local_ai_api_cockpit/api/status.py &&
  test -f /a0/plugins/local_ai_api_cockpit/webui/cockpit.js
'
docker tag "$candidate" "$stable"
