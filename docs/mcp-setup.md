# MCP Server Setup

The Local AI API gateway includes a built-in MCP (Model Context Protocol) server at `/mcp`.
It exposes your local models as tools that Claude Code, Claude Desktop, and other MCP clients
can call directly — no separate service needed.

## Available tools

| Tool | Description |
|---|---|
| `chat` | Send a message to a locally-hosted language model (Ollama). Supports all model aliases. |
| `list_models` | List all available model aliases and their underlying Ollama tags. |
| `transcribe` | Transcribe base64-encoded WAV/MP3 audio to text using local Whisper. |
| `speak` | Convert text to speech with local Chatterbox TTS. Returns base64-encoded WAV. |
| `health_check` | Report the health of the gateway and Ollama backend. |
| `search_documents` | Semantic search over indexed documents (requires `RAG_ENABLED=true`). |

### Model aliases for `chat`

| Alias | Size | Notes |
|---|---|---|
| `main` | 9B | Default general-purpose model |
| `small` | 4B | Faster, lighter tasks |
| `dev` | 0.8B | Ultra-fast, development/testing |
| `agent` | 14B | Long-horizon reasoning |
| `agent-utility` | 8B | Agent subtasks |

## Enabling MCP in Docker

The MCP server requires `fastmcp>=2.0`. It is **not** installed by default to keep the base
image small. Build the gateway with the `INSTALL_MCP=true` argument:

```bash
docker build --build-arg INSTALL_MCP=true -t local-ai-api .
```

For the supplied Compose setup, rebuild the gateway with the same build
argument:

```bash
docker compose build --build-arg INSTALL_MCP=true gateway
docker compose up -d gateway
```

Or add the argument permanently to your local Compose override:

```yaml
services:
  gateway:
    build:
      context: .
      args:
        INSTALL_MCP: "true"
```

If `fastmcp` is not installed, the gateway starts normally and logs:

```
fastmcp not installed — MCP server not available
```

## Configuring Claude Code

Add the following entry to your Claude Code MCP settings.

**Location:** `~/.claude/settings.json` (or the project-level `.claude/settings.json`)

```json
{
  "mcpServers": {
    "local-ai-api": {
      "type": "http",
      "url": "https://<your-machine>.ts.net/mcp/"
    }
  }
}
```

Restart Claude Code after saving. The tools will appear in the tool list as
`local-ai-api:chat`, `local-ai-api:list_models`, etc.

## Configuring Claude Desktop

Open your Claude Desktop configuration file:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

Add the `mcpServers` block:

```json
{
  "mcpServers": {
    "local-ai-api": {
      "type": "http",
      "url": "https://<your-machine>.ts.net/mcp/"
    }
  }
}
```

Restart Claude Desktop. The tools appear in the attachment panel.

## Example tool calls

### Chat with the default model

```
Use the local-ai-api:chat tool to ask: "Summarise the Agile manifesto in two sentences."
```

### Use a specific model

```
Use local-ai-api:chat with model="agent" to plan a refactoring of the auth module.
```

### Check service health before a long task

```
Call local-ai-api:health_check and confirm Ollama is reachable before proceeding.
```

### Transcribe audio

```
Base64-encode your WAV file and call local-ai-api:transcribe with the encoded string.
```

### Text-to-speech

```
Call local-ai-api:speak with text="Hello world". Decode the returned base64 string to get WAV audio.
```

### Search documents (RAG)

```
Call local-ai-api:search_documents with query="authentication flow" to find relevant code comments.
```

## Security note

All traffic travels over Tailscale (WireGuard). The `/mcp` endpoint is subject to the same
`ENABLE_API_KEY_AUTH` / `API_KEY` settings as the rest of the gateway — if auth is enabled,
MCP clients must supply a `Bearer` token in the `Authorization` header.

Tailscale ACLs provide an additional network-level control layer: only devices in your
tailnet with explicit ACL grants can reach the gateway host. Review your Tailscale ACL policy
at https://login.tailscale.com/admin/acls if you want to restrict which devices can call the
MCP tools.

The MCP server calls the gateway's own HTTP endpoints over loopback
(`http://127.0.0.1:8080`). When API-key authentication is enabled, those
internal calls include the configured bearer token, so they pass through the
same gateway authentication middleware. They bypass Tailscale transport only;
keep the host machine trusted.
