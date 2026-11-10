from __future__ import annotations

import json
from pathlib import Path

import pytest

from pbi_mcp_server.pbip_references import read_pbip_references
from pbi_mcp_server.workspace_registry import WorkspaceRegistry


def make_pbip(root: Path, name: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    pbip = folder / f"{name}.pbip"
    pbip.write_text("{}", encoding="utf-8")
    return pbip


def test_pbip_explicit_references(tmp_path: Path) -> None:
    model = tmp_path / "Different.SemanticModel"
    report = tmp_path / "Different.Report"
    model.mkdir()
    report.mkdir()
    pbip = tmp_path / "Workspace.pbip"
    pbip.write_text(
        json.dumps({"artifacts": {"semanticModel": "Different.SemanticModel", "report": "Different.Report"}}),
        encoding="utf-8",
    )
    result = read_pbip_references(pbip)
    assert result["semantic_model_dir"] == str(model.resolve())
    assert result["report_dir"] == str(report.resolve())


def test_discovers_pbip_files(tmp_path: Path) -> None:
    interactive = tmp_path / "interactive"
    product = tmp_path / "product"
    interactive.mkdir()
    product.mkdir()

    (interactive / "Report A.pbip").write_text("{}", encoding="utf-8")
    (product / "Report B.pbip").write_text("{}", encoding="utf-8")

    registry = WorkspaceRegistry([interactive, product])
    workspaces = registry.list_workspaces()

    assert len(workspaces) == 2
    assert {item["name"] for item in workspaces} == {"Report A", "Report B"}


def test_resolves_workspace_by_name_and_id(tmp_path: Path) -> None:
    make_pbip(tmp_path, "Feature Enablement Dashboard")
    registry = WorkspaceRegistry([tmp_path])
    item = registry.list_workspaces()[0]
    assert registry.get_workspace(item["workspace_id"])["name"] == "Feature Enablement Dashboard"
    assert registry.get_workspace("Feature Enablement Dashboard")["workspace_id"] == item["workspace_id"]


def test_list_workspaces_with_query(tmp_path: Path) -> None:
    make_pbip(tmp_path, "Feature Enablement Dashboard")
    make_pbip(tmp_path, "Basic Work")
    registry = WorkspaceRegistry([tmp_path])
    matches = registry.list_workspaces(query="ENABLEMENT")
    assert [x["name"] for x in matches] == ["Feature Enablement Dashboard"]


def test_rejects_workspace_outside_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()

    pbip = outside / "Secret.pbip"
    pbip.write_text("{}", encoding="utf-8")

    registry = WorkspaceRegistry([allowed])
    with pytest.raises(PermissionError):
        registry.resolve_workspace(str(pbip))


def test_deep_discovery_ids_and_duplicates(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    for base, depth in [(a, 1), (a, 4), (b, 3)]:
        d = base.joinpath(*(["nested"] * depth))
        d.mkdir(parents=True, exist_ok=True)
        (d / "Same.pbip").write_text("{}", encoding="utf-8")
    registry = WorkspaceRegistry([a, b])
    items = registry.list_workspaces()
    assert len(items) == 3
    assert len({x["workspace_id"] for x in items}) == 3
    assert all(x.get("duplicate_name") for x in items)
    with pytest.raises(ValueError, match="ambiguous"):
        registry.get_workspace("Same")
    assert registry.resolve_workspace(items[0]["workspace_id"]).suffix == ".pbip"


def test_refresh_finds_new_workspace(tmp_path: Path) -> None:
    root = tmp_path / "reports"
    root.mkdir()
    registry = WorkspaceRegistry([root])
    assert registry.list_workspaces() == []
    (root / "New.pbip").write_text("{}", encoding="utf-8")
    assert len(registry.refresh()) == 1
