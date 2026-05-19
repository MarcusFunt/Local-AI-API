"""Tests for the graphical installer helper logic."""
from __future__ import annotations

import importlib.util
import py_compile
import sys
from pathlib import Path

import pytest

from gateway.normalize import CHATTERBOX_MODEL_MAP, MODEL_MAP, WHISPER_MODEL_MAP


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = REPO_ROOT / "scripts" / "install_gui.py"


@pytest.fixture(scope="module")
def installer():
    spec = importlib.util.spec_from_file_location("install_gui", INSTALLER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_gui_installer_has_valid_python_syntax():
    py_compile.compile(str(INSTALLER_PATH), doraise=True)


def test_gui_model_map_matches_gateway_aliases(installer):
    assert installer.read_model_map(REPO_ROOT) == MODEL_MAP


def test_gui_audio_model_maps_match_gateway_aliases(installer):
    assert installer.read_whisper_model_map(REPO_ROOT) == WHISPER_MODEL_MAP
    assert installer.read_chatterbox_model_map(REPO_ROOT) == CHATTERBOX_MODEL_MAP


def test_env_updates_keep_ollama_private_and_select_models(installer):
    config = installer.InstallConfig(
        models=["small", "dev"],
        default_profile="small",
        port=8080,
    )

    updates = installer.build_env_updates(config, MODEL_MAP)

    assert updates["OLLAMA_BASE_URL"] == "http://127.0.0.1:11434"
    assert updates["HOST"] == "127.0.0.1"
    assert updates["ENABLE_ARBITRARY_MODELS"] == "false"
    assert updates["OLLAMA_MODELS"] == "qwen3.5:4b qwen3.5:0.8b"
    assert updates["DEFAULT_WHISPER_MODEL"] == "none"
    assert updates["CHATTERBOX_MODEL"] == "chatterbox"


def test_env_updates_can_select_whisper_model(installer):
    config = installer.InstallConfig(
        models=["dev"],
        default_profile="dev",
        whisper_model="base",
    )

    updates = installer.build_env_updates(config, MODEL_MAP, WHISPER_MODEL_MAP, CHATTERBOX_MODEL_MAP)

    assert updates["DEFAULT_WHISPER_MODEL"] == "base"


def test_gui_rejects_unknown_whisper_model(installer):
    config = installer.InstallConfig(
        models=["dev"],
        default_profile="dev",
        whisper_model="large",
    )

    errors = installer.validate_config(config, MODEL_MAP, whisper_model_map=WHISPER_MODEL_MAP)

    assert any("Whisper" in error for error in errors)


def test_gui_rejects_gateway_port_reserved_for_ollama(installer):
    config = installer.InstallConfig(models=["dev"], default_profile="dev", port=11434)

    errors = installer.validate_config(config, MODEL_MAP, system="Windows")

    assert any("11434" in error for error in errors)


def test_windows_install_command_includes_schedule_options(installer):
    config = installer.InstallConfig(
        models=["main"],
        default_profile="main",
        accelerator="cpu",
        configure_tailscale=False,
        sync_repo=False,
        update_schedule="every-hours",
        update_time="02:30",
        every_hours=6,
    )

    command = installer.build_install_command(REPO_ROOT, config, system="Windows")

    assert command[:5] == ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    assert "-Accelerator" in command
    assert "cpu" in command
    assert "-SkipRepoSync" in command
    assert "-SkipTailscaleServe" in command
    assert command[command.index("-UpdateSchedule") + 1] == "every-hours"
    assert command[command.index("-UpdateTime") + 1] == "02:30"
    assert command[command.index("-EveryHours") + 1] == "6"


def test_linux_install_command_includes_schedule_options(installer):
    config = installer.InstallConfig(
        models=["dev"],
        default_profile="dev",
        accelerator="amd",
        update_schedule="weekly",
        update_time="04:15",
        weekly_day="Monday",
    )

    command = installer.build_install_command(REPO_ROOT, config, system="Linux")

    assert command[:2] == ["bash", str(REPO_ROOT / "scripts" / "install-or-update.sh")]
    assert command[command.index("--accelerator") + 1] == "amd"
    assert command[command.index("--update-schedule") + 1] == "weekly"
    assert command[command.index("--update-time") + 1] == "04:15"
    assert command[command.index("--weekly-day") + 1] == "Monday"
