from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    workspace: Path
    log_level: str = "INFO"
    create_backups: bool = True

    @classmethod
    def from_values(cls, workspace: str | None = None) -> Settings:
        raw = workspace or os.getenv("PBI_WORKSPACE_PATH")
        if not raw:
            raise ValueError("Provide --workspace or set PBI_WORKSPACE_PATH")
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Workspace does not exist: {path}")
        backups = os.getenv("PBI_MCP_CREATE_BACKUPS", "true").lower() in {"1", "true", "yes"}
        return cls(path, os.getenv("LOG_LEVEL", "INFO").upper(), backups)
