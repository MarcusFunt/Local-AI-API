# Agent learning and promotion

The gateway agent and the isolated repository worker write a shared
append-only `records.jsonl` format. A record contains an identifier, policy
version, outcome, scalar metrics, Git base revision when applicable, and hashes
plus sizes of intermediate artifacts. It deliberately excludes raw prompts,
answers, source text, API keys, and tool output.

Gateway telemetry is opt-in through `AGENT_LEARNING_DIR`; repo-ops stores its
redacted ledger under its host-backed archive root. A policy candidate may alter
at most two approved fields: stage ordering, stage token limits, system prompt,
or tool preference. Candidates are evaluated outside production and retained as
positive or negative evidence.

Code candidates use a manifest with a sibling patch, matching local-main base
revision, fresh named-check evidence, and passing private/public/dependency
gates. `scripts/promote_agent_candidate.py` performs the only privileged action:
it independently re-runs named checks in a temporary worktree and, when invoked
with `--apply`, fast-forwards clean local `main` while recording a rollback tag.
It does not deploy or push.
