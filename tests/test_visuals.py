from __future__ import annotations

import shutil
from pathlib import Path

from pbi_mcp_server.service import PbipService


def clone_sample_workspace(tmp_path: Path) -> Path:
    repo_root = Path(__file__).parents[1]
    shutil.copy2(repo_root / "sample_project.pbip", tmp_path / "sample_project.pbip")
    shutil.copytree(repo_root / "sample_project.Report", tmp_path / "sample_project.Report")
    shutil.copytree(repo_root / "sample_project.SemanticModel", tmp_path / "sample_project.SemanticModel")
    return tmp_path / "sample_project.pbip"


def test_list_pages_and_visuals_by_title(tmp_path: Path) -> None:
    pbip = clone_sample_workspace(tmp_path)
    svc = PbipService(pbip)

    pages = svc.pages()
    assert len(pages) == 2
    assert pages[0]["display_name"] == "Executive Summary"

    visuals = svc.visuals(page="Executive Summary")
    assert len(visuals) == 3

    # Lookup by title ("Total Revenue" card)
    card = svc.get_visual("Total Revenue")
    assert card is not None
    assert card["visual_id"] == "vis_card_revenue"
    assert card["visual_type"] == "card"
    assert card["role_bindings"] == {"Values": ["Sales.Total Revenue"]}


def test_update_visual_fields_add_and_remove(tmp_path: Path) -> None:
    pbip = clone_sample_workspace(tmp_path)
    svc = PbipService(pbip)

    # Dry run adding Sales[Total Cost] to bar chart
    preview = svc.update_visual_fields(
        "vis_bar_revenue_region",
        add_fields=["Sales[Total Cost]"],
        dry_run=True,
    )
    assert preview["dry_run"] is True
    assert len(preview["changes"]) == 1
    bar_before = svc.get_visual("vis_bar_revenue_region")
    assert bar_before is not None
    assert "Sales.Total Cost" not in bar_before["bound_fields"]

    # Execute adding Sales[Total Cost] and removing Sales[Total Revenue]
    executed = svc.update_visual_fields(
        "vis_bar_revenue_region",
        add_fields=["Sales[Total Cost]"],
        remove_fields=["Sales[Total Revenue]"],
        dry_run=False,
    )
    assert executed["dry_run"] is False
    assert executed["rolled_back"] is False

    bar_after = svc.get_visual("vis_bar_revenue_region")
    assert bar_after is not None
    assert "Sales.Total Cost" in bar_after["bound_fields"]
    assert "Sales.Total Revenue" not in bar_after["bound_fields"]
    assert bar_after["role_bindings"]["Category"] == ["Customer.Region"]
    assert bar_after["role_bindings"]["Y"] == ["Sales.Total Cost"]
