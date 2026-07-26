"""Deployment-level checks for the Docker installer assets."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_VARIANTS = {
    "cpu": ["compose.yaml", "compose.cpu.yaml", "compose.agent-zero.yaml"],
    "nvidia": ["compose.yaml", "compose.gpu-nvidia.yaml", "compose.agent-zero.yaml"],
    "amd": ["compose.yaml", "compose.gpu-amd.yaml", "compose.agent-zero.yaml"],
}
AGENT_ZERO_COMPOSE_FILES = COMPOSE_VARIANTS["cpu"]
SHELL_SCRIPTS = [
    "scripts/setup-docker.sh",
    "scripts/install-or-update.sh",
    "scripts/bootstrap-ubuntu26-ai-server.sh",
    "scripts/setup-agent-runtime.sh",
    "scripts/docker-repo-updater.sh",
    "scripts/run-agent-container.sh",
    "scripts/backup-server-state.sh",
    "scripts/verify-server-plan.sh",
    "sd-card/prepare.sh",
    "sd-card/start.sh",
]
POWERSHELL_SCRIPTS = [
    "scripts/setup-docker.ps1",
    "scripts/install-or-update.ps1",
]


def _compose_config(files: list[str], env_overrides: dict[str, str] | None = None) -> dict:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")

    env = os.environ.copy()
    for key in (
        "INSTALL_AUDIO",
        "OLLAMA_MODELS",
        "WHISPER_CACHE_DIR",
        "AGENT_ZERO_ENABLED",
        "AGENT_ZERO_PORT",
        "AGENT_ZERO_IMAGE_TAG",
        "API_KEY",
    ):
        env.pop(key, None)
    if env_overrides:
        env.update(env_overrides)

    command = ["docker", "compose"]
    for file_name in files:
        command.extend(["-f", file_name])
    command.extend(["config", "--format", "json"])

    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("variant,files", COMPOSE_VARIANTS.items())
def test_compose_config_is_valid_for_accelerator_variants(variant: str, files: list[str]):
    config = _compose_config(files)

    assert config["services"]["ollama"]["labels"]["local-ai-api.accelerator"] == variant


@pytest.mark.parametrize("files", COMPOSE_VARIANTS.values())
def test_compose_does_not_publish_raw_ollama_port(files: list[str]):
    config = _compose_config(files)

    for service in config["services"].values():
        for port in service.get("ports", []):
            assert port.get("target") != 11434
            assert port.get("published") != "11434"


@pytest.mark.parametrize("files", COMPOSE_VARIANTS.values())
def test_gateway_keeps_ollama_loopback_and_shared_namespace(files: list[str]):
    config = _compose_config(files)

    gateway = config["services"]["gateway"]
    model_init = config["services"]["model-init"]
    ollama = config["services"]["ollama"]

    assert gateway["environment"]["OLLAMA_BASE_URL"] == "http://127.0.0.1:11434"
    assert gateway["environment"]["HOST"] == "0.0.0.0"
    assert gateway["environment"]["PORT"] == "8080"
    assert gateway["environment"]["AGENT_ZERO_ENABLED"] == "true"
    assert gateway["network_mode"] == "service:ollama"
    assert model_init["network_mode"] == "service:ollama"
    assert model_init["entrypoint"][:2] == ["/bin/sh", "-c"]
    assert (
        'models="qwen3.5:9b qwen3.5:4b qwen3.5:0.8b qwen3:14b qwen3:8b"'
        in model_init["entrypoint"][2]
    )
    assert "for model in $$models qwen3:14b qwen3:8b" in model_init["entrypoint"][2]
    assert 'case " $$pulled " in' in model_init["entrypoint"][2]
    assert 'ollama pull "$$model"' in model_init["entrypoint"][2]
    assert ollama["environment"]["OLLAMA_HOST"] == "127.0.0.1:11434"


def test_compose_source_keeps_ollama_models_fallback_expression():
    compose_source = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert (
        'models="${OLLAMA_MODELS:-qwen3.5:9b qwen3.5:4b qwen3.5:0.8b qwen3:14b qwen3:8b}"'
        in compose_source
    )


def test_compose_includes_a_docker_side_repo_update_monitor():
    config = _compose_config(COMPOSE_VARIANTS["cpu"])
    updater = config["services"]["repo-updater"]

    assert updater["image"] == "docker:27-cli"
    assert updater["restart"] == "unless-stopped"
    assert updater["environment"]["REPO_UPDATE_BRANCH"] == "main"
    assert updater["environment"]["REPO_UPDATE_INTERVAL_SECONDS"] == "300"
    assert any(
        volume["type"] == "bind"
        and volume["source"] == "/var/run/docker.sock"
        and volume["target"] == "/var/run/docker.sock"
        for volume in updater["volumes"]
    )
    assert "exec /bin/sh /repo/scripts/docker-repo-updater.sh" in updater["entrypoint"][2]


def test_docker_repo_updater_uses_only_clean_fast_forward_updates():
    script = (REPO_ROOT / "scripts" / "docker-repo-updater.sh").read_text(encoding="utf-8")

    assert 'git -C "${REPO_DIR}" pull --ff-only "${REMOTE}" "${BRANCH}"' in script
    assert 'refs/heads/${BRANCH}:refs/remotes/${REMOTE}/${BRANCH}' in script
    assert "export GIT_TERMINAL_PROMPT=0" in script
    assert 'git -C "${REPO_DIR}" diff --quiet' in script
    assert 'git -C "${REPO_DIR}" diff --cached --quiet' in script
    assert 'docker compose "$@" build --pull gateway' in script
    assert 'docker compose "$@" build --pull agent-zero' in script
    assert 'docker compose "$@" up -d --no-deps --force-recreate gateway' in script


def test_agent_zero_models_are_pulled_with_custom_ollama_models():
    config = _compose_config(COMPOSE_VARIANTS["cpu"], {"OLLAMA_MODELS": "qwen3.5:0.8b"})
    entrypoint = config["services"]["model-init"]["entrypoint"][2]

    assert 'models="qwen3.5:0.8b"' in entrypoint
    assert "for model in $$models qwen3:14b qwen3:8b" in entrypoint


@pytest.mark.parametrize("files", COMPOSE_VARIANTS.values())
def test_gateway_container_owns_python_and_model_caches(files: list[str]):
    config = _compose_config(files)

    gateway = config["services"]["gateway"]
    environment = gateway["environment"]
    volumes = gateway["volumes"]

    assert gateway["build"]["args"]["INSTALL_AUDIO"] == "true"
    assert environment["XDG_CACHE_HOME"] == "/models/cache"
    assert environment["HF_HOME"] == "/models/cache/huggingface"
    assert environment["TORCH_HOME"] == "/models/cache/torch"
    assert environment["WHISPER_CACHE_DIR"] == "/models/cache/whisper"
    assert {
        "type": "volume",
        "source": "gateway-model-cache",
        "target": "/models/cache",
        "volume": {},
    } in volumes
    assert "gateway-model-cache" in config["volumes"]


@pytest.mark.parametrize("files", COMPOSE_VARIANTS.values())
def test_only_gateway_and_agent_zero_ports_are_published_on_loopback(files: list[str]):
    config = _compose_config(files)
    published_ports = []

    for service_name, service in config["services"].items():
        for port in service.get("ports", []):
            published_ports.append(
                (
                    service_name,
                    port.get("host_ip"),
                    port.get("published"),
                    port.get("target"),
                )
            )

    assert sorted(published_ports) == sorted([
        ("ollama", "127.0.0.1", "8080", 8080),
        ("agent-zero", "127.0.0.1", "50080", 80),
    ])


def test_agent_zero_compose_publishes_ui_only_on_loopback():
    config = _compose_config(AGENT_ZERO_COMPOSE_FILES)

    agent_zero = config["services"]["agent-zero"]
    assert agent_zero["image"] == "local-ai-api-agent-zero-cockpit:latest"
    assert agent_zero["build"]["dockerfile"] == "Dockerfile.agent-zero-cockpit"
    assert agent_zero["environment"]["API_KEY_OTHER"] == "unused"
    assert agent_zero["command"][:2] == ["/bin/sh", "-c"]
    assert "API_KEY_OTHER" in agent_zero["command"][2]
    assert "/a0/usr/.env" in agent_zero["command"][2]
    assert 'exec /exe/initialize.sh "$${BRANCH:-main}"' in agent_zero["command"][2]
    assert agent_zero["environment"]["A0_SET__model_config__chat_model__provider"] == "other"
    assert agent_zero["environment"]["A0_SET__model_config__chat_model__name"] == "agent"
    assert (
        agent_zero["environment"]["A0_SET__model_config__chat_model__api_base"]
        == "http://host.docker.internal:8080/v1"
    )
    assert agent_zero["environment"]["A0_SET__model_config__utility_model__name"] == "agent-utility"
    assert agent_zero["ports"] == [
        {
            "host_ip": "127.0.0.1",
            "mode": "ingress",
            "protocol": "tcp",
            "published": "50080",
            "target": 80,
        }
    ]
    assert {
        "type": "volume",
        "source": "agent-zero-data",
        "target": "/a0/usr",
        "volume": {},
    } in agent_zero["volumes"]


def test_agent_zero_compose_forwards_gateway_api_key_when_set():
    config = _compose_config(AGENT_ZERO_COMPOSE_FILES, {"API_KEY": "local-test-key"})

    agent_zero = config["services"]["agent-zero"]
    assert agent_zero["environment"]["API_KEY_OTHER"] == "local-test-key"


@pytest.mark.parametrize("script", SHELL_SCRIPTS)
def test_shell_script_has_valid_bash_syntax(script: str):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not installed")

    result = subprocess.run(
        [bash, "-n", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", POWERSHELL_SCRIPTS)
def test_powershell_script_has_valid_syntax(script: str):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not installed")

    parse_command = (
        "$tokens = $null; "
        "$errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script}', [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { "
        "$errors | ForEach-Object { Write-Error $_.Message }; "
        "exit 1 "
        "}"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", parse_command],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
