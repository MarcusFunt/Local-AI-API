"""Regression coverage for issues found during the live dashboard audit."""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_status_dashboard_hides_empty_badges_and_explains_runtime_scope() -> None:
    source = (REPO_ROOT / "gateway" / "static" / "status.html").read_text(encoding="utf-8")

    assert 'rel="icon"' in source
    assert ".tab-badge[hidden]" in source
    assert 'className: "runtime-summary"' in source
    assert '"Gateway listener"' in source
    assert 'statusLabel: "Endpoints available"' in source
    assert "not reported" not in source
    assert 'class="tab-scroll-cue"' in source
    assert 'class="table-scroll-cue"' in source
    assert "Swipe to see the remaining dashboard sections" in source
    assert "Swipe the table to view every column" in source
    assert 'if (repoStatus === "failed") return ["degraded", "Maintenance failure"]' in source
    assert "function currentRepoStatus(repository)" in source
    assert "Latest scheduled update failed" in source
    assert 'const repoStatusLabel = repo?.dirty === true ? "Local changes"' in source
    assert "agent_zero_cockpit_url ?? autonomy.agent_zero_url" in source


def test_live_call_has_a_clear_empty_transcript_and_a_form_owned_api_key() -> None:
    source = (REPO_ROOT / "gateway" / "static" / "live-call.html").read_text(encoding="utf-8")

    assert '<form class="settings" id="call-settings">' in source
    assert "Overrides the gateway default for this call only." in source
    assert 'id="transcript-empty"' in source
    assert "document.querySelector('#transcript-empty')?.remove();" in source
    assert "call-settings').addEventListener('submit'" in source
    assert "<!-- API_KEY_FIELD -->" in source
    assert "document.querySelector('#api-key')?.value" in source


def test_cockpit_is_reactive_and_can_open_as_a_native_surface_modal() -> None:
    overlay = REPO_ROOT / "agent_zero_overlay" / "plugins" / "local_ai_api_cockpit"
    frontend = (overlay / "webui" / "cockpit.js").read_text(encoding="utf-8")
    modal = (overlay / "webui" / "cockpit.html").read_text(encoding="utf-8")
    registration = (
        overlay / "extensions" / "webui" / "right_canvas_register_surfaces" / "register-local-ai-api-cockpit.js"
    ).read_text(encoding="utf-8")

    assert 'const UPDATE_EVENT = "local-ai-api-cockpit:update"' in frontend
    assert "function cockpitView()" in frontend
    assert "globalThis.localAiApiCockpitView = cockpitView" in frontend
    assert 'x-data="window.localAiApiCockpitView()"' in modal
    assert 'data-surface-id="local-ai-api-cockpit"' in modal
    assert "local-ai-api-cockpit-button" in modal
    assert 'modalPath: "/plugins/local_ai_api_cockpit/webui/cockpit.html"' in registration
    assert "arguments =" not in frontend
    assert "arguments: params" in frontend
    assert "function mountStandaloneCockpit()" in frontend
    assert 'this.call("autonomous_status"' in frontend
    assert "async archive(taskId)" in frontend
    assert "data-cockpit-action" in frontend
    assert "workspace.run_state === 'running'" in modal
    assert "workspace.run_state === 'paused'" in modal
    assert "workspace.run_state === 'review_ready'" in modal
    assert "x-show=\"!embedded\"" in modal
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in modal
    assert 'rel="icon"' in modal
    assert "align-content: start;" in modal
