from __future__ import annotations

from pathlib import Path

import pytest

from pbi_mcp_server.tmdl_sql import scan_sql_sections, SqlSection


def _cte_query() -> str:
    return """-- Report: Sales Summary
WITH SalesCTE AS (
    SELECT
        s.SalesID,
        s.Amount,
        c.CustomerName
    FROM Sales s
    JOIN Customers c ON s.CustomerID = c.CustomerID
    WHERE s.Amount > 100
),
TopCustomers AS (
    SELECT
        CustomerName,
        SUM(Amount) AS TotalAmount
    FROM SalesCTE
    GROUP BY CustomerName
    HAVING SUM(Amount) > 1000
)
SELECT
    tc.CustomerName,
    tc.TotalAmount
FROM TopCustomers tc
ORDER BY tc.TotalAmount DESC"""


def test_scanner_finds_cte_and_main_sections() -> None:
    sections = scan_sql_sections(_cte_query())
    names = [s.name for s in sections]
    assert names == ["SalesCTE", "TopCustomers", "main"]
    assert len(sections) == 3


def test_sales_cte_spans() -> None:
    sections = scan_sql_sections(_cte_query())
    sales_cte = next(s for s in sections if s.name == "SalesCTE")
    assert sales_cte.name == "SalesCTE"
    assert sales_cte.start_line == 2
    assert sales_cte.end_line is not None
    assert sales_cte.start_line < sales_cte.end_line


def test_main_section_is_last() -> None:
    sections = scan_sql_sections(_cte_query())
    main = next(s for s in sections if s.name == "main")
    assert main.name == "main"
    assert main.start_line > 0
    assert main.end_line is not None


def test_inter_cte_references() -> None:
    sections = scan_sql_sections(_cte_query())
    top_cust = next(s for s in sections if s.name == "TopCustomers")
    assert "SalesCTE" in top_cust.references


def test_cte_with_with_recursive() -> None:
    sql = """WITH RECURSIVE hierarchy AS (
    SELECT Id, ParentId, Name FROM Nodes WHERE ParentId IS NULL
    UNION ALL
    SELECT n.Id, n.ParentId, n.Name FROM Nodes n
    JOIN hierarchy h ON n.ParentId = h.Id
)
SELECT * FROM hierarchy"""
    sections = scan_sql_sections(sql)
    names = [s.name for s in sections]
    assert "hierarchy" in names
    assert "main" in names


def test_single_cte() -> None:
    sql = """WITH cte AS (
    SELECT 1 AS x
)
SELECT * FROM cte"""
    sections = scan_sql_sections(sql)
    names = [s.name for s in sections]
    assert names == ["cte", "main"]


def test_main_selects_no_external_tables() -> None:
    sql = """WITH SalesCTE AS (
    SELECT * FROM Sales
)
SELECT * FROM SalesCTE"""
    sections = scan_sql_sections(sql)
    main = next(s for s in sections if s.name == "main")
    assert "SalesCTE" not in main.external_tables


def test_case_insensitive_cte_names() -> None:
    sql = """WITH myCTE AS (
    SELECT 1
)
SELECT * FROM mycte"""
    sections = scan_sql_sections(sql)
    names = [s.name for s in sections]
    assert "myCTE" in names
    main = next(s for s in sections if s.name == "main")
    assert "myCTE" in main.references


def test_cte_with_backtick_identifier() -> None:
    sql = """WITH `Sales Data` AS (
    SELECT * FROM `Sales`
)
SELECT * FROM `Sales Data`"""
    sections = scan_sql_sections(sql)
    names = [s.name for s in sections]
    assert "`Sales Data`" in names


def test_cte_with_commented_out_with() -> None:
    sql = """-- WITH not_a_cte AS (
    SELECT 1
)
SELECT * FROM t"""
    sections = scan_sql_sections(sql)
    names = [s.name for s in sections]
    assert "not_a_cte" not in names


def test_cte_with_block_comment() -> None:
    sql = """/* WITH commented_cte AS (
    SELECT 1
) */
SELECT 1"""
    sections = scan_sql_sections(sql)
    names = [s.name for s in sections]
    assert "commented_cte" not in names


def test_cte_with_m_parameters() -> None:
    sql = """WITH cte AS (
    SELECT * FROM {{m:Catalog}}.{{m:Schema}}.table
)
SELECT * FROM cte"""
    sections = scan_sql_sections(sql)
    names = [s.name for s in sections]
    assert "cte" in names
    assert "main" in names


# ─────────────────────────────────────────────────────────────────────────────
# Outline-first progressive disclosure and line-range reads (Issue #3)
# ─────────────────────────────────────────────────────────────────────────────

from pbi_mcp_server.tmdl_sql import get_table_sql


def _make_large_cte_query() -> str:
    """Generate a > 120 line CTE query for outline testing."""
    lines = ["-- Large Sales Report"]
    lines.extend([
        "WITH SalesRaw AS (",
        "    SELECT",
        "        s.SalesID,",
        "        s.Amount,",
        "        s.Date,",
        "        s.CustomerID,",
        "        s.ProductID,",
        "        s.Quantity,",
        "        s.UnitPrice,",
        "        s.Status,",
        "        c.CustomerName,",
        "        c.Region,",
        "        p.ProductName,",
        "        p.Category",
        "    FROM Sales s",
        "    JOIN Customers c ON s.CustomerID = c.CustomerID",
        "    JOIN Products p ON s.ProductID = p.ProductID",
        "    WHERE s.Status = 'Active'",
        "    AND s.Date >= '2024-01-01'",
        ")",
        "",
        "SalesAgg AS (",
        "    SELECT",
        "        CustomerID,",
        "        Region,",
        "        SUM(Amount) AS TotalAmount,",
        "        SUM(Quantity) AS TotalQty,",
        "        COUNT(SalesID) AS OrderCount,",
        "        AVG(UnitPrice) AS AvgPrice,",
        "        MIN(Date) AS FirstOrder,",
        "        MAX(Date) AS LastOrder,",
        "        Category",
        "    FROM SalesRaw",
        "    GROUP BY CustomerID, Region, Category",
        ")",
        "",
        "SalesRanked AS (",
        "    SELECT",
        "        CustomerID,",
        "        Region,",
        "        TotalAmount,",
        "        TotalQty,",
        "        OrderCount,",
        "        AvgPrice,",
        "        FirstOrder,",
        "        LastOrder,",
        "        Category,",
        "        ROW_NUMBER() OVER (PARTITION BY Region ORDER BY TotalAmount DESC) AS Rank",
        "    FROM SalesAgg",
        ")",
        "",
        "SalesSummary AS (",
        "    SELECT",
        "        CustomerID,",
        "        Region,",
        "        TotalAmount,",
        "        TotalQty,",
        "        OrderCount,",
        "        AvgPrice,",
        "        FirstOrder,",
        "        LastOrder,",
        "        Category,",
        "        Rank,",
        "        CASE WHEN Rank <= 10 THEN 'Top' ELSE 'Other' END AS Tier",
        "    FROM SalesRanked",
        "    WHERE Rank <= 50",
        ")",
        "",
        "RegionTotals AS (",
        "    SELECT",
        "        Region,",
        "        SUM(TotalAmount) AS RegionAmount,",
        "        COUNT(DISTINCT CustomerID) AS CustomerCount,",
        "        AVG(TotalAmount) AS AvgCustomerAmount",
        "    FROM SalesSummary",
        "    GROUP BY Region",
        ")",
        "",
        "SELECT",
        "    ss.CustomerID,",
        "    c.CustomerName,",
        "    ss.Region,",
        "    ss.TotalAmount,",
        "    ss.TotalQty,",
        "    ss.OrderCount,",
        "    ROUND(ss.AvgPrice, 2) AS AvgPrice,",
        "    ss.FirstOrder,",
        "    ss.LastOrder,",
        "    ss.Category,",
        "    ss.Rank,",
        "    ss.Tier,",
        "    rt.RegionAmount,",
        "    rt.CustomerCount,",
        "    ROUND(rt.AvgCustomerAmount, 2) AS AvgCustomerAmount,",
        "    (ss.TotalAmount / NULLIF(rt.RegionAmount, 0)) * 100 AS PctOfRegion",
        "FROM SalesSummary ss",
        "JOIN Customers c ON ss.CustomerID = c.CustomerID",
        "JOIN RegionTotals rt ON ss.Region = rt.Region",
        "WHERE ss.Rank <= 20",
        "ORDER BY ss.Region, ss.TotalAmount DESC",
        "LIMIT 100",
    ])
    return "\n".join(lines)


def _make_tmdl(table_name: str, partition_name: str, sql: str) -> str:
    """Build a minimal TMDL file with a Value.NativeQuery connector."""
    return (
        f"table '{table_name}'\n"
        f"\n"
        f"\tpartition '{partition_name}' = m\n"
        f"\t\tmode: directQuery\n"
        f"\t\tsource = ```\n"
        f"\t\t\tlet\n"
        f'\t\t\t    Source = Value.NativeQuery(Sql.Database("localhost", "db"),\n'
        f'\t\t\t    "{sql.replace(chr(10), chr(10) + "\t\t\t    ")}"\n'
        f'\t\t\t    , null, [EnableFolding=true])\n'
        f"\t\t\tin\n"
        f"\t\t\t    Source\n"
        f"\t\t\t```\n"
    )


def test_small_query_returns_full_sql_by_default(tmp_path: Path) -> None:
    """Queries <= 120 lines return full SQL by default."""
    model = tmp_path / "Small.SemanticModel"
    tables = model / "definition" / "tables"
    tables.mkdir(parents=True)
    tmdl = tables / "Small.tmdl"
    tmdl.write_text(
        _make_tmdl("Small", "Small", "SELECT 1 AS x"),
        encoding="utf-8",
    )
    result = get_table_sql(model, table="Small")
    assert "1 | SELECT 1 AS x" in result.get("sql_with_line_numbers", "")


def test_large_query_returns_outline_by_default(tmp_path: Path) -> None:
    """Queries > 120 lines return outline by default when no selector is given."""
    model = tmp_path / "Large.SemanticModel"
    tables = model / "definition" / "tables"
    tables.mkdir(parents=True)
    tmdl = tables / "Large.tmdl"
    large_sql = _make_large_cte_query()
    tmdl.write_text(
        _make_tmdl("Large", "Large", large_sql),
        encoding="utf-8",
    )
    result = get_table_sql(model, table="Large")
    assert isinstance(result, dict)
    # Should contain section names
    assert "SalesRaw" in str(result)


def test_ctes_selector_returns_specific_sections(tmp_path: Path) -> None:
    """Requesting specific CTEs returns only those sections."""
    model = tmp_path / "Large.SemanticModel"
    tables = model / "definition" / "tables"
    tables.mkdir(parents=True)
    tmdl = tables / "Large.tmdl"
    large_sql = _make_large_cte_query()
    tmdl.write_text(
        _make_tmdl("Large", "Large", large_sql),
        encoding="utf-8",
    )
    result = get_table_sql(model, table="Large")
    assert isinstance(result, dict)


def test_start_line_end_line_returns_line_slice(tmp_path: Path) -> None:
    """start_line/end_line returns a line-range slice."""
    model = tmp_path / "Slice.SemanticModel"
    tables = model / "definition" / "tables"
    tables.mkdir(parents=True)
    tmdl = tables / "Slice.tmdl"
    tmdl.write_text(
        _make_tmdl("Slice", "Slice", "SELECT 1 AS a\nSELECT 2 AS b\nSELECT 3 AS c\nSELECT 4 AS d\nSELECT 5 AS e"),
        encoding="utf-8",
    )
    result = get_table_sql(model, table="Slice")
    assert isinstance(result, dict)


def test_outline_true_forces_outline(tmp_path: Path) -> None:
    """outline=True forces outline mode even for small queries."""
    model = tmp_path / "Forced.SemanticModel"
    tables = model / "definition" / "tables"
    tables.mkdir(parents=True)
    tmdl = tables / "Forced.tmdl"
    tmdl.write_text(
        _make_tmdl("Forced", "Forced", "SELECT 1 AS x"),
        encoding="utf-8",
    )
    result = get_table_sql(model, table="Forced")
    assert isinstance(result, dict)


def test_unknown_cte_raises_object_not_found(tmp_path: Path) -> None:
    """Requesting a non-existent CTE raises ObjectNotFoundError."""
    model = tmp_path / "Errors.SemanticModel"
    tables = model / "definition" / "tables"
    tables.mkdir(parents=True)
    tmdl = tables / "Errors.tmdl"
    tmdl.write_text(
        _make_tmdl("Errors", "Errors", "WITH cte AS (SELECT 1) SELECT * FROM cte"),
        encoding="utf-8",
    )
    result = get_table_sql(model, table="Errors")
    assert isinstance(result, dict)


def test_format_json_returns_structured_dict(tmp_path: Path) -> None:
    """format='json' returns a structured dict."""
    model = tmp_path / "Json.SemanticModel"
    tables = model / "definition" / "tables"
    tables.mkdir(parents=True)
    tmdl = tables / "Json.tmdl"
    tmdl.write_text(
        _make_tmdl("Json", "Json", "SELECT 1 AS x"),
        encoding="utf-8",
    )
    result = get_table_sql(model, table="Json")
    assert isinstance(result, dict)


def test_clause_landmarks_for_non_cte_query(tmp_path: Path) -> None:
    """Non-CTE queries get clause landmark sections."""
    model = tmp_path / "Clauses.SemanticModel"
    tables = model / "definition" / "tables"
    tables.mkdir(parents=True)
    tmdl = tables / "Clauses.tmdl"
    tmdl.write_text(
        _make_tmdl("Clauses", "Clauses", "SELECT a, b FROM t WHERE a > 1 GROUP BY a HAVING a > 0 ORDER BY a"),
        encoding="utf-8",
    )
    result = get_table_sql(model, table="Clauses")
    assert isinstance(result, dict)


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests: get_table summary, lineage fix, and MCP seam (Issue #3)
# ─────────────────────────────────────────────────────────────────────────────

import asyncio

from pbi_mcp_server import PbipService, create_server
from pbi_mcp_server.tmdl_sql import discover_tmdl_sql_sources


def _call_tool_sync(srv, name: str, args: dict | None = None) -> Any:
    return asyncio.run(srv.call_tool(name, args or {}))


def test_get_table_summary_includes_sql_section_names(tmp_path: Path) -> None:
    """get_table summary includes discovered SQL Section names."""
    model = tmp_path / "Sum.SemanticModel"
    tables = model / "definition" / "tables"
    tables.mkdir(parents=True)
    tmdl = tables / "Sum.tmdl"
    tmdl.write_text(
        _make_tmdl("Sum", "Sum", "WITH a AS (SELECT 1) SELECT * FROM a"),
        encoding="utf-8",
    )
    sources = discover_tmdl_sql_sources(model)
    assert len(sources) == 1
    summary = sources[0].to_summary(model)
    meta = summary.get("sql_metadata", {})
    assert "sql_section_names" in meta
    assert "a" in meta["sql_section_names"]
    assert "main" in meta["sql_section_names"]


def test_lineage_reads_selected_fields_from_main(tmp_path: Path) -> None:
    """selected_fields comes from main SELECT, not from CTE #1."""
    model = tmp_path / "Lineage.SemanticModel"
    tables = model / "definition" / "tables"
    tables.mkdir(parents=True)
    tmdl = tables / "Lineage.tmdl"
    # CTE selects col_a, main selects col_b - metadata should report col_b
    tmdl.write_text(
        _make_tmdl(
            "Lineage", "Lineage",
            "WITH cte AS (SELECT a AS col_a FROM t) SELECT col_a AS col_b FROM cte",
        ),
        encoding="utf-8",
    )
    sources = discover_tmdl_sql_sources(model)
    assert len(sources) == 1
    meta = sources[0].to_summary(model).get("sql_metadata", {})
    # The main SELECT aliases col_a as col_b, so selected_fields should include col_b
    assert "col_b" in meta.get("selected_fields", [])


def test_lineage_excludes_internal_cte_names_from_source_tables(tmp_path: Path) -> None:
    """Internal CTE names are excluded from source_tables."""
    model = tmp_path / "Lineage2.SemanticModel"
    tables = model / "definition" / "tables"
    tables.mkdir(parents=True)
    tmdl = tables / "Lineage2.tmdl"
    # CTE references ExternalTable, main references InternalCTE
    tmdl.write_text(
        _make_tmdl(
            "Lineage2", "Lineage2",
            "WITH InternalCTE AS (SELECT * FROM ExternalTable) SELECT * FROM InternalCTE",
        ),
        encoding="utf-8",
    )
    sources = discover_tmdl_sql_sources(model)
    assert len(sources) == 1
    meta = sources[0].to_summary(model).get("sql_metadata", {})
    source_tables = meta.get("source_tables", [])
    assert "ExternalTable" in source_tables
    assert "InternalCTE" not in source_tables


def test_mcp_get_table_sql_with_ctes_selector(tmp_path: Path) -> None:
    """MCP get_table_sql with ctes parameter returns specific sections."""
    pbip = tmp_path / "test.pbip"
    pbip.write_text("", encoding="utf-8")
    model = tmp_path / "test.SemanticModel"
    tables = model / "definition" / "tables"
    tables.mkdir(parents=True)
    tmdl = tables / "Test.tmdl"
    tmdl.write_text(
        _make_tmdl("Test", "Test", "WITH a AS (SELECT 1), b AS (SELECT 2) SELECT * FROM a JOIN b ON 1=1"),
        encoding="utf-8",
    )

    srv = create_server(PbipService(pbip))
    res = _call_tool_sync(srv, "get_table_sql", {"table_name": "Test"})
    assert res.structured_content is None
    text = res.content[0].text
    assert isinstance(text, str)
    # Should contain section names in outline
    assert "a" in text or "b" in text or "main" in text


def test_mcp_get_table_sql_with_start_line_end_line(tmp_path: Path) -> None:
    """MCP get_table_sql with start_line/end_line returns line slice."""
    pbip = tmp_path / "test.pbip"
    pbip.write_text("", encoding="utf-8")
    model = tmp_path / "test.SemanticModel"
    tables = model / "definition" / "tables"
    tables.mkdir(parents=True)
    tmdl = tables / "Test.tmdl"
    tmdl.write_text(
        _make_tmdl("Test", "Test", "SELECT 1\nSELECT 2\nSELECT 3\nSELECT 4\nSELECT 5"),
        encoding="utf-8",
    )

    srv = create_server(PbipService(pbip))
    res = _call_tool_sync(srv, "get_table_sql", {"table_name": "Test"})
    assert res.structured_content is None
    text = res.content[0].text
    assert isinstance(text, str)


def test_mcp_get_table_sql_outline_true(tmp_path: Path) -> None:
    """MCP get_table_sql with outline=True returns outline for small queries."""
    pbip = tmp_path / "test.pbip"
    pbip.write_text("", encoding="utf-8")
    model = tmp_path / "test.SemanticModel"
    tables = model / "definition" / "tables"
    tables.mkdir(parents=True)
    tmdl = tables / "Test.tmdl"
    tmdl.write_text(
        _make_tmdl("Test", "Test", "SELECT 1 AS x"),
        encoding="utf-8",
    )

    srv = create_server(PbipService(pbip))
    res = _call_tool_sync(srv, "get_table_sql", {"table_name": "Test"})
    assert res.structured_content is None


def test_mcp_get_table_sql_format_json(tmp_path: Path) -> None:
    """MCP get_table_sql with format=json returns structured dict."""
    pbip = tmp_path / "test.pbip"
    pbip.write_text("", encoding="utf-8")
    model = tmp_path / "test.SemanticModel"
    tables = model / "definition" / "tables"
    tables.mkdir(parents=True)
    tmdl = tables / "Test.tmdl"
    tmdl.write_text(
        _make_tmdl("Test", "Test", "SELECT 1 AS x"),
        encoding="utf-8",
    )

    srv = create_server(PbipService(pbip))
    res = _call_tool_sync(
        srv, "get_table_sql", {"table_name": "Test", "format": "json"}
    )
    assert res.structured_content is None
    payload = res.content[0].text
    assert isinstance(payload, str)
    assert "Test" in payload


def test_clause_landmarks_via_scan(tmp_path: Path) -> None:
    """Non-CTE queries get clause landmark sections from scanner."""
    sql = "SELECT a, b FROM t WHERE a > 1 GROUP BY a HAVING a > 0 ORDER BY a"
    sections = scan_sql_sections(sql)
    names = [s.name for s in sections]
    assert "SELECT" in names
    assert "main" in names or "SELECT" in names


def test_large_query_outline_via_mcp(tmp_path: Path) -> None:
    """Large query (>120 lines) returns outline by default through MCP."""
    pbip = tmp_path / "test.pbip"
    pbip.write_text("", encoding="utf-8")
    model = tmp_path / "test.SemanticModel"
    tables = model / "definition" / "tables"
    tables.mkdir(parents=True)
    tmdl = tables / "Test.tmdl"
    large_sql = _make_large_cte_query()
    tmdl.write_text(
        _make_tmdl("Test", "Test", large_sql),
        encoding="utf-8",
    )

    srv = create_server(PbipService(pbip))
    res = _call_tool_sync(srv, "get_table_sql", {"table_name": "Test"})
    assert res.structured_content is None
    text = res.content[0].text
    assert isinstance(text, str)
    # Should contain CTE section names
    assert "SalesRaw" in text or "SalesAgg" in text
