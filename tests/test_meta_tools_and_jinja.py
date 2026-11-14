from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from pbi_mcp_server import PbipService, create_server
from pbi_mcp_server.jinja_templates import (
    PRESET_NAMES,
    inject_into_base_template,
    render_preset_template,
)
from pbi_mcp_server.main import main, parse_args
from pbi_mcp_server.meta_tools import format_compact_catalog
from pbi_mcp_server.server import get_tool_specs


def clone_sample_workspace(tmp_path: Path) -> Path:
    repo_root = Path(__file__).parents[1]
    shutil.copy2(repo_root / "sample_project.pbip", tmp_path / "sample_project.pbip")
    shutil.copytree(repo_root / "sample_project.Report", tmp_path / "sample_project.Report")
    shutil.copytree(
        repo_root / "sample_project.SemanticModel",
        tmp_path / "sample_project.SemanticModel",
    )
    return tmp_path / "sample_project.pbip"


def _parse_tool_result(res: object) -> dict[str, object] | list[object]:
    """Extract JSON payload from MCPServer.call_tool result."""
    content = getattr(res, "content", res)
    if isinstance(content, list) and content:
        items = [
            json.loads(getattr(item, "text", ""))
            for item in content
            if isinstance(getattr(item, "text", None), str)
        ]
        if len(items) == 1:
            return items[0]  # type: ignore[no-any-return]
        if items:
            return items
    if isinstance(res, dict | list):
        return res
    raise AssertionError(f"Unexpected tool result format: {res!r}")


def test_meta_tools_mode_exposes_only_pbi_call_and_pbi_help(tmp_path: Path) -> None:
    pbip = clone_sample_workspace(tmp_path)
    srv = create_server(PbipService(pbip), meta_tools=True)
    tools = asyncio.run(srv.list_tools())
    assert [t.name for t in tools] == ["pbi_call", "pbi_help"]


def test_pbi_help_lists_searches_and_inspects_tools(tmp_path: Path) -> None:
    pbip = clone_sample_workspace(tmp_path)
    srv = create_server(PbipService(pbip), meta_tools=True)

    all_tools = _parse_tool_result(asyncio.run(srv.call_tool("pbi_help", {})))
    assert isinstance(all_tools, dict)
    assert all_tools["count"] == 20

    filtered = _parse_tool_result(asyncio.run(srv.call_tool("pbi_help", {"query": "measure"})))
    assert isinstance(filtered, dict)
    names = {t["tool_name"] for t in filtered["tools"]}  # type: ignore[index]
    assert "search_measures" in names
    assert "update_measure_dax" in names

    detail = _parse_tool_result(
        asyncio.run(srv.call_tool("pbi_help", {"tool_name": "update_measure_dax"}))
    )
    assert isinstance(detail, dict)
    assert detail["tool_name"] == "update_measure_dax"
    params = {p["name"]: p for p in detail["parameters"]}  # type: ignore[index]
    assert params["measure_name"]["required"] is True
    assert params["dry_run"]["required"] is False
    assert params["dry_run"]["default"] is True


def test_pbi_call_dispatches_and_preserves_dry_run(tmp_path: Path) -> None:
    pbip = clone_sample_workspace(tmp_path)
    srv = create_server(PbipService(pbip), meta_tools=True)

    tables = _parse_tool_result(
        asyncio.run(srv.call_tool("pbi_call", {"tool_name": "list_tables", "arguments": {}}))
    )
    assert isinstance(tables, list)
    assert len(tables) >= 1

    # Mutation tool via pbi_call still defaults to dry_run=True
    plan = _parse_tool_result(
        asyncio.run(
            srv.call_tool(
                "pbi_call",
                {
                    "tool_name": "update_measure_dax",
                    "arguments": {
                        "measure_name": "Total Revenue",
                        "expression": "SUM(Sales[Amount])",
                    },
                },
            )
        )
    )
    assert isinstance(plan, dict)
    assert plan["dry_run"] is True


def test_pbi_call_rejects_unknown_tool_and_bad_args(tmp_path: Path) -> None:
    pbip = clone_sample_workspace(tmp_path)
    srv = create_server(PbipService(pbip), meta_tools=True)

    with pytest.raises(Exception) as exc_unknown:
        asyncio.run(srv.call_tool("pbi_call", {"tool_name": "get_tab", "arguments": {}}))
    assert "Unknown PBI tool" in str(exc_unknown.value.__cause__ or exc_unknown.value)

    with pytest.raises(Exception) as exc_bad_args:
        asyncio.run(srv.call_tool("pbi_call", {"tool_name": "get_table", "arguments": {}}))
    assert "Invalid arguments" in str(exc_bad_args.value.__cause__ or exc_bad_args.value)


def test_catalog_and_presets_include_all_20_tools() -> None:
    specs = get_tool_specs()
    assert len(specs) == 20
    catalog = format_compact_catalog(specs)
    for spec in specs:
        assert spec.name in catalog

    for preset in PRESET_NAMES:
        rendered = render_preset_template(preset, catalog)
        assert "{#- PBI_MCP_CATALOG_START -#}" in rendered
        assert "_pbi_tools_catalog" in rendered
        for spec in specs:
            assert spec.name in rendered


def test_inject_into_base_template_is_idempotent() -> None:
    specs = get_tool_specs()
    catalog = format_compact_catalog(specs)
    base = (
        "{%- for message in messages %}\n"
        "{{- '<|im_start|>' + message.role + '\\n' + message.content + '<|im_end|>\\n' }}\n"
        "{%- endfor %}\n"
    )
    injected_once = inject_into_base_template(base, catalog)
    injected_twice = inject_into_base_template(injected_once, catalog)
    assert injected_once == injected_twice
    assert injected_once.count("{#- PBI_MCP_CATALOG_START -#}") == 1


def test_cli_export_jinja_and_meta_tools_flag(tmp_path: Path) -> None:
    # --export-jinja works without --workspace
    out_dir = tmp_path / "out_templates"
    main(["--export-jinja", "all", "--output-dir", str(out_dir)])
    for preset in PRESET_NAMES:
        assert (out_dir / f"{preset}.jinja").is_file()

    # --base-template works without --workspace
    base_file = tmp_path / "custom.jinja"
    base_file.write_text("{%- for m in messages %}{{ m.content }}{%- endfor %}", encoding="utf-8")
    out_file = tmp_path / "custom_pbi.jinja"
    main(["--base-template", str(base_file), "--output", str(out_file)])
    assert out_file.is_file()
    assert "list_workspaces" in out_file.read_text(encoding="utf-8")

    # --meta-tools flag parses cleanly with --workspace
    args = parse_args(["--workspace", str(tmp_path), "--meta-tools"])
    assert args.meta_tools is True


def test_inject_into_gemma4_and_gpt_oss_base_templates() -> None:
    specs = get_tool_specs()
    catalog = format_compact_catalog(specs)

    gemma4_base = "{%- for m in messages %}<|turn>{{ m.role }}\n{{ m.content }}<turn|>\n{%- endfor %}"
    gemma4_out = inject_into_base_template(gemma4_base, catalog)
    assert "<|turn>system" in gemma4_out
    assert "list_workspaces" in gemma4_out

    harmony_base = (
        "{%- for m in messages %}<|start|>{{ m.role }}<|message|>{{ m.content }}<|end|>{%- endfor %}"
    )
    # Simulate Harmony template containing system<|message|> marker
    harmony_base_with_sys = "{# <|start|>system<|message|> #}\n" + harmony_base
    harmony_out = inject_into_base_template(harmony_base_with_sys, catalog)
    assert "<|start|>system<|message|>" in harmony_out
    assert "list_workspaces" in harmony_out

