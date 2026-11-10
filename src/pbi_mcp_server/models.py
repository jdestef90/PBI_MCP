from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TmdlObject(BaseModel):
    kind: Literal["table", "column", "calculated_column", "measure", "relationship"]
    name: str
    table: str | None = None
    expression: str | None = None
    description: str | None = None
    data_type: str | None = None
    format_string: str | None = None
    is_hidden: bool = False
    summarize_by: str | None = None
    from_column: str | None = None
    to_column: str | None = None
    cross_filtering: str | None = None
    file: str
    line: int


class VisualSummary(BaseModel):
    page: str
    page_display_name: str | None = None
    visual_id: str
    visual_type: str | None = None
    title: str | None = None
    role_bindings: dict[str, list[str]] = Field(default_factory=dict)
    bound_fields: list[str] = Field(default_factory=list)
    file: str


class FileChange(BaseModel):
    file: str
    replacements: int = 0
    reason: str


class ChangePlan(BaseModel):
    operation: str
    dry_run: bool
    changes: list[FileChange] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    rolled_back: bool = False


class ValidationIssue(BaseModel):
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    file: str | None = None


class BpaIssue(BaseModel):
    rule_id: str
    severity: Literal["error", "warning", "info"]
    category: Literal["Performance", "Maintenance", "Formatting", "Relationships", "DAX"]
    object_type: str
    object_name: str
    table: str | None = None
    message: str
    recommendation: str
    file: str | None = None


class WorkspaceInfo(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    root: Path
    pbip_file: Path | None
    semantic_model_dir: Path | None
    report_dir: Path | None


JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None
