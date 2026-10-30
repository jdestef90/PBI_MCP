"""MCP Server implementation exposing tools for PBIP authoring and editing."""

from __future__ import annotations
import os
import logging
from typing import Any, Dict, List, Optional
from mcp.server.mcpserver import MCPServer
from mcp_server.cache.state_cache import StateCache
from mcp_server.engine_a.parser_wrapper import SemanticModelEngine
from mcp_server.engine_a.models import (
    ColumnMetadata,
    MeasureMetadata,
    TableMetadata,
    TableSummary,
    SemanticModelSummary,
)
from mcp_server.engine_b.layout_flattener import ReportLayoutEngine
from mcp_server.engine_b.models import VisualShadow
from mcp_server.integrity.rename_engine import RenameEngine
from mcp_server.integrity.validator import StructureValidator

logger = logging.getLogger("pbi-mcp-server")


def create_server(workspace_path: Optional[str] = None) -> MCPServer:
    """Create and configure the Power BI MCP server instance."""
    app = MCPServer("pbi-mcp-server")

    default_workspace = os.path.abspath(
        workspace_path
        or os.environ.get("PBI_WORKSPACE_PATH")
        or os.getcwd()
    )

    shared_cache = StateCache()

    def get_engines(override_path: Optional[str] = None):
        target_path = os.path.abspath(override_path or default_workspace)
        sem_engine = SemanticModelEngine(target_path, cache=shared_cache)
        lay_engine = ReportLayoutEngine(target_path, cache=shared_cache)
        ren_engine = RenameEngine(target_path, semantic_engine=sem_engine, layout_engine=lay_engine)
        val_engine = StructureValidator(target_path)
        return sem_engine, lay_engine, ren_engine, val_engine

    # ==========================================
    # Engine A: Semantic Model Read Operations
    # ==========================================

    @app.tool()
    def get_model_summary(project_path: Optional[str] = None) -> SemanticModelSummary:
        """Expose a clean high-level summary of the semantic model without context flooding."""
        sem, _, _, _ = get_engines(project_path)
        return sem.get_model_summary()

    @app.tool()
    def list_tables(project_path: Optional[str] = None) -> List[TableSummary]:
        """List tables with column and measure summaries."""
        sem, _, _, _ = get_engines(project_path)
        names = sem.list_table_names()
        return [sem.get_table_summary(n) for n in names]

    @app.tool()
    def get_table_schema(table_name: str, project_path: Optional[str] = None) -> TableMetadata:
        """Get full schema for a table, including columns, measures, and partitions."""
        sem, _, _, _ = get_engines(project_path)
        return sem.get_table(table_name)

    @app.tool()
    def get_measure(table_name: str, measure_name: str, project_path: Optional[str] = None) -> Optional[MeasureMetadata]:
        """Get DAX formula, description, and formatting for a specific measure."""
        sem, _, _, _ = get_engines(project_path)
        return sem.get_measure(table_name, measure_name)

    # ==========================================
    # Engine A: Semantic Model Write Operations
    # ==========================================

    @app.tool()
    def update_measure_dax(
        table_name: str,
        measure_name: str,
        new_dax: str,
        project_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update a measure's DAX expression in TMDL while preserving comments and indentation."""
        sem, _, _, val = get_engines(project_path)
        success = sem.update_measure_dax(table_name, measure_name, new_dax)
        validation = val.validate()
        return {
            "success": success,
            "table_name": table_name,
            "measure_name": measure_name,
            "validation_passed": validation.is_valid,
            "validation_errors": validation.errors,
        }

    # ==========================================
    # Engine B: Report Layout Operations
    # ==========================================

    @app.tool()
    def list_pages(project_path: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all pages in the report with dimensions and visual counts."""
        _, lay, _, _ = get_engines(project_path)
        return lay.list_pages()

    @app.tool()
    def get_page_visuals(page_name_or_id: str, project_path: Optional[str] = None) -> List[VisualShadow]:
        """Get flat shadow model of all visuals on a page."""
        _, lay, _, _ = get_engines(project_path)
        return lay.get_page_visuals(page_name_or_id)

    @app.tool()
    def get_visual_property(
        page_name_or_id: str,
        visual_id: str,
        property_path: str,
        project_path: Optional[str] = None,
    ) -> Any:
        """Query visual property using path syntax (e.g. 'position/width', 'visual_type', 'bound_fields')."""
        _, lay, _, _ = get_engines(project_path)
        full_path = f"{page_name_or_id}/{visual_id}/{property_path}"
        return lay.get_property(full_path)

    @app.tool()
    def query_report_layout(jmespath_query: str, project_path: Optional[str] = None) -> Any:
        """Execute a JMESPath query against the Shadow Model of the report layout."""
        _, lay, _, _ = get_engines(project_path)
        return lay.query(jmespath_query)

    # ==========================================
    # Epic 4: Cross-Layer Integration & Integrity
    # ==========================================

    @app.tool()
    def rename_measure(
        table_name: str,
        old_measure_name: str,
        new_measure_name: str,
        project_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Safely rename a measure across both TMDL definitions and report layout JSON bindings."""
        _, _, ren, _ = get_engines(project_path)
        result = ren.rename_measure(table_name, old_measure_name, new_measure_name)
        return result.to_dict()

    @app.tool()
    def validate_pbip_structure(project_path: Optional[str] = None) -> Dict[str, Any]:
        """Run post-write syntax validator across all TMDL files and report layout JSON."""
        _, _, _, val = get_engines(project_path)
        res = val.validate()
        return {
            "is_valid": res.is_valid,
            "errors": res.errors,
            "warnings": res.warnings,
            "checked_files": res.checked_files,
        }

    return app
