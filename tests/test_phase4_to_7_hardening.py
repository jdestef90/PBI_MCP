"""Phase 4, 5, 6, & 7 tests: Mutation Engine, Analyst BPA, Workflows, and Production Guardrails."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from pbi_mcp_server.errors import (
    AmbiguousObjectError,
    PreconditionFailedError,
)
from pbi_mcp_server.logging_utils import redact_sensitive_paths
from pbi_mcp_server.service import PbipService

REPO_ROOT = Path(__file__).parents[1]


def clone_workspace(tmp_path: Path) -> Path:
    dest = tmp_path / "proj"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "sample_project.pbip", dest / "sample_project.pbip")
    shutil.copytree(REPO_ROOT / "sample_project.Report", dest / "sample_project.Report")
    shutil.copytree(REPO_ROOT / "sample_project.SemanticModel", dest / "sample_project.SemanticModel")
    return dest / "sample_project.pbip"


def test_phase4_mutation_diffs_preconditions_and_multiline_dax(tmp_path: Path) -> None:
    pbip = clone_workspace(tmp_path)
    svc = PbipService(pbip)
    current_rev_expr = "SUMX('Sales', 'Sales'[Quantity] * 'Sales'[UnitPrice])"

    # 1. Precondition failure when expected_current_expression does not match
    with pytest.raises(PreconditionFailedError) as exc_info:
        svc.update_dax(
            "Total Revenue",
            "SUM(Sales[Quantity]) * 2",
            expected_current_expression="SUM(Sales[WrongCol])",
            dry_run=False,
        )
    assert exc_info.value.code == "PRECONDITION_FAILED"

    # 2. Dry-run returns unified diff without modifying file
    dry_plan = svc.update_dax(
        "Total Revenue",
        "VAR Base = SUM('Sales'[Quantity])\nRETURN\n    Base",
        expected_current_expression=current_rev_expr,
        dry_run=True,
    )
    assert dry_plan["status"] == "dry_run"
    assert len(dry_plan["diffs"]) == 1
    assert "--- a/" in dry_plan["diffs"][0]["unified_diff"]
    assert "+++ b/" in dry_plan["diffs"][0]["unified_diff"]
    assert dry_plan["workflow"]["workflow_id"] == "measure_lookup_and_update"

    # 3. Apply multi-line DAX update and verify round-trip parse + validation
    applied = svc.update_dax(
        "Total Revenue",
        "VAR Base = SUM('Sales'[Quantity])\nRETURN\n    Base",
        expected_current_expression=current_rev_expr,
        dry_run=False,
    )
    assert applied["status"] == "applied"
    assert applied["rolled_back"] is False
    updated_m = svc.resolve_measure("Total Revenue")
    assert "VAR Base = SUM('Sales'[Quantity])" in updated_m["expression"]


def test_phase4_multifile_rename_and_transactional_rollback(tmp_path: Path) -> None:
    pbip = clone_workspace(tmp_path)
    svc = PbipService(pbip)

    # Expected replacements precondition failure on rename_measure
    with pytest.raises(PreconditionFailedError):
        svc.rename_measure("Total Revenue", "Gross Revenue", expected_replacements=999, dry_run=False)

    # Multi-file rename preview includes diffs for both TMDL and report JSON
    preview = svc.rename_measure("Total Revenue", "Gross Revenue", dry_run=True)
    assert len(preview["changed_files"]) >= 2
    assert len(preview["diffs"]) >= 2

    # Duplicate measure name across tables raises AmbiguousObjectError when table is omitted
    cust_tmdl = pbip.parent / "sample_project.SemanticModel" / "definition" / "tables" / "Customer.tmdl"
    cust_tmdl.write_text(
        cust_tmdl.read_text(encoding="utf-8") + "\n\tmeasure 'Total Revenue' = COUNTROWS(Customer)\n",
        encoding="utf-8",
    )
    with pytest.raises(AmbiguousObjectError):
        svc.update_dax("Total Revenue", "100", dry_run=True)


def test_phase5_expanded_model_and_report_bpa_rules(tmp_path: Path) -> None:
    pbip = clone_workspace(tmp_path)

    # Inject:
    # - broken relationship endpoint & many-to-many relationship in relationships.tmdl
    # - complex calculated column in Sales.tmdl
    # - broken visual binding in card visual.json
    rel_tmdl = pbip.parent / "sample_project.SemanticModel" / "definition" / "relationships.tmdl"
    rel_tmdl.write_text(
        rel_tmdl.read_text(encoding="utf-8")
        + "\nrelationship rel_Broken\n"
        + "\tfromCardinality: many\n"
        + "\ttoCardinality: many\n"
        + "\tfromColumn: Sales.MissingFK\n"
        + "\ttoColumn: Customer.CustomerID\n",
        encoding="utf-8",
    )

    sales_tmdl = pbip.parent / "sample_project.SemanticModel" / "definition" / "tables" / "Sales.tmdl"
    sales_tmdl.write_text(
        sales_tmdl.read_text(encoding="utf-8")
        + "\n\tcalculatedColumn ComplexCol = CALCULATE(SUM('Sales'[Quantity]))\n"
        + "\t\tdataType: decimal\n",
        encoding="utf-8",
    )

    # Add an empty page and a broken visual binding
    empty_page_dir = pbip.parent / "sample_project.Report" / "definition" / "pages" / "ReportSectionEmpty"
    empty_page_dir.mkdir(parents=True)
    (empty_page_dir / "page.json").write_text(
        '{"name": "ReportSectionEmpty", "displayName": "Blank Page"}',
        encoding="utf-8",
    )

    svc = PbipService(pbip)
    # Bind a nonexistent field to card visual
    svc.update_visual_fields("vis_card_revenue", add_fields=["Sales[DeletedField]"], dry_run=False)

    bpa = svc.check_model_bpa()
    rule_ids = {i["rule_id"] for i in bpa["issues"]}
    assert "BPA_BROKEN_RELATIONSHIP_ENDPOINT" in rule_ids
    assert "BPA_MANY_TO_MANY_RELATIONSHIP" in rule_ids
    assert "BPA_CALCULATED_COLUMN_COMPLEXITY" in rule_ids
    assert "BPA_EMPTY_REPORT_PAGE" in rule_ids
    assert "BPA_BROKEN_VISUAL_BINDING" in rule_ids
    assert bpa["health_summary"]["status"] == "critical"


def test_phase6_workflow_orchestrator_end_to_end(tmp_path: Path) -> None:
    pbip = clone_workspace(tmp_path)
    # Add a native SQL partition to Customer.tmdl for sql_inspection_and_replacement workflow
    cust_tmdl = pbip.parent / "sample_project.SemanticModel" / "definition" / "tables" / "Customer.tmdl"
    cust_tmdl.write_text(
        "table Customer\n"
        "\tcolumn CustomerID\n"
        "\t\tdataType: int64\n"
        "\t\tsourceColumn: CustomerID\n"
        "\tcolumn CustomerName\n"
        "\t\tdataType: string\n"
        "\t\tsourceColumn: CustomerName\n"
        "\tcolumn Region\n"
        "\t\tdataType: string\n"
        "\t\tsourceColumn: Region\n"
        "\tpartition Customer = m\n"
        "\t\tmode: import\n"
        "\t\tsource = ```\n"
        "\t\t\tlet\n"
        '\t\t\t    Source = Value.NativeQuery(Sql.Database("localhost", "SalesDB"), "SELECT CustomerID, CustomerName, Region FROM dbo.Customer", null, [EnableFolding=true])\n'
        "\t\t\tin\n"
        "\t\t\t    Source\n"
        "\t\t```\n",
        encoding="utf-8",
    )

    svc = PbipService(pbip)

    # 1. Measure lookup & update workflow
    res1 = svc.workflows.measure_lookup_and_update(
        "Total Cost",
        "SUMX('Sales', 'Sales'[Quantity] * 12)",
        table="Sales",
        expected_current_expression="SUMX('Sales', 'Sales'[Quantity] * 10)",
        apply=True,
    )
    assert res1["status"] == "applied"
    assert res1["steps_completed"] == ["resolve", "analyze_impact", "dry_run_preview", "apply_mutation", "validate"]

    # 2. SQL inspection & replacement workflow
    res2 = svc.workflows.sql_inspection_and_replacement(
        "Customer",
        "SELECT CustomerID, CustomerName, Region FROM dbo.Customer",
        "SELECT CustomerID, CustomerName, Region FROM dbo.Customer WHERE Region IS NOT NULL",
        apply=True,
    )
    assert res2["status"] == "applied"

    # 3. Visual field binding workflow
    res3 = svc.workflows.visual_field_binding_update(
        "vis_bar_revenue_region",
        add_fields=["Sales[Total Cost]"],
        apply=True,
    )
    assert res3["status"] == "applied"

    # 4. Cross-layer measure rename workflow
    res4 = svc.workflows.cross_layer_measure_rename(
        "Total Cost",
        "Operating Cost",
        table="Sales",
        apply=True,
    )
    assert res4["status"] == "applied"
    assert svc.resolve_measure("Operating Cost")["name"] == "Operating Cost"


def test_phase7_logging_redaction_and_large_project_performance(tmp_path: Path) -> None:
    # Sensitive path redaction
    raw_msg = (
        r"Loaded workspace from C:\Users\jdestef\OneDrive - Corp\Projects\Sales.pbip "
        "and /home/justin/Documents/Secret/Model.tmdl"
    )
    redacted = redact_sensitive_paths(raw_msg)
    assert "jdestef" not in redacted
    assert "justin" not in redacted
    assert "<user>" in redacted

    # Performance benchmark on synthetic workspace with 25 tables, 127 measures, and visuals
    perf_pbip = clone_workspace(tmp_path)
    perf_root = perf_pbip.parent
    tables_dir = perf_root / "sample_project.SemanticModel" / "definition" / "tables"
    for idx in range(25):
        t_file = tables_dir / f"Fact_{idx}.tmdl"
        lines = [f"table Fact_{idx}"]
        for c_idx in range(5):
            lines.append(f"\tcolumn Col_{c_idx}")
            lines.append("\t\tdataType: int64")
            lines.append(f"\t\tsourceColumn: Col_{c_idx}")
        for m_idx in range(5):
            lines.append(f"\t/// Measure {m_idx} on Fact_{idx}")
            lines.append(f"\tmeasure 'Metric_{idx}_{m_idx}' = SUM(Fact_{idx}[Col_0])")
            lines.append("\t\tformatString: #,##0")
        t_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    start = time.perf_counter()
    svc = PbipService(perf_root / "sample_project.pbip")
    summary = svc.summary()
    impact = svc.impact("Sales[Quantity]")
    bpa = svc.check_model_bpa()
    elapsed = time.perf_counter() - start

    assert summary["counts"]["tables"] == 27
    assert summary["counts"]["measures"] == 127
    assert impact["counts"]["dependent_measures"] >= 2
    assert bpa["issue_count"] >= 1
    assert elapsed < 3.0  # Full scan + lineage + BPA completes well under 3 seconds
