"""Static deployment checks for the opt-in repo-ops and skill-sandbox overlays."""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_repo_ops_overlay_is_internal_and_source_is_read_only():
    compose = (REPO_ROOT / "compose.repo-ops.yaml").read_text(encoding="utf-8")

    assert "ports:" not in compose
    assert 'target: /source' in compose
    assert "read_only: true" in compose
    assert 'target: /workspaces' in compose
    assert "cap_drop:" in compose
    assert "no-new-privileges:true" in compose
    assert "repo-ops-lifecycle:" in compose
    assert "repo-ops-preview:" in compose
    assert "network_mode: none" in compose
    assert "REPO_OPS_ARCHIVE_ROOT: /archives" in compose


def test_lifecycle_cleaner_has_no_source_or_agent_zero_mount():
    compose = (REPO_ROOT / "compose.repo-ops.yaml").read_text(encoding="utf-8")
    lifecycle = compose.split("repo-ops-lifecycle:", maxsplit=1)[1]

    assert "target: /source" not in lifecycle
    assert "agent-zero-data" not in lifecycle
    assert "ports:" not in lifecycle


def test_skill_sandbox_has_no_repo_mount_or_published_port():
    compose = (REPO_ROOT / "compose.skill-sandbox.yaml").read_text(encoding="utf-8")

    assert "ports:" not in compose
    assert "/source" not in compose
    assert "agent-zero-data" not in compose
    assert "agent-skill-quarantine" in compose
    assert "no-new-privileges:true" in compose


def test_preview_worker_is_unnetworked_and_has_no_source_or_archive_mount():
    compose = (REPO_ROOT / "compose.repo-ops.yaml").read_text(encoding="utf-8")
    preview = compose.split("repo-ops-preview:", maxsplit=1)[1]

    assert "network_mode: none" in preview
    assert "read_only: true" in preview
    assert "target: /source" not in preview
    assert "target: /archives" not in preview
    assert "ports:" not in preview
    assert "/var/run/docker.sock" not in preview
