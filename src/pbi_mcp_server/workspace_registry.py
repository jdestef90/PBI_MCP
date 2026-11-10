from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .pbip_references import read_pbip_references
from .service import PbipService
from .workspace import discover_workspace


class WorkspaceRegistry:
    def __init__(self, roots: list[Path], create_backups: bool = True) -> None:
        if not roots:
            raise ValueError("At least one workspace root is required.")
        self.roots = [root.expanduser().resolve() for root in roots]
        self.create_backups = create_backups
        self._workspaces: list[dict[str, Any]] = []
        for root in self.roots:
            if not root.exists():
                raise FileNotFoundError(f"Workspace root does not exist: {root}")
            if not root.is_dir():
                raise NotADirectoryError(f"Workspace root is not a directory: {root}")
        self.refresh()

    def _workspace_id(self, path: Path) -> str:
        return "pbip-" + hashlib.sha256(str(path.resolve()).casefold().encode()).hexdigest()[:16]

    def refresh(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        seen: set[Path] = set()
        for search_root in self.roots:
            for pbip in sorted(search_root.rglob("*.pbip"), key=lambda p: str(p).casefold()):
                pbip = pbip.resolve()
                if pbip in seen or not self._is_allowed(pbip):
                    continue
                seen.add(pbip)
                heuristic = discover_workspace(pbip)
                refs = read_pbip_references(pbip)
                semantic = Path(refs["semantic_model_dir"]) if refs["semantic_model_dir"] else heuristic.semantic_model_dir
                report = Path(refs["report_dir"]) if refs["report_dir"] else heuristic.report_dir
                source = "pbip" if refs["semantic_model_dir"] or refs["report_dir"] else "heuristic"
                warnings = list(refs["warnings"])
                if semantic and semantic.stem.casefold() != pbip.stem.casefold():
                    warnings.append(f"PBIP name differs from semantic model name: {pbip.stem!r} vs {semantic.stem!r}.")
                if report and report.stem.casefold() != pbip.stem.casefold():
                    warnings.append(f"PBIP name differs from report name: {pbip.stem!r} vs {report.stem!r}.")
                issues = []
                if not semantic:
                    issues.append("semantic model missing")
                if not report:
                    issues.append("report missing")
                found.append({
                    "workspace_id": self._workspace_id(pbip),
                    "name": pbip.stem,
                    "workspace": pbip.stem,
                    "pbip_file": str(pbip),
                    "path": str(pbip),
                    "search_root": str(search_root),
                    "root": str(heuristic.root),
                    "semantic_model_dir": str(semantic) if semantic else None,
                    "report_dir": str(report) if report else None,
                    "association_source": source,
                    "pbip_references": refs["raw_references"],
                    "valid": bool(semantic),
                    "issues": issues,
                    "warnings": warnings,
                })
        for name in {x["name"].casefold() for x in found}:
            group = [x for x in found if x["name"].casefold() == name]
            if len(group) > 1:
                for x in group:
                    x["duplicate_name"] = True
        self._workspaces = found
        return self.list_workspaces()

    def list_workspaces(self, query: str | None = None) -> list[dict[str, Any]]:
        if query:
            return self.find_workspaces(query)
        return [dict(x) for x in self._workspaces]

    def list_roots(self) -> list[dict[str, Any]]:
        return [
            {
                "root": str(r),
                "exists": r.exists(),
                "workspace_count": sum(x["search_root"] == str(r) for x in self._workspaces),
            }
            for r in self.roots
        ]

    def get_workspace(self, workspace: str) -> dict[str, Any]:
        path = self.resolve_workspace(workspace)
        return next(dict(x) for x in self._workspaces if Path(x["pbip_file"]) == path)

    def find_workspaces(self, query: str) -> list[dict[str, Any]]:
        q = query.strip().casefold()
        if not q:
            raise ValueError("query must not be empty.")
        fields = ("workspace_id", "name", "pbip_file", "search_root", "root", "semantic_model_dir", "report_dir")
        return [
            {
                "workspace_id": x["workspace_id"],
                "name": x["name"],
                "pbip_file": x["pbip_file"],
                "search_root": x["search_root"],
                "valid": x["valid"],
                "issues": list(x["issues"]),
                "warnings": list(x["warnings"]),
            }
            for x in self._workspaces
            if q in "\n".join(str(x.get(f) or "") for f in fields).casefold()
        ]

    def resolve_workspace(self, workspace: str) -> Path:
        candidate = Path(workspace).expanduser()
        if candidate.is_absolute():
            candidate = candidate.resolve()
            if not candidate.exists():
                raise FileNotFoundError(f"PBIP workspace does not exist: {candidate}")
            if candidate.suffix.lower() != ".pbip":
                raise ValueError(f"Workspace must be a .pbip file: {candidate}")
            if not self._is_allowed(candidate):
                raise PermissionError(f"Workspace is outside configured roots: {candidate}")
            return candidate
        q = workspace.casefold()
        matches = []
        for x in self._workspaces:
            p = Path(x["pbip_file"])
            if q in {x["workspace_id"].casefold(), x["name"].casefold(), p.name.casefold()}:
                matches.append(p)
        if not matches:
            raise FileNotFoundError(
                f"No PBIP workspace matching {workspace!r} was found. Call list_workspaces or refresh_workspaces."
            )
        if len(matches) > 1:
            raise ValueError(
                f"Workspace {workspace!r} is ambiguous. Use workspace_id or full path:\n"
                + "\n".join(map(str, matches))
            )
        return matches[0]

    def get_service(self, workspace: str) -> PbipService:
        return PbipService(self.resolve_workspace(workspace), create_backups=self.create_backups)

    def _is_allowed(self, path: Path) -> bool:
        return any(_within(path.resolve(), root) for root in self.roots)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
