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


UNFENCED_DATABRICKS_TMDL = (
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
    'DateTime.ToText(RangeStart, "yyyy-MM-dd") & "\'#(lf)ORDER BY e.event_ts",\n'
    "\t\t\t        null,\n"
    "\t\t\t        [EnableFolding = true]\n"
    "\t\t\t    )\n"
    "\t\tin\n"
    "\t\t\t    Source\n"
)


def test_update_table_sql_plain_text_and_clean_sql_diff_on_dry_run_and_apply(
    tmp_path: Path,
) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    tmdl_path = _write_table_tmdl(pbip, "FactEvents.tmdl", UNFENCED_DATABRICKS_TMDL)

    direct_srv = create_server(PbipService(pbip))
    meta_srv = create_server(PbipService(pbip), meta_tools=True)

    # 1. dry_run=True (default) returns unescaped plain text with non-empty Clean SQL unified diff
    dry_direct = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactEvents",
            "old_text": "ORDER BY e.event_ts",
            "new_text": "ORDER BY e.event_ts DESC",
        },
    )
    dry_meta = _call_tool_sync(
        meta_srv,
        "pbi_call",
        {
            "tool_name": "update_table_sql",
            "arguments": {
                "table_name": "FactEvents",
                "old_text": "ORDER BY e.event_ts",
                "new_text": "ORDER BY e.event_ts DESC",
            },
        },
    )

    for res in (dry_direct, dry_meta):
        assert res.structured_content is None
        assert len(res.content) == 1
        text = res.content[0].text
        assert isinstance(text, str)
        assert not text.lstrip().startswith("{")
        assert "\\n" not in text
        assert '\\"' not in text
        assert "#(lf)" not in text
        assert "#(tab)" not in text

        assert "Status: dry_run" in text
        assert "FactEvents" in text
        assert "--- a/" in text
        assert "+++ b/" in text
        assert "-ORDER BY e.event_ts" in text
        assert "+ORDER BY e.event_ts DESC" in text
        # 3 lines of clean-SQL context before the changed line
        assert " \te.event_ts" in text
        assert " FROM {{m:Catalog}}.{{m:Schema}}.events e" in text
        assert "Impact:" in text
        assert "dry_run=False" in text

    # Disk must remain untouched after dry_run=True
    assert "ORDER BY e.event_ts DESC" not in tmdl_path.read_text(encoding="utf-8")

    # 2. opt-in format="json" returns structured ChangePlan with clean-SQL unified diff
    dry_json_res = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactEvents",
            "old_text": "ORDER BY e.event_ts",
            "new_text": "ORDER BY e.event_ts DESC",
            "format": "json",
        },
    )
    assert dry_json_res.structured_content is None
    dry_payload = json.loads(dry_json_res.content[0].text)
    assert dry_payload["status"] == "dry_run"
    assert dry_payload["dry_run"] is True
    assert dry_payload["replacement_count"] == 1
    assert len(dry_payload["diffs"]) == 1
    diff_str = dry_payload["diffs"][0]["unified_diff"]
    assert "-ORDER BY e.event_ts\n+ORDER BY e.event_ts DESC" in diff_str
    assert "#(lf)" not in diff_str

    # 3. dry_run=False applies edit and also emits clean-SQL unified diff
    apply_res = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactEvents",
            "old_text": "ORDER BY e.event_ts",
            "new_text": "ORDER BY e.event_ts DESC",
            "dry_run": False,
        },
    )
    apply_text = apply_res.content[0].text
    assert "Status: applied" in apply_text
    assert "-ORDER BY e.event_ts" in apply_text
    assert "+ORDER BY e.event_ts DESC" in apply_text
    assert "#(lf)" not in apply_text

    # Disk file preserves original #(lf) + #(tab) M serialization style and creates backup
    disk = tmdl_path.read_text(encoding="utf-8")
    assert "#(lf)ORDER BY e.event_ts DESC\"" in disk
    assert "#(tab)e.event_id AS \"\"Event ID\"\"" in disk
    assert tmdl_path.with_suffix(".tmdl.bak").exists()

    # 4. Invalid format raises InvalidArgumentError
    with pytest.raises(Exception) as exc_info:
        _call_tool_sync(
            direct_srv,
            "update_table_sql",
            {
                "table_name": "FactEvents",
                "old_text": "ORDER BY e.event_ts DESC",
                "new_text": "ORDER BY e.event_ts",
                "format": "yaml",
            },
        )
    assert "text" in str(exc_info.value.__cause__ or exc_info.value).lower()


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

DATABRICKS_QUERY_TMDL = (
    "table DatabricksOrders\n"
    "\tcolumn id\n"
    "\t\tdataType: int64\n"
    "\t\tsourceColumn: id\n"
    "\n"
    "\tpartition DatabricksOrders = m\n"
    "\t\tmode: directQuery\n"
    "\t\tsource =\n"
    "\t\t\tlet\n"
    "\t\t\t    Source = Databricks.Query(\n"
    '\t\t\t        "adb-123.azuredatabricks.net",\n'
    '\t\t\t        "/sql/1.0/warehouses/abc",\n'
    '\t\t\t        "SELECT#(lf)    o.id,#(lf)    o.status#(lf)FROM " & Catalog & '
    '".sales.orders o#(lf)WHERE o.active = 1"\n'
    "\t\t\t    )\n"
    "\t\tin\n"
    "\t\t\t    Source\n"
)

SQL_DATABASE_QUERY_TMDL = (
    "table SqlDbCustomers\n"
    "\tcolumn CustomerID\n"
    "\t\tdataType: int64\n"
    "\t\tsourceColumn: CustomerID\n"
    "\n"
    "\tpartition SqlDbCustomers = m\n"
    "\t\tmode: import\n"
    "\t\tsource =\n"
    "\t\t\tlet\n"
    '\t\t\t    Source = Sql.Database("localhost", "SalesDB", '
    '[CreateNavigationProperties = false, '
    'Query = "SELECT#(cr)#(lf)    CustomerID,#(cr)#(lf)    Region#(cr)#(lf)'
    'FROM dbo.Customer"])\n'
    "\t\tin\n"
    "\t\t\t    Source\n"
)


def test_update_table_sql_line_range_edits_bounds_and_m_round_trip(tmp_path: Path) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    fenced_path = _write_table_tmdl(pbip, "Basic Work TB.tmdl", FENCED_DATABRICKS_TMDL)
    dbx_path = _write_table_tmdl(pbip, "DatabricksOrders.tmdl", DATABRICKS_QUERY_TMDL)
    sqldb_path = _write_table_tmdl(pbip, "SqlDbCustomers.tmdl", SQL_DATABASE_QUERY_TMDL)

    direct_srv = create_server(PbipService(pbip))
    meta_srv = create_server(PbipService(pbip), meta_tools=True)

    # 1. Inclusive 1-based start_line/end_line dry-run preview + apply on fenced ""..."" Databricks
    dry_text = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "Basic Work TB",
            "start_line": 4,
            "end_line": 5,
            "new_text": "    p.SSN,\n    p.FullName AS Name",
        },
    ).content[0].text
    assert "Status: dry_run" in dry_text
    assert "-    p.SSN" in dry_text
    assert "-    , CONCAT('***-**-', RIGHT(p.SSN,4)) AS SSN" in dry_text
    assert "+    p.SSN," in dry_text
    assert "+    p.FullName AS Name" in dry_text

    _call_tool_sync(
        meta_srv,
        "pbi_call",
        {
            "tool_name": "update_table_sql",
            "arguments": {
                "table_name": "Basic Work TB",
                "start_line": 4,
                "end_line": 5,
                "new_text": "    p.SSN,\n    p.FullName AS Name",
                "dry_run": False,
            },
        },
    )
    disk_fenced = fenced_path.read_text(encoding="utf-8")
    assert '""--Report Name = Basic Work' in disk_fenced
    assert (
        "\t\t\t    p.SSN,\n"
        "\t\t\t    p.FullName AS Name\n"
        '\t\t\tFROM "&Catalog&"."&Schema&".person p'
    ) in disk_fenced
    assert ", null, [EnableFolding=true])" in disk_fenced

    # 2. Single-line start_line=end_line edit on Databricks.Query unfenced #(lf)
    _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "DatabricksOrders",
            "start_line": 5,
            "end_line": 5,
            "new_text": "WHERE o.active = 1\n  AND o.status = 'OPEN'",
            "dry_run": False,
        },
    )
    disk_dbx = dbx_path.read_text(encoding="utf-8")
    assert "```" not in disk_dbx
    assert (
        '"SELECT#(lf)    o.id,#(lf)    o.status#(lf)FROM " & Catalog & '
        '".sales.orders o#(lf)WHERE o.active = 1#(lf)  AND o.status = \'OPEN\'"'
    ) in disk_dbx

    # 3. Line-range edit on Sql.Database([Query=...]) preserving #(cr)#(lf)
    _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "SqlDbCustomers",
            "start_line": 4,
            "end_line": 4,
            "new_text": "FROM dbo.Customer\nWHERE Region IS NOT NULL",
            "dry_run": False,
        },
    )
    disk_sqldb = sqldb_path.read_text(encoding="utf-8")
    assert (
        'Query = "SELECT#(cr)#(lf)    CustomerID,#(cr)#(lf)    Region#(cr)#(lf)'
        'FROM dbo.Customer#(cr)#(lf)WHERE Region IS NOT NULL"'
    ) in disk_sqldb

    # 4. Invalid / inverted / out-of-range / conflicting arguments raise InvalidArgumentError
    for bad_args in (
        {"table_name": "Basic Work TB", "start_line": 0, "end_line": 2, "new_text": "SELECT 1"},
        {"table_name": "Basic Work TB", "start_line": 5, "end_line": 3, "new_text": "SELECT 1"},
        {"table_name": "Basic Work TB", "start_line": 1, "end_line": 99, "new_text": "SELECT 1"},
        {"table_name": "Basic Work TB", "start_line": 99, "end_line": 100, "new_text": "SELECT 1"},
        {"table_name": "Basic Work TB", "end_line": 2, "new_text": "SELECT 1"},
        {
            "table_name": "Basic Work TB",
            "old_text": "SELECT",
            "start_line": 1,
            "end_line": 2,
            "new_text": "SELECT 1",
        },
        {"table_name": "Basic Work TB", "start_line": 1, "end_line": 2},
    ):
        with pytest.raises(Exception) as exc_info:
            _call_tool_sync(direct_srv, "update_table_sql", bad_args)
        cause = exc_info.value.__cause__ or exc_info.value
        assert getattr(cause, "code", None) == "INVALID_ARGUMENT"


DUPLICATE_LINE_TMDL = (
    "table UnionOrders\n"
    "\tcolumn id\n"
    "\t\tdataType: int64\n"
    "\t\tsourceColumn: id\n"
    "\n"
    "\tpartition UnionOrders = m\n"
    "\t\tmode: import\n"
    "\t\tsource =\n"
    "\t\t\tlet\n"
    '\t\t\t    Source = Value.NativeQuery(Sql.Database("srv", "db"), '
    '"SELECT#(lf)    o.id#(lf)FROM dbo.orders_a o#(lf)WHERE o.active = 1#(lf)'
    'UNION ALL#(lf)SELECT#(lf)    o.id#(lf)FROM dbo.orders_b o#(lf)WHERE o.active = 1")\n'
    "\t\tin\n"
    "\t\t\t    Source\n"
)


def test_update_table_sql_forgiving_whitespace_matching_and_prefix_stripping(
    tmp_path: Path,
) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    events_path = _write_table_tmdl(pbip, "FactEvents.tmdl", UNFENCED_DATABRICKS_TMDL)
    union_path = _write_table_tmdl(pbip, "UnionOrders.tmdl", DUPLICATE_LINE_TMDL)
    fenced_path = _write_table_tmdl(pbip, "Basic Work TB.tmdl", FENCED_DATABRICKS_TMDL)

    direct_srv = create_server(PbipService(pbip))

    # 1. Forgiving leading-whitespace + CRLF normalized match (spaces matching tabs)
    ws_text = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactEvents",
            "old_text": '    e.event_id AS "Event ID",\r\n    e.event_ts',
            "new_text": '    e.event_id AS "Event ID",\r\n    e.event_ts,\r\n    e.region',
            "dry_run": False,
        },
    ).content[0].text
    assert "Status: applied" in ws_text
    assert "+\te.region" in ws_text
    # Original tab indentation (#(tab)) must be preserved on disk
    disk_events = events_path.read_text(encoding="utf-8")
    assert '#(tab)e.event_ts,#(lf)#(tab)e.region#(lf)FROM ' in disk_events

    # 2. Ambiguous whitespace-normalized match rejected when expected_replacements is omitted
    with pytest.raises(Exception) as exc_ambig:
        _call_tool_sync(
            direct_srv,
            "update_table_sql",
            {
                "table_name": "UnionOrders",
                "old_text": "\to.id",  # tab indent -> normalized match hits 2 lines
                "new_text": "\to.order_id",
                "dry_run": True,
            },
        )
    ambig_cause = exc_ambig.value.__cause__ or exc_ambig.value
    assert getattr(ambig_cause, "code", None) == "PRECONDITION_FAILED"

    # Succeeds when expected_replacements=2 matches the normalized count
    multi_ws = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "UnionOrders",
            "old_text": "\to.id",
            "new_text": "\to.order_id",
            "expected_replacements": 2,
            "dry_run": False,
        },
    ).content[0].text
    assert "Status: applied" in multi_ws
    assert "Replacements: 2" in multi_ws
    assert union_path.read_text(encoding="utf-8").count("    o.order_id") == 2

    # 3. Automatic line-number prefix stripping (`42 | ` and `42: `) on old_text and new_text
    pipe_res = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "Basic Work TB",
            "old_text": (
                " 4 |     p.SSN\n"
                " 5 |     , CONCAT('***-**-', RIGHT(p.SSN,4)) AS SSN"
            ),
            "new_text": (
                " 4 |     p.SSN,\n"
                " 5 |     p.DisplayName AS Name"
            ),
            "dry_run": False,
        },
    ).content[0].text
    assert "Status: applied" in pipe_res
    assert " 4 | " not in pipe_res
    disk_fenced = fenced_path.read_text(encoding="utf-8")
    assert "4 | " not in disk_fenced
    assert "\t\t\t    p.SSN,\n\t\t\t    p.DisplayName AS Name" in disk_fenced

    # Colon-style line prefixes stripped on start_line/end_line new_text too
    colon_res = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "Basic Work TB",
            "start_line": 5,
            "end_line": 5,
            "new_text": "5:     p.FullName AS Name",
            "dry_run": False,
        },
    ).content[0].text
    assert "Status: applied" in colon_res
    disk_fenced_after = fenced_path.read_text(encoding="utf-8")
    assert "5:" not in disk_fenced_after
    assert "\t\t\t    p.FullName AS Name" in disk_fenced_after


def test_update_table_sql_expected_sql_hash_precondition_guard(tmp_path: Path) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    tmdl_path = _write_table_tmdl(pbip, "FactEvents.tmdl", UNFENCED_DATABRICKS_TMDL)

    direct_srv = create_server(PbipService(pbip))
    meta_srv = create_server(PbipService(pbip), meta_tools=True)

    current_info = json.loads(
        _call_tool_sync(
            direct_srv,
            "get_table_sql",
            {"table_name": "FactEvents", "format": "json"},
        ).content[0].text
    )
    current_hash = current_info["sql_hash"]

    # 1. Stale hash raises PreconditionFailedError containing current sql_hash
    with pytest.raises(Exception) as exc_stale:
        _call_tool_sync(
            direct_srv,
            "update_table_sql",
            {
                "table_name": "FactEvents",
                "start_line": 6,
                "end_line": 6,
                "new_text": "ORDER BY e.event_ts DESC",
                "expected_sql_hash": "deadbeef0000",
                "dry_run": False,
            },
        )
    cause = exc_stale.value.__cause__ or exc_stale.value
    assert getattr(cause, "code", None) == "PRECONDITION_FAILED"
    assert current_hash in str(cause)
    assert "ORDER BY e.event_ts DESC" not in tmdl_path.read_text(encoding="utf-8")

    # 2. Matching hash succeeds via pbi_call and changes sql_hash
    ok_res = _call_tool_sync(
        meta_srv,
        "pbi_call",
        {
            "tool_name": "update_table_sql",
            "arguments": {
                "table_name": "FactEvents",
                "start_line": 6,
                "end_line": 6,
                "new_text": "ORDER BY e.event_ts DESC",
                "expected_sql_hash": current_hash,
                "dry_run": False,
            },
        },
    ).content[0].text
    assert "Status: applied" in ok_res
    assert f"sql_hash: {current_hash} ->" in ok_res
    assert "ORDER BY e.event_ts DESC" in tmdl_path.read_text(encoding="utf-8")

    # 3. Re-using the old hash after mutation fails with the new current sql_hash
    new_hash = json.loads(
        _call_tool_sync(
            direct_srv,
            "get_table_sql",
            {"table_name": "FactEvents", "format": "json"},
        ).content[0].text
    )["sql_hash"]
    assert new_hash != current_hash
    with pytest.raises(Exception) as exc_second:
        _call_tool_sync(
            direct_srv,
            "update_table_sql",
            {
                "table_name": "FactEvents",
                "start_line": 6,
                "end_line": 6,
                "new_text": "ORDER BY e.event_ts ASC",
                "expected_sql_hash": current_hash,
                "dry_run": True,
            },
        )
    cause2 = exc_second.value.__cause__ or exc_second.value
    assert getattr(cause2, "code", None) == "PRECONDITION_FAILED"
    assert new_hash in str(cause2)

    # 4. pbi_help exposes start_line, end_line, expected_sql_hash, and format
    help_res = json.loads(
        _call_tool_sync(
            meta_srv, "pbi_help", {"tool_name": "update_table_sql"}
        ).content[0].text
    )
    params = {p["name"]: p for p in help_res["parameters"]}
    assert {"start_line", "end_line", "expected_sql_hash", "format"}.issubset(params)
    assert params["format"]["type"] == "'text'|'json'"
    assert params["format"]["default"] == "text"


def test_update_table_sql_structural_validation_comments_m_param_warning_and_rollback(
    tmp_path: Path,
) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    events_path = _write_table_tmdl(pbip, "FactEvents.tmdl", UNFENCED_DATABRICKS_TMDL)
    fenced_path = _write_table_tmdl(pbip, "Basic Work TB.tmdl", FENCED_DATABRICKS_TMDL)

    direct_srv = create_server(PbipService(pbip))

    # 1. Unbalanced single quotes, double quotes, backticks, parentheses, block comment, and
    #    malformed {{m:...}} placeholders raise InvalidArgumentError without modifying disk.
    original_events = events_path.read_text(encoding="utf-8")
    invalid_replacements = (
        (
            "ORDER BY e.event_ts",
            "WHERE e.note = 'unterminated\nORDER BY e.event_ts",
            "single quote",
        ),
        ("ORDER BY e.event_ts", 'WHERE e."unterminated = 1\nORDER BY e.event_ts', "double quote"),
        ("ORDER BY e.event_ts", "WHERE e.`unterminated = 1\nORDER BY e.event_ts", "backtick"),
        ("ORDER BY e.event_ts", "WHERE (e.event_ts IS NOT NULL\nORDER BY e.event_ts", "parenthes"),
        ("ORDER BY e.event_ts", "WHERE e.event_ts IS NOT NULL)\nORDER BY e.event_ts", "parenthes"),
        ("ORDER BY e.event_ts", "/* unclosed block comment\nORDER BY e.event_ts", "comment"),
        ("ORDER BY e.event_ts", "WHERE e.cat = '{{m:Unclosed'\nORDER BY e.event_ts", "placeholder"),
        (
            "ORDER BY e.event_ts",
            'WHERE e.dt >= \'{{m:DateTime.ToText(RangeStart, "yyyy-MM-dd"}}\'\nORDER BY e.event_ts',
            "m expression",
        ),
    )
    for old_frag, bad_new, expected_hint in invalid_replacements:
        with pytest.raises(Exception) as exc_bad:
            _call_tool_sync(
                direct_srv,
                "update_table_sql",
                {
                    "table_name": "FactEvents",
                    "old_text": old_frag,
                    "new_text": bad_new,
                    "dry_run": False,
                },
            )
        cause = exc_bad.value.__cause__ or exc_bad.value
        assert getattr(cause, "code", None) == "INVALID_ARGUMENT"
        assert expected_hint in str(cause).lower()
    assert events_path.read_text(encoding="utf-8") == original_events

    # 2. Unbalanced quotes/parens/backticks inside SQL comments (-- and /* */) are IGNORED
    comment_ok = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactEvents",
            "old_text": "ORDER BY e.event_ts",
            "new_text": (
                "-- Don't fail on apostrophe or (unclosed paren or `backtick\n"
                "/* Block comment with 'quote \"double` (paren */\n"
                "ORDER BY e.event_ts"
            ),
            "dry_run": True,
        },
    ).content[0].text
    assert "Status: dry_run" in comment_ok

    # 3. Removing or modifying an existing {{m:<expr>}} parameter emits an explicit warning
    warn_text = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactEvents",
            "old_text": "FROM {{m:Catalog}}.{{m:Schema}}.events e",
            "new_text": "FROM prod_catalog.{{m:NewSchema}}.events e",
            "dry_run": True,
        },
    ).content[0].text
    assert "Status: dry_run" in warn_text
    assert "Warnings:" in warn_text
    assert "{{m:Catalog}}" in warn_text
    assert "{{m:Schema}}" in warn_text

    warn_json = json.loads(
        _call_tool_sync(
            direct_srv,
            "update_table_sql",
            {
                "table_name": "FactEvents",
                "old_text": "FROM {{m:Catalog}}.{{m:Schema}}.events e",
                "new_text": "FROM prod_catalog.{{m:NewSchema}}.events e",
                "format": "json",
                "dry_run": True,
            },
        ).content[0].text
    )
    assert any("{{m:Catalog}}" in w for w in warn_json["warnings"])
    assert any("{{m:Schema}}" in w for w in warn_json["warnings"])

    # 4. Mutation Pipeline post-write validation failure triggers automatic rollback on disk
    original_fenced = fenced_path.read_text(encoding="utf-8")
    rollback_text = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "Basic Work TB",
            "old_text": "SELECT",
            "new_text": "/* fence break\n```\n*/\nSELECT",
            "dry_run": False,
        },
    ).content[0].text
    assert "Status: rolled_back" in rollback_text
    assert "UNBALANCED_TMDL_FENCE" in rollback_text
    assert "rolled back" in rollback_text.lower()
    # Original .tmdl content must be restored identically on disk
    assert fenced_path.read_text(encoding="utf-8") == original_fenced




