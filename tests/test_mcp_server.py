"""Tests for MCP server tool registration and execution."""

import os
import json
import pytest
from mcp_server.server import create_server


@pytest.fixture
def server():
    return create_server(os.path.abspath("sample_project.pbip"))


@pytest.mark.asyncio
async def test_server_tools_registered(server):
    tools = await server.list_tools()
    tool_names = [t.name for t in tools]

    expected = [
        "get_model_summary",
        "list_tables",
        "get_table_schema",
        "get_measure",
        "update_measure_dax",
        "list_pages",
        "get_page_visuals",
        "get_visual_property",
        "query_report_layout",
        "rename_measure",
        "validate_pbip_structure",
    ]
    for name in expected:
        assert name in tool_names, f"Missing tool: {name}"


@pytest.mark.asyncio
async def test_server_call_get_model_summary(server):
    result = await server.call_tool("get_model_summary", {})
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["project_name"] == "sample_project"
    assert result.structured_content["tables_count"] == 2


@pytest.mark.asyncio
async def test_server_call_list_pages(server):
    result = await server.call_tool("list_pages", {})
    assert result.is_error is False
    pages = result.structured_content.get("result", result.structured_content)
    assert isinstance(pages, list)
    assert len(pages) == 2
    assert pages[0]["display_name"] == "Executive Summary"


@pytest.mark.asyncio
async def test_server_call_query_report_layout(server):
    result = await server.call_tool("query_report_layout", {"jmespath_query": "pages[0].display_name"})
    assert result.is_error is False
    assert len(result.content) > 0
    assert result.content[0].text == "Executive Summary"


@pytest.mark.asyncio
async def test_server_call_validate(server):
    result = await server.call_tool("validate_pbip_structure", {})
    assert result.is_error is False
    res_dict = result.structured_content.get("result", result.structured_content)
    assert res_dict["is_valid"] is True
