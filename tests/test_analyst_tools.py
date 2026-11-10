from __future__ import annotations

import shutil
from pathlib import Path

from pbi_mcp_server.service import PbipService


def clone_sample_workspace(tmp_path: Path) -> Path:
    repo_root = Path(__file__).parents[1]
    shutil.copy2(repo_root / "sample_project.pbip", tmp_path / "sample_project.pbip")
    shutil.copytree(repo_root / "sample_project.Report", tmp_path / "sample_project.Report")
    shutil.copytree(repo_root / "sample_project.SemanticModel", tmp_path / "sample_project.SemanticModel")
    return tmp_path / "sample_project.pbip"


def test_find_unused_objects(tmp_path: Path) -> None:
    pbip = clone_sample_workspace(tmp_path)
    svc = PbipService(pbip)

    unused = svc.find_unused_objects()
    unused_col_names = {c["name"] for c in unused["unused_columns"]}
    # OrderID and Line Total are not used in relationships, visuals, or other measures
    assert "OrderID" in unused_col_names
    assert "Line Total" in unused_col_names
    # CustomerID is used in relationship; Quantity and UnitPrice are used in Total Revenue
    assert "CustomerID" not in unused_col_names
    assert "Quantity" not in unused_col_names
    assert unused["counts"]["unused_measures"] == 0


def test_format_dax_standalone_and_in_place(tmp_path: Path) -> None:
    pbip = clone_sample_workspace(tmp_path)
    svc = PbipService(pbip)

    standalone = svc.format_dax(dax_expression="calculate(sum('Sales'[Quantity])*sum('Sales'[UnitPrice]))")
    assert standalone["formatted_dax"] == "CALCULATE(SUM('Sales'[Quantity]) * SUM('Sales'[UnitPrice]))"

    in_place = svc.format_dax(
        measure_name="Total Cost",
        dax_expression="sumx('Sales','Sales'[Quantity]*12)",
        dry_run=False,
    )
    assert in_place["formatted_dax"] == "SUMX('Sales', 'Sales'[Quantity] * 12)"
    updated = svc.search_measures(query="Total Cost")[0]
    assert updated["expression"] == "SUMX('Sales', 'Sales'[Quantity] * 12)"


def test_generate_measure_docs(tmp_path: Path) -> None:
    pbip = clone_sample_workspace(tmp_path)
    svc = PbipService(pbip)

    res = svc.generate_measure_docs("Total Revenue", dry_run=False)
    assert "Computes Total Revenue in Sales" in res["description"]
    assert "SUMX" in res["description"] or "row-by-row sum iteration" in res["description"]

    updated = svc.search_measures(query="Total Revenue")[0]
    assert updated["description"] == res["description"]


def test_check_model_bpa(tmp_path: Path) -> None:
    pbip = clone_sample_workspace(tmp_path)
    svc = PbipService(pbip)

    bpa = svc.check_model_bpa()
    assert bpa["issue_count"] >= 1
    rule_ids = {i["rule_id"] for i in bpa["issues"]}
    # Sales.CustomerID is a foreign key and is not marked isHidden
    assert "BPA_UNHIDDEN_FOREIGN_KEY" in rule_ids


def test_post_write_validation_auto_rollback(tmp_path: Path) -> None:
    pbip = clone_sample_workspace(tmp_path)
    svc = PbipService(pbip)

    # Setting an empty DAX expression triggers EMPTY_MEASURE_EXPRESSION validation error
    res = svc.update_dax("Total Revenue", "   ", dry_run=False)
    assert res["rolled_back"] is True
    assert any("EMPTY_MEASURE_EXPRESSION" in w for w in res["warnings"])

    # Verify original expression was restored from backup
    restored = svc.search_measures(query="Total Revenue")[0]
    assert restored["expression"] == "SUMX('Sales', 'Sales'[Quantity] * 'Sales'[UnitPrice])"
