"""Tests for measure creation (create_if_missing), calculation groups, RLS roles, relationship authoring, and MCP prompts/resources."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbi_mcp_server.server import create_server
from pbi_mcp_server.service import PbipService

REPO_ROOT = Path(__file__).parents[1]


def clone_workspace(tmp_path: Path) -> Path:
    dest = tmp_path / "proj"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "sample_project.pbip", dest / "sample_project.pbip")
    shutil.copytree(REPO_ROOT / "sample_project.Report", dest / "sample_project.Report")
    shutil.copytree(REPO_ROOT / "sample_project.SemanticModel", dest / "sample_project.SemanticModel")
    return dest / "sample_project.pbip"


def test_create_measure_if_missing_and_update_properties(tmp_path: Path) -> None:
    pbip = clone_workspace(tmp_path)
    svc = PbipService(pbip)

    # Create a brand new measure with formatString, displayFolder, and description
    res = svc.update_dax(
        "Average Order Value",
        "DIVIDE([Total Revenue], DISTINCTCOUNT('Sales'[OrderID]))",
        table="Sales",
        create_if_missing=True,
        format_string="$#,##0.00",
        display_folder="KPIs",
        description="Average revenue per distinct order",
        dry_run=False,
    )
    assert res["status"] == "applied"
    assert res["rolled_back"] is False

    created = svc.resolve_measure("Average Order Value", table="Sales")
    assert created["expression"] == "DIVIDE([Total Revenue], DISTINCTCOUNT('Sales'[OrderID]))"
    assert created["format_string"] == "$#,##0.00"
    assert created["display_folder"] == "KPIs"
    assert created["description"] == "Average revenue per distinct order"

    # Update format_string and display_folder on existing measure
    svc.update_dax(
        "Average Order Value",
        "DIVIDE([Total Revenue], DISTINCTCOUNT('Sales'[OrderID]))",
        table="Sales",
        format_string="$#,##0",
        display_folder="Executive KPIs",
        dry_run=False,
    )
    updated = svc.resolve_measure("Average Order Value", table="Sales")
    assert updated["format_string"] == "$#,##0"
    assert updated["display_folder"] == "Executive KPIs"


def test_calculation_groups_rls_roles_and_relationship_upsert(tmp_path: Path) -> None:
    pbip = clone_workspace(tmp_path)
    def_dir = pbip.parent / "sample_project.SemanticModel" / "definition"

    # 1. Create a Calculation Group table in tables/TimeIntelligence.tmdl
    calc_tmdl = def_dir / "tables" / "TimeIntelligence.tmdl"
    calc_tmdl.write_text(
        "table 'Time Intelligence'\n"
        "\tcalculationGroup\n"
        "\t\tcalculationItem YTD = TOTALYTD(SELECTEDMEASURE(), 'Sales'[OrderID])\n"
        "\t\t\tformatString: $#,##0\n"
        "\t\tcalculationItem YoY = ```\n"
        "\t\t\tVAR Curr = SELECTEDMEASURE()\n"
        "\t\t\tRETURN\n"
        "\t\t\t    Curr\n"
        "\t\t```\n"
        "\tcolumn 'Time Calc'\n"
        "\t\tdataType: string\n",
        encoding="utf-8",
    )

    # 2. Create an RLS role in roles/RegionalManager.tmdl
    roles_dir = def_dir / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)
    (roles_dir / "RegionalManager.tmdl").write_text(
        "role 'Regional Manager'\n"
        "\tmodelPermission: read\n"
        "\ttablePermission Customer = 'Customer'[Region] == \"North America\"\n",
        encoding="utf-8",
    )

    svc = PbipService(pbip)
    summary = svc.summary()
    assert summary["counts"]["calculation_groups"] == 1
    assert summary["counts"]["calculation_items"] == 2
    assert summary["counts"]["roles"] == 1

    ti_schema = svc.get_table("Time Intelligence")
    assert ti_schema is not None
    assert ti_schema["is_calculation_group"] is True
    assert ti_schema["calculation_item_count"] == 2

    roles = svc.objects(kind="role")
    assert len(roles) == 1
    assert roles[0]["name"] == "Regional Manager"
    assert roles[0]["model_permission"] == "read"
    assert "Customer" in (roles[0]["expression"] or "")

    # 3. Upsert relationship in relationships.tmdl
    rel_res = svc.upsert_relationship(
        "rel_Sales_Customer_Inactive",
        from_column="Sales.CustomerID",
        to_column="Customer.CustomerID",
        cross_filtering="oneDirection",
        is_active=False,
        create_if_missing=True,
        dry_run=False,
    )
    assert rel_res["status"] == "applied"
    rels = svc.objects(kind="relationship")
    inactive_rel = next(r for r in rels if r["name"] == "rel_Sales_Customer_Inactive")
    assert inactive_rel["is_active"] is False
    assert inactive_rel["cross_filtering"] == "oneDirection"


@pytest.mark.asyncio
async def test_mcp_prompts_and_resources_registered_with_20_tools(tmp_path: Path) -> None:
    pbip = clone_workspace(tmp_path)
    svc = PbipService(pbip)
    mcp = create_server(svc)

    tools = await mcp.list_tools()
    assert len(tools) == 20

    prompts = await mcp.list_prompts()
    prompt_names = {p.name for p in prompts}
    assert {"workflow_measure_update", "workflow_model_audit"}.issubset(prompt_names)

    resources = await mcp.list_resources()
    resource_uris = {str(r.uri) for r in resources}
    assert "pbip://workspaces" in resource_uris

    templates = await mcp.list_resource_templates()
    template_uris = {str(t.uri_template) for t in templates}
    assert "pbip://workspace/{workspace}/summary" in template_uris
    assert "pbip://workspace/{workspace}/bpa" in template_uris
