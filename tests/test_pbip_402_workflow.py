"""PBIP-402: Integration Test — The "Rename Measure" Workflow.

Executes a full end-to-end rename workflow (Total Revenue -> Gross Revenue):
1. Updates TMDL definitions
2. Updates JSON layout bound references (queryRef, Name, Measure.Property, NativeReferenceName, Restatement)
3. Verifies file consistency and absence of naive string corruption
4. Runs post-write syntax validator
"""

import os
import shutil
import json
from mcp_server.integrity.rename_engine import RenameEngine
from mcp_server.engine_a.tmdl_document import TMDLDocument
from mcp_server.engine_b.layout_flattener import ReportLayoutEngine


def test_rename_measure_e2e_workflow(tmp_path):
    # 1. Setup isolated test environment
    proj_root = tmp_path / "test_project"
    proj_root.mkdir()

    shutil.copy("sample_project.pbip", proj_root / "sample_project.pbip")
    shutil.copytree("sample_project.SemanticModel", proj_root / "sample_project.SemanticModel")
    shutil.copytree("sample_project.Report", proj_root / "sample_project.Report")

    pbip_file = str(proj_root / "sample_project.pbip")

    # 2. Run RenameEngine
    engine = RenameEngine(pbip_file)
    result = engine.rename_measure("Sales", "Total Revenue", "Gross Revenue")

    assert result.success is True
    assert result.old_name == "Total Revenue"
    assert result.new_name == "Gross Revenue"
    assert result.table_name == "Sales"
    assert result.visual_references_updated > 0
    assert result.validation.is_valid is True

    # 3. Verify TMDL changes
    sales_tmdl = proj_root / "sample_project.SemanticModel" / "definition" / "tables" / "Sales.tmdl"
    with open(sales_tmdl, "r", encoding="utf-8") as f:
        tmdl_content = f.read()

    assert "measure 'Gross Revenue' =" in tmdl_content
    assert "measure 'Total Revenue' =" not in tmdl_content
    # Ensure comments and other measures remain intact
    assert "/// Total revenue calculated from sales transactions" in tmdl_content
    assert "measure 'Total Cost' =" in tmdl_content

    # 4. Verify report.json changes
    report_json_path = proj_root / "sample_project.Report" / "report.json"
    with open(report_json_path, "r", encoding="utf-8") as f:
        rep_content = f.read()

    assert "Sales.Gross Revenue" in rep_content
    assert "Sales.Total Revenue" not in rep_content

    # Load report using Engine B to verify all visual containers
    b_engine = ReportLayoutEngine(pbip_file)
    shadow = b_engine.get_shadow_model()

    card_visual = b_engine.find_visual("Executive Summary", "vis_card_revenue")
    assert card_visual is not None
    assert "Sales.Gross Revenue" in card_visual.bound_fields
    assert "Sales.Total Revenue" not in card_visual.bound_fields

    bar_visual = b_engine.find_visual("Executive Summary", "vis_bar_revenue_region")
    assert bar_visual is not None
    assert "Sales.Gross Revenue" in bar_visual.bound_fields

    table_visual = b_engine.find_visual("Details", "vis_table_details")
    assert table_visual is not None
    assert "Sales.Gross Revenue" in table_visual.bound_fields
    assert "Sales.Total Cost" in table_visual.bound_fields
