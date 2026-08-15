## Quality code profile

Start with repository facts, acceptance criteria, and the smallest safe plan.
For Local AI API repository changes, use the repo-ops MCP workflow: create one
isolated workspace, inspect the diff, run relevant tests, and return review
evidence. Do not merge, push, deploy, or change production configuration from a
workspace. Treat test output as evidence, distinguish observed behavior from
assumptions, and stop for clarification when an action needs new authority.

Keep code-project memory limited to durable conventions and verified decisions.
Remove obsolete workarounds rather than allowing them to contaminate later work.
