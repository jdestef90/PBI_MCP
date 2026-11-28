# pbi-mcp-server (npm / npx launcher)

Thin `npx` launcher for [`pbi-mcp-server`](https://github.com/jdestef90/PBI_MCP) — a local-first MCP server for inspecting, auditing, and safely editing Power BI PBIP projects (`.SemanticModel` TMDL and `.Report` PBIR layouts).

## Quick Start

```bash
# Inspect / edit a single PBIP project
npx -y pbi-mcp-server --workspace "./sample_project.pbip"

# Serve all PBIP projects under a root directory
npx -y pbi-mcp-server --workspace-root "/path/to/pbip-projects"

# Local llama-server 2-tool meta-tools mode
npx -y pbi-mcp-server --workspace "./sample_project.pbip" --meta-tools
```

### MCP Client Configuration (Claude Desktop / Cursor / VS Code)

```json
{
  "mcpServers": {
    "pbi-local": {
      "command": "npx",
      "args": ["-y", "pbi-mcp-server", "--workspace-root", "C:\\Users\\you\\PowerBI"]
    }
  }
}
```

## How It Works

1. If [`uv`](https://docs.astral.sh/uv/) (`uvx`) is installed *(recommended)*, this launcher delegates directly to `uvx --from pbi-mcp-server==<version> pbi-mcp-server ...`, which automatically manages Python 3.12 and caches the environment.
2. Otherwise, it falls back to a local Python 3.11 or 3.12 interpreter and creates a cached virtual environment under `~/.cache/pbi-mcp-server` (or `%LOCALAPPDATA%\pbi-mcp-server` on Windows).
3. All launcher diagnostics write exclusively to `stderr` so MCP `stdio` JSON-RPC frames on `stdout` stay clean.

## Environment Overrides

- `PBI_MCP_SPEC`: Override the Python package spec (e.g., `git+https://github.com/jdestef90/PBI_MCP` or a local wheel/checkout path).
- `PBI_MCP_CACHE_DIR`: Override the fallback virtual environment cache directory.
- `PBI_MCP_NO_UVX=1`: Skip `uvx` detection and force the Python `venv` fallback.
