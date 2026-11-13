from __future__ import annotations

from typing import Annotated, Any

from mcp.server import MCPServer
from pydantic import Field

from .errors import AmbiguousWorkspaceError, WorkspaceNotFoundError
from .service import PbipService
from .workspace_registry import WorkspaceRegistry

Backend = PbipService | WorkspaceRegistry
WorkspaceArg = Annotated[
    str | None,
    Field(
        description=(
            "Workspace identifier returned by list_workspaces (prefer workspace_id or unique name). "
            "Optional when the server is bound to a single workspace."
        )
    ),
]
DryRunArg = Annotated[
    bool,
    Field(
        description=(
            "When true, return the proposed ChangePlan without modifying files. "
            "Defaults to true. Set false to execute the write with post-write validation."
        )
    ),
]
LimitArg = Annotated[
    int | None,
    Field(description="Optional maximum number of items to return."),
]
OffsetArg = Annotated[
    int,
    Field(description="Optional 0-based pagination offset. Defaults to 0."),
]


def create_server(backend: Backend) -> MCPServer:
    """Create the consolidated PBI MCP server with 20 high-leverage tools."""
    mcp = MCPServer("pbi-local")

    def get_service(workspace: str | None = None) -> PbipService:
        if isinstance(backend, PbipService):
            return backend
        if not workspace:
            workspaces = backend.list_workspaces()
            if len(workspaces) == 1:
                return backend.get_service(workspaces[0]["workspace_id"])
            if not workspaces:
                raise WorkspaceNotFoundError(
                    "No PBIP workspaces were discovered under configured workspace roots. "
                    "Call refresh_workspaces or verify --workspace-root."
                )
            raise AmbiguousWorkspaceError(
                "workspace is required when multiple workspaces are registered. "
                "Call list_workspaces first and pass workspace_id or workspace name.",
                candidates=[
                    {"workspace_id": w["workspace_id"], "name": w["name"], "root": w["root"]}
                    for w in workspaces
                ],
            )
        return backend.get_service(workspace)

    # -------------------------------------------------------------------------
    # 1. Workspace Discovery & Lifecycle (3 tools)
    # -------------------------------------------------------------------------

    @mcp.tool()
    def list_workspaces(
        query: Annotated[
            str | None,
            Field(description="Optional case-insensitive filter on workspace name or path."),
        ] = None,
        limit: LimitArg = None,
        offset: OffsetArg = 0,
    ) -> list[dict[str, Any]]:
        """List discovered PBIP workspaces, optionally filtered by query."""
        if isinstance(backend, WorkspaceRegistry):
            return backend.list_workspaces(query=query, limit=limit, offset=offset)
        pbip_file = backend.info.pbip_file
        name = pbip_file.stem if pbip_file else backend.info.root.name
        item = {
            "workspace_id": backend.info.workspace_id or "single-workspace",
            "name": name,
            "workspace": name,
            "pbip_file": str(pbip_file) if pbip_file else None,
            "path": str(pbip_file) if pbip_file else str(backend.info.root),
            "root": str(backend.info.root),
            "valid": bool(backend.info.semantic_model_dir),
        }
        if query and query.strip().casefold() not in "\n".join(str(v) for v in item.values()).casefold():
            return []
        items = [item]
        if offset > 0:
            items = items[offset:]
        if limit is not None and limit >= 0:
            items = items[:limit]
        return items

    @mcp.tool()
    def get_workspace(workspace: WorkspaceArg = None) -> dict[str, Any]:
        """Get metadata and object counts for a PBIP workspace."""
        svc = get_service(workspace)
        summary = svc.summary()
        if isinstance(backend, WorkspaceRegistry):
            meta = backend.get_workspace(workspace or summary["workspace_id"])
            return {**meta, "summary": summary, "diagnostics": backend.diagnostics}
        return summary

    @mcp.tool()
    def refresh_workspaces() -> list[dict[str, Any]]:
        """Re-scan configured workspace roots and refresh discovered PBIP projects."""
        if isinstance(backend, WorkspaceRegistry):
            return backend.refresh()
        return list_workspaces()

    # -------------------------------------------------------------------------
    # 2. Semantic Model Inspection & Search (5 tools)
    # -------------------------------------------------------------------------

    @mcp.tool()
    def list_tables(
        workspace: WorkspaceArg = None,
        limit: LimitArg = None,
        offset: OffsetArg = 0,
    ) -> list[dict[str, Any]]:
        """List all tables in the semantic model with column/measure counts and native SQL status."""
        return get_service(workspace).list_tables(limit=limit, offset=offset)

    @mcp.tool()
    def get_table(
        table_name: Annotated[str, Field(description="Table name to inspect.")],
        workspace: WorkspaceArg = None,
    ) -> dict[str, Any] | None:
        """Retrieve complete table schema including columns, measures, relationships, and SQL source."""
        return get_service(workspace).get_table(table_name)

    @mcp.tool()
    def search_measures(
        query: Annotated[
            str | None,
            Field(description="Case-insensitive search term matching measure name, DAX, or description."),
        ] = None,
        table: Annotated[
            str | None,
            Field(description="Optional table name filter."),
        ] = None,
        workspace: WorkspaceArg = None,
        limit: LimitArg = None,
        offset: OffsetArg = 0,
    ) -> list[dict[str, Any]]:
        """Find measures across the semantic model and return their table, name, DAX expression, and description."""
        return get_service(workspace).search_measures(
            query=query, table=table, limit=limit, offset=offset
        )

    @mcp.tool()
    def get_table_sql(
        table_name: Annotated[
            str | None,
            Field(description="Table name containing a Value.NativeQuery SQL partition."),
        ] = None,
        partition: Annotated[
            str | None,
            Field(description="Optional partition name if the table has multiple SQL partitions."),
        ] = None,
        workspace: WorkspaceArg = None,
    ) -> dict[str, Any]:
        """Extract the partition SQL query (with line numbers and M context) for a table."""
        return get_service(workspace).get_table_sql(table_name=table_name, partition=partition)

    @mcp.tool()
    def analyze_impact(
        term: Annotated[
            str,
            Field(description="Measure or column name whose usage across TMDL and visuals should be analyzed."),
        ],
        workspace: WorkspaceArg = None,
    ) -> dict[str, Any]:
        """Analyze where a measure or column is referenced across TMDL formulas and report visuals."""
        return get_service(workspace).impact(term)

    # -------------------------------------------------------------------------
    # 3. Report Layout & Visual Inspection (3 tools)
    # -------------------------------------------------------------------------

    @mcp.tool()
    def list_report_pages(
        workspace: WorkspaceArg = None,
        limit: LimitArg = None,
        offset: OffsetArg = 0,
    ) -> list[dict[str, Any]]:
        """List all report pages with their display names and visual counts."""
        return get_service(workspace).pages(limit=limit, offset=offset)

    @mcp.tool()
    def list_visuals(
        page: Annotated[
            str | None,
            Field(description="Optional page ID or display name filter."),
        ] = None,
        query: Annotated[
            str | None,
            Field(description="Optional search term matching visual title, visual type, ID, or bound field."),
        ] = None,
        workspace: WorkspaceArg = None,
        limit: LimitArg = None,
        offset: OffsetArg = 0,
    ) -> list[dict[str, Any]]:
        """List visuals in the report with their titles, types, and field bindings."""
        return get_service(workspace).visuals(
            page=page, query=query, limit=limit, offset=offset
        )

    @mcp.tool()
    def get_visual(
        visual_id_or_title: Annotated[
            str,
            Field(description="Visual ID or human-readable visual title."),
        ],
        page: Annotated[
            str | None,
            Field(description="Optional page name or ID to disambiguate visuals with identical titles."),
        ] = None,
        workspace: WorkspaceArg = None,
    ) -> dict[str, Any] | None:
        """Get a visual's details and current role-grouped field bindings by ID or title."""
        return get_service(workspace).get_visual(visual_id_or_title, page=page)

    # -------------------------------------------------------------------------
    # 4. Safe Mutations - Dry-Run by Default + Auto-Rollback (4 tools)
    # -------------------------------------------------------------------------

    @mcp.tool()
    def update_measure_dax(
        measure_name: Annotated[str, Field(description="Exact measure name to update or create.")],
        expression: Annotated[str, Field(description="Replacement or new DAX expression.")],
        table: Annotated[
            str | None,
            Field(description="Table name to disambiguate the measure (required when create_if_missing=True)."),
        ] = None,
        create_if_missing: Annotated[
            bool,
            Field(description="When true, create a new measure in the specified table if it does not already exist."),
        ] = False,
        format_string: Annotated[
            str | None,
            Field(description="Optional formatString (e.g. '$#,##0.00' or '0.0%') to set on the measure."),
        ] = None,
        display_folder: Annotated[
            str | None,
            Field(description="Optional displayFolder to organize the measure in the semantic model."),
        ] = None,
        description: Annotated[
            str | None,
            Field(description="Optional /// TMDL description comment to attach to the measure."),
        ] = None,
        expected_current_expression: Annotated[
            str | None,
            Field(description="Optional precondition guard: current DAX expression that must match before write."),
        ] = None,
        expected_replacements: Annotated[
            int | None,
            Field(description="Optional guard requiring an exact number of matches."),
        ] = None,
        workspace: WorkspaceArg = None,
        dry_run: DryRunArg = True,
    ) -> dict[str, Any]:
        """Update an existing measure's DAX expression in TMDL (or create a new measure when create_if_missing=True)."""
        return get_service(workspace).update_dax(
            measure_name,
            expression,
            table=table,
            create_if_missing=create_if_missing,
            format_string=format_string,
            display_folder=display_folder,
            description=description,
            expected_current_expression=expected_current_expression,
            expected_replacements=expected_replacements,
            dry_run=dry_run,
        )

    @mcp.tool()
    def update_table_sql(
        old_text: Annotated[str, Field(description="Exact SQL line or block to replace.")],
        new_text: Annotated[str, Field(description="Replacement SQL line or block.")],
        table_name: Annotated[
            str | None,
            Field(description="Table name containing the SQL partition."),
        ] = None,
        partition: Annotated[
            str | None,
            Field(description="Optional partition name."),
        ] = None,
        expected_replacements: Annotated[
            int | None,
            Field(description="Optional guard requiring an exact number of matches."),
        ] = None,
        workspace: WorkspaceArg = None,
        dry_run: DryRunArg = True,
    ) -> dict[str, Any]:
        """Surgically replace a line or block inside a table's Value.NativeQuery SQL expression."""
        return get_service(workspace).update_table_sql(
            old_text,
            new_text,
            table_name=table_name,
            partition=partition,
            expected_replacements=expected_replacements,
            dry_run=dry_run,
        )

    @mcp.tool()
    def update_visual_fields(
        visual_id_or_title: Annotated[
            str,
            Field(description="Visual ID or human-readable title of the visual to edit."),
        ],
        add_fields: Annotated[
            list[dict[str, str] | str] | None,
            Field(
                description=(
                    "Fields to add, e.g. ['Sales[Total Revenue]'] or "
                    "[{'field': 'Customer[Region]', 'role': 'Category'}]."
                )
            ),
        ] = None,
        remove_fields: Annotated[
            list[str] | None,
            Field(description="Fields to remove, e.g. ['Sales[Total Cost]'] or ['Total Cost']."),
        ] = None,
        page: Annotated[
            str | None,
            Field(description="Optional page name or ID."),
        ] = None,
        expected_replacements: Annotated[
            int | None,
            Field(description="Optional guard requiring an exact number of field binding modifications."),
        ] = None,
        workspace: WorkspaceArg = None,
        dry_run: DryRunArg = True,
    ) -> dict[str, Any]:
        """Add or remove measures and columns on a visual without modifying visual styling or layout."""
        return get_service(workspace).update_visual_fields(
            visual_id_or_title,
            add_fields=add_fields,
            remove_fields=remove_fields,
            page=page,
            expected_replacements=expected_replacements,
            dry_run=dry_run,
        )

    @mcp.tool()
    def rename_measure(
        old_name: Annotated[str, Field(description="Existing measure name.")],
        new_name: Annotated[str, Field(description="New measure name.")],
        table: Annotated[
            str | None,
            Field(description="Optional table name to disambiguate the measure."),
        ] = None,
        expected_replacements: Annotated[
            int | None,
            Field(description="Optional guard requiring an exact number of replacements."),
        ] = None,
        workspace: WorkspaceArg = None,
        dry_run: DryRunArg = True,
    ) -> dict[str, Any]:
        """Atomically rename a measure across TMDL declarations, DAX formulas, and report visuals."""
        return get_service(workspace).rename_measure(
            old_name,
            new_name,
            table=table,
            expected_replacements=expected_replacements,
            dry_run=dry_run,
        )

    # -------------------------------------------------------------------------
    # 5. Power BI Analyst Superpowers (4 tools)
    # -------------------------------------------------------------------------

    @mcp.tool()
    def find_unused_objects(workspace: WorkspaceArg = None) -> dict[str, Any]:
        """Scan the semantic model and report layout to identify unused measures and unreferenced columns."""
        return get_service(workspace).find_unused_objects()

    @mcp.tool()
    def format_dax(
        measure_name: Annotated[
            str | None,
            Field(description="Measure name to format in-place in TMDL."),
        ] = None,
        dax_expression: Annotated[
            str | None,
            Field(description="Optional DAX string to format (standalone or replacement)."),
        ] = None,
        table: Annotated[
            str | None,
            Field(description="Optional table name."),
        ] = None,
        workspace: WorkspaceArg = None,
        dry_run: DryRunArg = True,
    ) -> dict[str, Any]:
        """Standardize DAX formatting (uppercase functions, clean operator spacing) standalone or in TMDL."""
        return get_service(workspace).format_dax(
            measure_name=measure_name,
            dax_expression=dax_expression,
            table=table,
            dry_run=dry_run,
        )

    @mcp.tool()
    def generate_measure_docs(
        measure_name: Annotated[str, Field(description="Measure name to document in TMDL.")],
        description: Annotated[
            str | None,
            Field(description="Optional custom description; auto-generated from DAX if omitted."),
        ] = None,
        table: Annotated[
            str | None,
            Field(description="Optional table name."),
        ] = None,
        workspace: WorkspaceArg = None,
        dry_run: DryRunArg = True,
    ) -> dict[str, Any]:
        """Generate and write a /// TMDL documentation docstring for a measure."""
        return get_service(workspace).generate_measure_docs(
            measure_name,
            description=description,
            table=table,
            dry_run=dry_run,
        )

    @mcp.tool()
    def check_model_bpa(workspace: WorkspaceArg = None) -> dict[str, Any]:
        """Run Best Practice Analyzer rules against the semantic model (keys, relationships, types, DAX)."""
        return get_service(workspace).check_model_bpa()

    # -------------------------------------------------------------------------
    # 6. Validation (1 tool)
    # -------------------------------------------------------------------------

    @mcp.tool()
    def validate_workspace(workspace: WorkspaceArg = None) -> list[dict[str, Any]]:
        """Run structural and syntax validation across TMDL and PBIR report JSON."""
        return get_service(workspace).validation()

    # -------------------------------------------------------------------------
    # 7. MCP Resources & Prompts (Zero tool-surface overhead)
    # -------------------------------------------------------------------------

    @mcp.resource("pbip://workspaces")
    def resource_workspaces() -> str:
        """JSON list of discovered PBIP workspaces."""
        import json

        return json.dumps(list_workspaces(), indent=2)

    @mcp.resource("pbip://workspace/{workspace}/summary")
    def resource_workspace_summary(workspace: str) -> str:
        """JSON summary, object counts, and BPA health summary for a PBIP workspace."""
        import json

        ws_arg = None if workspace in {"default", "single-workspace"} else workspace
        return json.dumps(get_workspace(ws_arg), indent=2)

    @mcp.resource("pbip://workspace/{workspace}/bpa")
    def resource_workspace_bpa(workspace: str) -> str:
        """JSON Best Practice Analyzer report for a PBIP workspace."""
        import json

        ws_arg = None if workspace in {"default", "single-workspace"} else workspace
        return json.dumps(check_model_bpa(ws_arg), indent=2)

    @mcp.prompt()
    def workflow_measure_update(measure_name: str, new_dax: str, workspace: str = "") -> str:
        """Guided 5-stage prompt for safely updating or creating a DAX measure."""
        ws_clause = f" in workspace {workspace!r}" if workspace else ""
        return (
            f"Execute the 'measure_lookup_and_update' workflow for measure {measure_name!r}{ws_clause}:\n"
            f"1. Call search_measures(query={measure_name!r}) to resolve the exact measure, table, and current DAX.\n"
            f"2. Call analyze_impact(term={measure_name!r}) to inspect upstream columns and downstream visuals.\n"
            f"3. Call update_measure_dax(measure_name={measure_name!r}, expression={new_dax!r}, dry_run=True) and review the unified diff.\n"
            f"4. Call update_measure_dax(measure_name={measure_name!r}, expression={new_dax!r}, dry_run=False) with expected_current_expression.\n"
            f"5. Verify post-write validation status."
        )

    @mcp.prompt()
    def workflow_model_audit(workspace: str = "") -> str:
        """Guided prompt for running a comprehensive Power BI Semantic Model & Report Layout audit."""
        ws_clause = f" for workspace {workspace!r}" if workspace else ""
        return (
            f"Run a comprehensive Power BI model and report audit{ws_clause}:\n"
            f"1. Call get_workspace() to inspect object counts and overall health_summary.\n"
            f"2. Call check_model_bpa() to identify relationship, DAX, performance, and report visual binding issues.\n"
            f"3. Call find_unused_objects() to list unused measures, unused columns, and dead-branch objects.\n"
            f"4. Call validate_workspace() to confirm structural integrity and summarize prioritized recommendations."
        )

    return mcp
