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


def _write_table_tmdl(pbip_path: Path, table_filename: str, content: str) -> Path:
    tables_dir = pbip_path.parent / "sample_project.SemanticModel" / "definition" / "tables"
    target = tables_dir / table_filename
    target.write_text(content, encoding="utf-8")
    return target


def _call_tool_sync(
    srv: Any, name: str, args: dict[str, Any] | None = None
) -> Any:
    return asyncio.run(srv.call_tool(name, args or {}))


FENCED_DATABRICKS_TMDL = (
    "table 'Basic Work TB'\n"
    "\tlineageTag: 11111111-1111-1111-1111-111111111111\n"
    "\n"
    "\tcolumn SSN\n"
    "\t\tdataType: string\n"
    "\t\tsourceColumn: SSN\n"
    "\n"
    "\tpartition 'Basic Work TB' = m\n"
    "\t\tmode: directQuery\n"
    "\t\tsource = ```\n"
    "\t\t\tlet\n"
    '\t\t\t    Source = Value.NativeQuery(Databricks.Catalogs(""&DatabricksSource&"", '
    '""&Warehouse&"", [Catalog=""&Catalog&""])'
    '{[Name=""&Catalog&"",Kind="Database"]}[Data],\n'
    '\t\t\t""--Report Name = Basic Work\n'
    '\t\t\t--User Key = "&AGFilter&"|"&Userkey&"\n'
    "\t\t\tSELECT\n"
    "\t\t\t    p.SSN\n"
    "\t\t\t    , CONCAT('***-**-', RIGHT(p.SSN,4)) AS SSN\n"
    '\t\t\tFROM "&Catalog&"."&Schema&".person p\n'
    '\t\t\t""\n'
    "\t\t\t            , null, [EnableFolding=true])\n"
    "\t\t\tin\n"
    "\t\t\t    Source\n"
    "\t\t\t```\n"
)


def test_get_table_sql_returns_plain_text_clean_sql_view_by_default(tmp_path: Path) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    _write_table_tmdl(pbip, "Basic Work TB.tmdl", FENCED_DATABRICKS_TMDL)

    direct_srv = create_server(PbipService(pbip))
    meta_srv = create_server(PbipService(pbip), meta_tools=True)

    res_direct = _call_tool_sync(direct_srv, "get_table_sql", {"table_name": "Basic Work TB"})
    res_meta = _call_tool_sync(
        meta_srv,
        "pbi_call",
        {"tool_name": "get_table_sql", "arguments": {"table_name": "Basic Work TB"}},
    )

    for res in (res_direct, res_meta):
        assert res.structured_content is None
        assert len(res.content) == 1
        text = res.content[0].text
        assert isinstance(text, str)
        assert not text.lstrip().startswith("{")
        assert "\\n" not in text
        assert '\\"' not in text

        assert "Basic Work TB" in text
        assert "directQuery" in text
        assert "Value.NativeQuery" in text
        assert "sql_hash:" in text
        assert "AGFilter" in text
        assert "Catalog" in text

        assert "1 | --Report Name = Basic Work" in text
        assert "2 | --User Key = {{m:AGFilter}}|{{m:Userkey}}" in text
        assert "3 | SELECT" in text
        assert "4 |     p.SSN" in text
        assert "6 | FROM {{m:Catalog}}.{{m:Schema}}.person p" in text
        assert text.count("CONCAT('***-**-', RIGHT(p.SSN,4)) AS SSN") == 1


def test_get_table_sql_format_json_is_lean_and_validates_format(tmp_path: Path) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    _write_table_tmdl(pbip, "Basic Work TB.tmdl", FENCED_DATABRICKS_TMDL)

    direct_srv = create_server(PbipService(pbip))
    meta_srv = create_server(PbipService(pbip), meta_tools=True)

    res_json = _call_tool_sync(
        meta_srv,
        "pbi_call",
        {
            "tool_name": "get_table_sql",
            "arguments": {"table_name": "Basic Work TB", "format": "JSON"},
        },
    )
    assert res_json.structured_content is None
    payload = json.loads(res_json.content[0].text)
    assert payload["table"] == "Basic Work TB"
    assert payload["partition"] == "Basic Work TB"
    assert payload["mode"] == "directQuery"
    assert payload["connector"] == "Value.NativeQuery"
    assert payload["line_count"] == 6
    assert isinstance(payload["sql_hash"], str) and len(payload["sql_hash"]) >= 8
    assert payload["dynamic_parameters"] == ["AGFilter", "Userkey", "Catalog", "Schema"]
    assert "{{m:Catalog}}.{{m:Schema}}.person p" in payload["sql"]

    assert "sql_m_expression" not in payload
    assert "sql_with_line_numbers" not in payload
    assert "source_m_expression" not in payload
    assert "lines" not in payload

    # Defensive format=None via pbi_call falls back to plain text
    res_none = _call_tool_sync(
        meta_srv,
        "pbi_call",
        {
            "tool_name": "get_table_sql",
            "arguments": {"table_name": "Basic Work TB", "format": None},
        },
    )
    assert "1 | --Report Name = Basic Work" in res_none.content[0].text

    with pytest.raises(Exception) as exc_info:
        _call_tool_sync(
            direct_srv,
            "get_table_sql",
            {"table_name": "Basic Work TB", "format": "xml"},
        )
    assert "text" in str(exc_info.value.__cause__ or exc_info.value).lower()


UNFENCED_DATABRICKS_LF_TMDL = (
    "table FactEvents\n"
    "\tlineageTag: 22222222-2222-2222-2222-222222222222\n"
    "\n"
    "\tcolumn 'Event ID'\n"
    "\t\tdataType: string\n"
    "\t\tsourceColumn: Event ID\n"
    "\n"
    "\tpartition FactEvents = m\n"
    "\t\tmode: directQuery\n"
    "\t\tsource =\n"
    "\t\t\tlet\n"
    "\t\t\t    Source = Value.NativeQuery(\n"
    "\t\t\t        Databricks.Catalogs(Host, HttpPath, [Catalog = Catalog])"
    '{[Name = Catalog, Kind = "Database"]}[Data],\n'
    '\t\t\t        "SELECT#(lf)#(tab)e.event_id AS ""Event ID"",#(lf)#(tab)e.event_ts'
    '#(lf)FROM " & Catalog & "." & Schema & ".events e#(lf)WHERE e.dt >= \'" & '
    'DateTime.ToText(RangeStart, "yyyy-MM-dd") & "\'",\n'
    "\t\t\t        null,\n"
    "\t\t\t        [EnableFolding = true]\n"
    "\t\t\t    )\n"
    "\t\t\tin\n"
    "\t\t\t    Source\n"
    "\n"
    "\t\tannotation PBI_NavigationStepName = Navigation\n"
)


UNFENCED_CRLF_MULTICHUNK_TMDL = (
    "table DailyMetrics\n"
    "\tlineageTag: 33333333-3333-3333-3333-333333333333\n"
    "\n"
    "\tcolumn metric_date\n"
    "\t\tdataType: dateTime\n"
    "\t\tsourceColumn: metric_date\n"
    "\n"
    "\tpartition DailyMetrics = m\n"
    "\t\tsource = let\n"
    "\t\t\t// CRLF escapes, multiline concat, and 2-arg Value.NativeQuery\n"
    "\t\t\tSource = Value.NativeQuery(\n"
    '\t\t\t    Sql.Database("sql.corp.local", "Analytics"),\n'
    '\t\t\t    "SELECT#(cr)#(lf)" &\n'
    '\t\t\t    "    m.metric_date,#(cr)#(lf)" &\n'
    '\t\t\t    "    m.total_val#(cr)#(lf)" &\n'
    '\t\t\t    "FROM dbo.daily_metrics m#(cr)#(lf)" &\n'
    '\t\t\t    "WHERE m.tag = \'#(#)(urgent)\'"\n'
    "\t\t\t)\n"
    "\t\tin\n"
    "\t\t\tSource\n"
    "\t\tmode: import\n"
)


def test_get_table_sql_decodes_unfenced_lf_tab_quotes_and_complex_m_params(
    tmp_path: Path,
) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    _write_table_tmdl(pbip, "FactEvents.tmdl", UNFENCED_DATABRICKS_LF_TMDL)
    _write_table_tmdl(pbip, "DailyMetrics.tmdl", UNFENCED_CRLF_MULTICHUNK_TMDL)

    srv = create_server(PbipService(pbip))

    res1 = _call_tool_sync(srv, "get_table_sql", {"table_name": "FactEvents"})
    text1 = res1.content[0].text
    assert "Partition: FactEvents (mode: directQuery, connector: Value.NativeQuery)" in text1
    assert "1 | SELECT" in text1
    assert '2 | \te.event_id AS "Event ID",' in text1
    assert "3 | \te.event_ts" in text1
    assert "4 | FROM {{m:Catalog}}.{{m:Schema}}.events e" in text1
    assert '5 | WHERE e.dt >= \'{{m:DateTime.ToText(RangeStart, "yyyy-MM-dd")}}\'' in text1
    assert 'DateTime.ToText(RangeStart, "yyyy-MM-dd")' in text1

    res2 = _call_tool_sync(srv, "get_table_sql", {"table_name": "DailyMetrics"})
    text2 = res2.content[0].text
    assert "Partition: DailyMetrics (mode: import, connector: Value.NativeQuery)" in text2
    assert "1 | SELECT" in text2
    assert "2 |     m.metric_date," in text2
    assert "5 | WHERE m.tag = '#(urgent)'" in text2
    assert "#(cr)" not in text2
    assert "#(lf)" not in text2


CONNECTOR_TMDLS = {
    "DatabricksPositional.tmdl": (
        "table DatabricksPositional\n"
        "\tcolumn id\n"
        "\t\tdataType: int64\n"
        "\t\tsourceColumn: id\n"
        "\n"
        "\tpartition DatabricksPositional = m\n"
        "\t\tmode: directQuery\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        "\t\t\t    Source = Databricks.Query(\n"
        '\t\t\t        "adb-123.azuredatabricks.net",\n'
        '\t\t\t        "/sql/1.0/warehouses/abc",\n'
        '\t\t\t        "SELECT#(lf)    id#(lf)FROM " & Catalog & ".sales.orders"\n'
        "\t\t\t    )\n"
        "\t\t\tin\n"
        "\t\t\t    Source\n"
    ),
    "DatabricksRecord.tmdl": (
        "table DatabricksRecord\n"
        "\tcolumn id\n"
        "\t\tdataType: int64\n"
        "\t\tsourceColumn: id\n"
        "\n"
        "\tpartition DatabricksRecord = m\n"
        "\t\tmode: directQuery\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        "\t\t\t    Source = Databricks.Query(\n"
        '\t\t\t        "adb-123.azuredatabricks.net",\n'
        '\t\t\t        "/sql/1.0/warehouses/abc",\n'
        '\t\t\t        [Catalog = "prod", Query = "SELECT id FROM prod.sales.items"]\n'
        "\t\t\t    )\n"
        "\t\t\tin\n"
        "\t\t\t    Source\n"
    ),
    "SqlDbQueryOption.tmdl": (
        "table SqlDbQueryOption\n"
        "\tcolumn CustomerID\n"
        "\t\tdataType: int64\n"
        "\t\tsourceColumn: CustomerID\n"
        "\n"
        "\tpartition SqlDbQueryOption = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        '\t\t\t    Source = Sql.Database("localhost", "SalesDB", '
        '[CreateNavigationProperties = false, '
        'Query = "SELECT#(lf)    CustomerID#(lf)FROM dbo.Customer"])\n'
        "\t\t\tin\n"
        "\t\t\t    Source\n"
    ),
    "InlineUnfenced.tmdl": (
        "table InlineUnfenced\n"
        "\tcolumn id\n"
        "\t\tdataType: int64\n"
        "\t\tsourceColumn: id\n"
        "\n"
        "\tpartition InlineUnfenced = m\n"
        "\t\tmode: import\n"
        '\t\tsource = Value.NativeQuery(Sql.Database("x", "y"), "SELECT 0")\n'
    ),
}

ODBC_TMDL = (
    "table OdbcQueryTable\n"
    "\tcolumn total\n"
    "\t\tdataType: double\n"
    "\t\tsourceColumn: total\n"
    "\n"
    "\tpartition OdbcQueryTable = m\n"
    "\t\tmode: import\n"
    "\t\tsource =\n"
    "\t\t\tlet\n"
    '\t\t\t    Source = Odbc.Query("dsn=DW", "SELECT total FROM legacy_sales")\n'
    "\t\t\tin\n"
    "\t\t\t    Source\n"
)

FENCED_STD_INDENT_TMDL = (
    "table FencedStdIndent\n"
    "\tcolumn a\n"
    "\t\tdataType: string\n"
    "\t\tsourceColumn: a\n"
    "\n"
    "\tpartition FencedStdIndent = m\n"
    "\t\tmode: directQuery\n"
    "\t\tsource = ```\n"
    "\t\t\tlet\n"
    "\t\t\t    Source = Value.NativeQuery(\n"
    '\t\t\t        Sql.Database("srv", "db"),\n'
    '\t\t\t        "\n'
    "\t\t\t        SELECT\n"
    '\t\t\t            ""a""\n'
    "\t\t\t        FROM dbo.t\n"
    '\t\t\t        ",\n'
    "\t\t\t        null,\n"
    "\t\t\t        [EnableFolding = true]\n"
    "\t\t\t    )\n"
    "\t\t\tin\n"
    "\t\t\t    Source\n"
    "\t\t\t```\n"
)


def test_connectors_databricks_query_sql_database_query_option_and_odbc_query(
    tmp_path: Path,
) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    for fname, body in CONNECTOR_TMDLS.items():
        _write_table_tmdl(pbip, fname, body)
    _write_table_tmdl(pbip, "OdbcQueryTable.tmdl", ODBC_TMDL)
    _write_table_tmdl(pbip, "FencedStdIndent.tmdl", FENCED_STD_INDENT_TMDL)

    srv = create_server(PbipService(pbip))

    list_res = _call_tool_sync(srv, "list_tables")
    tables = [json.loads(c.text) for c in list_res.content]
    native_tables = {t["name"]: t["has_native_sql"] for t in tables}
    assert native_tables["DatabricksPositional"] is True
    assert native_tables["DatabricksRecord"] is True
    assert native_tables["SqlDbQueryOption"] is True
    assert native_tables["OdbcQueryTable"] is True
    assert native_tables["InlineUnfenced"] is True
    assert native_tables["FencedStdIndent"] is True

    pos_text = _call_tool_sync(
        srv, "get_table_sql", {"table_name": "DatabricksPositional"}
    ).content[0].text
    assert "connector: Databricks.Query" in pos_text
    assert "3 | FROM {{m:Catalog}}.sales.orders" in pos_text

    rec_text = _call_tool_sync(
        srv, "get_table_sql", {"table_name": "DatabricksRecord"}
    ).content[0].text
    assert "connector: Databricks.Query" in rec_text
    assert "1 | SELECT id FROM prod.sales.items" in rec_text

    sql_text = _call_tool_sync(
        srv, "get_table_sql", {"table_name": "SqlDbQueryOption"}
    ).content[0].text
    assert "connector: Sql.Database" in sql_text
    assert "2 |     CustomerID" in sql_text
    assert "3 | FROM dbo.Customer" in sql_text

    odbc_text = _call_tool_sync(
        srv, "get_table_sql", {"table_name": "OdbcQueryTable"}
    ).content[0].text
    assert "connector: Odbc.Query" in odbc_text
    assert "1 | SELECT total FROM legacy_sales" in odbc_text

    inline_text = _call_tool_sync(
        srv, "get_table_sql", {"table_name": "InlineUnfenced"}
    ).content[0].text
    assert "1 | SELECT 0" in inline_text

    fenced_text = _call_tool_sync(
        srv, "get_table_sql", {"table_name": "FencedStdIndent"}
    ).content[0].text
    assert "1 | SELECT" in fenced_text
    assert '2 |     "a"' in fenced_text
    assert "3 | FROM dbo.t" in fenced_text


def test_lossless_codec_round_trip_via_update_table_sql(tmp_path: Path) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    fenced_path = _write_table_tmdl(pbip, "Basic Work TB.tmdl", FENCED_DATABRICKS_TMDL)
    unfenced_path = _write_table_tmdl(pbip, "FactEvents.tmdl", UNFENCED_DATABRICKS_LF_TMDL)
    sqldb_path = _write_table_tmdl(
        pbip, "SqlDbQueryOption.tmdl", CONNECTOR_TMDLS["SqlDbQueryOption.tmdl"]
    )
    std_path = _write_table_tmdl(pbip, "FencedStdIndent.tmdl", FENCED_STD_INDENT_TMDL)
    crlf_path = _write_table_tmdl(pbip, "DailyMetrics.tmdl", UNFENCED_CRLF_MULTICHUNK_TMDL)

    direct_srv = create_server(PbipService(pbip))
    meta_srv = create_server(PbipService(pbip), meta_tools=True)

    # 1. Fenced ""..."" + "&Catalog&" round-trip (via pbi_call)
    before_basic = json.loads(
        _call_tool_sync(
            direct_srv, "get_table_sql", {"table_name": "Basic Work TB", "format": "json"}
        ).content[0].text
    )
    _call_tool_sync(
        meta_srv,
        "pbi_call",
        {
            "tool_name": "update_table_sql",
            "arguments": {
                "table_name": "Basic Work TB",
                "old_text": "FROM {{m:Catalog}}.{{m:Schema}}.person p",
                "new_text": "FROM {{m:Catalog}}.{{m:Schema}}.person_v2 p\nWHERE p.active = 1",
                "dry_run": False,
            },
        },
    )
    disk_basic = fenced_path.read_text(encoding="utf-8")
    assert '""--Report Name = Basic Work' in disk_basic
    assert (
        'FROM "&Catalog&"."&Schema&".person_v2 p\n\t\t\tWHERE p.active = 1\n\t\t\t""'
        in disk_basic
    )
    assert ", null, [EnableFolding=true])" in disk_basic
    after_basic = json.loads(
        _call_tool_sync(
            direct_srv, "get_table_sql", {"table_name": "Basic Work TB", "format": "json"}
        ).content[0].text
    )
    assert after_basic["sql_hash"] != before_basic["sql_hash"]
    assert "7 | WHERE p.active = 1" in _call_tool_sync(
        direct_srv, "get_table_sql", {"table_name": "Basic Work TB"}
    ).content[0].text

    # 2. Unfenced #(lf) + #(tab) + ""quote"" + DateTime.ToText(...) round-trip
    _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactEvents",
            "old_text": "\te.event_ts",
            "new_text": '\te.event_ts,\n\te.region AS "Region"',
            "dry_run": False,
        },
    )
    disk_events = unfenced_path.read_text(encoding="utf-8")
    assert "```" not in disk_events
    assert (
        '"SELECT#(lf)#(tab)e.event_id AS ""Event ID"",#(lf)#(tab)e.event_ts,'
        '#(lf)#(tab)e.region AS ""Region""#(lf)FROM " & Catalog & "." & Schema & '
        '".events e#(lf)WHERE e.dt >= \'" & DateTime.ToText(RangeStart, "yyyy-MM-dd") & "\'"'
    ) in disk_events
    events_after = _call_tool_sync(
        direct_srv, "get_table_sql", {"table_name": "FactEvents"}
    ).content[0].text
    assert '4 | \te.region AS "Region"' in events_after

    # 3. Sql.Database([Query=...]) option record preservation
    _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "SqlDbQueryOption",
            "old_text": "FROM dbo.Customer",
            "new_text": "FROM dbo.Customer\nWHERE Region = 'West'",
            "dry_run": False,
        },
    )
    disk_sqldb = sqldb_path.read_text(encoding="utf-8")
    assert (
        '[CreateNavigationProperties = false, Query = '
        '"SELECT#(lf)    CustomerID#(lf)FROM dbo.Customer#(lf)WHERE Region = \'West\'"]'
    ) in disk_sqldb

    # 4. Fenced standard multiline indent preservation
    _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FencedStdIndent",
            "old_text": '    "a"',
            "new_text": '    "a",\n    "b"',
            "dry_run": False,
        },
    )
    disk_std = std_path.read_text(encoding="utf-8")
    assert '\t\t\t            ""a"",\n\t\t\t            ""b""' in disk_std
    assert "[EnableFolding = true]" in disk_std

    # 5. #(cr)#(lf) + #(#) preservation round-trip
    _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "DailyMetrics",
            "old_text": "WHERE m.tag = '#(urgent)'",
            "new_text": "WHERE m.tag = '#(urgent)'\nORDER BY m.metric_date",
            "dry_run": False,
        },
    )
    disk_crlf = crlf_path.read_text(encoding="utf-8")
    assert (
        '"SELECT#(cr)#(lf)    m.metric_date,#(cr)#(lf)    m.total_val#(cr)#(lf)'
        'FROM dbo.daily_metrics m#(cr)#(lf)WHERE m.tag = \'#(#)(urgent)\'#(cr)#(lf)'
        'ORDER BY m.metric_date"'
    ) in disk_crlf


MULTI_PARTITION_TMDL = (
    "table MultiSqlTable\n"
    "\tcolumn id\n"
    "\t\tdataType: int64\n"
    "\t\tsourceColumn: id\n"
    "\n"
    "\tpartition PartA = m\n"
    "\t\tmode: directQuery\n"
    "\t\tsource =\n"
    "\t\t\tlet\n"
    '\t\t\t    Source = Value.NativeQuery(Sql.Database("srv", "db"), '
    '"SELECT id FROM dbo.part_a")\n'
    "\t\t\tin\n"
    "\t\t\t    Source\n"
    "\n"
    "\tpartition PartB = m\n"
    "\t\tmode: directQuery\n"
    "\t\tsource =\n"
    "\t\t\tlet\n"
    '\t\t\t    Source = Value.NativeQuery(Sql.Database("srv", "db"), '
    '"SELECT id FROM dbo.part_b")\n'
    "\t\t\tin\n"
    "\t\t\t    Source\n"
)


def test_get_table_trims_sql_source_to_summary_metadata_only(tmp_path: Path) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    _write_table_tmdl(pbip, "FactEvents.tmdl", UNFENCED_DATABRICKS_LF_TMDL)
    _write_table_tmdl(pbip, "MultiSqlTable.tmdl", MULTI_PARTITION_TMDL)

    srv = create_server(PbipService(pbip))

    events_tbl = json.loads(
        _call_tool_sync(srv, "get_table", {"table_name": "FactEvents"}).content[0].text
    )
    sql_src = events_tbl["sql_source"]
    assert isinstance(sql_src, dict)
    assert sql_src["connector"] == "Value.NativeQuery"
    assert sql_src["mode"] == "directQuery"
    assert sql_src["line_count"] == 5
    assert isinstance(sql_src["sql_hash"], str) and len(sql_src["sql_hash"]) >= 8
    meta = sql_src["sql_metadata"]
    assert "Catalog" in meta["dynamic_parameters"]
    assert len(meta["source_tables"]) >= 1
    assert len(meta["selected_fields"]) >= 1
    for forbidden in ("sql", "sql_m_expression", "sql_with_line_numbers", "source_m_expression"):
        assert forbidden not in sql_src
    assert "WHERE e.dt >=" not in json.dumps(events_tbl)

    multi_tbl = json.loads(
        _call_tool_sync(srv, "get_table", {"table_name": "MultiSqlTable"}).content[0].text
    )
    multi_src = multi_tbl["sql_source"]
    assert isinstance(multi_src, dict)
    assert multi_src["multiple_partitions"] is True
    assert multi_src["partitions"] == ["PartA", "PartB"]
    assert "SELECT id FROM" not in json.dumps(multi_tbl)

    sales_tbl = json.loads(
        _call_tool_sync(srv, "get_table", {"table_name": "Sales"}).content[0].text
    )
    assert sales_tbl["sql_source"] is None


def test_pbi_help_and_right_aligned_line_numbers(tmp_path: Path) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    select_cols = "#(lf)".join(f"    c{i}," for i in range(1, 10))
    ten_line_tmdl = (
        "table TenLines\n"
        "\tcolumn c1\n"
        "\t\tdataType: int64\n"
        "\t\tsourceColumn: c1\n"
        "\n"
        "\tpartition TenLines = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        '\t\t\t    Source = Value.NativeQuery(Sql.Database("srv", "db"), '
        f'"SELECT#(lf){select_cols}#(lf)    c10#(lf)FROM dbo.ten")\n'
        "\t\t\tin\n"
        "\t\t\t    Source\n"
    )
    _write_table_tmdl(pbip, "TenLines.tmdl", ten_line_tmdl)

    meta_srv = create_server(PbipService(pbip), meta_tools=True)
    help_res = json.loads(
        _call_tool_sync(
            meta_srv, "pbi_help", {"tool_name": "get_table_sql"}
        ).content[0].text
    )
    params = {p["name"]: p for p in help_res["parameters"]}
    assert params["format"]["type"] == "'text'|'json'"
    assert params["format"]["default"] == "text"

    sql_text = _call_tool_sync(
        meta_srv,
        "pbi_call",
        {"tool_name": "get_table_sql", "arguments": {"table_name": "TenLines"}},
    ).content[0].text
    assert " 1 | SELECT" in sql_text
    assert "11 |     c10" in sql_text
