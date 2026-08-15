"""Static isolation contracts for the user-visible sandbox stack."""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_sandbox_is_loopback_only_and_keeps_the_auditor_internal() -> None:
    compose = (REPO_ROOT / "compose.sandbox.yaml").read_text(encoding="utf-8")
    auditor = compose.split("sandbox-ui-auditor:", maxsplit=1)[1]

    assert '"127.0.0.1:${SANDBOX_AGENT_ZERO_PORT:-50082}:80"' in compose
    assert "internal: true" in compose
    assert "sandbox-ui" in auditor
    assert "/var/run/docker.sock" not in compose
    assert "target: /source" in compose
    assert "read_only: true" in compose
    assert "host.docker.internal:8080/v1" in compose
    assert '"--max-pages", "25"' in compose
    assert '"--max-depth", "3"' in compose


def test_sandbox_release_helper_keeps_publish_outside_the_sandbox_stack() -> None:
    script = (REPO_ROOT / "scripts" / "sandbox_release.py").read_text(encoding="utf-8")

    assert 'choices=("stage", "status", "approve")' in script
    assert '"git", "push", "origin", "main", "--follow-tags"' in script
    assert '"docker", "compose", "up"' not in script
