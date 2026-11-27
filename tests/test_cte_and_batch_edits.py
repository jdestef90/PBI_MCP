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


MULTI_CTE_DATABRICKS_TMDL = (
    "table FactPipeline\n"
    "\tlineageTag: 33333333-3333-3333-3333-333333333333\n"
    "\n"
    "\tcolumn order_id\n"
    "\t\tdataType: int64\n"
    "\t\tsourceColumn: order_id\n"
    "\n"
    "\tpartition FactPipeline = m\n"
    "\t\tmode: directQuery\n"
    "\t\tsource =\n"
    "\t\t\tlet\n"
    "\t\t\t    Source = Value.NativeQuery(\n"
    "\t\t\t        Databricks.Catalogs(Host, HttpPath, [Catalog = Catalog])"
    '{[Name = Catalog, Kind = "Database"]}[Data],\n'
    '\t\t\t        "WITH cte_x AS (#(lf)'
    '    SELECT#(lf)'
    '        o.order_id,#(lf)'
    '        o.amount#(lf)'
    '    FROM " & Catalog & "." & Schema & ".orders o#(lf)'
    "    WHERE o.status = 'ACTIVE'#(lf)"
    '),#(lf)'
    'cte_z AS (#(lf)'
    '    SELECT#(lf)'
    '        x.order_id,#(lf)'
    '        x.amount#(lf)'
    '    FROM cte_x x#(lf)'
    "    WHERE o.status = 'ACTIVE'#(lf)"
    ')#(lf)'
    'SELECT#(lf)'
    '    z.order_id,#(lf)'
    '    z.amount#(lf)'
    'FROM cte_z z#(lf)'
    "WHERE o.status = 'ACTIVE'\",\n"
    "\t\t\t        null,\n"
    "\t\t\t        [EnableFolding = true]\n"
    "\t\t\t    )\n"
    "\t\tin\n"
    "\t\t\t    Source\n"
)


def test_cte_scoped_text_replacement_and_unknown_cte_error(tmp_path: Path) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    tmdl_path = _write_table_tmdl(pbip, "FactPipeline.tmdl", MULTI_CTE_DATABRICKS_TMDL)

    direct_srv = create_server(PbipService(pbip))
    meta_srv = create_server(PbipService(pbip), meta_tools=True)

    # 1. Scoped replacement inside cte_z (case-insensitive) leaves cte_x and main untouched
    dry_res = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactPipeline",
            "cte": "CTE_Z",
            "old_text": "WHERE o.status = 'ACTIVE'",
            "new_text": "WHERE x.amount > 0",
        },
    )
    assert dry_res.structured_content is None
    dry_text = dry_res.content[0].text
    assert "Status: dry_run" in dry_text
    assert "Replacements: 1" in dry_text
    assert "-    WHERE o.status = 'ACTIVE'" in dry_text
    assert "+    WHERE x.amount > 0" in dry_text
    assert "#(lf)" not in dry_text

    # Apply via pbi_call with forgiving whitespace + accidental line-number prefix
    apply_res = _call_tool_sync(
        meta_srv,
        "pbi_call",
        {
            "tool_name": "update_table_sql",
            "arguments": {
                "table_name": "FactPipeline",
                "cte": "CTE_Z",
                "old_text": " 13 |   WHERE o.status = 'ACTIVE'",
                "new_text": " 13 |   WHERE x.amount > 0",
                "expected_replacements": 1,
                "dry_run": False,
            },
        },
    )
    apply_text = apply_res.content[0].text
    assert "Status: applied" in apply_text
    assert "Replacements: 1" in apply_text
    assert "13 |" not in apply_text

    # Verify raw .tmdl disk preserves #(lf) style and only modified cte_z
    disk = tmdl_path.read_text(encoding="utf-8")
    assert disk.count("WHERE o.status = 'ACTIVE'") == 2  # cte_x and main still intact
    assert "    WHERE x.amount > 0#(lf))#(lf)SELECT" in disk

    # 2. Scoped replacement inside 'main' leaves cte_x untouched
    main_res = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactPipeline",
            "cte": "main",
            "old_text": "WHERE o.status = 'ACTIVE'",
            "new_text": "WHERE z.amount >= 10",
            "dry_run": False,
        },
    )
    assert "Status: applied" in main_res.content[0].text
    disk_after_main = tmdl_path.read_text(encoding="utf-8")
    assert disk_after_main.count("WHERE o.status = 'ACTIVE'") == 1  # only cte_x remains
    assert "FROM cte_z z#(lf)WHERE z.amount >= 10\"" in disk_after_main

    # 3. Unknown CTE raises ObjectNotFoundError listing all valid SQL Section names
    with pytest.raises(Exception) as exc_info:
        _call_tool_sync(
            direct_srv,
            "update_table_sql",
            {
                "table_name": "FactPipeline",
                "cte": "cte_typo",
                "old_text": "SELECT",
                "new_text": "SELECT DISTINCT",
            },
        )
    cause = exc_info.value.__cause__ or exc_info.value
    assert getattr(cause, "code", None) == "OBJECT_NOT_FOUND"
    msg = str(cause)
    assert "cte_x" in msg
    assert "cte_z" in msg
    assert "main" in msg
    info = json.loads(
        _call_tool_sync(
            direct_srv,
            "get_table_sql",
            {"table_name": "FactPipeline", "format": "json"},
        ).content[0].text
    )
    assert [s["name"] for s in info["sections"]] == ["cte_x", "cte_z", "main"]


def test_whole_cte_body_replacement_auto_unwrap_and_unknown_cte_error(tmp_path: Path) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    tmdl_path = _write_table_tmdl(pbip, "FactPipeline.tmdl", MULTI_CTE_DATABRICKS_TMDL)

    direct_srv = create_server(PbipService(pbip))
    meta_srv = create_server(PbipService(pbip), meta_tools=True)

    # 1. Whole-CTE body replacement preserves WITH, `cte_x AS (`, `)`, and trailing comma
    dry_res = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactPipeline",
            "cte": "cte_x",
            "new_text": (
                "SELECT\n"
                "    o.order_id,\n"
                "    o.amount * 2 AS amount\n"
                "FROM {{m:Catalog}}.{{m:Schema}}.orders o\n"
                "WHERE o.status = 'OPEN'"
            ),
        },
    )
    dry_text = dry_res.content[0].text
    assert "Status: dry_run" in dry_text
    assert "Replacements: 1" in dry_text
    assert "-        o.amount" in dry_text
    assert "+        o.amount * 2 AS amount" in dry_text
    assert "+    WHERE o.status = 'OPEN'" in dry_text

    # Apply whole-CTE body replacement and verify raw .tmdl preserves WITH/parens/comma & #(lf)
    _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactPipeline",
            "cte": "cte_x",
            "new_text": (
                "SELECT\n"
                "    o.order_id,\n"
                "    o.amount * 2 AS amount\n"
                "FROM {{m:Catalog}}.{{m:Schema}}.orders o\n"
                "WHERE o.status = 'OPEN'"
            ),
            "dry_run": False,
        },
    )
    disk = tmdl_path.read_text(encoding="utf-8")
    assert '"WITH cte_x AS (#(lf)    SELECT#(lf)' in disk
    assert "o.amount * 2 AS amount#(lf)" in disk
    assert "WHERE o.status = 'OPEN'#(lf)),#(lf)cte_z AS (" in disk

    # 2. Auto-unwrap accidental `cte_z AS (...),` wrapper with informational note
    unwrap_named_res = _call_tool_sync(
        meta_srv,
        "pbi_call",
        {
            "tool_name": "update_table_sql",
            "arguments": {
                "table_name": "FactPipeline",
                "cte": "cte_z",
                "new_text": (
                    "cte_z AS (\n"
                    "    SELECT x.order_id, SUM(x.amount) AS amount\n"
                    "    FROM cte_x x\n"
                    "    GROUP BY x.order_id\n"
                    "),"
                ),
                "dry_run": False,
            },
        },
    )
    unwrap_text = unwrap_named_res.content[0].text
    assert "Status: applied" in unwrap_text
    assert "Warnings:" in unwrap_text
    assert "unwrap" in unwrap_text.lower()
    disk_after_unwrap = tmdl_path.read_text(encoding="utf-8")
    assert "cte_z AS (#(lf)cte_z AS (" not in disk_after_unwrap
    assert "cte_z AS (#(lf)    SELECT x.order_id, SUM(x.amount) AS amount" in disk_after_unwrap
    assert "GROUP BY x.order_id#(lf))#(lf)SELECT" in disk_after_unwrap

    # 3. Auto-unwrap accidental outer matching parentheses `(...)` with informational note
    unwrap_paren_json = json.loads(
        _call_tool_sync(
            direct_srv,
            "update_table_sql",
            {
                "table_name": "FactPipeline",
                "cte": "cte_z",
                "new_text": "(\n    SELECT x.order_id, x.amount\n    FROM cte_x x\n)",
                "format": "json",
                "dry_run": False,
            },
        ).content[0].text
    )
    assert unwrap_paren_json["status"] == "applied"
    assert any("unwrap" in w.lower() for w in unwrap_paren_json["warnings"])
    disk_after_paren = tmdl_path.read_text(encoding="utf-8")
    assert "cte_z AS (#(lf)(" not in disk_after_paren
    assert (
        "cte_z AS (#(lf)    SELECT x.order_id, x.amount#(lf)"
        "    FROM cte_x x#(lf))#(lf)SELECT"
    ) in disk_after_paren

    # 4. Whole-CTE body replacement for 'main' replaces only the final SELECT
    main_res = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactPipeline",
            "cte": "main",
            "new_text": "SELECT z.order_id FROM cte_z z",
            "dry_run": False,
        },
    )
    assert "Status: applied" in main_res.content[0].text
    disk_after_main = tmdl_path.read_text(encoding="utf-8")
    assert ")#(lf)SELECT z.order_id FROM cte_z z\"" in disk_after_main

    # 5. Unknown CTE without insert anchor raises ObjectNotFoundError listing valid sections
    with pytest.raises(Exception) as exc_info:
        _call_tool_sync(
            direct_srv,
            "update_table_sql",
            {
                "table_name": "FactPipeline",
                "cte": "missing_cte",
                "new_text": "SELECT 1",
            },
        )
    cause = exc_info.value.__cause__ or exc_info.value
    assert getattr(cause, "code", None) == "OBJECT_NOT_FOUND"
    assert "cte_x" in str(cause)
    assert "cte_z" in str(cause)
    assert "main" in str(cause)


PLAIN_SELECT_WITH_COMMENTS_TMDL = (
    "table PlainEvents\n"
    "\tlineageTag: 44444444-4444-4444-4444-444444444444\n"
    "\n"
    "\tcolumn event_id\n"
    "\t\tdataType: string\n"
    "\t\tsourceColumn: event_id\n"
    "\n"
    "\tpartition PlainEvents = m\n"
    "\t\tmode: directQuery\n"
    "\t\tsource = ```\n"
    "\t\t\tlet\n"
    '\t\t\t    Source = Value.NativeQuery(Databricks.Catalogs(""&Host&"", ""&Wh&"", '
    '[Catalog=""&Catalog&""]){[Name=""&Catalog&"",Kind="Database"]}[Data],\n'
    '\t\t\t""-- Report: Events Summary\n'
    "\t\t\t-- Owner: Data Team\n"
    "\t\t\tSELECT\n"
    "\t\t\t    e.event_id,\n"
    "\t\t\t    e.amount\n"
    '\t\t\tFROM "&Catalog&"."&Schema&".events e\n'
    '\t\t\t""\n'
    "\t\t\t    , null, [EnableFolding=true])\n"
    "\t\tin\n"
    "\t\t\t    Source\n"
    "\t\t\t```\n"
)


def test_positional_cte_insertion_with_bootstrap_and_validation_errors(tmp_path: Path) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    pipeline_path = _write_table_tmdl(pbip, "FactPipeline.tmdl", MULTI_CTE_DATABRICKS_TMDL)
    plain_path = _write_table_tmdl(pbip, "PlainEvents.tmdl", PLAIN_SELECT_WITH_COMMENTS_TMDL)

    direct_srv = create_server(PbipService(pbip))
    meta_srv = create_server(PbipService(pbip), meta_tools=True)

    # 1. Insert cte_y between cte_x and cte_z (auto-unwrapping accidental `cte_y AS (...)`)
    insert_mid = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactPipeline",
            "cte": "cte_y",
            "after_cte": "cte_x",
            "before_cte": "cte_z",
            "new_text": (
                "cte_y AS (\n"
                "    SELECT x.order_id, x.amount\n"
                "    FROM cte_x x\n"
                "    WHERE x.amount > 0\n"
                ")"
            ),
            "dry_run": False,
        },
    ).content[0].text
    assert "Status: applied" in insert_mid
    assert "Warnings:" in insert_mid
    assert "unwrap" in insert_mid.lower()
    assert "+cte_y AS (" in insert_mid

    info_after_mid = json.loads(
        _call_tool_sync(
            direct_srv,
            "get_table_sql",
            {"table_name": "FactPipeline", "format": "json"},
        ).content[0].text
    )
    assert [s["name"] for s in info_after_mid["sections"]] == ["cte_x", "cte_y", "cte_z", "main"]
    disk_mid = pipeline_path.read_text(encoding="utf-8")
    assert "),#(lf)cte_y AS (#(lf)    SELECT x.order_id, x.amount" in disk_mid
    assert "WHERE x.amount > 0#(lf)),#(lf)cte_z AS (" in disk_mid

    # 2. Insert before the first CTE (before_cte="cte_x")
    insert_first = _call_tool_sync(
        meta_srv,
        "pbi_call",
        {
            "tool_name": "update_table_sql",
            "arguments": {
                "table_name": "FactPipeline",
                "cte": "cte_raw",
                "before_cte": "cte_x",
                "new_text": "SELECT 1 AS flag",
                "dry_run": False,
            },
        },
    ).content[0].text
    assert "Status: applied" in insert_first
    info_after_first = json.loads(
        _call_tool_sync(
            direct_srv,
            "get_table_sql",
            {"table_name": "FactPipeline", "format": "json"},
        ).content[0].text
    )
    assert [s["name"] for s in info_after_first["sections"]] == [
        "cte_raw",
        "cte_x",
        "cte_y",
        "cte_z",
        "main",
    ]
    assert '"WITH cte_raw AS (#(lf)    SELECT 1 AS flag#(lf)),#(lf)cte_x AS (' in (
        pipeline_path.read_text(encoding="utf-8")
    )

    # 3. Insert after last CTE via before_cte="main" on an existing CTE query
    _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactPipeline",
            "cte": "cte_final",
            "before_cte": "main",
            "new_text": "SELECT * FROM cte_z",
            "dry_run": False,
        },
    )
    info_after_last = json.loads(
        _call_tool_sync(
            direct_srv,
            "get_table_sql",
            {"table_name": "FactPipeline", "format": "json"},
        ).content[0].text
    )
    assert [s["name"] for s in info_after_last["sections"]] == [
        "cte_raw",
        "cte_x",
        "cte_y",
        "cte_z",
        "cte_final",
        "main",
    ]

    # 4. Bootstrap `WITH <cte> AS (...)` via before_cte="main" on a non-CTE query,
    #    preserving leading comments cleanly before SELECT!
    boot_res = _call_tool_sync(
        meta_srv,
        "pbi_call",
        {
            "tool_name": "update_table_sql",
            "arguments": {
                "table_name": "PlainEvents",
                "cte": "filtered_events",
                "before_cte": "main",
                "new_text": (
                    "SELECT event_id, amount\n"
                    "FROM {{m:Catalog}}.{{m:Schema}}.events\n"
                    "WHERE amount > 0"
                ),
                "dry_run": False,
            },
        },
    ).content[0].text
    assert "Status: applied" in boot_res
    assert "+WITH filtered_events AS (" in boot_res
    boot_info = json.loads(
        _call_tool_sync(
            direct_srv,
            "get_table_sql",
            {"table_name": "PlainEvents", "format": "json"},
        ).content[0].text
    )
    assert [s["name"] for s in boot_info["sections"]] == ["filtered_events", "main"]
    plain_disk = plain_path.read_text(encoding="utf-8")
    assert (
        '""-- Report: Events Summary\n'
        "\t\t\t-- Owner: Data Team\n"
        "\t\t\tWITH filtered_events AS (\n"
        "\t\t\t    SELECT event_id, amount\n"
        '\t\t\t    FROM "&Catalog&"."&Schema&".events\n'
        "\t\t\t    WHERE amount > 0\n"
        "\t\t\t)\n"
        "\t\t\tSELECT\n"
    ) in plain_disk

    # 5. Error checks: duplicate CTE name, unknown anchor, inverted order, after_cte="main"
    # Duplicate CTE name -> InvalidArgumentError
    with pytest.raises(Exception) as exc_dup:
        _call_tool_sync(
            direct_srv,
            "update_table_sql",
            {
                "table_name": "FactPipeline",
                "cte": "cte_x",
                "after_cte": "cte_raw",
                "new_text": "SELECT 1",
            },
        )
    assert getattr(exc_dup.value.__cause__ or exc_dup.value, "code", None) == "INVALID_ARGUMENT"

    # Unknown anchor -> ObjectNotFoundError listing valid sections
    with pytest.raises(Exception) as exc_unknown:
        _call_tool_sync(
            direct_srv,
            "update_table_sql",
            {
                "table_name": "FactPipeline",
                "cte": "cte_new",
                "after_cte": "no_such_cte",
                "new_text": "SELECT 1",
            },
        )
    unknown_cause = exc_unknown.value.__cause__ or exc_unknown.value
    assert getattr(unknown_cause, "code", None) == "OBJECT_NOT_FOUND"
    assert "cte_x" in str(unknown_cause)
    assert "main" in str(unknown_cause)

    # Inverted order (after_cte comes after before_cte) -> PreconditionFailedError
    with pytest.raises(Exception) as exc_inv:
        _call_tool_sync(
            direct_srv,
            "update_table_sql",
            {
                "table_name": "FactPipeline",
                "cte": "cte_bad_order",
                "after_cte": "cte_z",
                "before_cte": "cte_x",
                "new_text": "SELECT 1",
            },
        )
    assert getattr(exc_inv.value.__cause__ or exc_inv.value, "code", None) == "PRECONDITION_FAILED"

    # after_cte="main" -> PreconditionFailedError
    with pytest.raises(Exception) as exc_after_main:
        _call_tool_sync(
            direct_srv,
            "update_table_sql",
            {
                "table_name": "FactPipeline",
                "cte": "cte_after_main",
                "after_cte": "main",
                "new_text": "SELECT 1",
            },
        )
    assert (
        getattr(exc_after_main.value.__cause__ or exc_after_main.value, "code", None)
        == "PRECONDITION_FAILED"
    )


THREE_CTE_RECURSIVE_TMDL = (
    "table FactThreeCtes\n"
    "\tlineageTag: 55555555-5555-5555-5555-555555555555\n"
    "\n"
    "\tcolumn id\n"
    "\t\tdataType: int64\n"
    "\t\tsourceColumn: id\n"
    "\n"
    "\tpartition FactThreeCtes = m\n"
    "\t\tmode: import\n"
    "\t\tsource =\n"
    "\t\t\tlet\n"
    '\t\t\t    Source = Value.NativeQuery(Sql.Database("srv", "db"), '
    '"WITH RECURSIVE cte_a AS (#(lf)'
    '    SELECT id FROM dbo.raw_a#(lf)'
    '),#(lf)'
    'cte_b AS (#(lf)'
    '    SELECT id FROM cte_a#(lf)'
    '),#(lf)'
    'cte_c AS (#(lf)'
    '    SELECT id FROM dbo.raw_c#(lf)'
    ')#(lf)'
    'SELECT id FROM cte_c")\n'
    "\t\tin\n"
    "\t\t\t    Source\n"
)


def test_cte_deletion_commas_with_removal_and_downstream_warning(tmp_path: Path) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    tmdl_path = _write_table_tmdl(pbip, "FactThreeCtes.tmdl", THREE_CTE_RECURSIVE_TMDL)

    direct_srv = create_server(PbipService(pbip))
    meta_srv = create_server(PbipService(pbip), meta_tools=True)

    # 1. Deleting cte_a while cte_b still references it emits a dry-run warning
    warn_text = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactThreeCtes",
            "cte": "cte_a",
            "delete_cte": True,
        },
    ).content[0].text
    assert "Status: dry_run" in warn_text
    assert "Warnings:" in warn_text
    assert "cte_a" in warn_text
    assert "cte_b" in warn_text

    warn_json = json.loads(
        _call_tool_sync(
            direct_srv,
            "update_table_sql",
            {
                "table_name": "FactThreeCtes",
                "cte": "cte_a",
                "delete_cte": True,
                "format": "json",
            },
        ).content[0].text
    )
    assert any("cte_a" in w and "cte_b" in w for w in warn_json["warnings"])

    # 2. Delete middle unused CTE (cte_b) — no downstream warning, fixes adjacent commas cleanly
    del_mid = _call_tool_sync(
        meta_srv,
        "pbi_call",
        {
            "tool_name": "update_table_sql",
            "arguments": {
                "table_name": "FactThreeCtes",
                "cte": "cte_b",
                "delete_cte": True,
                "dry_run": False,
            },
        },
    ).content[0].text
    assert "Status: applied" in del_mid
    assert "Warnings:" not in del_mid
    disk_mid = tmdl_path.read_text(encoding="utf-8")
    assert "cte_b" not in disk_mid
    assert '"WITH RECURSIVE cte_a AS (#(lf)    SELECT id FROM dbo.raw_a#(lf)),#(lf)cte_c AS (' in (
        disk_mid
    )

    # 3. Delete first CTE (cte_a) — now unused, fixes commas cleanly
    del_first = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactThreeCtes",
            "cte": "cte_a",
            "delete_cte": True,
            "dry_run": False,
        },
    ).content[0].text
    assert "Status: applied" in del_first
    assert "Warnings:" not in del_first
    disk_first = tmdl_path.read_text(encoding="utf-8")
    assert (
        '"WITH RECURSIVE cte_c AS (#(lf)    SELECT id FROM dbo.raw_c#(lf))'
        '#(lf)SELECT id FROM cte_c"'
    ) in disk_first

    # 4. Delete final remaining CTE (cte_c) — removes WITH RECURSIVE AND warns main references cte_c
    del_last = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactThreeCtes",
            "cte": "cte_c",
            "delete_cte": True,
            "dry_run": False,
        },
    ).content[0].text
    assert "Status: applied" in del_last
    assert "Warnings:" in del_last
    assert "cte_c" in del_last
    assert "main" in del_last
    disk_last = tmdl_path.read_text(encoding="utf-8")
    assert "WITH" not in disk_last
    assert '"SELECT id FROM cte_c"' in disk_last

    # 5. Reject deleting 'main' with InvalidArgumentError
    with pytest.raises(Exception) as exc_main:
        _call_tool_sync(
            direct_srv,
            "update_table_sql",
            {
                "table_name": "FactThreeCtes",
                "cte": "main",
                "delete_cte": True,
            },
        )
    assert getattr(exc_main.value.__cause__ or exc_main.value, "code", None) == "INVALID_ARGUMENT"

    # 6. Unknown CTE raises ObjectNotFoundError listing valid sections
    with pytest.raises(Exception) as exc_unknown:
        _call_tool_sync(
            direct_srv,
            "update_table_sql",
            {
                "table_name": "FactThreeCtes",
                "cte": "ghost_cte",
                "delete_cte": True,
            },
        )
    cause = exc_unknown.value.__cause__ or exc_unknown.value
    assert getattr(cause, "code", None) == "OBJECT_NOT_FOUND"
    assert "main" in str(cause)

    # 7. Combining delete_cte=True with new_text/old_text/anchors raises InvalidArgumentError
    with pytest.raises(Exception) as exc_combo:
        _call_tool_sync(
            direct_srv,
            "update_table_sql",
            {
                "table_name": "FactThreeCtes",
                "cte": "ghost_cte",
                "delete_cte": True,
                "new_text": "SELECT 1",
            },
        )
    assert getattr(exc_combo.value.__cause__ or exc_combo.value, "code", None) == "INVALID_ARGUMENT"


def test_atomic_batch_edits_insert_rewire_delete_and_conflict_rejection(tmp_path: Path) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    pipeline_path = _write_table_tmdl(pbip, "FactPipeline.tmdl", MULTI_CTE_DATABRICKS_TMDL)
    three_path = _write_table_tmdl(pbip, "FactThreeCtes.tmdl", THREE_CTE_RECURSIVE_TMDL)

    direct_srv = create_server(PbipService(pbip))
    meta_srv = create_server(PbipService(pbip), meta_tools=True)

    # 1. Insert cte_y between cte_x and cte_z + rewire cte_z + edit main by pre-edit line number
    #    in ONE atomic batch call (zero line-number drift!)
    original_pipeline = pipeline_path.read_text(encoding="utf-8")
    batch_edits = [
        {
            "cte": "cte_y",
            "after_cte": "cte_x",
            "before_cte": "cte_z",
            "new_text": (
                "SELECT x.order_id, x.amount\n"
                "FROM cte_x x\n"
                "WHERE x.amount > 0"
            ),
        },
        {
            "cte": "cte_z",
            "old_text": "FROM cte_x x",
            "new_text": "FROM cte_y x",
        },
        {
            # Pre-edit line 19 is `WHERE o.status = 'ACTIVE'` in `main`
            "start_line": 19,
            "end_line": 19,
            "new_text": "WHERE z.amount > 0",
        },
    ]

    dry_batch = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactPipeline",
            "edits": batch_edits,
        },
    ).content[0].text
    assert "Status: dry_run" in dry_batch
    assert "Replacements: 3" in dry_batch
    assert "+cte_y AS (" in dry_batch
    assert "-    FROM cte_x x" in dry_batch
    assert "+    FROM cte_y x" in dry_batch
    assert "-WHERE o.status = 'ACTIVE'" in dry_batch
    assert "+WHERE z.amount > 0" in dry_batch
    assert pipeline_path.read_text(encoding="utf-8") == original_pipeline

    # Apply via pbi_call in ONE atomic disk write
    apply_batch = _call_tool_sync(
        meta_srv,
        "pbi_call",
        {
            "tool_name": "update_table_sql",
            "arguments": {
                "table_name": "FactPipeline",
                "edits": batch_edits,
                "dry_run": False,
            },
        },
    ).content[0].text
    assert "Status: applied" in apply_batch
    assert "Replacements: 3" in apply_batch

    disk_pipeline = pipeline_path.read_text(encoding="utf-8")
    assert "),#(lf)cte_y AS (#(lf)    SELECT x.order_id, x.amount" in disk_pipeline
    assert "FROM cte_y x#(lf)" in disk_pipeline
    assert "FROM cte_z z#(lf)WHERE z.amount > 0\"" in disk_pipeline
    # Dynamic M Parameters preserved intact
    assert 'FROM " & Catalog & "." & Schema & ".orders o' in disk_pipeline
    info_after = json.loads(
        _call_tool_sync(
            direct_srv,
            "get_table_sql",
            {"table_name": "FactPipeline", "format": "json"},
        ).content[0].text
    )
    assert [s["name"] for s in info_after["sections"]] == ["cte_x", "cte_y", "cte_z", "main"]

    # 2. Delete cte_a AND rewire cte_b off cte_a in ONE batch -> NO bogus downstream warning!
    del_rewire_res = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactThreeCtes",
            "edits": [
                {"cte": "cte_a", "delete_cte": True},
                {
                    "cte": "cte_b",
                    "old_text": "FROM cte_a",
                    "new_text": "FROM dbo.raw_a",
                },
            ],
            "dry_run": False,
        },
    ).content[0].text
    assert "Status: applied" in del_rewire_res
    assert "Warnings:" not in del_rewire_res
    disk_three = three_path.read_text(encoding="utf-8")
    assert "cte_a" not in disk_three
    assert '"WITH RECURSIVE cte_b AS (#(lf)    SELECT id FROM dbo.raw_a#(lf)),#(lf)cte_c AS (' in (
        disk_three
    )

    # 3. Delete ALL remaining CTEs AND rewire main in ONE batch -> removes WITH, ZERO warnings!
    del_all_res = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactThreeCtes",
            "edits": [
                {"cte": "cte_b", "delete_cte": True},
                {"cte": "cte_c", "delete_cte": True},
                {
                    "cte": "main",
                    "old_text": "FROM cte_c",
                    "new_text": "FROM dbo.raw_c",
                },
            ],
            "dry_run": False,
        },
    ).content[0].text
    assert "Status: applied" in del_all_res
    assert "Warnings:" not in del_all_res
    disk_plain = three_path.read_text(encoding="utf-8")
    assert "WITH" not in disk_plain
    assert '"SELECT id FROM dbo.raw_c"' in disk_plain

    # 4. Overlapping and conflicting batch edits are rejected cleanly without writing to disk
    before_conflict = pipeline_path.read_text(encoding="utf-8")
    conflict_cases: list[tuple[dict[str, Any], str]] = [
        # Overlapping line ranges
        (
            {
                "table_name": "FactPipeline",
                "edits": [
                    {"start_line": 1, "end_line": 4, "new_text": "-- a"},
                    {"start_line": 3, "end_line": 6, "new_text": "-- b"},
                ],
                "dry_run": False,
            },
            "INVALID_ARGUMENT",
        ),
        # Editing a CTE that is also deleted in the same batch
        (
            {
                "table_name": "FactPipeline",
                "edits": [
                    {"cte": "cte_y", "delete_cte": True},
                    {"cte": "cte_y", "old_text": "amount > 0", "new_text": "amount > 1"},
                ],
                "dry_run": False,
            },
            "INVALID_ARGUMENT",
        ),
        # Using a deleted CTE as insertion anchor
        (
            {
                "table_name": "FactPipeline",
                "edits": [
                    {"cte": "cte_y", "delete_cte": True},
                    {"cte": "cte_w", "after_cte": "cte_y", "new_text": "SELECT 1"},
                ],
                "dry_run": False,
            },
            "INVALID_ARGUMENT",
        ),
        # Two insertions at the exact same slot
        (
            {
                "table_name": "FactPipeline",
                "edits": [
                    {"cte": "cte_w1", "after_cte": "cte_x", "new_text": "SELECT 1"},
                    {"cte": "cte_w2", "after_cte": "cte_x", "new_text": "SELECT 2"},
                ],
                "dry_run": False,
            },
            "INVALID_ARGUMENT",
        ),
        # Combining top-level edits with top-level cte/old_text
        (
            {
                "table_name": "FactPipeline",
                "cte": "cte_x",
                "edits": [{"cte": "cte_y", "delete_cte": True}],
            },
            "INVALID_ARGUMENT",
        ),
        # Empty edits list
        (
            {
                "table_name": "FactPipeline",
                "edits": [],
            },
            "INVALID_ARGUMENT",
        ),
        # Unmatched old_text in one batch entry aborts entire batch atomically
        (
            {
                "table_name": "FactPipeline",
                "edits": [
                    {"cte": "cte_y", "delete_cte": True},
                    {"cte": "cte_z", "old_text": "DOES_NOT_EXIST", "new_text": "X"},
                ],
                "dry_run": False,
            },
            "PRECONDITION_FAILED",
        ),
    ]
    for bad_args, expected_code in conflict_cases:
        with pytest.raises(Exception) as exc_bad:
            _call_tool_sync(direct_srv, "update_table_sql", bad_args)
        cause = exc_bad.value.__cause__ or exc_bad.value
        assert getattr(cause, "code", None) == expected_code
    assert pipeline_path.read_text(encoding="utf-8") == before_conflict


def test_cte_and_batch_guardrails_hash_m_warnings_rollback_and_pbi_help(
    tmp_path: Path,
) -> None:
    pbip = _clone_sample_workspace(tmp_path)
    pipeline_path = _write_table_tmdl(pbip, "FactPipeline.tmdl", MULTI_CTE_DATABRICKS_TMDL)
    plain_path = _write_table_tmdl(pbip, "PlainEvents.tmdl", PLAIN_SELECT_WITH_COMMENTS_TMDL)

    direct_srv = create_server(PbipService(pbip))
    meta_srv = create_server(PbipService(pbip), meta_tools=True)

    current_hash = json.loads(
        _call_tool_sync(
            direct_srv,
            "get_table_sql",
            {"table_name": "FactPipeline", "format": "json"},
        ).content[0].text
    )["sql_hash"]

    # 1. Stale expected_sql_hash rejected on batch edit with current sql_hash in error
    with pytest.raises(Exception) as exc_hash:
        _call_tool_sync(
            direct_srv,
            "update_table_sql",
            {
                "table_name": "FactPipeline",
                "edits": [{"cte": "cte_z", "old_text": "amount", "new_text": "total_amount"}],
                "expected_sql_hash": "00000000",
                "dry_run": False,
            },
        )
    hash_cause = exc_hash.value.__cause__ or exc_hash.value
    assert getattr(hash_cause, "code", None) == "PRECONDITION_FAILED"
    assert current_hash in str(hash_cause)

    # 2. Pre-write delimiter check catches unbalanced quotes/parens inside batch/CTE edits
    original_pipeline = pipeline_path.read_text(encoding="utf-8")
    with pytest.raises(Exception) as exc_delim:
        _call_tool_sync(
            direct_srv,
            "update_table_sql",
            {
                "table_name": "FactPipeline",
                "cte": "cte_z",
                "new_text": "SELECT x.order_id FROM cte_x x WHERE x.note = 'unterminated",
                "dry_run": False,
            },
        )
    delim_cause = exc_delim.value.__cause__ or exc_delim.value
    assert getattr(delim_cause, "code", None) == "INVALID_ARGUMENT"
    assert "single quote" in str(delim_cause).lower()
    assert pipeline_path.read_text(encoding="utf-8") == original_pipeline

    # 3. Replacing/deleting a CTE that drops {{m:Catalog}}/{{m:Schema}} emits a warning
    m_warn_text = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "FactPipeline",
            "cte": "cte_x",
            "new_text": "SELECT o.order_id, o.amount FROM prod.sales.orders o",
            "expected_sql_hash": current_hash,
            "dry_run": True,
        },
    ).content[0].text
    assert "Status: dry_run" in m_warn_text
    assert "Warnings:" in m_warn_text
    assert "{{m:Catalog}}" in m_warn_text
    assert "{{m:Schema}}" in m_warn_text

    # 4. Post-write Mutation Pipeline rollback on a batch edit that breaks a TMDL fence
    original_plain = plain_path.read_text(encoding="utf-8")
    rollback_text = _call_tool_sync(
        direct_srv,
        "update_table_sql",
        {
            "table_name": "PlainEvents",
            "edits": [
                {
                    "cte": "bad_cte",
                    "before_cte": "main",
                    "new_text": "/* fence break\n```\n*/\nSELECT 1 AS event_id",
                }
            ],
            "dry_run": False,
        },
    ).content[0].text
    assert "Status: rolled_back" in rollback_text
    assert "UNBALANCED_TMDL_FENCE" in rollback_text
    assert plain_path.read_text(encoding="utf-8") == original_plain

    # 5. pbi_help exposes all new CTE & batch parameters cleanly
    help_payload = json.loads(
        _call_tool_sync(
            meta_srv,
            "pbi_help",
            {"tool_name": "update_table_sql"},
        ).content[0].text
    )
    params = {p["name"]: p for p in help_payload["parameters"]}
    assert {"cte", "after_cte", "before_cte", "delete_cte", "edits"}.issubset(params)
    assert params["cte"]["type"] == "str"
    assert params["after_cte"]["type"] == "str"
    assert params["before_cte"]["type"] == "str"
    assert params["delete_cte"]["type"] == "bool"
    assert params["delete_cte"]["default"] is False
    assert params["edits"]["type"] == "list"





