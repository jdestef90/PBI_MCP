"""Higher-level agent workflow recipes and orchestration metadata (Phase 6).

Provides:
1. Canonical workflow recipes for common Power BI tasks:
   - `measure_lookup_and_update`
   - `sql_inspection_and_replacement`
   - `visual_field_binding_update`
   - `cross_layer_measure_rename`
2. Concise `workflow` guidance metadata embedded in tool responses so MCP clients
   always know the current stage, next recommended step, and recovery options.
3. `WorkflowOrchestrator` for executing end-to-end multi-step workflows with
   deterministic workspace resolution, ambiguity checks, impact analysis,
   mandatory dry-run previews, and post-write validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .service import PbipService


@dataclass(frozen=True)
class WorkflowRecipe:
    workflow_id: str
    title: str
    description: str
    stages: list[str]
    tools_sequence: list[str]


WORKFLOW_RECIPES: dict[str, WorkflowRecipe] = {
    "measure_lookup_and_update": WorkflowRecipe(
        workflow_id="measure_lookup_and_update",
        title="Measure Lookup and Safe DAX Update",
        description="Resolve a DAX measure unambiguously, inspect cross-layer impact, preview unified diff in dry-run mode, apply with precondition check, and validate.",
        stages=["resolve", "analyze_impact", "dry_run_preview", "apply_mutation", "validate"],
        tools_sequence=[
            "list_workspaces",
            "search_measures",
            "analyze_impact",
            "update_measure_dax(dry_run=True)",
            "update_measure_dax(dry_run=False)",
            "validate_workspace",
        ],
    ),
    "sql_inspection_and_replacement": WorkflowRecipe(
        workflow_id="sql_inspection_and_replacement",
        title="Partition SQL Inspection and Surgical Replacement",
        description="Inspect a table's Value.NativeQuery SQL partition, analyze downstream column/measure/visual impact, preview surgical replacement, and apply with expected_replacements guard.",
        stages=["resolve", "inspect_sql", "analyze_impact", "dry_run_preview", "apply_mutation", "validate"],
        tools_sequence=[
            "list_workspaces",
            "get_table_sql",
            "analyze_impact",
            "update_table_sql(dry_run=True)",
            "update_table_sql(dry_run=False)",
            "validate_workspace",
        ],
    ),
    "visual_field_binding_update": WorkflowRecipe(
        workflow_id="visual_field_binding_update",
        title="Visual Field Binding Update",
        description="Resolve a report visual by ID or title (disambiguating by page if needed), inspect bound fields, preview role-aware field additions/removals, and validate report JSON.",
        stages=["resolve", "inspect_visual", "dry_run_preview", "apply_mutation", "validate"],
        tools_sequence=[
            "list_workspaces",
            "list_visuals",
            "get_visual",
            "update_visual_fields(dry_run=True)",
            "update_visual_fields(dry_run=False)",
            "validate_workspace",
        ],
    ),
    "cross_layer_measure_rename": WorkflowRecipe(
        workflow_id="cross_layer_measure_rename",
        title="Cross-Layer Atomic Measure Rename",
        description="Analyze all TMDL and PBIR visual references to a measure, preview multi-file atomic rename across semantic model and report layout, apply, and verify.",
        stages=["resolve", "analyze_impact", "dry_run_preview", "apply_mutation", "validate"],
        tools_sequence=[
            "list_workspaces",
            "analyze_impact",
            "rename_measure(dry_run=True)",
            "rename_measure(dry_run=False)",
            "validate_workspace",
        ],
    ),
}


def build_workflow_guidance(
    workflow_id: str,
    *,
    current_stage: str,
    dry_run: bool,
    rolled_back: bool = False,
    target_object: str | None = None,
    next_tool_call: dict[str, Any] | None = None,
) -> dict[str, Any]:
    recipe = WORKFLOW_RECIPES.get(workflow_id)
    stages = recipe.stages if recipe else ["resolve", "analyze_impact", "dry_run_preview", "apply_mutation", "validate"]

    if rolled_back:
        next_step = "Inspect validation errors in 'warnings'/'validation' and fix syntax before retrying in dry_run=True mode."
    elif dry_run:
        next_step = "Review 'diffs' and 'impact_summary', then re-invoke with dry_run=False to apply."
    else:
        next_step = "Mutation applied and validated. Optionally call validate_workspace or check_model_bpa."

    guidance: dict[str, Any] = {
        "workflow_id": workflow_id,
        "current_stage": current_stage,
        "stages": stages,
        "target_object": target_object,
        "next_step": next_step,
    }
    if next_tool_call:
        guidance["recommended_call"] = next_tool_call
    return guidance


@dataclass
class WorkflowExecutionResult:
    workflow_id: str
    status: str  # "dry_run_completed", "applied", "rolled_back"
    target_object: str | None
    steps_completed: list[str] = field(default_factory=list)
    resolved: dict[str, Any] | None = None
    impact: dict[str, Any] | None = None
    dry_run_plan: dict[str, Any] | None = None
    applied_plan: dict[str, Any] | None = None
    validation: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "status": self.status,
            "target_object": self.target_object,
            "steps_completed": self.steps_completed,
            "resolved": self.resolved,
            "impact_counts": self.impact.get("counts") if self.impact else None,
            "dry_run_plan": self.dry_run_plan,
            "applied_plan": self.applied_plan,
            "validation": self.validation,
        }


class WorkflowOrchestrator:
    """Executes multi-step agent workflows against a `PbipService` instance.

    Enforces:
    - Unambiguous object resolution before analysis or mutation
    - Cross-layer impact analysis before mutation
    - Mandatory dry-run preview before applying changes
    - Post-write validation and automatic rollback on failure
    """

    def __init__(self, service: PbipService) -> None:
        self.service = service

    def measure_lookup_and_update(
        self,
        measure_name: str,
        new_expression: str,
        *,
        table: str | None = None,
        expected_current_expression: str | None = None,
        apply: bool = False,
    ) -> dict[str, Any]:
        target = self.service.resolve_measure(measure_name, table=table)
        impact = self.service.impact(f"{target['table']}[{target['name']}]" if target.get("table") else target["name"])
        dry_plan = self.service.update_dax(
            target["name"],
            new_expression,
            table=target.get("table"),
            expected_current_expression=expected_current_expression,
            dry_run=True,
        )
        steps = ["resolve", "analyze_impact", "dry_run_preview"]
        applied_plan = None
        status = "dry_run_completed"
        validation: list[dict[str, Any]] = []

        if apply:
            applied_plan = self.service.update_dax(
                target["name"],
                new_expression,
                table=target.get("table"),
                expected_current_expression=expected_current_expression,
                dry_run=False,
            )
            steps.append("apply_mutation")
            validation = self.service.validation()
            steps.append("validate")
            status = "rolled_back" if applied_plan.get("rolled_back") else "applied"

        return WorkflowExecutionResult(
            workflow_id="measure_lookup_and_update",
            status=status,
            target_object=target.get("object_id") or target["name"],
            steps_completed=steps,
            resolved=target,
            impact=impact,
            dry_run_plan=dry_plan,
            applied_plan=applied_plan,
            validation=validation,
        ).to_dict()

    def sql_inspection_and_replacement(
        self,
        table_name: str,
        old_text: str,
        new_text: str,
        *,
        partition: str | None = None,
        expected_replacements: int | None = 1,
        apply: bool = False,
    ) -> dict[str, Any]:
        sql_meta = self.service.get_table_sql(table_name=table_name, partition=partition)
        impact = self.service.impact(table_name)
        dry_plan = self.service.update_table_sql(
            old_text,
            new_text,
            table_name=table_name,
            partition=partition,
            expected_replacements=expected_replacements,
            dry_run=True,
        )
        steps = ["resolve", "inspect_sql", "analyze_impact", "dry_run_preview"]
        applied_plan = None
        status = "dry_run_completed"
        validation: list[dict[str, Any]] = []

        if apply:
            applied_plan = self.service.update_table_sql(
                old_text,
                new_text,
                table_name=table_name,
                partition=partition,
                expected_replacements=expected_replacements,
                dry_run=False,
            )
            steps.append("apply_mutation")
            validation = self.service.validation()
            steps.append("validate")
            status = "rolled_back" if applied_plan.get("rolled_back") else "applied"

        return WorkflowExecutionResult(
            workflow_id="sql_inspection_and_replacement",
            status=status,
            target_object=f"{sql_meta['table']}.{sql_meta['partition']}",
            steps_completed=steps,
            resolved=sql_meta,
            impact=impact,
            dry_run_plan=dry_plan,
            applied_plan=applied_plan,
            validation=validation,
        ).to_dict()

    def visual_field_binding_update(
        self,
        visual_id_or_title: str,
        *,
        add_fields: list[dict[str, str] | str] | None = None,
        remove_fields: list[str] | None = None,
        page: str | None = None,
        apply: bool = False,
    ) -> dict[str, Any]:
        vis = self.service.get_visual(visual_id_or_title, page=page)
        if not vis:
            from .errors import ObjectNotFoundError
            raise ObjectNotFoundError(f"Visual {visual_id_or_title!r} was not found.")
        dry_plan = self.service.update_visual_fields(
            vis["visual_id"],
            add_fields=add_fields,
            remove_fields=remove_fields,
            page=vis.get("page"),
            dry_run=True,
        )
        steps = ["resolve", "inspect_visual", "dry_run_preview"]
        applied_plan = None
        status = "dry_run_completed"
        validation: list[dict[str, Any]] = []

        if apply:
            applied_plan = self.service.update_visual_fields(
                vis["visual_id"],
                add_fields=add_fields,
                remove_fields=remove_fields,
                page=vis.get("page"),
                dry_run=False,
            )
            steps.append("apply_mutation")
            validation = self.service.validation()
            steps.append("validate")
            status = "rolled_back" if applied_plan.get("rolled_back") else "applied"

        return WorkflowExecutionResult(
            workflow_id="visual_field_binding_update",
            status=status,
            target_object=vis.get("object_id") or vis["visual_id"],
            steps_completed=steps,
            resolved=vis,
            impact=None,
            dry_run_plan=dry_plan,
            applied_plan=applied_plan,
            validation=validation,
        ).to_dict()

    def cross_layer_measure_rename(
        self,
        old_name: str,
        new_name: str,
        *,
        table: str | None = None,
        apply: bool = False,
    ) -> dict[str, Any]:
        target = self.service.resolve_measure(old_name, table=table)
        impact = self.service.impact(target["name"])
        dry_plan = self.service.rename_measure(target["name"], new_name, table=target.get("table"), dry_run=True)
        steps = ["resolve", "analyze_impact", "dry_run_preview"]
        applied_plan = None
        status = "dry_run_completed"
        validation: list[dict[str, Any]] = []

        if apply:
            applied_plan = self.service.rename_measure(target["name"], new_name, table=target.get("table"), dry_run=False)
            steps.append("apply_mutation")
            validation = self.service.validation()
            steps.append("validate")
            status = "rolled_back" if applied_plan.get("rolled_back") else "applied"

        return WorkflowExecutionResult(
            workflow_id="cross_layer_measure_rename",
            status=status,
            target_object=target.get("object_id") or target["name"],
            steps_completed=steps,
            resolved=target,
            impact=impact,
            dry_run_plan=dry_plan,
            applied_plan=applied_plan,
            validation=validation,
        ).to_dict()
