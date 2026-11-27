from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal, TypeVar, cast

from mcp.server import MCPServer
from pydantic import Field

from .errors import AmbiguousWorkspaceError, InvalidArgumentError, WorkspaceNotFoundError
from .m_codec import format_get_table_sql_json, format_get_table_sql_text
from .meta_tools import ToolSpec, build_pbi_help, dispatch_pbi_call, extract_tool_specs
from .service import PbipService
from .workspace_registry import WorkspaceRegistry

F = TypeVar("F", bound=Callable[..., Any])
Backend = PbipService | WorkspaceRegistry
WorkspaceArg = Annotated[
    str | None,
    Field(
        description=(
            "Workspace identifier returned by list_workspaces "
            "(prefer workspace_id or unique name). "
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


def _build_server_and_tools(
    backend: Backend, *, meta_tools: bool = False
) -> tuple[MCPServer, dict[str, Callable[..., Any]]]:
    """Build the consolidated PBI MCP server and its underlying 20-tool map."""
    mcp = MCPServer("pbi-local")
    tools_map: dict[str, Callable[..., Any]] = {}

    def register_tool(fn: F) -> F:
        tools_map[fn.__name__] = fn
        if not meta_tools:
            structured = False if fn.__name__ == "get_table_sql" else None
            mcp.add_tool(fn, structured_output=structured)
        return fn

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

    @register_tool
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
        haystack = "\n".join(str(v) for v in item.values()).casefold()
        if query and query.strip().casefold() not in haystack:
            return []
        items = [item]
        if offset > 0:
            items = items[offset:]
        if limit is not None and limit >= 0:
            items = items[:limit]
        return items

    @register_tool
    def get_workspace(workspace: WorkspaceArg = None) -> dict[str, Any]:
        """Get metadata and object counts for a PBIP workspace."""
        svc = get_service(workspace)
        summary = svc.summary()
        if isinstance(backend, WorkspaceRegistry):
            meta = backend.get_workspace(workspace or summary["workspace_id"])
            return {**meta, "summary": summary, "diagnostics": backend.diagnostics}
        return summary

    @register_tool
    def refresh_workspaces() -> list[dict[str, Any]]:
        """Re-scan configured workspace roots and refresh discovered PBIP projects."""
        if isinstance(backend, WorkspaceRegistry):
            return backend.refresh()
        return list_workspaces()

    # -------------------------------------------------------------------------
    # 2. Semantic Model Inspection & Search (5 tools)
    # -------------------------------------------------------------------------

    @register_tool
    def list_tables(
        workspace: WorkspaceArg = None,
        limit: LimitArg = None,
        offset: OffsetArg = 0,
    ) -> list[dict[str, Any]]:
        """List all tables in the semantic model with column/measure counts and native SQL status."""  # noqa: E501
        return get_service(workspace).list_tables(limit=limit, offset=offset)

    @register_tool
    def get_table(
        table_name: Annotated[str, Field(description="Table name to inspect.")],
        workspace: WorkspaceArg = None,
    ) -> dict[str, Any] | None:
        """Retrieve complete table schema including columns, measures, relationships, and SQL source."""  # noqa: E501
        return get_service(workspace).get_table(table_name)

    @register_tool
    def search_measures(
        query: Annotated[
            str | None,
            Field(
                description=(
                    "Case-insensitive search term matching measure name, DAX, or description."
                )
            ),
        ] = None,
        table: Annotated[
            str | None,
            Field(description="Optional table name filter."),
        ] = None,
        workspace: WorkspaceArg = None,
        limit: LimitArg = None,
        offset: OffsetArg = 0,
    ) -> list[dict[str, Any]]:
        """Find measures across the semantic model and return their table, name, DAX expression, and description."""  # noqa: E501
        return get_service(workspace).search_measures(
            query=query, table=table, limit=limit, offset=offset
        )

    @register_tool
    def get_table_sql(
        table_name: Annotated[
            str | None,
            Field(description="Table name containing a Partition SQL query."),
        ] = None,
        partition: Annotated[
            str | None,
            Field(
                description="Optional partition name if the table has multiple SQL partitions."
            ),
        ] = None,
        outline: Annotated[
            bool,
            Field(
                description=(
                    "When true, return only the structural outline (SQL Sections or clause "
                    "landmarks) without SQL body for any query size."
                )
            ),
        ] = False,
        ctes: Annotated[
            list[str] | str | None,
            Field(
                description=(
                    "Optional SQL Section name or list of names (case-insensitive, supports "
                    "'main') to read with canonical 1-based line numbers (400-line ceiling)."
                )
            ),
        ] = None,
        start_line: Annotated[
            int | None,
            Field(
                description=(
                    "Optional inclusive 1-based start line (defaults to a 120-line window "
                    "when end_line is omitted)."
                )
            ),
        ] = None,
        end_line: Annotated[
            int | None,
            Field(
                description=(
                    "Optional inclusive 1-based end line (starts at line 1 when start_line "
                    "is omitted)."
                )
            ),
        ] = None,
        format: Annotated[
            Literal["text", "json"],
            Field(
                description=(
                    "Output format: 'text' (default, unescaped Clean SQL View) or 'json'."
                )
            ),
        ] = "text",
        workspace: WorkspaceArg = None,
    ) -> str | dict[str, Any]:
        """Extract Partition SQL (Clean SQL View or outline) for a table."""
        if format is None:
            fmt = "text"
        elif isinstance(format, str):
            fmt = format.strip().lower()
        else:
            fmt = ""
        if fmt not in {"text", "json"}:
            raise InvalidArgumentError(
                f"Unsupported format {format!r}; expected 'text' or 'json'.",
                code="INVALID_ARGUMENT",
            )
        data = get_service(workspace).get_table_sql(
            table_name=table_name,
            partition=partition,
            outline=outline,
            ctes=ctes,
            start_line=start_line,
            end_line=end_line,
        )
        if fmt == "json":
            return format_get_table_sql_json(data)
        return format_get_table_sql_text(data)

    @register_tool
    def analyze_impact(
        term: Annotated[
            str,
            Field(
                description=(
                    "Measure or column name whose usage across TMDL and visuals "
                    "should be analyzed."
                )
            ),
        ],
        workspace: WorkspaceArg = None,
    ) -> dict[str, Any]:
        """Analyze where a measure or column is referenced across TMDL formulas and report visuals."""  # noqa: E501
        return get_service(workspace).impact(term)

    # -------------------------------------------------------------------------
    # 3. Report Layout & Visual Inspection (3 tools)
    # -------------------------------------------------------------------------

    @register_tool
    def list_report_pages(
        workspace: WorkspaceArg = None,
        limit: LimitArg = None,
        offset: OffsetArg = 0,
    ) -> list[dict[str, Any]]:
        """List all report pages with their display names and visual counts."""
        return get_service(workspace).pages(limit=limit, offset=offset)

    @register_tool
    def list_visuals(
        page: Annotated[
            str | None,
            Field(description="Optional page ID or display name filter."),
        ] = None,
        query: Annotated[
            str | None,
            Field(
                description=(
                    "Optional search term matching visual title, visual type, ID, or bound field."
                )
            ),
        ] = None,
        workspace: WorkspaceArg = None,
        limit: LimitArg = None,
        offset: OffsetArg = 0,
    ) -> list[dict[str, Any]]:
        """List visuals in the report with their titles, types, and field bindings."""
        return get_service(workspace).visuals(
            page=page, query=query, limit=limit, offset=offset
        )

    @register_tool
    def get_visual(
        visual_id_or_title: Annotated[
            str,
            Field(description="Visual ID or human-readable visual title."),
        ],
        page: Annotated[
            str | None,
            Field(
                description=(
                    "Optional page name or ID to disambiguate visuals with identical titles."
                )
            ),
        ] = None,
        workspace: WorkspaceArg = None,
    ) -> dict[str, Any] | None:
        """Get a visual's details and current role-grouped field bindings by ID or title."""
        return get_service(workspace).get_visual(visual_id_or_title, page=page)

    # -------------------------------------------------------------------------
    # 4. Safe Mutations - Dry-Run by Default + Auto-Rollback (4 tools)
    # -------------------------------------------------------------------------

    @register_tool
    def update_measure_dax(
        measure_name: Annotated[str, Field(description="Exact measure name to update or create.")],
        expression: Annotated[str, Field(description="Replacement or new DAX expression.")],
        table: Annotated[
            str | None,
            Field(
                description=(
                    "Table name to disambiguate the measure (required when create_if_missing=True)."
                )
            ),
        ] = None,
        create_if_missing: Annotated[
            bool,
            Field(
                description=(
                    "When true, create a new measure in the specified table "
                    "if it does not already exist."
                )
            ),
        ] = False,
        format_string: Annotated[
            str | None,
            Field(
                description=(
                    "Optional formatString (e.g. '$#,##0.00' or '0.0%') to set on the measure."
                )
            ),
        ] = None,
        display_folder: Annotated[
            str | None,
            Field(
                description=(
                    "Optional displayFolder to organize the measure in the semantic model."
                )
            ),
        ] = None,
        description: Annotated[
            str | None,
            Field(description="Optional /// TMDL description comment to attach to the measure."),
        ] = None,
        expected_current_expression: Annotated[
            str | None,
            Field(
                description=(
                    "Optional precondition guard: current DAX expression that must match "
                    "before write."
                )
            ),
        ] = None,
        expected_replacements: Annotated[
            int | None,
            Field(description="Optional guard requiring an exact number of matches."),
        ] = None,
        workspace: WorkspaceArg = None,
        dry_run: DryRunArg = True,
    ) -> dict[str, Any]:
        """Update an existing measure's DAX expression in TMDL (or create a new measure when create_if_missing=True)."""  # noqa: E501
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

    @register_tool
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

    @register_tool
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
            Field(
                description="Optional guard requiring an exact number of field binding modifications."  # noqa: E501
            ),
        ] = None,
        workspace: WorkspaceArg = None,
        dry_run: DryRunArg = True,
    ) -> dict[str, Any]:
        """Add or remove measures and columns on a visual without modifying visual styling or layout."""  # noqa: E501
        return get_service(workspace).update_visual_fields(
            visual_id_or_title,
            add_fields=add_fields,
            remove_fields=remove_fields,
            page=page,
            expected_replacements=expected_replacements,
            dry_run=dry_run,
        )

    @register_tool
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
        """Atomically rename a measure across TMDL declarations, DAX formulas, and report visuals."""  # noqa: E501
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

    @register_tool
    def find_unused_objects(workspace: WorkspaceArg = None) -> dict[str, Any]:
        """Scan the semantic model and report layout to identify unused measures and unreferenced columns."""  # noqa: E501
        return get_service(workspace).find_unused_objects()

    @register_tool
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
        """Standardize DAX formatting (uppercase functions, clean operator spacing) standalone or in TMDL."""  # noqa: E501
        return get_service(workspace).format_dax(
            measure_name=measure_name,
            dax_expression=dax_expression,
            table=table,
            dry_run=dry_run,
        )

    @register_tool
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

    @register_tool
    def check_model_bpa(workspace: WorkspaceArg = None) -> dict[str, Any]:
        """Run Best Practice Analyzer rules against the semantic model (keys, relationships, types, DAX)."""  # noqa: E501
        return get_service(workspace).check_model_bpa()

    # -------------------------------------------------------------------------
    # 6. Validation (1 tool)
    # -------------------------------------------------------------------------

    @register_tool
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
            f"Execute the 'measure_lookup_and_update' workflow for measure "
            f"{measure_name!r}{ws_clause}:\n"
            f"1. Call search_measures(query={measure_name!r}) to resolve the exact measure, "
            "table, and current DAX.\n"
            f"2. Call analyze_impact(term={measure_name!r}) to inspect upstream columns "
            "and downstream visuals.\n"
            f"3. Call update_measure_dax(measure_name={measure_name!r}, "
            f"expression={new_dax!r}, dry_run=True) and review the unified diff.\n"
            f"4. Call update_measure_dax(measure_name={measure_name!r}, "
            f"expression={new_dax!r}, dry_run=False) with expected_current_expression.\n"
            "5. Verify post-write validation status."
        )

    @mcp.prompt()
    def workflow_model_audit(workspace: str = "") -> str:
        """Guided prompt for running a comprehensive Power BI Semantic Model & Report Layout audit."""  # noqa: E501
        ws_clause = f" for workspace {workspace!r}" if workspace else ""
        return (
            f"Run a comprehensive Power BI model and report audit{ws_clause}:\n"
            "1. Call get_workspace() to inspect object counts and overall health_summary.\n"
            "2. Call check_model_bpa() to identify relationship, DAX, performance, "
            "and report visual binding issues.\n"
            "3. Call find_unused_objects() to list unused measures, unused columns, "
            "and dead-branch objects.\n"
            "4. Call validate_workspace() to confirm structural integrity and summarize "
            "prioritized recommendations."
        )

    if meta_tools:
        specs = extract_tool_specs(tools_map)

        @mcp.tool()
        def pbi_call(
            tool_name: Annotated[
                str,
                Field(
                    description=(
                        "Exact PBI tool name to invoke (e.g. 'get_table', 'search_measures')."
                    )
                ),
            ],
            arguments: Annotated[
                dict[str, Any] | None,
                Field(
                    description=(
                        "Optional JSON object of keyword arguments for the target PBI tool."
                    )
                ),
            ] = None,
        ) -> Any:
            """Execute any PBI_MCP tool by name with an arguments object."""
            return dispatch_pbi_call(tools_map, specs, tool_name, arguments)

        @mcp.tool()
        def pbi_help(
            tool_name: Annotated[
                str | None,
                Field(
                    description=(
                        "Optional exact PBI tool name to inspect full parameter docs and defaults."
                    )
                ),
            ] = None,
            query: Annotated[
                str | None,
                Field(
                    description=(
                        "Optional keyword to search tool names, descriptions, and parameters."
                    )
                ),
            ] = None,
        ) -> dict[str, Any]:
            """Look up full parameter documentation for a PBI tool or search available tools."""
            return build_pbi_help(specs, tool_name=tool_name, query=query)

    return mcp, tools_map


def create_server(backend: Backend, *, meta_tools: bool = False) -> MCPServer:
    """Create the consolidated PBI MCP server (20 tools, or 2 meta-tools when meta_tools=True)."""
    mcp, _ = _build_server_and_tools(backend, meta_tools=meta_tools)
    return mcp


def get_tool_specs() -> list[ToolSpec]:
    """Introspect the 20 PBI tool definitions without requiring an active workspace."""
    _, tools_map = _build_server_and_tools(cast(Backend, None), meta_tools=False)
    return extract_tool_specs(tools_map)

