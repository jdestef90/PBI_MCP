from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

_WINDOWS_USER_RE = re.compile(r"(?i)([A-Za-z]:[\\/]+Users[\\/]+)([^\\/]+)")
_POSIX_HOME_RE = re.compile(r"(/home/|/Users/)([^/]+)")
_ONEDRIVE_RE = re.compile(r"(?i)(OneDrive\s*-\s*)([^\\/]+)")


def redact_sensitive_paths(text: str, redact: bool = True) -> str:
    """Mask usernames and tenant identifiers inside local or OneDrive filesystem paths."""
    if not redact or not text:
        return text
    result = text
    home = str(Path.home())
    if home and home != "/" and home in result:
        result = result.replace(home, "~")
    result = _WINDOWS_USER_RE.sub(r"\1<user>", result)
    result = _POSIX_HOME_RE.sub(r"\1<user>", result)
    result = _ONEDRIVE_RE.sub(r"\1<org>", result)
    return result


class StructuredLogFormatter(logging.Formatter):
    """Formats log records as JSON or redacted structured text."""

    def __init__(self, *, json_format: bool = False, redact_paths: bool = False) -> None:
        super().__init__()
        self.json_format = json_format
        self.redact_paths = redact_paths

    def format(self, record: logging.LogRecord) -> str:
        msg = redact_sensitive_paths(record.getMessage(), redact=self.redact_paths)
        if self.json_format:
            payload: dict[str, Any] = {
                "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "message": msg,
            }
            for attr in ("code", "workspace_id", "operation", "stage"):
                val = getattr(record, attr, None)
                if val is not None:
                    payload[attr] = val
            return json.dumps(payload, ensure_ascii=False)
        return f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} {record.levelname} {record.name}: {msg}"


def configure_logging(
    level: str | None = None,
    *,
    json_format: bool | None = None,
    redact_paths: bool | None = None,
) -> logging.Logger:
    """Configure stderr logging for the MCP server without polluting stdout."""
    resolved_level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    use_json = (
        json_format
        if json_format is not None
        else os.getenv("PBI_MCP_LOG_FORMAT", "text").lower() == "json"
    )
    use_redact = (
        redact_paths
        if redact_paths is not None
        else os.getenv("PBI_MCP_REDACT_PATHS", "false").lower() in {"1", "true", "yes"}
    )

    root_logger = logging.getLogger("pbi_mcp_server")
    root_logger.setLevel(getattr(logging, resolved_level, logging.INFO))
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(StructuredLogFormatter(json_format=use_json, redact_paths=use_redact))
    root_logger.handlers = [handler]
    root_logger.propagate = False
    return root_logger
