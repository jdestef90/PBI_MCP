from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from pbi_mcp_server import PbipService, create_server

REPO_ROOT = Path(__file__).parents[1]


def _clone_sample_workspace(tmp_path: Path) -> Path:
    dest = tmp_path / "proj"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "sample_project.pbip", dest / "sample_project.pbip")
    shutil.copytree(REPO_ROOT / "sample_project.Report", dest / "sample_project.Report")
    shutil.copytree(
        REPO_ROOT / "sample_project.SemanticModel",
        dest / "sample_project.SemanticModel",
    )
    return dest / "sample_project.pbip"


def _write_table_tmdl(pbip_path: Path, filename: str, content: str) -> Path:
    tables_dir = pbip_path.parent / "sample_project.SemanticModel" / "definition" / "tables"
    target = tables_dir / filename
    target.write_text(content, encoding="utf-8")
    return target


def _make_databricks_table_tmdl(
    table_name: str,
    sql_body: str,
    *,
    columns: list[str] | None = None,
    partition_name: str | None = None,
) -> str:
    cols = columns or ["id"]
    part = partition_name or table_name
    col_blocks = "\n\n".join(
        f"\tcolumn {col}\n\t\tdataType: string\n\t\tsourceColumn: {col}" for col in cols
    )
    indented_sql = "\n".join(f"\t\t\t{line}" if line else "" for line in sql_body.splitlines())
    return (
        f"table {table_name}\n"
        f"\tlineageTag: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n\n"
        f"{col_blocks}\n\n"
        f"\tpartition {part} = m\n"
        f"\t\tmode: directQuery\n"
        f"\t\tsource = ```\n"
        f"\t\t\tlet\n"
        f'\t\t\t    Source = Value.NativeQuery(Databricks.Catalogs(""&Host&"", ""&Wh&"")'
        f'{{[Name=""&Catalog&"",Kind="Database"]}}[Data],\n'
        f'\t\t\t""{indented_sql.lstrip()}\n'
        f'\t\t\t""\n'
        f"\t\t\t    , null, [EnableFolding=true])\n"
        f"\t\t\tin\n"
        f"\t\t\t    Source\n"
        f"\t\t\t```\n"
    )


def _call_tool_sync(srv: Any, name: str, args: dict[str, Any] | None = None) -> Any:
    return asyncio.run(srv.call_tool(name, args or {}))


def test_analyze_impact_cte_lineage_reads_main_fields_and_excludes_internal_ctes(
    tmp_path: Path,
) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    cte_sql = (
        "-- Leading comment with WITH fake_cte AS (SELECT ghost FROM ghost_tbl)\n"
        "/* Multi-line block comment\n"
        "   SELECT fake_col FROM comment_tbl\n"
        "*/\n"
        "WITH `raw_orders` AS (\n"
        "    SELECT\n"
        "        o.cte_only_col,\n"
        "        o.order_id,\n"
        "        o.customer_id,\n"
        "        o.gross_amount,\n"
        "        'WITH string_cte AS (SELECT 1 FROM string_tbl)' AS note_txt,\n"
        "        ROW_NUMBER() OVER (PARTITION BY o.customer_id ORDER BY o.order_id) AS rn\n"
        '    FROM "&Catalog&"."&Schema&".orders_tbl o\n'
        "),\n"
        "enriched_orders AS (\n"
        "    SELECT\n"
        "        r.order_id,\n"
        "        r.gross_amount - COALESCE(c.discount, 0) AS net_amount,\n"
        '        (SELECT MAX(cal.dt) FROM "&Catalog&"."&Schema&".calendar cal) AS max_dt\n'
        "    FROM RAW_ORDERS r\n"
        '    LEFT JOIN "&Catalog&"."&Schema&".customers c\n'
        "        ON r.customer_id = c.customer_id\n"
        ")\n"
        "SELECT\n"
        "    e.order_id,\n"
        "    e.net_amount\n"
        "FROM enriched_orders e\n"
    )
    _write_table_tmdl(
        pbip,
        "FactSalesCte.tmdl",
        _make_databricks_table_tmdl(
            "FactSalesCte",
            cte_sql,
            columns=["order_id", "net_amount"],
        ),
    )

    srv = create_server(PbipService(pbip))

    # 1. Dependency Graph upstream lineage via analyze_impact
    impact_raw = _call_tool_sync(srv, "analyze_impact", {"term": "FactSalesCte.net_amount"})
    impact = json.loads(impact_raw.content[0].text)
    upstream_sql = impact["upstream"]["sql_and_partitions"]

    sql_tables = {n["name"] for n in upstream_sql if n["kind"] == "sql_table"}
    assert "{{m:Catalog}}.{{m:Schema}}.orders_tbl" in sql_tables
    assert "{{m:Catalog}}.{{m:Schema}}.customers" in sql_tables
    assert "{{m:Catalog}}.{{m:Schema}}.calendar" in sql_tables
    # Internal CTE names and comment/string decoy tables must NOT appear as external source_tables
    forbidden_tables = (
        "raw_orders",
        "RAW_ORDERS",
        "enriched_orders",
        "ghost_tbl",
        "comment_tbl",
        "string_tbl",
    )
    for forbidden in forbidden_tables:
        assert forbidden not in sql_tables

    partitions = [n for n in upstream_sql if n["kind"] == "partition"]
    assert len(partitions) == 1
    assert partitions[0]["sql_fields"] == ["order_id", "net_amount"]
    assert "cte_only_col" not in partitions[0]["sql_fields"]

    # 2. get_table summary includes SQL Section names / count without SQL body
    tbl_raw = _call_tool_sync(srv, "get_table", {"table_name": "FactSalesCte"})
    tbl = json.loads(tbl_raw.content[0].text)
    sql_src = tbl["sql_source"]
    assert sql_src["sql_sections"] == ["raw_orders", "enriched_orders", "main"]
    assert sql_src["sql_section_count"] == 3
    assert "cte_only_col" not in json.dumps(tbl)


def _make_120_line_cte_sql() -> str:
    # Build an exact 120-line query with 2 CTEs + main
    lines = ["WITH base_cte AS (", "    SELECT"]
    for i in range(1, 51):
        comma = "," if i < 50 else ""
        lines.append(f"        b.col_{i}{comma}")
    lines.append('    FROM "&Catalog&"."&Schema&".base_table b')
    lines.append("),")
    lines.append("agg_cte AS (")
    lines.append("    SELECT")
    for i in range(1, 51):
        comma = "," if i < 50 else ""
        lines.append(f"        a.col_{i}{comma}")
    lines.append("    FROM base_cte a")
    lines.append(")")
    lines.append("SELECT")
    for i in range(1, 11):
        comma = "," if i < 10 else ""
        lines.append(f"    x.col_{i}{comma}")
    lines.append("FROM agg_cte x")
    assert len(lines) == 120
    return "\n".join(lines)


def _make_large_recursive_cte_sql() -> str:
    # > 120 lines (138 lines) with leading multi-line block comment + WITH RECURSIVE + 3 CTEs + main
    lines = [
        "/*",
        " * Leading comment block",
        " * Line 3",
        " */",
        "WITH RECURSIVE hierarchy_cte AS (",
        "    SELECT",
        "        h.node_id,",
        "        h.parent_id",
        '    FROM "&Catalog&"."&Schema&".org_nodes h',
        "    UNION ALL",
        "    SELECT",
        "        c.node_id,",
        "        c.parent_id",
        '    FROM "&Catalog&"."&Schema&".org_nodes c',
        "    JOIN hierarchy_cte p ON c.parent_id = p.node_id",
        "),",
        "metrics_cte AS (",
        "    SELECT",
    ]
    for i in range(1, 61):
        comma = "," if i < 60 else ""
        lines.append(f"        m.metric_{i}{comma}")
    lines.append('    FROM "&Catalog&"."&Schema&".fact_metrics m')
    lines.append("    JOIN hierarchy_cte h ON m.node_id = h.node_id")
    lines.append("),")
    lines.append("filtered_cte AS (")
    lines.append("    SELECT")
    for i in range(1, 46):
        comma = "," if i < 45 else ""
        lines.append(f"        f.metric_{i}{comma}")
    lines.append("    FROM metrics_cte f")
    lines.append("    WHERE f.metric_1 > 0")
    lines.append(")")
    lines.append("SELECT")
    for i in range(1, 11):
        comma = "," if i < 10 else ""
        lines.append(f"    res.metric_{i}{comma}")
    lines.append("FROM filtered_cte res")
    assert len(lines) > 120
    return "\n".join(lines)


def test_get_table_sql_small_vs_large_default_and_explicit_outline(tmp_path: Path) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    _write_table_tmdl(
        pbip,
        "Exact120.tmdl",
        _make_databricks_table_tmdl("Exact120", _make_120_line_cte_sql(), columns=["col_1"]),
    )
    _write_table_tmdl(
        pbip,
        "LargeCte.tmdl",
        _make_databricks_table_tmdl(
            "LargeCte", _make_large_recursive_cte_sql(), columns=["metric_1"]
        ),
    )

    direct_srv = create_server(PbipService(pbip))
    meta_srv = create_server(PbipService(pbip), meta_tools=True)

    # 1. Exact 120-line query returns full numbered SQL by default
    small_text = _call_tool_sync(
        direct_srv, "get_table_sql", {"table_name": "Exact120"}
    ).content[0].text
    assert "  1 | WITH base_cte AS (" in small_text
    assert "120 | FROM agg_cte x" in small_text

    # 2. Explicit outline=True on small query returns outline-only without SQL body
    small_outline = _call_tool_sync(
        meta_srv,
        "pbi_call",
        {"tool_name": "get_table_sql", "arguments": {"table_name": "Exact120", "outline": True}},
    ).content[0].text
    assert "base_cte" in small_outline
    assert "agg_cte" in small_outline
    assert "main" in small_outline
    assert "b.col_25" not in small_outline
    assert "ctes=" in small_outline

    # 3. >120-line query defaults to outline-only with canonical line ranges & inter-CTE refs
    large_outline = _call_tool_sync(
        direct_srv, "get_table_sql", {"table_name": "LargeCte"}
    ).content[0].text
    assert "m.metric_30" not in large_outline
    assert "hierarchy_cte" in large_outline
    assert "5-16" in large_outline
    assert "metrics_cte" in large_outline
    assert "filtered_cte" in large_outline
    assert "main" in large_outline
    assert "refs:" in large_outline.lower() or "references" in large_outline.lower()
    assert "hierarchy_cte" in large_outline
    assert "start_line" in large_outline
    assert "ctes=" in large_outline

    # 4. JSON outline exposes outer_span/inner_span, line ranges, refs, and source_tables
    large_json = json.loads(
        _call_tool_sync(
            direct_srv, "get_table_sql", {"table_name": "LargeCte", "format": "json"}
        ).content[0].text
    )
    assert large_json["view"] == "outline"
    assert large_json["sql"] is None
    sec_by_name = {s["name"]: s for s in large_json["sections"]}
    assert list(sec_by_name.keys()) == ["hierarchy_cte", "metrics_cte", "filtered_cte", "main"]
    hier = sec_by_name["hierarchy_cte"]
    assert hier["start_line"] == 5
    assert hier["end_line"] == 16
    assert hier["outer_span"]["start"] < hier["inner_span"]["start"]
    assert hier["inner_span"]["end"] < hier["outer_span"]["end"]
    assert sec_by_name["metrics_cte"]["references_ctes"] == ["hierarchy_cte"]
    assert sec_by_name["filtered_cte"]["references_ctes"] == ["metrics_cte"]
    assert sec_by_name["main"]["references_ctes"] == ["filtered_cte"]

    # 5. Regression guard: update_table_sql still works on >120-line table without KeyError
    dry_plan = json.loads(
        _call_tool_sync(
            direct_srv,
            "update_table_sql",
            {
                "table_name": "LargeCte",
                "old_text": "WHERE f.metric_1 > 0",
                "new_text": "WHERE f.metric_1 >= 10",
                "format": "json",
                "dry_run": True,
            },
        ).content[0].text
    )
    assert dry_plan["replacement_count"] == 1


def test_get_table_sql_non_cte_large_query_reports_clause_landmarks(tmp_path: Path) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    lines = ["SELECT"]
    for i in range(1, 95):
        lines.append(f"    t.col_{i},")
    lines.append("    ROW_NUMBER() OVER (PARTITION BY t.col_1 ORDER BY t.col_2 DESC) AS rn,")
    lines.append("    (SELECT MAX(x.dt) FROM dbo.sub_tbl x WHERE x.flag = 1) AS max_dt")
    lines.append("FROM dbo.main_fact t")
    lines.append("LEFT JOIN dbo.dim_region r")
    lines.append("    ON t.region_id = r.region_id")
    lines.append("WHERE t.active = 1")
    lines.append("    AND r.country = 'US'")
    lines.append("GROUP BY")
    lines.append("    t.col_1,")
    lines.append("    t.col_2")
    lines.append("HAVING SUM(t.col_3) > 100")
    lines.append("QUALIFY rn = 1")
    lines.append("ORDER BY t.col_1")
    lines.append("UNION ALL")
    lines.append("SELECT")
    for i in range(1, 20):
        comma = "," if i < 19 else ""
        lines.append(f"    u.col_{i}{comma}")
    lines.append("FROM dbo.archive_fact u")
    assert len(lines) > 120

    _write_table_tmdl(
        pbip,
        "LargePlainSelect.tmdl",
        _make_databricks_table_tmdl("LargePlainSelect", "\n".join(lines), columns=["col_1"]),
    )

    srv = create_server(PbipService(pbip))

    text_out = _call_tool_sync(
        srv, "get_table_sql", {"table_name": "LargePlainSelect"}
    ).content[0].text
    assert "t.col_50" not in text_out
    expected_clauses = (
        "SELECT",
        "FROM/JOIN",
        "WHERE",
        "GROUP BY",
        "HAVING",
        "QUALIFY",
        "ORDER BY",
        "UNION",
    )
    for kw in expected_clauses:
        assert kw in text_out

    json_out = json.loads(
        _call_tool_sync(
            srv, "get_table_sql", {"table_name": "LargePlainSelect", "format": "json"}
        ).content[0].text
    )
    assert json_out["view"] == "outline"
    clauses = json_out["clauses"]
    clause_names = [c["clause"] for c in clauses]
    assert clause_names == [
        "SELECT",
        "FROM/JOIN",
        "WHERE",
        "GROUP BY",
        "HAVING",
        "QUALIFY",
        "ORDER BY",
        "UNION",
        "SELECT",
        "FROM",
    ]
    for c in clauses:
        assert c["start_line"] >= 1
        assert c["end_line"] >= c["start_line"]
    # Verify exact canonical line for QUALIFY (after 1 + 94 + 2 + 3 + 2 + 3 + 1 = 106 -> line 107)
    qualify = next(c for c in clauses if c["clause"] == "QUALIFY")
    assert qualify["start_line"] == lines.index("QUALIFY rn = 1") + 1
    assert qualify["end_line"] == qualify["start_line"]


def test_get_table_sql_ctes_selector_case_insensitive_main_and_unknown_error(
    tmp_path: Path,
) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    large_sql = _make_large_recursive_cte_sql()
    large_lines = large_sql.splitlines()
    _write_table_tmdl(
        pbip,
        "LargeCte.tmdl",
        _make_databricks_table_tmdl("LargeCte", large_sql, columns=["metric_1"]),
    )

    direct_srv = create_server(PbipService(pbip))
    meta_srv = create_server(PbipService(pbip), meta_tools=True)

    # 1. Single CTE name string (case-insensitive)
    single_text = _call_tool_sync(
        direct_srv,
        "get_table_sql",
        {"table_name": "LargeCte", "ctes": "HIERARCHY_CTE"},
    ).content[0].text
    assert " 5 | WITH RECURSIVE hierarchy_cte AS (" in single_text
    assert "16 | )," in single_text
    assert "m.metric_30" not in single_text
    assert "|  5 |" not in single_text  # Never double-numbered

    # 2. Multi-CTE list including "MAIN" via pbi_call
    multi_text = _call_tool_sync(
        meta_srv,
        "pbi_call",
        {
            "tool_name": "get_table_sql",
            "arguments": {"table_name": "LargeCte", "ctes": ["filtered_cte", "MAIN"]},
        },
    ).content[0].text
    filtered_start = large_lines.index("filtered_cte AS (") + 1
    main_start = len(large_lines) - 11  # SELECT line of main
    assert f"{filtered_start} | filtered_cte AS (" in multi_text
    assert f"{main_start} | SELECT" in multi_text
    assert f"{len(large_lines)} | FROM filtered_cte res" in multi_text
    assert "m.metric_30" not in multi_text
    assert "h.parent_id" not in multi_text

    # 3. format="json" parity for ctes selector
    multi_json = json.loads(
        _call_tool_sync(
            direct_srv,
            "get_table_sql",
            {"table_name": "LargeCte", "ctes": ["hierarchy_cte", "main"], "format": "json"},
        ).content[0].text
    )
    assert multi_json["view"] == "ctes"
    assert multi_json["truncated"] is False
    assert [s["name"] for s in multi_json["selected_sections"]] == ["hierarchy_cte", "main"]
    assert multi_json["selected_sections"][0]["start_line"] == 5
    assert multi_json["selected_sections"][0]["end_line"] == 16
    assert "WITH RECURSIVE hierarchy_cte AS (" in multi_json["sql"]
    assert "5 | " not in multi_json["sql"]  # Unnumbered Clean SQL View in JSON

    # 4. Unknown CTE raises ObjectNotFoundError listing valid SQL Section names
    with pytest.raises(Exception) as exc_info:
        _call_tool_sync(
            direct_srv,
            "get_table_sql",
            {"table_name": "LargeCte", "ctes": ["no_such_cte"]},
        )
    err_msg = str(exc_info.value.__cause__ or exc_info.value)
    assert "no_such_cte" in err_msg
    for valid in ("hierarchy_cte", "metrics_cte", "filtered_cte", "main"):
        assert valid in err_msg


def test_get_table_sql_ctes_enforces_400_line_ceiling_with_continuation_hint(
    tmp_path: Path,
) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    lines = ["WITH huge_cte AS (", "    SELECT"]
    for i in range(1, 458):
        comma = "," if i < 457 else ""
        lines.append(f"        h.col_{i}{comma}")
    lines.append("    FROM dbo.wide_table h")
    lines.append(")")
    lines.append("SELECT")
    lines.append("    col_1")
    lines.append("FROM huge_cte")
    assert len(lines) == 464

    _write_table_tmdl(
        pbip,
        "HugeCte.tmdl",
        _make_databricks_table_tmdl("HugeCte", "\n".join(lines), columns=["col_1"]),
    )

    srv = create_server(PbipService(pbip))

    text_out = _call_tool_sync(
        srv,
        "get_table_sql",
        {"table_name": "HugeCte", "ctes": ["huge_cte", "main"]},
    ).content[0].text
    assert "  1 | WITH huge_cte AS (" in text_out
    assert "400 |         h.col_398," in text_out
    assert "401 | " not in text_out
    assert "start_line=401" in text_out
    assert "end_line=" in text_out

    json_out = json.loads(
        _call_tool_sync(
            srv,
            "get_table_sql",
            {"table_name": "HugeCte", "ctes": ["huge_cte", "main"], "format": "json"},
        ).content[0].text
    )
    assert json_out["view"] == "ctes"
    assert json_out["truncated"] is True
    assert json_out["returned_line_count"] == 400
    assert json_out["total_requested_lines"] == 464
    assert len(json_out["sql"].splitlines()) == 400
    hints = json_out["next_hints"]
    assert any("start_line=401" in hint and "end_line=461" in hint for hint in hints)
    assert any("ctes=['main']" in hint for hint in hints)


def test_get_table_sql_line_range_slicing_defaults_and_json_parity(tmp_path: Path) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    lines = ["SELECT"]
    for i in range(1, 199):
        comma = "," if i < 198 else ""
        lines.append(f"    t.col_{i}{comma}")
    lines.append("FROM dbo.big_table t")
    assert len(lines) == 200

    _write_table_tmdl(
        pbip,
        "RangeTable.tmdl",
        _make_databricks_table_tmdl("RangeTable", "\n".join(lines), columns=["col_1"]),
    )

    direct_srv = create_server(PbipService(pbip))
    meta_srv = create_server(PbipService(pbip), meta_tools=True)

    # 1. Explicit inclusive start_line..end_line
    slice_text = _call_tool_sync(
        direct_srv,
        "get_table_sql",
        {"table_name": "RangeTable", "start_line": 10, "end_line": 25},
    ).content[0].text
    assert "10 |     t.col_9," in slice_text
    assert "25 |     t.col_24," in slice_text
    assert " 9 | " not in slice_text
    assert "26 | " not in slice_text
    assert "start_line=26" in slice_text

    # 2. start_line only defaults to 120-line inclusive window (50..169)
    win_text = _call_tool_sync(
        direct_srv,
        "get_table_sql",
        {"table_name": "RangeTable", "start_line": 50},
    ).content[0].text
    assert " 50 |     t.col_49," in win_text
    assert "169 |     t.col_168," in win_text
    assert "170 | " not in win_text
    assert "start_line=170" in win_text

    # 3. end_line only starts at line 1 (via pbi_call)
    head_text = _call_tool_sync(
        meta_srv,
        "pbi_call",
        {
            "tool_name": "get_table_sql",
            "arguments": {"table_name": "RangeTable", "end_line": 15},
        },
    ).content[0].text
    assert " 1 | SELECT" in head_text
    assert "15 |     t.col_14," in head_text
    assert "16 | " not in head_text

    # 4. format="json" parity for line-range view
    slice_json = json.loads(
        _call_tool_sync(
            direct_srv,
            "get_table_sql",
            {"table_name": "RangeTable", "start_line": 10, "end_line": 25, "format": "json"},
        ).content[0].text
    )
    assert slice_json["view"] == "lines"
    assert slice_json["start_line"] == 10
    assert slice_json["end_line"] == 25
    assert len(slice_json["sql"].splitlines()) == 16
    assert slice_json["sql"].splitlines()[0] == "    t.col_9,"
    assert slice_json["sql"].splitlines()[-1] == "    t.col_24,"
    assert any("start_line=26" in h for h in slice_json["next_hints"])

    # 5. Invalid ranges raise InvalidArgumentError
    for bad_args, expected_fragment in (
        ({"table_name": "RangeTable", "start_line": 0}, "start_line"),
        ({"table_name": "RangeTable", "start_line": 20, "end_line": 10}, "end_line"),
        ({"table_name": "RangeTable", "start_line": 250}, "exceeds"),
        ({"table_name": "RangeTable", "ctes": "main", "start_line": 1}, "Cannot combine"),
    ):
        with pytest.raises(Exception) as bad_exc:
            _call_tool_sync(direct_srv, "get_table_sql", bad_args)
        assert expected_fragment in str(bad_exc.value.__cause__ or bad_exc.value)


def test_get_table_multi_partition_summary_and_edge_case_sql_syntax(tmp_path: Path) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    multi_tmdl = (
        "table MultiCteTable\n"
        "\tcolumn yr\n"
        "\t\tdataType: int64\n"
        "\t\tsourceColumn: yr\n"
        "\n"
        "\tpartition PartRecent = m\n"
        "\t\tmode: directQuery\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        '\t\t\t    Source = Value.NativeQuery(Sql.Database("srv", "db"), '
        '"WITH recent_cte AS (SELECT EXTRACT(YEAR FROM o.dt) AS yr FROM dbo.orders o) '
        'SELECT yr FROM recent_cte")\n'
        "\t\t\tin\n"
        "\t\t\t    Source\n"
        "\n"
        "\tpartition PartArchive = m\n"
        "\t\tmode: directQuery\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        '\t\t\t    Source = Value.NativeQuery(Sql.Database("srv", "db"), '
        '"SELECT yr FROM " & Catalog & ".dbo.orders_archive")\n'
        "\t\t\tin\n"
        "\t\t\t    Source\n"
    )
    _write_table_tmdl(pbip, "MultiCteTable.tmdl", multi_tmdl)

    direct_srv = create_server(PbipService(pbip))
    meta_srv = create_server(PbipService(pbip), meta_tools=True)

    # 1. Multi-partition get_table summary has sections, source_tables, dynamic_parameters, no SQL
    tbl = json.loads(
        _call_tool_sync(direct_srv, "get_table", {"table_name": "MultiCteTable"}).content[0].text
    )
    sql_src = tbl["sql_source"]
    assert sql_src["sql_sections"] == {
        "PartRecent": ["recent_cte", "main"],
        "PartArchive": ["main"],
    }
    assert sql_src["sql_section_counts"] == {"PartRecent": 2, "PartArchive": 1}
    assert sql_src["source_tables"] == {
        "PartRecent": ["dbo.orders"],
        "PartArchive": ["{{m:Catalog}}.dbo.orders_archive"],
    }
    assert sql_src["dynamic_parameters"] == {
        "PartRecent": [],
        "PartArchive": ["Catalog"],
    }
    serialized_tbl = json.dumps(tbl)
    assert "EXTRACT(YEAR" not in serialized_tbl
    assert "SELECT yr FROM" not in serialized_tbl

    # 2. Single-line CTE + EXTRACT(YEAR FROM o.dt) doesn't treat `o.dt` as a source table
    recent_json = json.loads(
        _call_tool_sync(
            direct_srv,
            "get_table_sql",
            {"table_name": "MultiCteTable", "partition": "PartRecent", "format": "json"},
        ).content[0].text
    )
    assert recent_json["source_tables"] == ["dbo.orders"]
    assert recent_json["selected_fields"] == ["yr"]
    assert [s["name"] for s in recent_json["sections"]] == ["recent_cte", "main"]

    # 3. Multi-partition outline hints preserve partition parameter
    outline_json = json.loads(
        _call_tool_sync(
            direct_srv,
            "get_table_sql",
            {
                "table_name": "MultiCteTable",
                "partition": "PartRecent",
                "outline": True,
                "format": "json",
            },
        ).content[0].text
    )
    assert all("partition='PartRecent'" in hint for hint in outline_json["next_hints"])

    # 4. pbi_help exposes outline, ctes, start_line, end_line
    help_res = json.loads(
        _call_tool_sync(
            meta_srv, "pbi_help", {"tool_name": "get_table_sql"}
        ).content[0].text
    )
    param_names = {p["name"] for p in help_res["parameters"]}
    assert {"outline", "ctes", "start_line", "end_line", "format"}.issubset(param_names)



