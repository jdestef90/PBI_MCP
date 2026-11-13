"""Phase 1 tests: Bulletproof Workspace Discovery & Path Normalization."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pbi_mcp_server.errors import AmbiguousWorkspaceError, WorkspaceNotFoundError
from pbi_mcp_server.pbip_references import (
    canonical_path_key,
    normalize_path_str,
    read_pbip_references,
)
from pbi_mcp_server.workspace import compute_workspace_id, discover_workspace
from pbi_mcp_server.workspace_registry import WorkspaceRegistry

REPO_ROOT = Path(__file__).parents[1]


def copy_sample_project(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "sample_project.pbip", dest / "sample_project.pbip")
    shutil.copytree(REPO_ROOT / "sample_project.Report", dest / "sample_project.Report")
    shutil.copytree(REPO_ROOT / "sample_project.SemanticModel", dest / "sample_project.SemanticModel")
    return dest / "sample_project.pbip"


def test_path_normalization_windows_unc_onedrive_and_spaces() -> None:
    # Extended-length UNC prefix and backslashes
    assert normalize_path_str(r"\\?\UNC\server\share\Power BI\Sales Report.pbip").startswith(
        "//server/share/Power BI/Sales Report.pbip"
    )
    # Extended-length drive prefix
    assert normalize_path_str(r"\\?\C:\Users\Alice\OneDrive - Contoso\Reports\Sales.pbip") == (
        "C:/Users/Alice/OneDrive - Contoso/Reports/Sales.pbip"
    )
    # Canonical key is casefolded and deterministic
    key_a = canonical_path_key(r"C:\Users\Alice\OneDrive - Contoso\Reports\Sales.pbip")
    key_b = canonical_path_key("c:/users/alice/onedrive - contoso/reports/sales.pbip")
    assert key_a == key_b


def test_deterministic_workspace_id_stability(tmp_path: Path) -> None:
    proj = tmp_path / "My Project With Spaces"
    pbip = copy_sample_project(proj)

    ws_id_1 = compute_workspace_id(proj, pbip)
    ws_id_2 = compute_workspace_id(proj, pbip)
    assert ws_id_1 == ws_id_2
    assert ws_id_1.startswith("pbip-")


def test_read_pbip_references_resolves_definition_pbir(tmp_path: Path) -> None:
    # Create a PBIP file that only declares the report artifact, while definition.pbir links to the model
    root = tmp_path / "pbir_linked"
    report_dir = root / "Linked.Report"
    model_dir = root / "Linked.SemanticModel"
    (model_dir / "definition" / "tables").mkdir(parents=True)
    report_dir.mkdir(parents=True)

    pbip_file = root / "Linked.pbip"
    pbip_file.write_text(
        json.dumps({"artifacts": [{"report": {"path": "Linked.Report"}}]}),
        encoding="utf-8",
    )
    pbir_file = report_dir / "definition.pbir"
    pbir_file.write_text(
        json.dumps(
            {
                "version": "4.0",
                "datasetReference": {
                    "byPath": {"path": "../Linked.SemanticModel"}
                },
            }
        ),
        encoding="utf-8",
    )

    refs = read_pbip_references(pbip_file)
    assert Path(refs["report_dir"]) == report_dir.resolve()
    assert Path(refs["semantic_model_dir"]) == model_dir.resolve()

    info = discover_workspace(pbip_file)
    assert info.semantic_model_dir == model_dir.resolve()
    assert info.report_dir == report_dir.resolve()


def test_registry_handles_orphan_partial_nested_and_ignored_dirs(tmp_path: Path) -> None:
    # 1. Valid project
    valid_dir = tmp_path / "valid_proj"
    copy_sample_project(valid_dir)

    # 2. Nested project inside valid_dir
    nested_dir = valid_dir / "sub_projects" / "nested_proj"
    copy_sample_project(nested_dir)

    # 3. Orphan .SemanticModel without .pbip
    orphan_model = tmp_path / "orphan_area" / "Standalone.SemanticModel"
    (orphan_model / "definition" / "tables").mkdir(parents=True)
    (orphan_model / "definition" / "model.tmdl").write_text("model Model\n", encoding="utf-8")

    # 4. Partial broken .pbip referencing missing SemanticModel
    broken_dir = tmp_path / "broken_proj"
    broken_dir.mkdir(parents=True)
    (broken_dir / "broken.pbip").write_text(
        json.dumps({"artifacts": [{"semanticModel": {"path": "Missing.SemanticModel"}}]}),
        encoding="utf-8",
    )

    # 5. Ignored cache directory (.git / node_modules) containing a .pbip that should be skipped
    ignored_dir = tmp_path / "node_modules" / "cached_proj"
    copy_sample_project(ignored_dir)

    reg = WorkspaceRegistry([tmp_path])
    workspaces = reg.list_workspaces()

    # Valid project, nested project,orphan model, and broken project are indexed; node_modules is ignored
    paths = [w["root"] for w in workspaces]
    assert str(valid_dir.resolve()) in paths
    assert str(nested_dir.resolve()) in paths
    assert not any("node_modules" in p for p in paths)

    diag = reg.diagnostics
    assert diag["valid_count"] == 2  # valid_proj and nested_proj
    assert diag["invalid_count"] == 1  # broken_proj
    assert len(diag["partial_projects"]) >= 2  # broken_proj + orphan .SemanticModel
    assert any("orphan" in " ".join(p.get("issues", [])) for p in diag["partial_projects"])
    assert len(diag["nested_projects"]) >= 1
    assert any("node_modules" in ic["path"] for ic in diag["ignored_candidates"])


def test_registry_duplicate_names_overlapping_roots_and_pagination(tmp_path: Path) -> None:
    dir_a = tmp_path / "dept_a" / "finance"
    dir_b = tmp_path / "dept_b" / "finance"
    copy_sample_project(dir_a)
    copy_sample_project(dir_b)

    # Pass overlapping roots: tmp_path and tmp_path / "dept_a"
    reg = WorkspaceRegistry([tmp_path, tmp_path / "dept_a"])
    all_ws = reg.list_workspaces()
    assert len(all_ws) == 2

    # Pagination
    page_1 = reg.list_workspaces(limit=1, offset=0)
    page_2 = reg.list_workspaces(limit=1, offset=1)
    assert len(page_1) == 1
    assert len(page_2) == 1
    assert page_1[0]["workspace_id"] != page_2[0]["workspace_id"]

    # Ambiguous lookup by duplicate stem "sample_project" raises AmbiguousWorkspaceError (and ValueError)
    with pytest.raises(AmbiguousWorkspaceError) as exc_info:
        reg.get_workspace("sample_project")
    assert exc_info.value.code == "AMBIGUOUS_WORKSPACE"
    assert len(exc_info.value.details["candidates"]) == 2

    # Missing workspace raises WorkspaceNotFoundError (and FileNotFoundError)
    with pytest.raises(WorkspaceNotFoundError) as nf_info:
        reg.get_workspace("nonexistent_workspace_xyz")
    assert nf_info.value.code == "WORKSPACE_NOT_FOUND"
