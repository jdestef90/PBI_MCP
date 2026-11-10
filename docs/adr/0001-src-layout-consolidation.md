# Standardize on `src/pbi_mcp_server` Package Layout

To resolve drift between legacy `mcp_server/`, uncommitted root modules, and `pyproject.toml`, all active server components, parsers, engines, and registries are consolidated under `src/pbi_mcp_server/`. This guarantees an isolated, installable package layout and eliminates root namespace pollution.
