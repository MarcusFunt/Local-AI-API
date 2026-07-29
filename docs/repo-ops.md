# Agent Zero repository operations

`repo-ops` is an opt-in MCP worker for Agent Zero. It is separate from the
gateway's `/mcp` endpoint: the gateway continues to expose only local AI and
audio tools, while `repo-ops` operates on an isolated repository clone.

## Start the worker

Start it alongside the normal Agent Zero deployment:

```bash
docker compose -f compose.yaml -f compose.agent-zero.yaml -f compose.repo-ops.yaml up -d --build
```

The worker has no published host port. Its source checkout is mounted at
`/source` as read-only. Every MCP task uses a branch in the
`repo-ops-workspaces` Docker volume; it cannot write the source checkout, push,
merge, deploy, or execute an arbitrary shell command.

Each workspace has a persisted 24-hour lease. Successful approved edits,
checks, UI captures, and experiment records renew it. The unnetworked
`repo-ops-lifecycle` companion archives inactive ordinary workspaces after
seven days and removes their archives after 14 days. Its only mounts are the
workspace volume and the host-backed archive directory
`.local/repo-ops-archives`; it has no MCP port, source checkout, Agent Zero
volume, or network.

Archives include the worktree, binary diff, report, checks, experiment history,
and UI evidence, with integrity hashes. They exclude Git metadata, environments,
caches, dependency folders, and common secret-file names. A review-ready task is
archived but protected from automatic expiry until manually removed.

In Agent Zero, add a remote Streamable HTTP MCP server through **Settings →
MCP/A2A** using this Docker-internal address:

```text
http://repo-ops:8090/mcp
```

Do not use a Tailscale URL, publish a port, or add this MCP server to a public
client. Agent Zero and `repo-ops` share the Compose default network; the
gateway uses Ollama's separate network namespace and cannot reach it.

## Working loop

1. Call `repo_status` and `improvement_inventory` to choose an evidence-based,
   small objective. Then call `symbol_context` and `impact_analysis` before
   changing an existing symbol.
2. Create a workspace with `create_workspace` and make only hash-checked
   `write_file` edits there.
3. Run named checks: `unit`, `compile`, `compose_config`, `status_ui_tests`,
   `repo_ops_tests`, and `dependency_health`. Use `capture_ui` for the
   only UI audit: a workspace-only screenshot plus accessibility and timing evidence.
4. Record each hypothesis and outcome with `record_experiment`, so later task
   runs can read `experiment_history` instead of repeating failed ideas.
5. Return `git_diff` and `task_report` for review. A human reviews and merges
   the reported branch; the worker has no commit, push, merge, or deploy tool.

For long-running work, call `workspace_status` or `workspace_health`, renew a
lease deliberately with `renew_workspace_lease`, and use `pause_workspace` for
intentional pauses. `cleanup_workspaces` is preview-only over MCP; deletion is
performed only by the separate lifecycle worker. `archive_workspace` creates a
recoverable ordinary snapshot. `mark_review_ready` requires a diff and a
recorded verification result, then creates a protected review archive.
`restore_workspace` recreates a new isolated workspace at the recorded base
revision without automatic rebasing.

The initial GitNexus index is built from a disposable clone at worker startup.
If startup reports an index failure, inspect `docker compose logs repo-ops` and
rebuild the worker; do not give it write access to `/source`.

## Autonomous local improvement

See [autonomous workspaces](autonomous-workspaces.md) for the bounded local
evaluation loop, the unnetworked preview worker, and the Agent Zero cockpit.

## Open-catalog skill quarantine

Start a completely separate Agent Zero instance only when evaluating an
untrusted external skill or plugin:

```bash
docker compose -f compose.skill-sandbox.yaml --profile skill-quarantine run --rm agent-skill-sandbox
```

It has no source checkout, gateway API key, persistent Agent Zero volume, or
published port. Before installing anything, snapshot or recreate the
`agent-skill-quarantine` volume. Record catalog provenance, artifact hash,
license, smoke-test outcome, and snapshot ID with:

```bash
python -m repo_ops.quarantine \
  --source '<catalog URL>' --package '<name>' --version '<version>' \
  --license '<license>' --artifact '<downloaded artifact>' \
  --smoke-result passed --snapshot '<snapshot ID>' \
  --output quarantine-evidence/<name>.json
```

Treat all sandbox output as untrusted. Promotion into the persistent Agent Zero
instance always requires explicit human approval.
