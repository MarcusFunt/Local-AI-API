"""Deployment-level checks for the Docker installer assets."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_VARIANTS = {
    "cpu": ["compose.yaml", "compose.cpu.yaml"],
    "nvidia": ["compose.yaml", "compose.gpu-nvidia.yaml"],
    "amd": ["compose.yaml", "compose.gpu-amd.yaml"],
}


def _compose_config(files: list[str]) -> dict:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")

    command = ["docker", "compose"]
    for file_name in files:
        command.extend(["-f", file_name])
    command.extend(["config", "--format", "json"])

    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
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
    assert gateway["environment"]["HOST"] == "127.0.0.1"
    assert gateway["environment"]["PORT"] == "8080"
    assert gateway["network_mode"] == "service:ollama"
    assert model_init["network_mode"] == "service:ollama"
    assert model_init["entrypoint"][:2] == ["/bin/sh", "-c"]
    assert "ollama pull qwen3.5:9b" in model_init["entrypoint"][2]
    assert "ollama pull qwen3.5:4b" in model_init["entrypoint"][2]
    assert "ollama pull qwen3.5:0.8b" in model_init["entrypoint"][2]
    assert ollama["environment"]["OLLAMA_HOST"] == "127.0.0.1:11434"


@pytest.mark.parametrize("files", COMPOSE_VARIANTS.values())
def test_only_gateway_port_is_published_on_loopback(files: list[str]):
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

    assert published_ports == [("ollama", "127.0.0.1", "8080", 8080)]


def test_install_or_update_script_has_valid_bash_syntax():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not installed")

    result = subprocess.run(
        [bash, "-n", "scripts/install-or-update.sh"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_windows_install_or_update_script_has_valid_powershell_syntax():
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not installed")

    parse_command = (
        "$tokens = $null; "
        "$errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "'scripts/install-or-update.ps1', [ref]$tokens, [ref]$errors) | Out-Null; "
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
