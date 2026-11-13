"""Phase 2 & Phase 3 tests: Read-Only Project Intelligence & Cross-Layer Lineage."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbi_mcp_server.errors import AmbiguousObjectError
from pbi_mcp_server.lineage import DependencyGraph, find_unused_model_objects
from pbi_mcp_server.service import PbipService

REPO_ROOT = Path(__file__).parents[1]


def clone_workspace(tmp_path: Path) -> Path:
    dest = tmp_path / "proj"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "sample_project.pbip", dest / "sample_project.pbip")
    shutil.copytree(REPO_ROOT / "sample_project.Report", dest / "sample_project.Report")
    shutil.copytree(REPO_ROOT / "sample_project.SemanticModel", dest / "sample_project.SemanticModel")
    return dest / "sample_project.pbip"


def test_canonical_object_ids_and_partition_metadata(tmp_path: Path) -> None:
    pbip = clone_workspace(tmp_path)
    svc = PbipService(pbip)

    tables = svc.list_tables()
    assert len(tables) == 2
    for tbl in tables:
        assert tbl["object_id"].startswith(f"{svc.info.workspace_id}::table::")
        assert tbl["partition_count"] >= 1

    sales = svc.get_table("Sales")
    assert sales is not None
    assert sales["object_id"] == f"{svc.info.workspace_id}::table::Sales"
    assert len(sales["partitions"]) == 1
    part = sales["partitions"][0]
    assert part["source_type"] == "m_table"
    assert "dbo.Sales" in part["source_tables"]

    # Check source_column on columns
    qty_col = next(c for c in sales["columns"] if c["name"] == "Quantity")
    assert qty_col["source_column"] == "Quantity"
    assert qty_col["object_id"] == f"{svc.info.workspace_id}::column::Sales[Quantity]"

    # Check VisualSummary field_bindings and object_ids
    visuals = svc.visuals()
    assert len(visuals) == 4
    bar = next(v for v in visuals if v["visual_id"] == "vis_bar_revenue_region")
    assert bar["object_id"].startswith(f"{svc.info.workspace_id}::visual::")
    assert len(bar["field_bindings"]) >= 2
    rev_binding = next(b for b in bar["field_bindings"] if b["property_name"] == "Total Revenue")
    assert rev_binding["field_kind"] == "measure"
    assert rev_binding["table"] == "Sales"


def test_multiline_dax_and_ambiguous_table_lookup(tmp_path: Path) -> None:
    pbip = clone_workspace(tmp_path)
    sales_tmdl = pbip.parent / "sample_project.SemanticModel" / "definition" / "tables" / "Sales.tmdl"
    sales_s2 = pbip.parent / "sample_project.SemanticModel" / "definition" / "tables" / "SalesArchive.tmdl"
    sales_s2.write_text(
        "table SalesArchive\n\tcolumn OrderID\n\t\tdataType: int64\n",
        encoding="utf-8",
    )

    # Append a multi-line fenced measure to Sales.tmdl
    text = sales_tmdl.read_text(encoding="utf-8")
    text += (
        "\n\t/// Multi-line YoY measure\n"
        "\tmeasure 'Revenue YoY' = ```\n"
        "\t\tVAR CurrentRev = [Total Revenue]\n"
        "\t\tVAR PriorRev = [Total Cost]\n"
        "\t\tRETURN\n"
        "\t\t    DIVIDE(CurrentRev - PriorRev, PriorRev)\n"
        "\t```\n"
        "\t\tformatString: 0.0%\n"
    )
    sales_tmdl.write_text(text, encoding="utf-8")

    svc = PbipService(pbip)
    measures = svc.search_measures(query="Revenue YoY")
    assert len(measures) == 1
    assert "VAR CurrentRev = [Total Revenue]" in measures[0]["expression"]
    assert "DIVIDE(CurrentRev - PriorRev, PriorRev)" in measures[0]["expression"]

    # Exact match "Sales" still resolves to Sales because exact match wins over partial match
    exact_sales = svc.get_table("Sales")
    assert exact_sales is not None
    assert exact_sales["table"] == "Sales"

    # Partial match "Sal" matches both Sales and SalesArchive -> raises AmbiguousObjectError
    with pytest.raises(AmbiguousObjectError) as exc_info:
        svc.get_table("Sal")
    assert exc_info.value.code == "AMBIGUOUS_OBJECT"


def test_cross_layer_lineage_sql_to_column_to_measure_to_visual(tmp_path: Path) -> None:
    pbip = clone_workspace(tmp_path)
    sales_tmdl = pbip.parent / "sample_project.SemanticModel" / "definition" / "tables" / "Sales.tmdl"
    # Add dependent measure Net Margin = [Total Revenue] - [Total Cost]
    sales_tmdl.write_text(
        sales_tmdl.read_text(encoding="utf-8")
        + "\n\tmeasure 'Net Margin' = [Total Revenue] - [Total Cost]\n\t\tformatString: $#,##0\n",
        encoding="utf-8",
    )
    svc = PbipService(pbip)

    # Analyze impact of column Sales[Quantity]
    impact_col = svc.impact("Sales[Quantity]")
    assert len(impact_col["resolved_objects"]) == 1
    # Upstream of Sales[Quantity] includes SQL table dbo.Sales, partition, and SQL field Sales.Quantity
    upstream_kinds = {u["kind"] for u in impact_col["upstream"]["sql_and_partitions"]}
    assert "sql_table" in upstream_kinds
    assert "partition" in upstream_kinds
    assert "sql_field" in upstream_kinds

    # Downstream of Sales[Quantity] includes Total Revenue, Total Cost, Net Margin, Line Total, visuals, and report page
    down_measures = {m["name"] for m in impact_col["downstream"]["measures"]}
    assert {"Total Revenue", "Total Cost", "Net Margin"}.issubset(down_measures)
    down_visuals = {v["visual_id"] for v in impact_col["downstream"]["visuals"]}
    assert "vis_bar_revenue_region" in down_visuals
    assert "vis_card_revenue" in down_visuals
    assert impact_col["counts"]["affected_pages"] >= 1

    # Analyze impact of measure Total Revenue
    impact_meas = svc.impact("Total Revenue")
    up_cols = {c["qualified_name"] for c in impact_meas["upstream"]["columns"]}
    assert "Sales[Quantity]" in up_cols
    assert "Sales[UnitPrice]" in up_cols
    down_meas_from_rev = {m["name"] for m in impact_meas["downstream"]["measures"]}
    assert "Net Margin" in down_meas_from_rev


def test_unused_objects_and_dead_branch_detection(tmp_path: Path) -> None:
    pbip = clone_workspace(tmp_path)
    sales_tmdl = pbip.parent / "sample_project.SemanticModel" / "definition" / "tables" / "Sales.tmdl"
    # Add a base helper measure referenced ONLY by another unused measure (dead branch)
    text = sales_tmdl.read_text(encoding="utf-8")
    text += (
        "\n\tmeasure 'Dead Branch Base' = SUM('Sales'[Quantity])\n"
        "\t\tformatString: #,##0\n"
        "\n\tmeasure 'Dead Branch Leaf' = [Dead Branch Base] * 1.1\n"
        "\t\tformatString: #,##0\n"
    )
    sales_tmdl.write_text(text, encoding="utf-8")

    svc = PbipService(pbip)
    unused = find_unused_model_objects(svc.info)
    unused_meas_names = {m["name"] for m in unused["unused_measures"]}
    indirect_names = {m["name"] for m in unused["indirectly_referenced_only"]}

    # Dead Branch Leaf has 0 downstream references -> in unused_measures
    assert "Dead Branch Leaf" in unused_meas_names
    # Dead Branch Base is referenced by Dead Branch Leaf, which never reaches any visual -> indirectly_referenced_only
    assert "Dead Branch Base" in indirect_names

    # Unresolved reference detection in DependencyGraph
    text_with_broken = sales_tmdl.read_text(encoding="utf-8") + "\n\tmeasure 'Broken Ref' = SUM(Sales[NonExistentCol])\n"
    sales_tmdl.write_text(text_with_broken, encoding="utf-8")
    graph = DependencyGraph(svc.info)
    assert any(u["missing_reference"] == "Sales[NonExistentCol]" for u in graph.unresolved_references)
