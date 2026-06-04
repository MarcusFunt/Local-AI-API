#!/usr/bin/env python3
"""Graphical installer for the Local AI API deployment scripts."""
from __future__ import annotations

import ast
import json
import os
import platform
import queue
import re
import secrets
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
GUI_STATE_PATH = Path(".local") / "install-gui.json"
FALLBACK_MODEL_MAP = {
    "main": "qwen3.5:9b",
    "small": "qwen3.5:4b",
    "dev": "qwen3.5:0.8b",
    "agent": "qwen3:14b",
    "agent-utility": "qwen3:8b",
}
FALLBACK_WHISPER_MODEL_MAP: dict[str, str | None] = {
    "none": None,
    "tiny": "tiny",
    "base": "base",
    "small": "small",
}
FALLBACK_CHATTERBOX_MODEL_MAP = {
    "chatterbox": "chatterbox",
    "chatterbox-multilingual": "chatterbox-multilingual",
}
SCHEDULE_LABELS = {
    "default": "At startup/logon and daily",
    "none": "No automatic updates",
    "logon": "At startup/logon only",
    "daily": "Daily",
    "weekly": "Weekly",
    "every-hours": "Every N hours",
}
SCHEDULE_KEYS_BY_LABEL = {label: key for key, label in SCHEDULE_LABELS.items()}
SCHEDULE_ORDER = ["default", "none", "logon", "daily", "weekly", "every-hours"]
WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
EVERY_HOURS_CHOICES = (1, 2, 3, 4, 6, 8, 12, 24)
ACCELERATOR_CHOICES = ("auto", "cpu", "nvidia", "amd")
WINDOWS_ACCELERATOR_CHOICES = ("auto", "cpu", "nvidia")
TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
DEFAULT_SELECTED_MODEL_ALIASES = ("main", "small", "dev")
LogFn = Callable[[str], None]


def _assignment_value_for_name(node: ast.stmt, name: str) -> ast.expr | None:
    if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == name for target in node.targets
    ):
        return node.value
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
        return node.value
    return None


@dataclass
class InstallConfig:
    models: list[str] = field(default_factory=lambda: ["main", "small", "dev"])
    default_profile: str = "main"
    port: int = 8080
    accelerator: str = "auto"
    configure_tailscale: bool = True
    sync_repo: bool = True
    update_schedule: str = "default"
    update_time: str = "03:00"
    weekly_day: str = "Sunday"
    every_hours: int = 6
    whisper_model: str = "none"
    chatterbox_model: str = "chatterbox"
    enable_api_key_auth: bool = False
    api_key: str = ""
    agent_zero_enabled: bool = False
    agent_zero_port: int = 50080
    agent_zero_tailscale_https_port: int = 8443


def read_model_map(repo_root: Path = REPO_ROOT) -> dict[str, str]:
    normalize_path = repo_root / "gateway" / "normalize.py"
    try:
        tree = ast.parse(normalize_path.read_text(encoding="utf-8"))
    except OSError:
        return FALLBACK_MODEL_MAP.copy()

    for node in tree.body:
        assignment_value = _assignment_value_for_name(node, "MODEL_MAP")
        if assignment_value is None:
            continue
        value = ast.literal_eval(assignment_value)
        if isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            return dict(value)

    return FALLBACK_MODEL_MAP.copy()


def read_whisper_model_map(repo_root: Path = REPO_ROOT) -> dict[str, str | None]:
    normalize_path = repo_root / "gateway" / "normalize.py"
    try:
        tree = ast.parse(normalize_path.read_text(encoding="utf-8"))
    except OSError:
        return FALLBACK_WHISPER_MODEL_MAP.copy()

    for node in tree.body:
        assignment_value = _assignment_value_for_name(node, "WHISPER_MODEL_MAP")
        if assignment_value is None:
            continue
        value = ast.literal_eval(assignment_value)
        if isinstance(value, dict) and all(
            isinstance(k, str) and (isinstance(v, str) or v is None) for k, v in value.items()
        ):
            return dict(value)

    return FALLBACK_WHISPER_MODEL_MAP.copy()


def read_chatterbox_model_map(repo_root: Path = REPO_ROOT) -> dict[str, str]:
    normalize_path = repo_root / "gateway" / "normalize.py"
    try:
        tree = ast.parse(normalize_path.read_text(encoding="utf-8"))
    except OSError:
        return FALLBACK_CHATTERBOX_MODEL_MAP.copy()

    for node in tree.body:
        assignment_value = _assignment_value_for_name(node, "CHATTERBOX_MODEL_MAP")
        if assignment_value is None:
            continue
        value = ast.literal_eval(assignment_value)
        if isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            return dict(value)

    return FALLBACK_CHATTERBOX_MODEL_MAP.copy()


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        values[key] = value
    return values


def _env_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _aliases_from_model_tags(model_map: dict[str, str], model_tags: str) -> list[str]:
    selected_tags = model_tags.split()
    aliases = [alias for alias, tag in model_map.items() if tag in selected_tags]
    return aliases or default_selected_model_aliases(model_map)


def default_selected_model_aliases(model_map: dict[str, str]) -> list[str]:
    aliases = [alias for alias in DEFAULT_SELECTED_MODEL_ALIASES if alias in model_map]
    return aliases or list(model_map)


def load_previous_config(
    repo_root: Path,
    model_map: dict[str, str],
    whisper_model_map: dict[str, str | None] | None = None,
    chatterbox_model_map: dict[str, str] | None = None,
) -> InstallConfig:
    config = InstallConfig(models=default_selected_model_aliases(model_map))
    whisper_model_map = whisper_model_map or FALLBACK_WHISPER_MODEL_MAP
    chatterbox_model_map = chatterbox_model_map or FALLBACK_CHATTERBOX_MODEL_MAP
    env_values = parse_env_file(repo_root / ".env")

    if env_values.get("OLLAMA_MODELS"):
        config.models = _aliases_from_model_tags(model_map, env_values["OLLAMA_MODELS"])
    if env_values.get("DEFAULT_MODEL_PROFILE") in model_map:
        config.default_profile = env_values["DEFAULT_MODEL_PROFILE"]
    if env_values.get("DEFAULT_WHISPER_MODEL") in whisper_model_map:
        config.whisper_model = env_values["DEFAULT_WHISPER_MODEL"]
    if env_values.get("CHATTERBOX_MODEL") in chatterbox_model_map:
        config.chatterbox_model = env_values["CHATTERBOX_MODEL"]
    if env_values.get("PORT", "").isdigit():
        config.port = int(env_values["PORT"])
    config.agent_zero_enabled = _env_bool(env_values.get("AGENT_ZERO_ENABLED"))
    if env_values.get("AGENT_ZERO_PORT", "").isdigit():
        config.agent_zero_port = int(env_values["AGENT_ZERO_PORT"])
    if env_values.get("AGENT_ZERO_TAILSCALE_HTTPS_PORT", "").isdigit():
        config.agent_zero_tailscale_https_port = int(env_values["AGENT_ZERO_TAILSCALE_HTTPS_PORT"])
    config.enable_api_key_auth = _env_bool(env_values.get("ENABLE_API_KEY_AUTH"))
    config.api_key = env_values.get("API_KEY", "")

    state_path = repo_root / GUI_STATE_PATH
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        if isinstance(state, dict):
            for key in asdict(config):
                if key in state:
                    setattr(config, key, state[key])

    if not isinstance(config.models, list):
        config.models = default_selected_model_aliases(model_map)
    config.models = [
        alias for alias in config.models if isinstance(alias, str) and alias in model_map
    ] or default_selected_model_aliases(model_map)
    if config.default_profile not in model_map:
        config.default_profile = next(iter(model_map))
    if config.whisper_model not in whisper_model_map:
        config.whisper_model = "none"
    if config.chatterbox_model not in chatterbox_model_map:
        config.chatterbox_model = "chatterbox"
    try:
        config.port = int(config.port)
    except (TypeError, ValueError):
        config.port = 8080
    try:
        config.every_hours = int(config.every_hours)
    except (TypeError, ValueError):
        config.every_hours = 6
    try:
        config.agent_zero_port = int(config.agent_zero_port)
    except (TypeError, ValueError):
        config.agent_zero_port = 50080
    try:
        config.agent_zero_tailscale_https_port = int(config.agent_zero_tailscale_https_port)
    except (TypeError, ValueError):
        config.agent_zero_tailscale_https_port = 8443

    return config


def selected_model_tags(model_map: dict[str, str], aliases: list[str]) -> list[str]:
    tags: list[str] = []
    for alias in aliases:
        tag = model_map[alias]
        if tag not in tags:
            tags.append(tag)
    return tags


def validate_config(
    config: InstallConfig,
    model_map: dict[str, str],
    system: str | None = None,
    whisper_model_map: dict[str, str | None] | None = None,
    chatterbox_model_map: dict[str, str] | None = None,
) -> list[str]:
    errors: list[str] = []
    selected = set(config.models)
    whisper_model_map = whisper_model_map or FALLBACK_WHISPER_MODEL_MAP
    chatterbox_model_map = chatterbox_model_map or FALLBACK_CHATTERBOX_MODEL_MAP

    if not selected:
        errors.append("Choose at least one model to install.")
    unknown_models = selected - set(model_map)
    if unknown_models:
        errors.append(f"Unknown model aliases: {', '.join(sorted(unknown_models))}.")
    if config.default_profile not in model_map:
        errors.append("Choose a valid default model profile.")
    elif config.default_profile not in selected:
        errors.append("The default model profile must also be selected for installation.")
    if config.agent_zero_enabled:
        missing_agent_aliases = {"agent", "agent-utility"} - set(model_map)
        if missing_agent_aliases:
            errors.append(
                "Agent Zero requires model aliases: "
                + ", ".join(sorted(missing_agent_aliases))
                + "."
            )
    gateway_port: int | None = None
    try:
        gateway_port = int(config.port)
    except (TypeError, ValueError):
        errors.append("Gateway port must be a number.")
    else:
        if not 1 <= gateway_port <= 65535:
            errors.append("Gateway port must be between 1 and 65535.")
        if gateway_port == 11434:
            errors.append("Port 11434 is reserved for private Ollama loopback traffic.")
    for label, value in (
        ("Agent Zero local UI port", config.agent_zero_port),
        ("Agent Zero Tailscale HTTPS port", config.agent_zero_tailscale_https_port),
    ):
        try:
            agent_port = int(value)
        except (TypeError, ValueError):
            errors.append(f"{label} must be a number.")
            continue
        if not 1 <= agent_port <= 65535:
            errors.append(f"{label} must be between 1 and 65535.")
        if agent_port == 11434:
            errors.append(f"{label} cannot use private Ollama port 11434.")
    try:
        if (
            config.agent_zero_enabled
            and gateway_port is not None
            and int(config.agent_zero_port) == gateway_port
        ):
            errors.append("Agent Zero local UI port must be different from the gateway port.")
    except (TypeError, ValueError):
        pass
    if config.accelerator not in ACCELERATOR_CHOICES:
        errors.append("Choose a valid accelerator profile.")
    if (system or platform.system()).lower().startswith("win") and config.accelerator == "amd":
        errors.append("AMD/ROCm acceleration is only supported by this project on Linux.")
    if config.update_schedule not in SCHEDULE_LABELS:
        errors.append("Choose a valid update schedule.")
    if not TIME_RE.match(config.update_time):
        errors.append("Update time must use 24-hour HH:mm format.")
    if config.weekly_day not in WEEKDAYS:
        errors.append("Choose a valid weekly update day.")
    try:
        every_hours = int(config.every_hours)
    except (TypeError, ValueError):
        errors.append("Every-hours interval must be numeric.")
    else:
        if every_hours not in EVERY_HOURS_CHOICES:
            errors.append("Every-hours interval must divide evenly into 24.")
    if config.whisper_model not in whisper_model_map:
        errors.append("Choose a valid Whisper model or none.")
    if config.chatterbox_model not in chatterbox_model_map:
        errors.append("Choose a valid Chatterbox model.")
    if config.enable_api_key_auth:
        if not config.api_key.strip():
            errors.append("API-key auth requires a non-empty API key.")
        elif any(char.isspace() for char in config.api_key):
            errors.append("API key cannot contain whitespace.")

    return errors


def ensure_valid_config(
    config: InstallConfig,
    model_map: dict[str, str],
    system: str | None = None,
    whisper_model_map: dict[str, str | None] | None = None,
    chatterbox_model_map: dict[str, str] | None = None,
) -> None:
    errors = validate_config(config, model_map, system, whisper_model_map, chatterbox_model_map)
    if errors:
        raise ValueError("\n".join(errors))


def build_env_updates(
    config: InstallConfig,
    model_map: dict[str, str],
    whisper_model_map: dict[str, str | None] | None = None,
    chatterbox_model_map: dict[str, str] | None = None,
) -> dict[str, str]:
    ensure_valid_config(config, model_map, whisper_model_map=whisper_model_map, chatterbox_model_map=chatterbox_model_map)
    model_aliases = list(config.models)
    if config.agent_zero_enabled:
        for alias in ("agent", "agent-utility"):
            if alias in model_map and alias not in model_aliases:
                model_aliases.append(alias)
    return {
        "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
        "HOST": "127.0.0.1",
        "PORT": str(config.port),
        "DEFAULT_MODEL_PROFILE": config.default_profile,
        "DEFAULT_WHISPER_MODEL": config.whisper_model,
        "WHISPER_DEVICE": "auto",
        "CHATTERBOX_MODEL": config.chatterbox_model,
        "CHATTERBOX_DEVICE": "auto",
        "OLLAMA_MODELS": " ".join(selected_model_tags(model_map, model_aliases)),
        "ENABLE_ARBITRARY_MODELS": "false",
        "AGENT_ZERO_ENABLED": "true" if config.agent_zero_enabled else "false",
        "AGENT_ZERO_PORT": str(config.agent_zero_port),
        "AGENT_ZERO_TAILSCALE_HTTPS_PORT": str(config.agent_zero_tailscale_https_port),
        "ENABLE_API_KEY_AUTH": "true" if config.enable_api_key_auth else "false",
        "API_KEY": config.api_key.strip() if config.enable_api_key_auth else "",
    }


def render_env_file(existing: str, updates: dict[str, str]) -> str:
    seen: set[str] = set()
    rendered: list[str] = []
    key_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")

    for line in existing.splitlines():
        match = key_re.match(line)
        if match and match.group(1) in updates:
            key = match.group(1)
            rendered.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            rendered.append(line)

    missing = [key for key in updates if key not in seen]
    if missing:
        if rendered and rendered[-1].strip():
            rendered.append("")
        rendered.append("# Written by scripts/install_gui.py")
        for key in missing:
            rendered.append(f"{key}={updates[key]}")

    return "\n".join(rendered).rstrip() + "\n"


def write_env_file(
    repo_root: Path,
    config: InstallConfig,
    model_map: dict[str, str],
    whisper_model_map: dict[str, str | None] | None = None,
    chatterbox_model_map: dict[str, str] | None = None,
) -> Path:
    env_path = repo_root / ".env"
    source_path = env_path if env_path.exists() else repo_root / ".env.example"
    existing = source_path.read_text(encoding="utf-8") if source_path.exists() else ""
    env_path.write_text(
        render_env_file(
            existing,
            build_env_updates(config, model_map, whisper_model_map, chatterbox_model_map),
        ),
        encoding="utf-8",
    )
    return env_path


def save_gui_state(repo_root: Path, config: InstallConfig) -> Path:
    state_path = repo_root / GUI_STATE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(asdict(config), indent=2) + "\n", encoding="utf-8")
    return state_path


def build_install_command(repo_root: Path, config: InstallConfig, system: str | None = None) -> list[str]:
    system_name = (system or platform.system()).lower()
    if system_name.startswith("win"):
        if config.accelerator == "amd":
            raise ValueError("AMD/ROCm acceleration is only supported by this project on Linux.")
        command = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(repo_root / "scripts" / "install-or-update.ps1"),
        ]
        if config.accelerator != "auto":
            command.extend(["-Accelerator", config.accelerator])
        if not config.sync_repo:
            command.append("-SkipRepoSync")
        if not config.configure_tailscale:
            command.append("-SkipTailscaleServe")
        if config.agent_zero_enabled:
            command.append("-AgentZero")
        command.extend(
            [
                "-UpdateSchedule",
                config.update_schedule,
                "-UpdateTime",
                config.update_time,
                "-WeeklyDay",
                config.weekly_day,
                "-EveryHours",
                str(config.every_hours),
            ]
        )
        return command

    if system_name == "linux":
        command = ["bash", str(repo_root / "scripts" / "install-or-update.sh")]
        if config.accelerator != "auto":
            command.extend(["--accelerator", config.accelerator])
        if not config.sync_repo:
            command.append("--skip-repo-sync")
        if not config.configure_tailscale:
            command.append("--skip-tailscale-serve")
        command.extend(
            [
                "--update-schedule",
                config.update_schedule,
                "--update-time",
                config.update_time,
                "--weekly-day",
                config.weekly_day,
                "--every-hours",
                str(config.every_hours),
            ]
        )
        return command

    raise ValueError(f"Unsupported operating system for the graphical installer: {system or platform.system()}")


def _command_for_log(command: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in command)


def run_process(command: list[str], cwd: Path, log: LogFn) -> None:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if platform.system().lower().startswith("win") else 0
    log(f"$ {_command_for_log(command)}")
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    assert process.stdout is not None
    for line in process.stdout:
        log(line.rstrip())
    exit_code = process.wait()
    if exit_code != 0:
        raise RuntimeError(f"Installer failed with exit code {exit_code}.")


def install_or_update(repo_root: Path, config: InstallConfig, log: LogFn) -> None:
    model_map = read_model_map(repo_root)
    whisper_model_map = read_whisper_model_map(repo_root)
    chatterbox_model_map = read_chatterbox_model_map(repo_root)
    ensure_valid_config(config, model_map, whisper_model_map=whisper_model_map, chatterbox_model_map=chatterbox_model_map)
    env_path = write_env_file(repo_root, config, model_map, whisper_model_map, chatterbox_model_map)
    state_path = save_gui_state(repo_root, config)
    log(f"Wrote {env_path}")
    log(f"Wrote {state_path}")
    log("Starting install/update. This can take a while when Docker builds images or pulls models.")
    run_process(build_install_command(repo_root, config), repo_root, log)
    log("Install/update complete.")
    log(f"Gateway: http://127.0.0.1:{config.port}/")
    if config.agent_zero_enabled:
        log(f"Agent Zero: http://127.0.0.1:{config.agent_zero_port}/")
        log(f"Agent Zero over Tailscale Serve: https://<machine>.ts.net:{config.agent_zero_tailscale_https_port}/")


def run_gui() -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    model_map = read_model_map(REPO_ROOT)
    whisper_model_map = read_whisper_model_map(REPO_ROOT)
    chatterbox_model_map = read_chatterbox_model_map(REPO_ROOT)
    initial_config = load_previous_config(REPO_ROOT, model_map, whisper_model_map, chatterbox_model_map)

    class InstallerApp(ttk.Frame):
        def __init__(self, master: Any) -> None:
            super().__init__(master, padding=14)
            self.master = master
            self.log_queue: queue.Queue[tuple[Any, ...]] = queue.Queue()
            self.model_vars: dict[str, Any] = {}
            self.default_profile_var = tk.StringVar(value=initial_config.default_profile)
            accelerator_values = WINDOWS_ACCELERATOR_CHOICES if platform.system() == "Windows" else ACCELERATOR_CHOICES
            accelerator = initial_config.accelerator if initial_config.accelerator in accelerator_values else "auto"
            self.accelerator_var = tk.StringVar(value=accelerator)
            self.configure_tailscale_var = tk.BooleanVar(value=initial_config.configure_tailscale)
            self.sync_repo_var = tk.BooleanVar(value=initial_config.sync_repo)
            self.auth_var = tk.BooleanVar(value=initial_config.enable_api_key_auth)
            self.api_key_var = tk.StringVar(value=initial_config.api_key)
            self.agent_zero_var = tk.BooleanVar(value=initial_config.agent_zero_enabled)
            self.whisper_model_var = tk.StringVar(value=initial_config.whisper_model)
            schedule_label = SCHEDULE_LABELS.get(initial_config.update_schedule, SCHEDULE_LABELS["default"])
            self.schedule_var = tk.StringVar(value=schedule_label)
            self.update_time_var = tk.StringVar(value=initial_config.update_time)
            self.weekly_day_var = tk.StringVar(value=initial_config.weekly_day)
            self.every_hours_var = tk.StringVar(value=str(initial_config.every_hours))
            self.install_button: ttk.Button | None = None
            self.save_button: ttk.Button | None = None
            self.log_text: ScrolledText | None = None
            self._build_widgets(accelerator_values)
            self.after(100, self._drain_log_queue)

        def _build_widgets(self, accelerator_values: tuple[str, ...]) -> None:
            self.grid(row=0, column=0, sticky="nsew")
            self.columnconfigure(0, weight=1)

            intro = ttk.Label(
                self,
                text=(
                    "Configure Local AI API, choose models, and set the repository auto-update schedule. "
                    "Auto-updates force-sync tracked files to the configured Git remote."
                ),
                wraplength=820,
            )
            intro.grid(row=0, column=0, sticky="ew", pady=(0, 12))

            model_frame = ttk.LabelFrame(self, text="Models", padding=10)
            model_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
            model_frame.columnconfigure(1, weight=1)
            for index, (alias, tag) in enumerate(model_map.items()):
                var = tk.BooleanVar(value=alias in initial_config.models)
                self.model_vars[alias] = var
                ttk.Checkbutton(model_frame, text=f"{alias} ({tag})", variable=var).grid(
                    row=index, column=0, sticky="w", padx=(0, 18), pady=2
                )
            ttk.Label(model_frame, text="Default model").grid(row=0, column=1, sticky="w")
            default_combo = ttk.Combobox(
                model_frame,
                textvariable=self.default_profile_var,
                values=tuple(model_map),
                state="readonly",
                width=18,
            )
            default_combo.grid(row=1, column=1, sticky="w")
            default_combo.bind("<<ComboboxSelected>>", self._select_default_model)

            speech_frame = ttk.LabelFrame(self, text="Speech", padding=10)
            speech_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))
            speech_frame.columnconfigure(1, weight=1)
            ttk.Label(speech_frame, text="Whisper transcription").grid(row=0, column=0, sticky="w", pady=2)
            ttk.Combobox(
                speech_frame,
                textvariable=self.whisper_model_var,
                values=tuple(whisper_model_map),
                state="readonly",
                width=18,
            ).grid(row=0, column=1, sticky="w", pady=2)
            ttk.Label(
                speech_frame,
                text="Choose none to disable speech-to-text; Chatterbox TTS is enabled by default.",
            ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

            gateway_frame = ttk.LabelFrame(self, text="Gateway and setup", padding=10)
            gateway_frame.grid(row=3, column=0, sticky="ew", pady=(0, 10))
            gateway_frame.columnconfigure(1, weight=1)
            ttk.Label(gateway_frame, text="Gateway port").grid(row=0, column=0, sticky="w", pady=2)
            ttk.Label(gateway_frame, text="8080 (fixed for Docker and Tailscale Serve)").grid(
                row=0, column=1, sticky="w", pady=2
            )
            ttk.Label(gateway_frame, text="Accelerator").grid(row=1, column=0, sticky="w", pady=2)
            ttk.Combobox(
                gateway_frame,
                textvariable=self.accelerator_var,
                values=accelerator_values,
                state="readonly",
                width=12,
            ).grid(row=1, column=1, sticky="w", pady=2)
            ttk.Checkbutton(
                gateway_frame,
                text="Configure Tailscale Serve for http://127.0.0.1:8080",
                variable=self.configure_tailscale_var,
            ).grid(row=2, column=0, columnspan=2, sticky="w", pady=2)
            ttk.Checkbutton(
                gateway_frame,
                text="Install Agent Zero UI on 127.0.0.1:50080 and Tailscale HTTPS port 8443",
                variable=self.agent_zero_var,
            ).grid(row=3, column=0, columnspan=2, sticky="w", pady=2)
            ttk.Label(
                gateway_frame,
                text="Use Tailscale Serve for Agent Zero. Do not enable its public Remote Link or tunnel options.",
            ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 6))
            ttk.Checkbutton(
                gateway_frame,
                text="Sync repository before install/update",
                variable=self.sync_repo_var,
            ).grid(row=5, column=0, columnspan=2, sticky="w", pady=2)
            ttk.Checkbutton(
                gateway_frame,
                text="Enable optional bearer-token auth",
                variable=self.auth_var,
            ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 2))
            ttk.Label(gateway_frame, text="API key").grid(row=7, column=0, sticky="w", pady=2)
            ttk.Entry(gateway_frame, textvariable=self.api_key_var, width=52, show="*").grid(
                row=7, column=1, sticky="w", pady=2
            )
            ttk.Button(gateway_frame, text="Generate key", command=self._generate_api_key).grid(
                row=7, column=2, sticky="w", padx=(8, 0), pady=2
            )

            schedule_frame = ttk.LabelFrame(self, text="Repository auto-updates", padding=10)
            schedule_frame.grid(row=4, column=0, sticky="ew", pady=(0, 10))
            schedule_frame.columnconfigure(1, weight=1)
            schedule_values = tuple(SCHEDULE_LABELS[key] for key in SCHEDULE_ORDER)
            ttk.Label(schedule_frame, text="Schedule").grid(row=0, column=0, sticky="w", pady=2)
            ttk.Combobox(
                schedule_frame,
                textvariable=self.schedule_var,
                values=schedule_values,
                state="readonly",
                width=30,
            ).grid(row=0, column=1, sticky="w", pady=2)
            ttk.Label(schedule_frame, text="Time (HH:mm)").grid(row=1, column=0, sticky="w", pady=2)
            ttk.Entry(schedule_frame, textvariable=self.update_time_var, width=10).grid(
                row=1, column=1, sticky="w", pady=2
            )
            ttk.Label(schedule_frame, text="Weekly day").grid(row=2, column=0, sticky="w", pady=2)
            ttk.Combobox(
                schedule_frame,
                textvariable=self.weekly_day_var,
                values=WEEKDAYS,
                state="readonly",
                width=14,
            ).grid(row=2, column=1, sticky="w", pady=2)
            ttk.Label(schedule_frame, text="Every").grid(row=3, column=0, sticky="w", pady=2)
            ttk.Combobox(
                schedule_frame,
                textvariable=self.every_hours_var,
                values=tuple(str(value) for value in EVERY_HOURS_CHOICES),
                state="readonly",
                width=8,
            ).grid(row=3, column=1, sticky="w", pady=2)
            ttk.Label(schedule_frame, text="hours").grid(row=3, column=1, sticky="w", padx=(72, 0), pady=2)

            button_frame = ttk.Frame(self)
            button_frame.grid(row=5, column=0, sticky="ew", pady=(0, 10))
            button_frame.columnconfigure(0, weight=1)
            self.save_button = ttk.Button(button_frame, text="Save settings", command=self._save_settings)
            self.save_button.grid(row=0, column=1, padx=(0, 8))
            self.install_button = ttk.Button(button_frame, text="Install / update", command=self._start_install)
            self.install_button.grid(row=0, column=2)

            log_frame = ttk.LabelFrame(self, text="Installer log", padding=8)
            log_frame.grid(row=6, column=0, sticky="nsew")
            log_frame.rowconfigure(0, weight=1)
            log_frame.columnconfigure(0, weight=1)
            self.rowconfigure(6, weight=1)
            self.log_text = ScrolledText(log_frame, height=15, wrap="word")
            self.log_text.grid(row=0, column=0, sticky="nsew")
            self._append_log("Ready.")

        def _select_default_model(self, _event: Any = None) -> None:
            alias = self.default_profile_var.get()
            if alias in self.model_vars:
                self.model_vars[alias].set(True)

        def _generate_api_key(self) -> None:
            self.api_key_var.set(secrets.token_urlsafe(32))
            self.auth_var.set(True)

        def _collect_config(self) -> InstallConfig:
            try:
                every_hours = int(self.every_hours_var.get())
            except ValueError as exc:
                raise ValueError("Every-hours interval must be a number.") from exc
            return InstallConfig(
                models=[alias for alias, var in self.model_vars.items() if var.get()],
                default_profile=self.default_profile_var.get(),
                port=8080,
                accelerator=self.accelerator_var.get(),
                configure_tailscale=self.configure_tailscale_var.get(),
                sync_repo=self.sync_repo_var.get(),
                update_schedule=SCHEDULE_KEYS_BY_LABEL.get(self.schedule_var.get(), "default"),
                update_time=self.update_time_var.get().strip(),
                weekly_day=self.weekly_day_var.get(),
                every_hours=every_hours,
                whisper_model=self.whisper_model_var.get(),
                chatterbox_model=initial_config.chatterbox_model,
                enable_api_key_auth=self.auth_var.get(),
                api_key=self.api_key_var.get().strip(),
                agent_zero_enabled=self.agent_zero_var.get(),
                agent_zero_port=50080,
                agent_zero_tailscale_https_port=8443,
            )

        def _save_settings(self) -> None:
            try:
                config = self._collect_config()
                ensure_valid_config(
                    config,
                    model_map,
                    whisper_model_map=whisper_model_map,
                    chatterbox_model_map=chatterbox_model_map,
                )
                env_path = write_env_file(REPO_ROOT, config, model_map, whisper_model_map, chatterbox_model_map)
                state_path = save_gui_state(REPO_ROOT, config)
            except Exception as exc:
                messagebox.showerror("Invalid settings", str(exc))
                return
            self._append_log(f"Wrote {env_path}")
            self._append_log(f"Wrote {state_path}")
            messagebox.showinfo("Settings saved", "Installer settings were saved.")

        def _start_install(self) -> None:
            try:
                config = self._collect_config()
                ensure_valid_config(
                    config,
                    model_map,
                    whisper_model_map=whisper_model_map,
                    chatterbox_model_map=chatterbox_model_map,
                )
            except Exception as exc:
                messagebox.showerror("Invalid settings", str(exc))
                return
            if self.install_button is not None:
                self.install_button.configure(state="disabled")
            if self.save_button is not None:
                self.save_button.configure(state="disabled")
            self._append_log("Starting install/update.")
            thread = threading.Thread(target=self._install_worker, args=(config,), daemon=True)
            thread.start()

        def _install_worker(self, config: InstallConfig) -> None:
            try:
                install_or_update(REPO_ROOT, config, self._thread_log)
            except Exception as exc:
                self.log_queue.put(("done", False, str(exc)))
                return
            self.log_queue.put(("done", True, "Install/update complete."))

        def _thread_log(self, message: str) -> None:
            self.log_queue.put(("log", message))

        def _append_log(self, message: str) -> None:
            if self.log_text is None:
                return
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")

        def _drain_log_queue(self) -> None:
            while True:
                try:
                    item = self.log_queue.get_nowait()
                except queue.Empty:
                    break
                kind = item[0]
                if kind == "log":
                    self._append_log(str(item[1]))
                elif kind == "done":
                    success = bool(item[1])
                    message = str(item[2])
                    self._append_log(message)
                    if self.install_button is not None:
                        self.install_button.configure(state="normal")
                    if self.save_button is not None:
                        self.save_button.configure(state="normal")
                    if success:
                        messagebox.showinfo("Install complete", message)
                    else:
                        messagebox.showerror("Install failed", message)
            self.after(100, self._drain_log_queue)

    root = tk.Tk()
    root.title("Local AI API Installer")
    root.minsize(900, 760)
    root.rowconfigure(0, weight=1)
    root.columnconfigure(0, weight=1)
    ttk.Style().configure("TFrame", font=("Segoe UI", 10))
    InstallerApp(root)
    root.mainloop()


def main() -> None:
    run_gui()


if __name__ == "__main__":
    main()
