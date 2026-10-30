"""PBIP-201 / PBIP-202 / PBIP-203: Engine A Semantic Model Tests."""

import os
import shutil
import tempfile
from mcp_server.engine_a import SemanticModelEngine


def test_engine_a_read_operations():
    project_path = os.path.abspath("sample_project.pbip")
    engine = SemanticModelEngine(project_path)

    # 1. Summary
    summary = engine.get_model_summary()
    assert summary.project_name == "sample_project"
    assert summary.tables_count == 2
    assert summary.relationships_count == 1

    table_names = [t.name for t in summary.tables]
    assert "Sales" in table_names
    assert "Customer" in table_names

    # 2. Table details
    sales_meta = engine.get_table("Sales")
    assert sales_meta.name == "Sales"
    assert len(sales_meta.measures) == 2
    assert len(sales_meta.columns) == 5

    # 3. Calculated column check
    line_total = next(c for c in sales_meta.columns if c.name == "Line Total")
    assert line_total.is_calculated is True
    assert line_total.expression == "[Quantity] * [UnitPrice]"

    # 4. Measure details
    rev_measure = engine.get_measure("Sales", "Total Revenue")
    assert rev_measure is not None
    assert rev_measure.name == "Total Revenue"
    assert "SUMX('Sales'" in rev_measure.expression
    assert rev_measure.description == "Total revenue calculated from sales transactions"


def test_engine_a_write_operations(tmp_path):
    # Copy sample project to isolated tmp dir
    shutil.copy("sample_project.pbip", tmp_path / "sample_project.pbip")
    shutil.copytree("sample_project.SemanticModel", tmp_path / "sample_project.SemanticModel")
    shutil.copytree("sample_project.Report", tmp_path / "sample_project.Report")

    engine = SemanticModelEngine(str(tmp_path / "sample_project.pbip"))

    # Update DAX
    new_dax = "SUMX('Sales', 'Sales'[Quantity] * 'Sales'[UnitPrice] * 1.1)"
    success = engine.update_measure_dax("Sales", "Total Revenue", new_dax)
    assert success is True

    # Re-read
    updated_measure = engine.get_measure("Sales", "Total Revenue")
    assert updated_measure.expression == new_dax

    # Rename Measure
    success_rename = engine.rename_measure("Sales", "Total Revenue", "Gross Revenue")
    assert success_rename is True

    # Verify old is gone, new is present
    assert engine.get_measure("Sales", "Total Revenue") is None
    renamed_measure = engine.get_measure("Sales", "Gross Revenue")
    assert renamed_measure is not None
    assert renamed_measure.name == "Gross Revenue"
    assert renamed_measure.expression == new_dax
