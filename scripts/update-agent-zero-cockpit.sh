#!/usr/bin/env sh
# Build and smoke-test a local Agent Zero cockpit candidate without replacing the stable image on failure.
set -eu

root="${REPO_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
base_tag="${AGENT_ZERO_IMAGE_TAG:-latest}"
candidate="local-ai-api-agent-zero-cockpit:candidate"
stable="local-ai-api-agent-zero-cockpit:latest"
report_dir="$root/.local"
report="$report_dir/agent-zero-candidate.json"

write_report() {
  mkdir -p "$report_dir"
  REPORT_PATH="$report" REPORT_STATUS="$1" REPORT_MESSAGE="$2" REPORT_TAG="$base_tag" python3 - <<'PY'
import json
import os
from datetime import UTC, datetime

payload = {
    "status": os.environ["REPORT_STATUS"],
    "message": os.environ["REPORT_MESSAGE"],
    "image_tag": os.environ["REPORT_TAG"],
    "checked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
}
with open(os.environ["REPORT_PATH"], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, sort_keys=True)
    handle.write("\n")
PY
}

if ! docker pull "agent0ai/agent-zero:${base_tag}"; then
  write_report "failed" "Could not pull the configured Agent Zero image tag."
  exit 1
fi
if ! docker build --build-arg "AGENT_ZERO_IMAGE_TAG=${base_tag}" -f "$root/Dockerfile.agent-zero-cockpit" -t "$candidate" "$root"; then
  write_report "failed" "Candidate image build failed."
  exit 1
fi
if ! docker run --rm --entrypoint /bin/sh "$candidate" -c '
  test -f /a0/plugins/local_ai_api_cockpit/plugin.yaml &&
  test -f /a0/plugins/local_ai_api_cockpit/api/status.py &&
  test -f /a0/plugins/local_ai_api_cockpit/webui/cockpit.js &&
  python -m compileall -q /a0/plugins/local_ai_api_cockpit
'; then
  write_report "failed" "Candidate overlay smoke test failed."
  exit 1
fi
docker tag "$candidate" "$stable"
write_report "passed" "Candidate overlay smoke test passed and was promoted locally."
