from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from pbi_mcp_server import PbipService, WorkspaceRegistry, create_server

EXPECTED_TOOLS = [
    "list_workspaces",
    "get_workspace",
    "refresh_workspaces",
    "list_tables",
    "get_table",
    "search_measures",
    "get_table_sql",
    "analyze_impact",
    "list_report_pages",
    "list_visuals",
    "get_visual",
    "update_measure_dax",
    "update_table_sql",
    "update_visual_fields",
    "rename_measure",
    "find_unused_objects",
    "format_dax",
    "generate_measure_docs",
    "check_model_bpa",
    "validate_workspace",
]


def clone_sample_workspace(tmp_path: Path) -> Path:
    repo_root = Path(__file__).parents[1]
    shutil.copy2(repo_root / "sample_project.pbip", tmp_path / "sample_project.pbip")
    shutil.copytree(repo_root / "sample_project.Report", tmp_path / "sample_project.Report")
    shutil.copytree(repo_root / "sample_project.SemanticModel", tmp_path / "sample_project.SemanticModel")
    return tmp_path / "sample_project.pbip"


def test_server_registers_exact_20_tools(tmp_path: Path) -> None:
    pbip = clone_sample_workspace(tmp_path)
    srv = create_server(PbipService(pbip))
    tools = asyncio.run(srv.list_tools())
    names = [t.name for t in tools]
    assert len(names) == 20
    assert names == EXPECTED_TOOLS


def test_server_with_workspace_registry_auto_resolves_single_workspace(tmp_path: Path) -> None:
    clone_sample_workspace(tmp_path)
    registry = WorkspaceRegistry([tmp_path])
    srv = create_server(registry)

    # Single workspace in registry should auto-resolve even when workspace arg is omitted
    res = asyncio.run(srv.call_tool("search_measures", {"query": "Revenue"}))
    assert res is not None
