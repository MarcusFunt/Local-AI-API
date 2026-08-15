"""Static contracts for the thin Agent Zero workspace-cockpit overlay."""
from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
OVERLAY = REPO_ROOT / "agent_zero_overlay" / "plugins" / "local_ai_api_cockpit"


def _load_status_module(monkeypatch):
    helpers = ModuleType("helpers")
    api = ModuleType("helpers.api")

    class ApiHandler:
        pass

    class Response:
        def __init__(self, body: str, status_code: int):
            self.body = body
            self.status_code = status_code

    api.ApiHandler = ApiHandler
    api.Input = dict
    api.Output = object
    api.Request = object
    api.Response = Response
    monkeypatch.setitem(sys.modules, "helpers", helpers)
    monkeypatch.setitem(sys.modules, "helpers.api", api)
    spec = importlib.util.spec_from_file_location("cockpit_status_test", OVERLAY / "api" / "status.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


async def test_cockpit_plugin_rejects_all_actions_when_disabled(monkeypatch):
    module = _load_status_module(monkeypatch)
    monkeypatch.setenv("AGENT_ZERO_COCKPIT_ENABLED", "false")

    response = await module.Status().process({"tool": "list_workspaces"}, object())

    assert response.status_code == 403
    assert "disabled" in response.body.lower()


async def test_cockpit_plugin_allows_routing_when_enabled(monkeypatch):
    module = _load_status_module(monkeypatch)
    monkeypatch.setenv("AGENT_ZERO_COCKPIT_ENABLED", " true ")

    response = await module.Status().process({"tool": "not-allowed"}, object())

    assert response.status_code == 400
    assert "Unsupported cockpit action" in response.body


def test_cockpit_decodes_streamable_http_sse_responses(monkeypatch):
    module = _load_status_module(monkeypatch)
    expected = {"jsonrpc": "2.0", "result": {"content": []}}
    response = SimpleNamespace(
        headers={"content-type": "text/event-stream; charset=utf-8"},
        text=f"event: message\\ndata: {json.dumps(expected)}\\n\\n",
    )

    response.text = response.text.replace("\\n", "\n")

    assert module._mcp_response_json(response) == expected


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
    assert "AGENT_ZERO_BASE_IMAGE" in compose
    assert "/opt/venv-a0/bin/python - <<'PY'" in compose
    assert "A0_SET_agent_profile: agent0" in compose
    assert '"agent_zero_tool_call"' in compose
    compact_tools = (
        REPO_ROOT / "agent_zero_overlay" / "profiles" / "local-14b" /
        "prompts" / "agent.system.tools.md"
    ).read_text(encoding="utf-8")
    assert '"tool_args":{"text"' in compact_tools
    assert "{{tools}}" not in compact_tools
    assert (
        REPO_ROOT / "agent_zero_overlay" / "profiles" / "local-14b" /
        "plugins" / "bmad_method" / ".toggle-0"
    ).is_file()
    assert (
        REPO_ROOT / "agent_zero_overlay" / "profiles" / "agent0" /
        "prompts" / "agent.system.tools.md"
    ).is_file()


def test_agent_zero_overlay_installs_and_verifies_pyyaml():
    dockerfile = (REPO_ROOT / "Dockerfile.agent-zero-cockpit").read_text(encoding="utf-8")

    assert "PyYAML==6.0.2" in dockerfile
    assert "import yaml" in dockerfile
    assert "/opt/venv-a0/bin/python -m pip" in dockerfile


def test_candidate_scripts_are_installer_wired_and_report_results():
    shell = (REPO_ROOT / "scripts" / "update-agent-zero-cockpit.sh").read_text(encoding="utf-8")
    powershell = (REPO_ROOT / "scripts" / "update-agent-zero-cockpit.ps1").read_text(encoding="utf-8")
    linux_installer = (REPO_ROOT / "scripts" / "install-or-update.sh").read_text(encoding="utf-8")
    windows_installer = (REPO_ROOT / "scripts" / "install-or-update.ps1").read_text(encoding="utf-8")

    assert "agent-zero-candidate.json" in shell
    assert "agent-zero-candidate.json" in powershell
    assert "update-agent-zero-cockpit.sh" in linux_installer
    assert "update-agent-zero-cockpit.ps1" in windows_installer
