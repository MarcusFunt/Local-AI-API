"""Unnetworked lifecycle sweeper for disposable repo-ops workspaces."""
from __future__ import annotations

import os
import time

from .core import RepoOpsConfig, RepoOpsManager


def main() -> None:
    """Run cleanup on a bounded recurring interval outside the MCP service."""
    manager = RepoOpsManager(RepoOpsConfig.from_environment())
    interval = max(300, int(os.environ.get("REPO_OPS_SWEEP_INTERVAL_SECONDS", "21600")))
    while True:
        manager.cleanup_workspaces(dry_run=False)
        time.sleep(interval)


if __name__ == "__main__":
    main()
