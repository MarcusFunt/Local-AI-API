from __future__ import annotations

import pytest

from gateway import app_update

pytestmark = pytest.mark.asyncio


async def test_repo_update_status_unavailable_without_git(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(app_update.shutil, "which", lambda _name: None)

    status = await app_update.get_repo_update_status()

    assert status["available"] is False
    assert status["update_owner"] == "installer_schedule"
    assert "git executable" in status["reason"]


async def test_repo_update_status_is_read_only(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(app_update, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(app_update, "_availability", lambda _root: {"available": True, "root": str(tmp_path)})
    monkeypatch.setattr(
        app_update,
        "_read_git_state",
        lambda _root: {"branch": "main", "head": "abc1234", "upstream": "origin/main", "dirty": False},
    )

    status = await app_update.get_repo_update_status()

    assert status["status"] == "idle"
    assert status["update_owner"] == "installer_schedule"
    assert status["head"] == "abc1234"


async def test_repo_update_status_survives_temporary_git_error(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(app_update, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(app_update, "_availability", lambda _root: {"available": True, "root": str(tmp_path)})

    def unavailable(_root):
        raise OSError("temporary resource exhaustion")

    monkeypatch.setattr(app_update, "_read_git_state", unavailable)

    status = await app_update.get_repo_update_status()

    assert status["available"] is True
    assert status["status"] == "idle"
    assert "temporarily unavailable" in status["reason"]
