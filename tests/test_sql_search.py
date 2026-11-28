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


def _make_escaped_unfenced_table_tmdl(table_name: str, partition_name: str) -> str:
    return (
        f"table {table_name}\n"
        f"\tlineageTag: 11111111-2222-3333-4444-555555555555\n\n"
        f"\tcolumn discount_amt\n"
        f"\t\tdataType: double\n"
        f"\t\tsourceColumn: discount_amt\n\n"
        f"\tpartition {partition_name} = m\n"
        f"\t\tmode: import\n"
        f"\t\tsource =\n"
        f"\t\t\tlet\n"
        f'\t\t\t    Source = Value.NativeQuery(Sql.Database("srv", "db"), '
        f'"WITH raw_returns AS (#(lf)    SELECT#(lf)        r.return_id,#(lf)'
        f'        r.DISCOUNT_AMT AS ""Discount Amt""#(lf)'
        f'    FROM " & Catalog & ".dbo.returns r#(lf))#(lf)'
        f'SELECT return_id, ""Discount Amt"" FROM raw_returns")\n'
        f"\t\t\tin\n"
        f"\t\t\t    Source\n"
    )


def test_workspace_wide_search_groups_by_table_and_partition_on_clean_sql_view(
    tmp_path: Path,
) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    orders_sql = (
        "WITH raw_orders AS (\n"
        "    SELECT\n"
        "        o.order_id,\n"
        "        o.gross_amount,\n"
        "        o.discount_rate\n"
        '    FROM "&Catalog&"."&Schema&".orders o\n'
        "),\n"
        "net_orders AS (\n"
        "    SELECT\n"
        "        r.order_id,\n"
        "        r.gross_amount * (1 - r.discount_rate) AS net_amount\n"
        "    FROM raw_orders r\n"
        ")\n"
        "SELECT\n"
        "    order_id,\n"
        "    net_amount\n"
        "FROM net_orders\n"
    )
    _write_table_tmdl(
        pbip,
        "FactOrders.tmdl",
        _make_databricks_table_tmdl(
            "FactOrders",
            orders_sql,
            columns=["order_id", "net_amount"],
            partition_name="OrdersMain",
        ),
    )
    _write_table_tmdl(
        pbip,
        "FactReturns.tmdl",
        _make_escaped_unfenced_table_tmdl("FactReturns", "ReturnsPart"),
    )

    direct_srv = create_server(PbipService(pbip))
    meta_srv = create_server(PbipService(pbip), meta_tools=True)

    # 1. Workspace-wide case-insensitive search (table_name omitted -> no AmbiguousObjectError)
    res_text = _call_tool_sync(
        direct_srv, "get_table_sql", {"search": "DISCOUNT"}
    ).content[0].text

    # Unescaped plain text with real newlines (no JSON escape noise)
    assert "\n" in res_text
    assert '\\n"' not in res_text
    assert '\\"' not in res_text

    # Groups hits by table and partition
    assert "FactOrders" in res_text
    assert "OrdersMain" in res_text
    assert "FactReturns" in res_text
    assert "ReturnsPart" in res_text

    # Enclosing SQL Section names, canonical 1-based line numbers, and `>` match markers
    assert "raw_orders" in res_text
    assert "net_orders" in res_text
    assert "raw_returns" in res_text
    assert "main" in res_text
    assert ">  5 |         o.discount_rate" in res_text
    assert "> 11 |         r.gross_amount * (1 - r.discount_rate) AS net_amount" in res_text
    # Decoded Clean SQL View (`""` -> `"`, `#(lf)` -> real lines)
    assert '> 4 |         r.DISCOUNT_AMT AS "Discount Amt"' in res_text
    assert "#(lf)" not in res_text

    # 2. Searching for Dynamic M Parameter placeholder via pbi_call matches decoded Clean SQL View
    param_text = _call_tool_sync(
        meta_srv,
        "pbi_call",
        {"tool_name": "get_table_sql", "arguments": {"search": "{{m:Catalog}}"}},
    ).content[0].text
    assert "FactOrders" in param_text
    assert "FactReturns" in param_text
    assert "{{m:Catalog}}" in param_text

    # 3. TMDL metadata outside Partition SQL (e.g. lineageTag) is NOT matched
    meta_only = _call_tool_sync(
        direct_srv, "get_table_sql", {"search": "lineageTag"}
    ).content[0].text
    assert "0" in meta_only
    assert "No matches" in meta_only


def test_table_and_partition_scoped_search_merges_overlapping_context_and_marks_matches(
    tmp_path: Path,
) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    # Build a >120-line CTE query so we also prove `search` bypasses the 120-line outline default
    lines = [
        "-- Report Header: Sales Pipeline",
        "WITH base_sales AS (",
        "    SELECT",
        "        s.sale_id,",
        "        s.promo_code,",       # line 5 (match 1)
        "        s.region_id,",
        "        s.promo_discount,",   # line 7 (match 2 - overlaps line 5 at context_lines=2)
        "        s.gross_amt",
    ]
    for i in range(1, 105):
        lines.append(f"        , s.extra_{i}")
    lines.extend([
        '    FROM "&Catalog&"."&Schema&".sales s',
        "),",
        "adjusted_sales AS (",
        "    SELECT",
        "        b.sale_id,",
        "        b.gross_amt - b.promo_discount AS net_amt",  # match 3 in adjusted_sales
        "    FROM base_sales b",
        "    WHERE b.promo_code IS NOT NULL",                 # match 4 in adjusted_sales
        ")",
        "SELECT",
        "    a.sale_id,",
        "    a.net_amt",
        "FROM adjusted_sales a",
        "WHERE a.net_amt > 0",
    ])
    assert len(lines) > 120
    big_sql = "\n".join(lines)
    match3_line = lines.index("        b.gross_amt - b.promo_discount AS net_amt") + 1
    match4_line = lines.index("    WHERE b.promo_code IS NOT NULL") + 1

    _write_table_tmdl(
        pbip,
        "BigSales.tmdl",
        _make_databricks_table_tmdl("BigSales", big_sql, columns=["sale_id", "net_amt"]),
    )
    # Add a multi-partition table containing "promo" in both partitions
    multi_tmdl = (
        "table MultiPromo\n"
        "\tcolumn id\n"
        "\t\tdataType: string\n"
        "\t\tsourceColumn: id\n\n"
        "\tpartition PartRecent = m\n"
        "\t\tmode: directQuery\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        '\t\t\t    Source = Value.NativeQuery(Sql.Database("srv", "db"), '
        '"SELECT id, promo_recent FROM dbo.recent")\n'
        "\t\t\tin\n"
        "\t\t\t    Source\n\n"
        "\tpartition PartArchive = m\n"
        "\t\tmode: directQuery\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        '\t\t\t    Source = Value.NativeQuery(Sql.Database("srv", "db"), '
        '"SELECT id, promo_archive FROM dbo.archive")\n'
        "\t\t\tin\n"
        "\t\t\t    Source\n"
    )
    _write_table_tmdl(pbip, "MultiPromo.tmdl", multi_tmdl)

    srv = create_server(PbipService(pbip))

    # 1. Table-scoped search on >120-line table merges overlapping context windows cleanly
    text_out = _call_tool_sync(
        srv,
        "get_table_sql",
        {"table_name": "BigSales", "search": "promo", "context_lines": 2},
    ).content[0].text
    assert "MultiPromo" not in text_out
    assert "-- Section: base_sales (lines 5, 7)" in text_out
    assert f"-- Section: adjusted_sales (lines {match3_line}, {match4_line})" in text_out
    # Overlapping window [3..7] + [5..9] -> single hunk [3..9]; line 6 appears exactly once
    assert text_out.count("|         s.region_id,") == 1
    assert ">   5 |         s.promo_code," in text_out
    assert ">   7 |         s.promo_discount," in text_out
    assert "    6 |         s.region_id," in text_out
    assert f"> {match3_line} |         b.gross_amt - b.promo_discount AS net_amt" in text_out
    assert f"> {match4_line} |     WHERE b.promo_code IS NOT NULL" in text_out
    # Unrelated middle lines are omitted
    assert "s.extra_50" not in text_out

    # 2. context_lines=0 returns only matching lines without surrounding context
    zero_ctx = _call_tool_sync(
        srv,
        "get_table_sql",
        {"table_name": "BigSales", "search": "promo", "context_lines": 0},
    ).content[0].text
    assert "s.region_id" not in zero_ctx
    assert "s.sale_id" not in zero_ctx
    assert ">   5 |         s.promo_code," in zero_ctx
    assert ">   7 |         s.promo_discount," in zero_ctx

    # 3. Table-scoped search on multi-partition table without partition searches both partitions
    multi_all = _call_tool_sync(
        srv,
        "get_table_sql",
        {"table_name": "MultiPromo", "search": "promo"},
    ).content[0].text
    assert "PartRecent" in multi_all
    assert "PartArchive" in multi_all
    assert "promo_recent" in multi_all
    assert "promo_archive" in multi_all

    # 4. Partition-scoped search filters to only the requested partition
    part_only = _call_tool_sync(
        srv,
        "get_table_sql",
        {"table_name": "MultiPromo", "partition": "PartRecent", "search": "promo"},
    ).content[0].text
    assert "PartRecent" in part_only
    assert "promo_recent" in part_only
    assert "PartArchive" not in part_only
    assert "promo_archive" not in part_only

    # 5. Multi-partition table + ctes without partition raises AmbiguousObjectError
    with pytest.raises(Exception) as amb_exc:
        _call_tool_sync(
            srv,
            "get_table_sql",
            {"table_name": "MultiPromo", "ctes": "main", "search": "promo"},
        )
    assert "multiple SQL sources" in str(amb_exc.value.__cause__ or amb_exc.value)


def test_cte_scoped_search_restricts_matches_and_context_and_errors_on_unknown_cte(
    tmp_path: Path,
) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    cte_sql = (
        "-- Preamble comment mentioning status\n"
        "WITH cte_alpha AS (\n"
        "    SELECT\n"
        "        a.id,\n"
        "        a.status\n"
        "    FROM dbo.alpha a\n"
        "),\n"
        "cte_beta AS (\n"
        "    SELECT\n"
        "        b.id,\n"
        "        b.status\n"
        "    FROM cte_alpha b\n"
        "    WHERE b.status = 'OPEN'\n"
        "),\n"
        "cte_gamma AS (\n"
        "    SELECT\n"
        "        g.id,\n"
        "        g.status\n"
        "    FROM cte_beta g\n"
        ")\n"
        "SELECT\n"
        "    id,\n"
        "    status\n"
        "FROM cte_gamma\n"
    )
    _write_table_tmdl(
        pbip,
        "StatusFact.tmdl",
        _make_databricks_table_tmdl("StatusFact", cte_sql, columns=["id", "status"]),
    )
    # Single-line multi-section query to test character-span disambiguation
    single_line_tmdl = (
        "table SingleLineCte\n"
        "\tcolumn yr\n"
        "\t\tdataType: int64\n"
        "\t\tsourceColumn: yr\n\n"
        "\tpartition SingleLineCte = m\n"
        "\t\tmode: directQuery\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        '\t\t\t    Source = Value.NativeQuery(Sql.Database("srv", "db"), '
        '"WITH recent_cte AS (SELECT o.yr FROM dbo.orders_only o) '
        'SELECT yr FROM recent_cte")\n'
        "\t\t\tin\n"
        "\t\t\t    Source\n"
    )
    _write_table_tmdl(pbip, "SingleLineCte.tmdl", single_line_tmdl)

    direct_srv = create_server(PbipService(pbip))
    meta_srv = create_server(PbipService(pbip), meta_tools=True)

    # 1. Single CTE string (case-insensitive) restricts both matches AND context to cte_beta
    beta_text = _call_tool_sync(
        direct_srv,
        "get_table_sql",
        {
            "table_name": "StatusFact",
            "ctes": "CTE_BETA",
            "search": "status",
            "context_lines": 5,
        },
    ).content[0].text
    assert "-- Section: cte_beta (lines 11, 13)" in beta_text
    assert "> 11 |         b.status" in beta_text
    assert "> 13 |     WHERE b.status = 'OPEN'" in beta_text
    # Context must NOT leak from cte_alpha or cte_gamma or preamble
    assert "dbo.alpha" not in beta_text
    assert "cte_gamma" not in beta_text
    assert "Preamble comment" not in beta_text

    # 2. List of SQL Sections including "MAIN" via pbi_call
    multi_cte_text = _call_tool_sync(
        meta_srv,
        "pbi_call",
        {
            "tool_name": "get_table_sql",
            "arguments": {
                "table_name": "StatusFact",
                "ctes": ["cte_alpha", "MAIN"],
                "search": "status",
            },
        },
    ).content[0].text
    assert "-- Section: cte_alpha (line 5)" in multi_cte_text
    assert "-- Section: main (line 23)" in multi_cte_text
    assert ">  5 |         a.status" in multi_cte_text
    assert "> 23 |     status" in multi_cte_text
    assert "b.status" not in multi_cte_text
    assert "g.status" not in multi_cte_text
    assert "Preamble comment" not in multi_cte_text

    # 3. Single-line CTE query scopes strictly to section span
    main_miss = json.loads(
        _call_tool_sync(
            direct_srv,
            "get_table_sql",
            {
                "table_name": "SingleLineCte",
                "ctes": "main",
                "search": "orders_only",
                "format": "json",
            },
        ).content[0].text
    )
    assert main_miss["total_matches"] == 0
    assert main_miss["results"] == []

    cte_hit = json.loads(
        _call_tool_sync(
            direct_srv,
            "get_table_sql",
            {
                "table_name": "SingleLineCte",
                "ctes": "recent_cte",
                "search": "orders_only",
                "format": "json",
            },
        ).content[0].text
    )
    assert cte_hit["total_matches"] == 1
    assert cte_hit["results"][0]["matches"][0]["section"] == "recent_cte"

    # 4. Unknown CTE raises ObjectNotFoundError listing valid SQL Section names
    with pytest.raises(Exception) as exc_info:
        _call_tool_sync(
            direct_srv,
            "get_table_sql",
            {
                "table_name": "StatusFact",
                "ctes": ["missing_cte"],
                "search": "status",
            },
        )
    err_msg = str(exc_info.value.__cause__ or exc_info.value)
    assert "missing_cte" in err_msg
    for valid_sec in ("cte_alpha", "cte_beta", "cte_gamma", "main"):
        assert valid_sec in err_msg


def test_search_json_parity_zero_matches_validation_and_pbi_help(tmp_path: Path) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    # 1. Sample workspace initially has 0 native SQL tables -> clean 0-match result
    empty_srv = create_server(PbipService(pbip))
    empty_text = _call_tool_sync(
        empty_srv, "get_table_sql", {"search": "anything"}
    ).content[0].text
    assert "Matches: 0 lines across 0 sources (searched 0 sources)" in empty_text
    assert 'No matches found for "anything".' in empty_text

    empty_json = json.loads(
        _call_tool_sync(
            empty_srv, "get_table_sql", {"search": "anything", "format": "json"}
        ).content[0].text
    )
    assert empty_json["view"] == "search"
    assert empty_json["scope"] == "workspace"
    assert empty_json["sources_searched"] == 0
    assert empty_json["matched_sources"] == 0
    assert empty_json["total_matches"] == 0
    assert empty_json["results"] == []

    # 2. Add two SQL tables for rich JSON parity & validation tests
    sql_a = (
        "-- Header comment with custom_token\n"
        "WITH first_cte AS (\n"
        "    SELECT\n"
        "        a.id,\n"
        "        a.custom_token\n"
        "    FROM dbo.tbl_a a\n"
        "),\n"
        "second_cte AS (\n"
        "    SELECT\n"
        "        b.id,\n"
        "        b.custom_token\n"
        "    FROM first_cte b\n"
        ")\n"
        "SELECT id, custom_token FROM second_cte\n"
    )
    sql_b = "SELECT id, custom_token FROM dbo.tbl_b\n"
    _write_table_tmdl(
        pbip, "TableA.tmdl", _make_databricks_table_tmdl("TableA", sql_a, columns=["id"])
    )
    _write_table_tmdl(
        pbip, "TableB.tmdl", _make_databricks_table_tmdl("TableB", sql_b, columns=["id"])
    )

    direct_srv = create_server(PbipService(pbip))
    meta_srv = create_server(PbipService(pbip), meta_tools=True)

    # 3. Workspace-wide format="json" parity via pbi_call
    ws_json = json.loads(
        _call_tool_sync(
            meta_srv,
            "pbi_call",
            {
                "tool_name": "get_table_sql",
                "arguments": {
                    "search": "CUSTOM_TOKEN",
                    "context_lines": 1,
                    "format": "json",
                },
            },
        ).content[0].text
    )
    assert ws_json["view"] == "search"
    assert ws_json["scope"] == "workspace"
    assert ws_json["case_sensitive"] is False
    assert ws_json["context_lines"] == 1
    assert ws_json["sources_searched"] == 2
    assert ws_json["matched_sources"] == 2
    assert ws_json["total_matches"] == 5  # 4 lines in TableA + 1 line in TableB
    by_table = {r["table"]: r for r in ws_json["results"]}
    assert set(by_table.keys()) == {"TableA", "TableB"}

    table_a = by_table["TableA"]
    assert table_a["match_count"] == 4
    assert [(m["line"], m["section"]) for m in table_a["matches"]] == [
        (1, None),
        (5, "first_cte"),
        (11, "second_cte"),
        (14, "main"),
    ]
    assert table_a["sql_hash"]
    # Verify hunks structure and line-level is_match flags
    assert len(table_a["hunks"]) >= 1
    for hunk in table_a["hunks"]:
        assert hunk["start_line"] <= hunk["end_line"]
        line_nums = [ln["line"] for ln in hunk["lines"]]
        assert line_nums == list(range(hunk["start_line"], hunk["end_line"] + 1))
        assert [ln["line"] for ln in hunk["lines"] if ln["is_match"]] == hunk["match_lines"]

    # 4. CTE-scoped zero match returns cleanly without error
    cte_zero_json = json.loads(
        _call_tool_sync(
            direct_srv,
            "get_table_sql",
            {
                "table_name": "TableA",
                "ctes": ["first_cte"],
                "search": "second_cte",
                "format": "json",
            },
        ).content[0].text
    )
    assert cte_zero_json["view"] == "search"
    assert cte_zero_json["scope"] == "ctes"
    assert cte_zero_json["ctes"] == ["first_cte"]
    assert cte_zero_json["total_matches"] == 0
    assert cte_zero_json["results"] == []

    # 4b. Overlapping context across adjacent sections merges cleanly without duplicating lines
    merged_cross_section = _call_tool_sync(
        direct_srv,
        "get_table_sql",
        {"table_name": "TableA", "search": "custom_token", "context_lines": 2},
    ).content[0].text
    assert "-- Sections: second_cte (line 11), main (line 14)" in merged_cross_section
    assert merged_cross_section.count("|     FROM first_cte b") == 1

    # 5. Invalid arguments raise clear errors
    for bad_args, expected_fragment in (
        ({"search": ""}, "non-empty"),
        ({"search": "   "}, "non-empty"),
        ({"search": "custom_token", "context_lines": -1}, "context_lines"),
        ({"table_name": "TableA", "search": "custom_token", "outline": True}, "Cannot combine"),
        (
            {"table_name": "TableA", "search": "custom_token", "start_line": 1},
            "Cannot combine",
        ),
        ({"table_name": "NoSuchTable", "search": "custom_token"}, "No Partition SQL"),
        ({"ctes": ["first_cte"], "search": "custom_token"}, "table_name"),
    ):
        with pytest.raises(Exception) as bad_exc:
            _call_tool_sync(direct_srv, "get_table_sql", bad_args)
        assert expected_fragment in str(bad_exc.value.__cause__ or bad_exc.value)

    # 6. pbi_help exposes search and context_lines with defaults
    help_res = json.loads(
        _call_tool_sync(
            meta_srv, "pbi_help", {"tool_name": "get_table_sql"}
        ).content[0].text
    )
    params = {p["name"]: p for p in help_res["parameters"]}
    assert "search" in params
    assert params["search"]["required"] is False
    assert "context_lines" in params
    assert params["context_lines"]["required"] is False
    assert params["context_lines"]["default"] == 2

