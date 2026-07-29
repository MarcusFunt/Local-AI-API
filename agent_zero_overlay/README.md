# Local AI API workspace cockpit

This overlay is intentionally small so it can be reapplied to Agent Zero
upstream releases. Its backend plugin proxies authenticated workspace status
requests to the internal `repo-ops` MCP server; browser JavaScript calls only
the Agent Zero same-origin plugin API and never receives the internal endpoint.

The installer runs a candidate-image build against the configured Agent Zero
image tag, checks the overlay files and Python syntax, writes a local status
report, and leaves the last known-good image active if the candidate fails.
