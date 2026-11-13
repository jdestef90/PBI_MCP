from __future__ import annotations

import hashlib
from pathlib import Path

from .models import WorkspaceInfo
from .pbip_references import canonical_path_key, normalize_platform_path, read_pbip_references


def compute_workspace_id(path: Path | str) -> str:
    """Compute a deterministic workspace ID from the canonical path key."""
    key = canonical_path_key(path)
    return "pbip-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def discover_workspace(path: Path | str) -> WorkspaceInfo:
    resolved_input = normalize_platform_path(path)
    root = resolved_input.parent if resolved_input.is_file() else resolved_input
    pbip = (
        resolved_input
        if resolved_input.suffix.lower() == ".pbip"
        else next(iter(sorted(root.glob("*.pbip"), key=lambda p: p.name.casefold())), None)
        if root.exists() and root.is_dir()
        else None
    )
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

    if root.exists() and root.is_dir():
        semantic = semantic or next(
            iter(sorted(root.glob("*.SemanticModel"), key=lambda p: p.name.casefold())),
            None,
        )
        report = report or next(
            iter(sorted(root.glob("*.Report"), key=lambda p: p.name.casefold())),
            None,
        )
    if root.name.endswith(".SemanticModel"):
        semantic = root
    if root.name.endswith(".Report"):
        report = root

    ws_id = compute_workspace_id(pbip if pbip else root)
    return WorkspaceInfo(
        root=root,
        pbip_file=pbip,
        semantic_model_dir=semantic,
        report_dir=report,
        workspace_id=ws_id,
    )


def relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")
