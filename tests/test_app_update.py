from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gateway import app_update

pytestmark = pytest.mark.asyncio


async def test_repo_update_status_unavailable_without_git(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(app_update.shutil, "which", lambda _name: None)
    monkeypatch.setattr(app_update, "_LAST_UPDATE", None)

    status = await app_update.get_repo_update_status()

    assert status["available"] is False
    assert "git executable" in status["reason"]


async def test_repo_update_runs_fast_forward_pull(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    pull_commands: list[list[str]] = []
    states = [
        {"branch": "main", "head": "abc1234", "upstream": "origin/main", "dirty": False},
        {"branch": "main", "head": "def5678", "upstream": "origin/main", "dirty": False},
    ]

    monkeypatch.setattr(app_update, "_LAST_UPDATE", None)
    monkeypatch.setattr(app_update, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(app_update, "_availability", lambda _root: {"available": True, "root": str(tmp_path)})
    monkeypatch.setattr(app_update, "_read_git_state", lambda _root: states.pop(0))

    def fake_run_git(args: list[str], root: Path, timeout: int = 30):
        pull_commands.append(args)
        assert root == tmp_path
        assert timeout == app_update._PULL_TIMEOUT_SECONDS
        return subprocess.CompletedProcess(
            ["git", *args],
            0,
            stdout="Updating abc1234..def5678\nFast-forward\n",
            stderr="",
        )

    monkeypatch.setattr(app_update, "_run_git", fake_run_git)

    result = await app_update.run_repo_update()

    assert pull_commands == [["pull", "--ff-only"]]
    assert result["status"] == "passed"
    assert result["updated"] is True
    assert result["restart_recommended"] is True
    assert result["head_before"] == "abc1234"
    assert result["head_after"] == "def5678"


async def test_repo_update_reports_failed_pull(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state = {"branch": "main", "head": "abc1234", "upstream": "origin/main", "dirty": True}

    monkeypatch.setattr(app_update, "_LAST_UPDATE", None)
    monkeypatch.setattr(app_update, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(app_update, "_availability", lambda _root: {"available": True, "root": str(tmp_path)})
    monkeypatch.setattr(app_update, "_read_git_state", lambda _root: state)
    monkeypatch.setattr(
        app_update,
        "_run_git",
        lambda args, _root, _timeout=30: subprocess.CompletedProcess(
            ["git", *args],
            128,
            stdout="",
            stderr="fatal: Not possible to fast-forward, aborting.\n",
        ),
    )

    result = await app_update.run_repo_update()

    assert result["status"] == "failed"
    assert result["updated"] is False
    assert result["head_before"] == "abc1234"
    assert result["head_after"] == "abc1234"
    assert "fast-forward" in result["error"]
