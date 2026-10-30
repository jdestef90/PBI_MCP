"""Entrypoint for the Power BI MCP Server."""

import os
import sys
import logging
from mcp_server.server import create_server


def setup_logging():
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,  # IMPORTANT: Logs must go to stderr, not stdout, to avoid interfering with stdio MCP protocol
    )


def main():
    setup_logging()
    workspace_path = os.environ.get("PBI_WORKSPACE_PATH")
    server = create_server(workspace_path)
    server.run("stdio")


if __name__ == "__main__":
    main()
