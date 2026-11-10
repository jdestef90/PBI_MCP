"""PBI MCP Server package."""

from __future__ import annotations

from .server import create_server
from .service import PbipService
from .workspace_registry import WorkspaceRegistry

__version__ = "0.3.0"

__all__ = [
    "PbipService",
    "WorkspaceRegistry",
    "__version__",
    "create_server",
]
