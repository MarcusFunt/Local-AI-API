"""Static contracts for the thin Agent Zero workspace-cockpit overlay."""
from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
OVERLAY = REPO_ROOT / "agent_zero_overlay" / "plugins" / "local_ai_api_cockpit"


def test_cockpit_is_a_plugin_with_an_allow_listed_backend_proxy():
    api = (OVERLAY / "api" / "status.py").read_text(encoding="utf-8")
    contract = json.loads((REPO_ROOT / "agent_zero_overlay" / "cockpit_contract.json").read_text(encoding="utf-8"))

    assert '"list_workspaces"' in api
    assert '"task_report"' in api
    assert '"create_workspace"' in api
    assert '"start_autonomous_run"' in api
    assert '"archive_workspace"' in api
    assert '"candidate_status"' in api
    assert "AGENT_ZERO_COCKPIT_ENABLED" in api
    assert "merge_workspace" not in api
    assert contract["security"]["browser_direct_repo_ops_access"] is False
    assert contract["security"]["allows_merge_push_deploy"] is False


def test_browser_cockpit_uses_only_same_origin_agent_zero_api():
    frontend = (OVERLAY / "webui" / "cockpit.js").read_text(encoding="utf-8")

    assert "/api/plugins/local_ai_api_cockpit/status" in frontend
    assert "repo-ops:8090" not in frontend
    assert "http://repo-ops" not in frontend
    assert "create_workspace" in frontend
    assert "stop_autonomous_run" in frontend
    assert "candidate_status" in frontend


def test_agent_zero_compose_builds_the_local_cockpit_overlay():
    compose = (REPO_ROOT / "compose.agent-zero.yaml").read_text(encoding="utf-8")

    assert "Dockerfile.agent-zero-cockpit" in compose
    assert "REPO_OPS_MCP_URL: http://repo-ops:8090/mcp" in compose
    assert "GATEWAY_STATUS_URL: http://host.docker.internal:8080/status.json" in compose


def test_candidate_scripts_are_installer_wired_and_report_results():
    shell = (REPO_ROOT / "scripts" / "update-agent-zero-cockpit.sh").read_text(encoding="utf-8")
    powershell = (REPO_ROOT / "scripts" / "update-agent-zero-cockpit.ps1").read_text(encoding="utf-8")
    linux_installer = (REPO_ROOT / "scripts" / "install-or-update.sh").read_text(encoding="utf-8")
    windows_installer = (REPO_ROOT / "scripts" / "install-or-update.ps1").read_text(encoding="utf-8")

    assert "agent-zero-candidate.json" in shell
    assert "agent-zero-candidate.json" in powershell
    assert "update-agent-zero-cockpit.sh" in linux_installer
    assert "update-agent-zero-cockpit.ps1" in windows_installer
