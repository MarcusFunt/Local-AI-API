from __future__ import annotations

import json
from pathlib import Path

import repo_ops.preview_worker as worker


def test_preview_worker_runs_audit_with_isolated_runtime_paths(tmp_path, monkeypatch):
    task_id = "preview-check"
    workspace = tmp_path / task_id
    workspace.mkdir()
    results = tmp_path / ".preview-results"
    results.mkdir()
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    monkeypatch.setattr(worker, "RESULTS", results)
    monkeypatch.setattr(worker, "_free_port", lambda: 48123)
    monkeypatch.setattr(worker.time, "sleep", lambda _seconds: None)

    class FakeServer:
        def terminate(self) -> None:
            return None

        def wait(self, timeout: int) -> None:
            assert timeout == 10

    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_args, **_kwargs: FakeServer())

    def fake_run(args, cwd, env, capture_output, text, timeout, check):
        assert cwd == workspace
        assert env["PYTHONPATH"] == "/app"
        assert env["PLAYWRIGHT_BROWSERS_PATH"] == "/ms-playwright"
        screenshot = Path(args[args.index("--screenshot") + 1])
        screenshot.write_bytes(b"png")
        return type("Result", (), {"returncode": 0, "stdout": json.dumps({"title": "Status"}), "stderr": ""})()

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    result = worker._run(task_id)

    assert result["status"] == "passed"
    assert result["screenshot"] == str(results / f"{task_id}.png")


def test_verification_runs_only_from_a_temporary_workspace_copy(tmp_path, monkeypatch):
    task_id = "verify-check"
    workspace = tmp_path / task_id
    workspace.mkdir()
    (workspace / "gateway").mkdir()
    (workspace / "gateway" / "sample.py").write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(worker, "ROOT", tmp_path)

    def fake_run(args, cwd, env, capture_output, text, timeout, check):
        assert args[-2:] == ["compileall", "gateway"]
        assert cwd != workspace
        assert (cwd / "gateway" / "sample.py").read_text(encoding="utf-8") == "value = 1\n"
        assert env["NO_PROXY"] == "*"
        assert env["PYTEST_ADDOPTS"] == "-p no:cacheprovider"
        return type("Result", (), {"returncode": 0, "stdout": "compiled", "stderr": ""})()

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    result = worker._run_verification(
        {"job_id": "job", "task_id": task_id, "preset": "compile"}
    )

    assert result["passed"] is True
    assert result["output"] == "compiled"
