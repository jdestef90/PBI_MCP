from __future__ import annotations

from typing import Any, Literal

ErrorCode = Literal[
    "WORKSPACE_NOT_FOUND",
    "AMBIGUOUS_WORKSPACE",
    "OBJECT_NOT_FOUND",
    "AMBIGUOUS_OBJECT",
    "PRECONDITION_FAILED",
    "VALIDATION_FAILED",
    "ROLLBACK_COMPLETED",
    "PERMISSION_DENIED",
    "INVALID_ARGUMENT",
    "PARSE_ERROR",
]


class PbiMcpError(Exception):
    """Base structured exception for predictable, machine-readable MCP error handling."""

    code: ErrorCode = "INVALID_ARGUMENT"

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        details: dict[str, Any] | None = None,
        remediation: str | None = None,
        stage: str | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message
        self.details: dict[str, Any] = details or {}
        self.remediation = remediation
        self.stage = stage

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": True,
            "code": self.code,
            "message": self.message,
        }
        if self.stage:
            payload["stage"] = self.stage
        if self.details:
            payload["details"] = self.details
        if self.remediation:
            payload["remediation"] = self.remediation
        return payload


class WorkspaceNotFoundError(PbiMcpError, FileNotFoundError):
    code: ErrorCode = "WORKSPACE_NOT_FOUND"


class AmbiguousWorkspaceError(PbiMcpError, ValueError):
    code: ErrorCode = "AMBIGUOUS_WORKSPACE"


class ObjectNotFoundError(PbiMcpError, FileNotFoundError):
    code: ErrorCode = "OBJECT_NOT_FOUND"


class AmbiguousObjectError(PbiMcpError, ValueError):
    code: ErrorCode = "AMBIGUOUS_OBJECT"


class PreconditionFailedError(PbiMcpError, ValueError):
    code: ErrorCode = "PRECONDITION_FAILED"


class ValidationFailedError(PbiMcpError, ValueError):
    code: ErrorCode = "VALIDATION_FAILED"


class RollbackCompletedError(PbiMcpError, RuntimeError):
    code: ErrorCode = "ROLLBACK_COMPLETED"


class WorkspacePermissionError(PbiMcpError, PermissionError):
    code: ErrorCode = "PERMISSION_DENIED"


class InvalidArgumentError(PbiMcpError, ValueError):
    code: ErrorCode = "INVALID_ARGUMENT"
