from __future__ import annotations

from pathlib import Path

from .models import WorkspaceInfo
from .pbip_references import read_pbip_references


def discover_workspace(path: Path) -> WorkspaceInfo:
    root = path.parent if path.is_file() else path
    pbip = path if path.suffix.lower() == ".pbip" else next(root.glob("*.pbip"), None)
    semantic: Path | None = None
    report: Path | None = None

    if pbip and pbip.exists():
        refs = read_pbip_references(pbip)
        if refs["semantic_model_dir"]:
            semantic = Path(refs["semantic_model_dir"])
        if refs["report_dir"]:
            report = Path(refs["report_dir"])

        if not semantic:
            candidate = pbip.parent / f"{pbip.stem}.SemanticModel"
            if candidate.exists() and candidate.is_dir():
                semantic = candidate.resolve()
        if not report:
            candidate = pbip.parent / f"{pbip.stem}.Report"
            if candidate.exists() and candidate.is_dir():
                report = candidate.resolve()

    semantic = semantic or next(root.glob("*.SemanticModel"), None)
    report = report or next(root.glob("*.Report"), None)
    if root.name.endswith(".SemanticModel"):
        semantic = root
    if root.name.endswith(".Report"):
        report = root
    return WorkspaceInfo(root=root, pbip_file=pbip, semantic_model_dir=semantic, report_dir=report)


def relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
