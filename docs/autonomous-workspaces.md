# Autonomous local workspaces

This document describes the current bounded `repo-ops` worker workflow. It is
not an autonomous release loop: a run remains isolated, evidence-producing, and
human-reviewed. The phase-2 controller now leases a separate, constrained
adapter that prepares an empty disposable `code_patch` workspace and stops; it
does not call this autonomous repo-ops flow. Evaluation and promotion boundaries
remain specified in [Lab controller v0.1](lab-controller-v0.1.md).

Enable `REPO_OPS_AUTONOMY_ENABLED=true` only with the `compose.repo-ops.yaml`
overlay. Agent Zero may use the new repo-ops MCP run, progress, evaluation,
preview, pause, resume, and report tools, but they retain the existing
repository boundary: no source writes, arbitrary commands, pushes, merges,
deployments, Docker control, or credentials.

Each run has fixed hard limits: 24 hours, 20 GiB of workspace plus evidence,
and three non-improving evaluation results. The tracked evaluation manifest
selects only named verification presets. A changed workspace with passing
evaluation evidence can become review-ready; a human still reviews and merges.

`repo-ops-preview` is a separate worker with `network_mode: none`, no host
port, source checkout, archives, Agent Zero state, or Docker socket. It reads a
file-queued task, runs the status page on loopback, captures browser/axe WCAG
A–AA evidence, and terminates the server before reporting the result.

The Agent Zero cockpit is a small plugin overlay. Browser code calls only the
same-origin authenticated Agent Zero plugin API; its allow-listed backend proxy
is the only component that reaches internal `repo-ops`. Rebuild it against
configured Agent Zero image tag with `scripts/update-agent-zero-cockpit.sh`;
a failed candidate does not replace the active image. Starting a bounded run
persists policy and evidence only: it does not itself invoke a model or modify
files. An approved agent must use the existing workspace MCP edit tools
separately.
