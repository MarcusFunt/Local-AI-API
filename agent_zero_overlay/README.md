# Local AI API workspace cockpit

This overlay is intentionally small so it can be reapplied to Agent Zero
upstream releases. Its backend plugin proxies authenticated workspace status
requests to the internal `repo-ops` MCP server; browser JavaScript calls only
the Agent Zero same-origin plugin API and never receives the internal endpoint.

The deployment's candidate-image updater builds this overlay against upstream,
runs the upstream smoke suite plus the cockpit contract tests, and leaves the
last known-good image active if either stage fails.
