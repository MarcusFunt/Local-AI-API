"""Build the disposable GitNexus index used by the repo-ops worker."""
from __future__ import annotations

import shutil

from .core import RepoOpsConfig, RepoOpsError, RepoOpsManager


def main() -> None:
    config = RepoOpsConfig.from_environment()
    manager = RepoOpsManager(config)
    index_root = config.workspaces_root / config.gitnexus_repo
    if index_root.exists():
        shutil.rmtree(index_root)
    result = manager._command(
        ["git", "clone", "--no-hardlinks", str(config.source_root), str(index_root)],
        timeout=300,
    )
    if result.returncode:
        raise RepoOpsError(f"Could not create GitNexus index clone: {manager._output(result)}")
    indexed = manager._command(["gitnexus", "analyze", "."], cwd=index_root, timeout=300)
    if indexed.returncode:
        raise RepoOpsError(f"Could not index repository: {manager._output(indexed)}")


if __name__ == "__main__":
    main()
