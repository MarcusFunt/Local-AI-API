"""FastMCP surface for safe Agent Zero repository workspaces."""
from __future__ import annotations

import os
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from .core import RepoOpsConfig, RepoOpsError, RepoOpsManager


mcp = FastMCP(
    name="local-ai-api-repo-ops",
    instructions=(
        "Repository intelligence and isolated editing for Local AI API. "
        "All changes stay in disposable agent branches. This server cannot merge, push, "
        "deploy, run arbitrary commands, access the source checkout for writing, or modify "
        "the persistent Agent Zero configuration."
    ),
)
manager = RepoOpsManager(RepoOpsConfig.from_environment())


def _tool_error(exc: RepoOpsError) -> ToolError:
    return ToolError(str(exc))


@mcp.tool()
async def repo_status() -> dict[str, Any]:
    """Inspect the read-only source checkout and existing disposable workspaces."""
    try:
        return manager.repo_status()
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def create_workspace(
    task_id: Annotated[str, Field(description="Lowercase task identifier, for example status-ui-a11y.")],
) -> dict[str, str]:
    """Create an isolated Git branch from committed source; it cannot be pushed or merged."""
    try:
        return manager.create_workspace(task_id)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def workspace_status(
    task_id: Annotated[str, Field(description="Task whose managed lifecycle state is needed.")],
) -> dict[str, Any]:
    """Inspect a workspace's lease, archive availability, and automatic-cleanup status."""
    try:
        return manager.workspace_status(task_id)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def list_workspaces() -> list[dict[str, Any]]:
    """List live and archived disposable workspaces with their lifecycle status."""
    try:
        return manager.list_workspaces()
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def renew_workspace_lease(
    task_id: Annotated[str, Field(description="Active or paused task workspace to renew.")],
) -> dict[str, Any]:
    """Renew a 24-hour workspace lease without granting additional filesystem permissions."""
    try:
        return manager.renew_workspace_lease(task_id)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def pause_workspace(
    task_id: Annotated[str, Field(description="Active task workspace to pause.")],
    reason: Annotated[str, Field(description="Brief reason this task is intentionally paused.")],
) -> dict[str, Any]:
    """Pause a live workspace while retaining it for an explicit later resumption."""
    try:
        return manager.pause_workspace(task_id, reason)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def read_file(
    path: Annotated[str, Field(description="Repository-relative regular file path.")],
    task_id: Annotated[str | None, Field(description="Optional isolated task workspace.")] = None,
    start_line: Annotated[int, Field(ge=1, description="First line to return.")] = 1,
    end_line: Annotated[int, Field(ge=1, description="Last line to return, max 500 lines.")] = 200,
) -> dict[str, Any]:
    """Read bounded source or workspace content; traversal and symlink paths are rejected."""
    try:
        return manager.read_file(path, task_id, start_line, end_line)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def search_code(
    query: Annotated[str, Field(description="Plain ripgrep search expression, maximum 300 characters.")],
    path_glob: Annotated[str, Field(description="Optional safe file glob, such as gateway/**/*.py.")] = "",
    task_id: Annotated[str | None, Field(description="Optional isolated task workspace.")] = None,
) -> list[dict[str, Any]]:
    """Search source or a workspace with bounded results and no shell execution."""
    try:
        return manager.search_code(query, path_glob, task_id)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def improvement_inventory(
    task_id: Annotated[str | None, Field(description="Optional isolated task workspace.")] = None,
) -> dict[str, Any]:
    """Find TODOs, oversized files, and test candidates to prioritize the next small improvement."""
    try:
        return manager.improvement_inventory(task_id)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def symbol_context(
    symbol: Annotated[str, Field(description="GitNexus symbol name to inspect.")],
) -> dict[str, Any]:
    """Return callers, callees, and execution-flow context from the worker's GitNexus index."""
    try:
        return manager.symbol_context(symbol)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def impact_analysis(
    symbol: Annotated[str, Field(description="GitNexus symbol name whose upstream blast radius is needed.")],
) -> dict[str, Any]:
    """Return GitNexus upstream impact data before changing an existing symbol."""
    try:
        return manager.impact_analysis(symbol)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def write_file(
    task_id: Annotated[str, Field(description="Existing isolated task workspace.")],
    path: Annotated[str, Field(description="Workspace-relative regular file path.")],
    content: Annotated[str, Field(description="Full UTF-8 replacement content, maximum 1 MB.")],
    expected_sha256: Annotated[str | None, Field(description="Current file hash required when replacing an existing file.")] = None,
) -> dict[str, str]:
    """Replace one workspace file after verifying its current hash; source is never writable."""
    try:
        return manager.write_file(task_id, path, content, expected_sha256)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def run_check(
    task_id: Annotated[str, Field(description="Existing isolated task workspace.")],
    preset: Annotated[str, Field(description="One of unit, compile, compose_config, or ui_audit.")],
) -> dict[str, Any]:
    """Run one named verification preset; arbitrary shell commands are unavailable."""
    try:
        return manager.run_check(task_id, preset)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def capture_ui(
    task_id: Annotated[str, Field(description="Existing isolated task workspace.")],
) -> dict[str, Any]:
    """Queue an unnetworked preview of this workspace's status page."""
    try:
        return manager.capture_ui(task_id)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def preview_workspace(
    task_id: Annotated[str, Field(description="Existing isolated task workspace to preview without network access.")],
) -> dict[str, Any]:
    """Queue a disposable workspace's loopback-only UI preview and accessibility audit."""
    try:
        return manager.preview_workspace(task_id)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def preview_status(
    task_id: Annotated[str, Field(description="Task whose queued or completed preview evidence is needed.")],
) -> dict[str, Any]:
    """Read preview evidence without opening a network-facing endpoint."""
    try:
        return manager.preview_status(task_id)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def start_autonomous_run(
    task_id: Annotated[str, Field(description="Existing isolated task workspace.")],
    evaluation_id: Annotated[str, Field(description="Tracked evaluation suite identifier.")] = "core-contracts",
    policy: Annotated[dict[str, Any] | None, Field(description="Optional reductions to fixed runtime, storage, and evaluation limits.")] = None,
) -> dict[str, Any]:
    """Start a durable, bounded local run; it gains no extra filesystem or deployment permissions."""
    try:
        return manager.start_autonomous_run(task_id, evaluation_id, policy)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def autonomous_status(
    task_id: Annotated[str, Field(description="Autonomous workspace task to inspect.")],
) -> dict[str, Any]:
    """Return phase, evidence trend, resource use, stop reason, and preview state."""
    try:
        return manager.autonomous_status(task_id)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def record_autonomous_progress(
    task_id: Annotated[str, Field(description="Running autonomous workspace task.")],
    summary: Annotated[str, Field(description="Bounded account of an edit, diagnosis, or decision.")],
) -> dict[str, Any]:
    """Record auditable local-agent progress without allowing arbitrary process execution."""
    try:
        return manager.record_autonomous_progress(task_id, summary)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def evaluate_workspace(
    task_id: Annotated[str, Field(description="Running autonomous workspace task to evaluate.")],
) -> dict[str, Any]:
    """Run the named evaluation suite's fixed verification presets and record a score."""
    try:
        return manager.evaluate_workspace(task_id)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def pause_autonomous_run(
    task_id: Annotated[str, Field(description="Running autonomous workspace task.")],
    reason: Annotated[str, Field(description="Why the safe local run should pause.")],
) -> dict[str, Any]:
    """Pause a run with all evidence recoverable."""
    try:
        return manager.pause_autonomous_run(task_id, reason)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def resume_autonomous_run(
    task_id: Annotated[str, Field(description="Paused autonomous workspace task.")],
) -> dict[str, Any]:
    """Resume only a previously paused bounded local run."""
    try:
        return manager.resume_autonomous_run(task_id)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def stop_autonomous_run(
    task_id: Annotated[str, Field(description="Running or paused autonomous workspace task.")],
    reason: Annotated[str, Field(description="Why the run must permanently stop.")],
) -> dict[str, Any]:
    """Stop a run without deleting its workspace, evidence, or recovery path."""
    try:
        return manager.stop_autonomous_run(task_id, reason)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def record_experiment(
    task_id: Annotated[str, Field(description="Existing isolated task workspace.")],
    title: Annotated[str, Field(description="Short experiment title.")],
    hypothesis: Annotated[str, Field(description="What this change is expected to improve.")],
    outcome: Annotated[str, Field(description="Observed result, including failures.")],
    evidence: Annotated[str, Field(description="Relevant checks, audit measurements, or diff summary.")],
) -> dict[str, Any]:
    """Record a bounded improvement hypothesis and outcome outside the Git workspace."""
    try:
        return manager.record_experiment(task_id, title, hypothesis, outcome, evidence)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def experiment_history(
    task_id: Annotated[str, Field(description="Existing isolated task workspace.")],
) -> list[dict[str, Any]]:
    """Read the persistent experiment ledger for a long-running improvement task."""
    try:
        return manager.experiment_history(task_id)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def git_diff(
    task_id: Annotated[str, Field(description="Existing isolated task workspace.")],
) -> dict[str, str]:
    """Return the current uncommitted diff for review."""
    try:
        return manager.git_diff(task_id)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def mark_review_ready(
    task_id: Annotated[str, Field(description="Task with a non-empty diff and recorded check evidence.")],
) -> dict[str, Any]:
    """Archive protected review evidence after confirming there is a diff and a recorded check."""
    try:
        return manager.mark_review_ready(task_id)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def archive_workspace(
    task_id: Annotated[str, Field(description="Active or paused task workspace to snapshot and remove.")],
) -> dict[str, Any]:
    """Manually archive a disposable workspace with its diff and evidence."""
    try:
        return manager.archive_workspace(task_id)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def restore_workspace(
    task_id: Annotated[str, Field(description="Archived task to restore into a new isolated workspace.")],
) -> dict[str, Any]:
    """Restore an integrity-validated archive at its recorded base revision without rebasing."""
    try:
        return manager.restore_workspace(task_id)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def cleanup_workspaces() -> dict[str, Any]:
    """Preview automatic cleanup actions; only the unnetworked cleaner performs deletion."""
    try:
        return manager.cleanup_workspaces(dry_run=True)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def workspace_health(
    task_id: Annotated[str, Field(description="Task workspace or archive to assess.")],
) -> dict[str, Any]:
    """Report source drift, disk use, lease state, verification freshness, and recoverability."""
    try:
        return manager.workspace_health(task_id)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
async def task_report(
    task_id: Annotated[str, Field(description="Existing isolated task workspace.")],
) -> dict[str, Any]:
    """Return branch, diff, and recorded check evidence for human review."""
    try:
        return manager.task_report(task_id)
    except RepoOpsError as exc:
        raise _tool_error(exc) from exc


def main() -> None:
    mcp.run(
        transport="streamable-http",
        host=os.environ.get("REPO_OPS_HOST", "0.0.0.0"),
        port=int(os.environ.get("REPO_OPS_PORT", "8090")),
    )


if __name__ == "__main__":
    main()
