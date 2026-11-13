from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ObjectKind = Literal[
    "workspace",
    "model",
    "table",
    "column",
    "calculated_column",
    "measure",
    "relationship",
    "partition",
    "page",
    "visual",
    "field_binding",
]


def make_object_id(workspace_id: str, kind: str, qualified_name: str) -> str:
    """Create a deterministic, fully qualified object identifier."""
    ws = (workspace_id or "single-workspace").strip()
    return f"{ws}::{kind}::{qualified_name}"


class ObjectReference(BaseModel):
    """Canonical internal object reference across Semantic Model and Report Layout."""

    object_id: str
    workspace_id: str = "single-workspace"
    kind: ObjectKind
    name: str
    qualified_name: str
    table: str | None = None
    page: str | None = None
    visual_id: str | None = None
    file: str | None = None
    line: int | None = None


class TmdlObject(BaseModel):
    kind: Literal["table", "column", "calculated_column", "measure", "relationship", "partition"]
    name: str
    table: str | None = None
    expression: str | None = None
    description: str | None = None
    data_type: str | None = None
    format_string: str | None = None
    is_hidden: bool = False
    summarize_by: str | None = None
    source_column: str | None = None
    from_column: str | None = None
    to_column: str | None = None
    cross_filtering: str | None = None
    from_cardinality: str | None = None
    to_cardinality: str | None = None
    is_active: bool = True
    partition_mode: str | None = None
    file: str
    line: int
    object_id: str | None = None
    qualified_name: str | None = None

    def model_post_init(self, __context: Any) -> None:
        if not self.qualified_name:
            if self.kind in {"column", "calculated_column", "measure"} and self.table:
                self.qualified_name = f"{self.table}[{self.name}]"
            elif self.kind == "partition" and self.table:
                self.qualified_name = f"{self.table}/{self.name}"
            else:
                self.qualified_name = self.name
        if not self.object_id:
            self.object_id = make_object_id("single-workspace", self.kind, self.qualified_name)


class FieldBinding(BaseModel):
    """Represents a single role-to-field binding on a visual."""

    role: str
    query_ref: str
    table: str | None = None
    property_name: str | None = None
    field_kind: Literal["measure", "column", "unknown"] = "unknown"
    object_id: str | None = None


class VisualSummary(BaseModel):
    page: str
    page_display_name: str | None = None
    visual_id: str
    visual_type: str | None = None
    title: str | None = None
    role_bindings: dict[str, list[str]] = Field(default_factory=dict)
    bound_fields: list[str] = Field(default_factory=list)
    field_bindings: list[FieldBinding] = Field(default_factory=list)
    filter_fields: list[str] = Field(default_factory=list)
    file: str
    object_id: str | None = None
    qualified_name: str | None = None

    def model_post_init(self, __context: Any) -> None:
        if not self.qualified_name:
            self.qualified_name = f"{self.page}/{self.visual_id}"
        if not self.object_id:
            self.object_id = make_object_id("single-workspace", "visual", self.qualified_name)


class FileChange(BaseModel):
    file: str
    replacements: int = 0
    reason: str


class MutationDiff(BaseModel):
    file: str
    before_excerpt: str = ""
    after_excerpt: str = ""
    unified_diff: str = ""
    lines_added: int = 0
    lines_removed: int = 0


class ValidationIssue(BaseModel):
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    file: str | None = None


class ChangePlan(BaseModel):
    operation: str
    status: Literal["dry_run", "applied", "rolled_back", "no_op", "failed"] = "dry_run"
    dry_run: bool
    target_object: str | dict[str, Any] | None = None
    replacement_count: int = 0
    changes: list[FileChange] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    diffs: list[MutationDiff] = Field(default_factory=list)
    impact_summary: dict[str, Any] | None = Field(default_factory=dict)
    validation: list[ValidationIssue | dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    rolled_back: bool = False
    error_code: str | None = None
    workflow: dict[str, Any] | None = None


class BpaIssue(BaseModel):
    rule_id: str
    severity: Literal["error", "warning", "info"]
    category: Literal["Performance", "Maintenance", "Formatting", "Relationships", "DAX", "Report"]
    object_type: str
    object_name: str
    table: str | None = None
    message: str
    recommendation: str
    file: str | None = None
    object_id: str | None = None


class ScanDiagnostics(BaseModel):
    roots_scanned: list[str] = Field(default_factory=list)
    valid_count: int = 0
    invalid_count: int = 0
    duplicate_names: list[str] = Field(default_factory=list)
    partial_projects: list[dict[str, Any]] = Field(default_factory=list)
    ignored_candidates: list[dict[str, Any]] = Field(default_factory=list)
    inaccessible_paths: list[dict[str, Any]] = Field(default_factory=list)
    nested_projects: list[dict[str, Any]] = Field(default_factory=list)


class WorkspaceInfo(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    root: Path
    pbip_file: Path | None
    semantic_model_dir: Path | None
    report_dir: Path | None
    workspace_id: str = "single-workspace"


JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None
