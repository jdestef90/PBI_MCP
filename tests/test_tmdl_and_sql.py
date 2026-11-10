from __future__ import annotations

from pathlib import Path

import pytest

from pbi_mcp_server.engine_a.parser import parse_file
from pbi_mcp_server.engine_a.writer import (
    replace_measure_declaration,
    update_measure_description,
    update_measure_expression,
)
from pbi_mcp_server.tmdl_sql import (
    get_table_sql,
    list_table_sql_sources,
    search_table_sql,
    update_table_sql,
)

SAMPLE_SQL_TMDL = """table 'Basic Work TB'
    lineageTag: abc

    column SSN
        dataType: string

    partition 'Basic Work TB' = m
        mode: directQuery
        source = ```
                let
                    Source = Value.NativeQuery(Databricks.Catalogs(""&DatabricksSource&"", ""&Warehouse&"", [Catalog=""&Catalog&""]){[Name=""&Catalog&"",Kind="Database"]}[Data],
                ""--Report Name = Basic Work
                --User Key = "&AGFilter&"|"&Userkey&"
                SELECT
                    p.SSN
                    , CONCAT('***-**-', RIGHT(p.SSN,4)) AS SSN
                FROM "&Catalog&"."&Schema&".person p
                ""
                            , null, [EnableFolding=true])
                in
                    Source
                ```
"""


def make_sql_model(tmp_path: Path) -> tuple[Path, Path]:
    model = tmp_path / "Basic Work.SemanticModel"
    tables = model / "definition" / "tables"
    tables.mkdir(parents=True)
    file = tables / "Basic Work TB.tmdl"
    file.write_text(SAMPLE_SQL_TMDL, encoding="utf-8")
    return model, file


def test_parse_sample_project_tmdl() -> None:
    root = Path(__file__).parents[1]
    path = root / "sample_project.SemanticModel" / "definition" / "tables" / "Sales.tmdl"
    objects = parse_file(path, root)
    measures = [item for item in objects if item.kind == "measure"]
    assert len(measures) == 2
    rev = next(m for m in measures if m.name == "Total Revenue")
    assert rev.table == "Sales"
    assert rev.expression == "SUMX('Sales', 'Sales'[Quantity] * 'Sales'[UnitPrice])"
    assert rev.description == "Total revenue calculated from sales transactions"
    assert rev.format_string is not None


def test_rename_and_update_expression_preserves_text() -> None:
    source = "table Sales\n\tmeasure 'Total Revenue' = SUM(Sales[Amount])\n"
    changed, count = replace_measure_declaration(source, "Total Revenue", "Gross Revenue")
    assert count == 1
    assert "measure 'Gross Revenue'" in changed
    assert "SUM(Sales[Amount])" in changed

    changed2, count2 = update_measure_expression(source, "Total Revenue", "SUMX(Sales, Sales[Amount])")
    assert count2 == 1
    assert changed2.endswith("= SUMX(Sales, Sales[Amount])\n")


def test_update_measure_description() -> None:
    source = "table Sales\n\t/// Old comment\n\tmeasure 'Total Revenue' = SUM(Sales[Amount])\n"
    changed, count = update_measure_description(source, "Total Revenue", "New business docstring")
    assert count == 1
    assert "\t/// New business docstring\n\tmeasure 'Total Revenue'" in changed
    assert "Old comment" not in changed


def test_lists_and_extracts_full_dynamic_native_query_sql(tmp_path: Path) -> None:
    model, _ = make_sql_model(tmp_path)
    assert list_table_sql_sources(model)[0]["table"] == "Basic Work TB"
    res = get_table_sql(model, table="Basic Work TB")
    value = res["sql_m_expression"]
    assert "CONCAT('***-**-', RIGHT(p.SSN,4)) AS SSN" in value
    assert "1: " in res["sql_with_line_numbers"]
    assert "null, [EnableFolding=true]" not in value
    assert "column SSN" not in value


def test_search_is_limited_to_full_sql_source(tmp_path: Path) -> None:
    model, _ = make_sql_model(tmp_path)
    assert search_table_sql(model, "p.SSN")["total_matches"] == 2
    assert search_table_sql(model, "lineageTag")["total_matches"] == 0
    assert search_table_sql(model, "column SSN")["total_matches"] == 0


def test_exact_dry_run_and_write_sql(tmp_path: Path) -> None:
    model, file = make_sql_model(tmp_path)
    old = "CONCAT('***-**-', RIGHT(p.SSN,4)) AS SSN"
    new = "p.SSN AS SSN"
    assert update_table_sql(model, old, new, table="Basic Work TB", expected_replacements=1)["replacement_count"] == 1
    assert old in file.read_text()
    assert (
        update_table_sql(model, old, new, table="Basic Work TB", expected_replacements=1, dry_run=False)[
            "replacement_count"
        ]
        == 1
    )
    text = file.read_text()
    assert old not in text
    assert new in text
    assert "null, [EnableFolding=true]" in text
    assert file.with_suffix(".tmdl.bak").exists()


def test_expected_count_blocks_write(tmp_path: Path) -> None:
    model, file = make_sql_model(tmp_path)
    old = "CONCAT('***-**-', RIGHT(p.SSN,4)) AS SSN"
    with pytest.raises(ValueError, match="Expected 2"):
        update_table_sql(model, old, "p.SSN AS SSN", expected_replacements=2, dry_run=False)
    assert old in file.read_text()
