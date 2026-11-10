from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from .server import create_server
from .service import PbipService
from .workspace_registry import WorkspaceRegistry


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Local MCP server for Power BI PBIP projects"
    )

    group = parser.add_mutually_exclusive_group(required=False)

    group.add_argument(
        "--workspace",
        help="Path to a single .pbip file or project directory",
    )

    group.add_argument(
        "--workspace-root",
        action="append",
        dest="workspace_roots",
        help="Directory containing PBIP projects. May be supplied multiple times.",
    )

    args = parser.parse_args(argv)
    if not args.workspace and not args.workspace_roots:
        env_ws = os.getenv("PBI_WORKSPACE_PATH")
        if env_ws:
            args.workspace = env_ws
        else:
            parser.error("one of the arguments --workspace --workspace-root (or PBI_WORKSPACE_PATH) is required")
    return args


def create_backups_enabled() -> bool:
    return os.getenv("PBI_MCP_CREATE_BACKUPS", "true").lower() in {
        "1",
        "true",
        "yes",
    }


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    create_backups = create_backups_enabled()

    if args.workspace:
        service = PbipService(
            Path(args.workspace).expanduser().resolve(),
            create_backups=create_backups,
        )
        mcp = create_server(service)
    else:
        registry = WorkspaceRegistry(
            roots=[Path(root) for root in args.workspace_roots],
            create_backups=create_backups,
        )
        mcp = create_server(registry)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
