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
    "scripts/update-agent-zero-cockpit.sh",
    "scripts/run-agent-container.sh",
    "scripts/backup-server-state.sh",
    "scripts/verify-server-plan.sh",
    "sd-card/prepare.sh",
    "sd-card/start.sh",
]
POWERSHELL_SCRIPTS = [
    "scripts/setup-docker.ps1",
    "scripts/install-or-update.ps1",
    "scripts/update-agent-zero-cockpit.ps1",
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
        "AGENT_ZERO_BASE_IMAGE",
        "API_KEY",
    ):
        env.pop(key, None)
    # Compose reads the repository's private .env file independently of this
    # process environment. Pin the documented default here so tests of default
    # deployment behavior do not inherit a developer's custom model list.
    env["OLLAMA_MODELS"] = "qwen3.5:9b qwen3.5:4b qwen3.5:0.8b qwen3:14b qwen3:8b"
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
def test_autoheal_watchdog_monitors_gateway(files: list[str]):
    """The autoheal sidecar must exist and the gateway must be labeled for it,
    so a gateway that goes unhealthy (not just one that exits) is restarted."""
    config = _compose_config(files)
    services = config["services"]

    assert "autoheal" in services, "autoheal watchdog service is missing"
    assert services["autoheal"]["image"].startswith("willfarrell/autoheal")
    assert services["gateway"].get("labels", {}).get("autoheal") == "true"


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
    assert "for model in $$models; do" in model_init["entrypoint"][2]
    assert 'case " $$pulled " in' in model_init["entrypoint"][2]
    assert 'ollama pull "$$model"' in model_init["entrypoint"][2]
    assert ollama["environment"]["OLLAMA_HOST"] == "127.0.0.1:11434"


def test_compose_source_keeps_ollama_models_fallback_expression():
    compose_source = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert (
        'models="${OLLAMA_MODELS:-qwen3.5:9b qwen3.5:4b qwen3.5:0.8b qwen3:14b qwen3:8b nomic-embed-text}"'
        in compose_source
    )


def test_compose_has_no_docker_side_repo_update_monitor():
    config = _compose_config(COMPOSE_VARIANTS["cpu"])
    assert "repo-updater" not in config["services"]


def test_installers_remove_only_the_obsolete_docker_updater_service():
    shell = (REPO_ROOT / "scripts" / "install-or-update.sh").read_text(encoding="utf-8")
    powershell = (REPO_ROOT / "scripts" / "install-or-update.ps1").read_text(encoding="utf-8")

    for source in (shell, powershell):
        assert "com.docker.compose.project=local-ai-api" in source
        assert "com.docker.compose.service=repo-updater" in source
        assert "docker rm -f /" not in source
    assert "remove_legacy_repo_updater" in shell
    assert "Remove-LegacyRepoUpdater" in powershell
    assert "UTF8Encoding($false)" in powershell


def test_external_runtime_images_are_digest_pinned():
    compose = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
    qdrant = (REPO_ROOT / "compose.qdrant.yaml").read_text(encoding="utf-8")
    agent_zero = (REPO_ROOT / "Dockerfile.agent-zero-cockpit").read_text(encoding="utf-8")

    assert "OLLAMA_IMAGE:-ollama/ollama@sha256:" in compose
    assert "AUTOHEAL_IMAGE:-willfarrell/autoheal@sha256:" in compose
    assert "QDRANT_IMAGE:-qdrant/qdrant@sha256:" in qdrant
    assert "AGENT_ZERO_BASE_IMAGE=agent0ai/agent-zero@sha256:" in agent_zero
    assert "PyYAML==6.0.2" in agent_zero


def test_agent_zero_models_are_pulled_with_custom_ollama_models():
    config = _compose_config(COMPOSE_VARIANTS["cpu"], {"OLLAMA_MODELS": "qwen3.5:0.8b"})
    entrypoint = config["services"]["model-init"]["entrypoint"][2]

    assert 'models="qwen3.5:0.8b"' in entrypoint
    assert "for model in $$models; do" in entrypoint
    assert 'ollama pull "$$model"' in entrypoint


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
    assert agent_zero["image"] == "local-ai-api-agent-zero-cockpit:1.0.0"
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
    assert agent_zero["environment"]["A0_SET__model_config__utility_model__name"] == "agent"
    assert agent_zero["environment"]["A0_SET__model_config__chat_model__ctx_length"] == "4096"
    assert agent_zero["environment"]["A0_SET__model_config__utility_model__ctx_length"] == "4096"
    assert '"ctx_length": 4096' in agent_zero["command"][2]
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
    if not (REPO_ROOT / script).is_file():
        pytest.skip(f"{script} is not present in this checkout")

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
    if not (REPO_ROOT / script).is_file():
        pytest.skip(f"{script} is not present in this checkout")

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


def test_audio_requirements_pin_setuptools_below_81():
    """Chatterbox's watermarker dependency (resemble-perth) imports the legacy
    pkg_resources module, which setuptools removed in 81.0. Without a <81 pin the
    audio image builds with a newer setuptools, perth's import silently fails, and
    Chatterbox TTS crashes at model load with "'NoneType' object is not callable".
    """
    reqs = (REPO_ROOT / "requirements-audio.txt").read_text(encoding="utf-8")
    assert "setuptools<81" in reqs, (
        "requirements-audio.txt must pin setuptools<81 so pkg_resources stays "
        "importable for resemble-perth / Chatterbox TTS."
    )

    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    final_compatibility_pin = 'python -m pip install "setuptools<81"'
    assert dockerfile.count(final_compatibility_pin) == 1
    assert dockerfile.index(final_compatibility_pin) > dockerfile.index(
        "python -m pip install -r requirements-rag.txt"
    ), (
        "The Docker image must restore setuptools<81 after optional dependencies, "
        "which can otherwise replace the audio compatibility pin."
    )
