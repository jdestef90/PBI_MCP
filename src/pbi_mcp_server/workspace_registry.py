from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .errors import (
    AmbiguousWorkspaceError,
    InvalidArgumentError,
    WorkspaceNotFoundError,
    WorkspacePermissionError,
)
from .models import ScanDiagnostics
from .pbip_references import canonical_path_key, normalize_platform_path, read_pbip_references
from .service import PbipService
from .workspace import compute_workspace_id, discover_workspace

_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


class WorkspaceList(list[dict[str, Any]]):
    """List of workspace records carrying scan diagnostics metadata."""

    def __init__(self, items: list[dict[str, Any]], diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(items)
        self.diagnostics: dict[str, Any] = diagnostics or {}

    def to_report(self, roots: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {
            "workspaces": list(self),
            "workspace_count": len(self),
            "roots": roots or [],
            "diagnostics": self.diagnostics,
        }


class WorkspaceRegistry:
    def __init__(self, roots: list[Path | str], create_backups: bool = True) -> None:
        if not roots:
            raise InvalidArgumentError(
                "At least one workspace root is required.",
                code="INVALID_ARGUMENT",
            )
        self.roots: list[Path] = []
        for raw_root in roots:
            resolved = normalize_platform_path(raw_root)
            if not resolved.exists():
                raise WorkspaceNotFoundError(
                    f"Workspace root does not exist: {resolved}",
                    code="WORKSPACE_NOT_FOUND",
                    details={"root": str(resolved)},
                )
            if not resolved.is_dir():
                raise NotADirectoryError(f"Workspace root is not a directory: {resolved}")
            self.roots.append(resolved)

        self.create_backups = create_backups
        self._workspaces: list[dict[str, Any]] = []
        self._diagnostics = ScanDiagnostics(roots_scanned=[str(r) for r in self.roots])
        self.refresh()

    @property
    def diagnostics(self) -> dict[str, Any]:
        return self._diagnostics.model_dump()

    def _workspace_id(self, path: Path) -> str:
        return compute_workspace_id(path)

    def refresh(self) -> WorkspaceList:
        found: list[dict[str, Any]] = []
        seen_keys: dict[str, str] = {}
        claimed_semantic_dirs: set[str] = set()
        claimed_report_dirs: set[str] = set()
        orphan_semantic_candidates: list[tuple[Path, Path]] = []
        orphan_report_candidates: list[tuple[Path, Path]] = []
        ignored_candidates: list[dict[str, Any]] = []
        inaccessible_paths: list[dict[str, Any]] = []
        partial_projects: list[dict[str, Any]] = []
        nested_projects: list[dict[str, Any]] = []

        # Sort roots deterministically by canonical key
        sorted_roots = sorted(self.roots, key=canonical_path_key)

        for search_root in sorted_roots:
            pbip_candidates: list[Path] = []

            def _on_walk_error(err: OSError) -> None:
                bad_path = getattr(err, "filename", None) or str(search_root)
                inaccessible_paths.append({
                    "path": str(bad_path),
                    "search_root": str(search_root),
                    "reason": str(err.strerror or err),
                })

            for dirpath, dirnames, filenames in os.walk(search_root, topdown=True, onerror=_on_walk_error):
                current_dir = Path(dirpath)

                # Check dirnames in-place for ignored directories or inaccessible subdirectories
                kept_dirs: list[str] = []
                for dname in sorted(dirnames, key=str.casefold):
                    dpath = current_dir / dname
                    if dname in _IGNORED_DIRS:
                        # Check if any .pbip sits directly inside this ignored directory tree
                        try:
                            for ignored_pbip in dpath.rglob("*.pbip"):
                                ignored_candidates.append({
                                    "path": str(ignored_pbip),
                                    "reason": f"inside ignored directory {dname!r}",
                                })
                        except OSError:
                            pass
                        continue

                    # Check read/execute access on directory
                    if not os.access(dpath, os.R_OK | os.X_OK):
                        inaccessible_paths.append({
                            "path": str(dpath),
                            "search_root": str(search_root),
                            "reason": "Permission denied",
                        })
                        continue

                    if dname.casefold().endswith(".semanticmodel"):
                        orphan_semantic_candidates.append((dpath.resolve(), search_root))
                        # Don't need to descend into .SemanticModel looking for .pbip files
                        continue
                    if dname.casefold().endswith(".report"):
                        orphan_report_candidates.append((dpath.resolve(), search_root))
                        continue

                    kept_dirs.append(dname)
                dirnames[:] = kept_dirs

                for fname in sorted(filenames, key=str.casefold):
                    fpath = current_dir / fname
                    low_name = fname.casefold()
                    if low_name.endswith(".pbip"):
                        pbip_candidates.append(fpath)
                    elif ".pbip." in low_name:
                        ignored_candidates.append({
                            "path": str(fpath),
                            "reason": "backup or temporary .pbip file",
                        })

            for raw_pbip in sorted(pbip_candidates, key=canonical_path_key):
                try:
                    pbip = raw_pbip.resolve()
                except OSError as exc:
                    inaccessible_paths.append({
                        "path": str(raw_pbip),
                        "search_root": str(search_root),
                        "reason": str(exc),
                    })
                    continue

                if not self._is_allowed(pbip):
                    ignored_candidates.append({
                        "path": str(pbip),
                        "reason": "outside configured workspace roots",
                    })
                    continue

                key = canonical_path_key(pbip)
                if key in seen_keys:
                    ignored_candidates.append({
                        "path": str(pbip),
                        "reason": f"duplicate path from overlapping root (already discovered via {seen_keys[key]})",
                    })
                    continue
                seen_keys[key] = str(search_root)

                heuristic = discover_workspace(pbip)
                refs = read_pbip_references(pbip)
                semantic = (
                    Path(refs["semantic_model_dir"])
                    if refs["semantic_model_dir"]
                    else heuristic.semantic_model_dir
                )
                report = (
                    Path(refs["report_dir"])
                    if refs["report_dir"]
                    else heuristic.report_dir
                )
                source = "pbip" if refs["semantic_model_dir"] or refs["report_dir"] else "heuristic"
                warnings = list(refs["warnings"])
                if semantic and semantic.stem.casefold() != pbip.stem.casefold():
                    warnings.append(
                        f"PBIP name differs from semantic model name: {pbip.stem!r} vs {semantic.stem!r}."
                    )
                if report and report.stem.casefold() != pbip.stem.casefold():
                    warnings.append(
                        f"PBIP name differs from report name: {pbip.stem!r} vs {report.stem!r}."
                    )

                if semantic:
                    claimed_semantic_dirs.add(canonical_path_key(semantic))
                if report:
                    claimed_report_dirs.add(canonical_path_key(report))

                issues: list[str] = []
                if refs["reference_source"] == "unreadable":
                    issues.append("unreadable .pbip JSON")
                if not semantic:
                    issues.append("semantic model missing")
                elif not (semantic / "definition").exists():
                    warnings.append("semantic model definition directory missing")
                if not report:
                    issues.append("report missing")

                ws_id = self._workspace_id(pbip)
                is_valid = bool(semantic) and refs["reference_source"] != "unreadable"

                entry: dict[str, Any] = {
                    "workspace_id": ws_id,
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
                    "valid": is_valid,
                    "issues": issues,
                    "warnings": warnings,
                }
                if issues:
                    partial_projects.append({
                        "workspace_id": ws_id,
                        "name": pbip.stem,
                        "path": str(pbip),
                        "issues": list(issues),
                    })
                found.append(entry)

        # Detect orphan .SemanticModel and .Report folders without a .pbip file
        seen_orphans: set[str] = set()
        for sem_dir, s_root in orphan_semantic_candidates:
            s_key = canonical_path_key(sem_dir)
            if s_key not in claimed_semantic_dirs and s_key not in seen_orphans:
                seen_orphans.add(s_key)
                partial_projects.append({
                    "workspace_id": None,
                    "name": sem_dir.stem,
                    "path": str(sem_dir),
                    "search_root": str(s_root),
                    "issues": ["orphan .SemanticModel directory without .pbip entry point"],
                })
        for rep_dir, s_root in orphan_report_candidates:
            r_key = canonical_path_key(rep_dir)
            if r_key not in claimed_report_dirs and r_key not in seen_orphans:
                seen_orphans.add(r_key)
                partial_projects.append({
                    "workspace_id": None,
                    "name": rep_dir.stem,
                    "path": str(rep_dir),
                    "search_root": str(s_root),
                    "issues": ["orphan .Report directory without .pbip entry point"],
                })

        # Sort workspaces deterministically independent of root discovery order
        found.sort(key=lambda x: (x["name"].casefold(), canonical_path_key(x["pbip_file"])))

        # Detect duplicate names
        duplicate_names: list[str] = []
        for name_cf in sorted({x["name"].casefold() for x in found}):
            group = [x for x in found if x["name"].casefold() == name_cf]
            if len(group) > 1:
                duplicate_names.append(group[0]["name"])
                for x in group:
                    x["duplicate_name"] = True

        # Detect nested projects (one workspace's root inside another workspace's root)
        for child in found:
            child_root = Path(child["root"])
            for parent in found:
                if child["workspace_id"] == parent["workspace_id"]:
                    continue
                parent_root = Path(parent["root"])
                if child_root != parent_root and _within(child_root, parent_root):
                    child["nested_under"] = parent["workspace_id"]
                    nested_projects.append({
                        "child_workspace_id": child["workspace_id"],
                        "child_path": child["pbip_file"],
                        "parent_workspace_id": parent["workspace_id"],
                        "parent_path": parent["pbip_file"],
                    })

        self._workspaces = found
        self._diagnostics = ScanDiagnostics(
            roots_scanned=[str(r) for r in sorted_roots],
            valid_count=sum(1 for x in found if x["valid"]),
            invalid_count=sum(1 for x in found if not x["valid"]),
            duplicate_names=duplicate_names,
            partial_projects=partial_projects,
            ignored_candidates=ignored_candidates,
            inaccessible_paths=inaccessible_paths,
            nested_projects=nested_projects,
        )
        return WorkspaceList(self.list_workspaces(), diagnostics=self.diagnostics)

    def refresh_with_diagnostics(self) -> dict[str, Any]:
        ws_list = self.refresh()
        return ws_list.to_report(roots=self.list_roots())

    def list_workspaces(
        self,
        query: str | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        items = self.find_workspaces(query) if query else [dict(x) for x in self._workspaces]
        if offset > 0:
            items = items[offset:]
        if limit is not None and limit >= 0:
            items = items[:limit]
        return items

    def list_roots(self) -> list[dict[str, Any]]:
        return [
            {
                "root": str(r),
                "exists": r.exists(),
                "workspace_count": sum(
                    canonical_path_key(x["search_root"]) == canonical_path_key(r)
                    for x in self._workspaces
                ),
            }
            for r in self.roots
        ]

    def get_workspace(self, workspace: str) -> dict[str, Any]:
        path = self.resolve_workspace(workspace)
        key = canonical_path_key(path)
        return next(dict(x) for x in self._workspaces if canonical_path_key(x["pbip_file"]) == key)

    def find_workspaces(self, query: str) -> list[dict[str, Any]]:
        q = query.strip().casefold()
        if not q:
            raise InvalidArgumentError("query must not be empty.", code="INVALID_ARGUMENT")
        fields = (
            "workspace_id",
            "name",
            "pbip_file",
            "search_root",
            "root",
            "semantic_model_dir",
            "report_dir",
        )
        return [
            {
                "workspace_id": x["workspace_id"],
                "name": x["name"],
                "workspace": x["workspace"],
                "pbip_file": x["pbip_file"],
                "path": x["path"],
                "search_root": x["search_root"],
                "root": x["root"],
                "semantic_model_dir": x["semantic_model_dir"],
                "report_dir": x["report_dir"],
                "valid": x["valid"],
                "duplicate_name": x.get("duplicate_name", False),
                "issues": list(x["issues"]),
                "warnings": list(x["warnings"]),
            }
            for x in self._workspaces
            if q in "\n".join(str(x.get(f) or "") for f in fields).casefold()
        ]

    def resolve_workspace(self, workspace: str) -> Path:
        raw = workspace.strip()
        if not raw:
            raise InvalidArgumentError("workspace identifier must not be empty.", code="INVALID_ARGUMENT")

        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            resolved = normalize_platform_path(candidate)
            if not resolved.exists():
                raise WorkspaceNotFoundError(
                    f"PBIP workspace does not exist: {resolved}",
                    code="WORKSPACE_NOT_FOUND",
                    details={"workspace": workspace},
                )
            if resolved.is_dir():
                nested_pbips = sorted(resolved.glob("*.pbip"), key=lambda p: p.name.casefold())
                if len(nested_pbips) == 1:
                    resolved = nested_pbips[0].resolve()
                elif len(nested_pbips) > 1:
                    raise AmbiguousWorkspaceError(
                        f"Directory {resolved} contains multiple .pbip files; specify exact .pbip file.",
                        code="AMBIGUOUS_WORKSPACE",
                        details={"candidates": [str(p) for p in nested_pbips]},
                    )
                else:
                    raise InvalidArgumentError(
                        f"Workspace must be a .pbip file: {resolved}",
                        code="INVALID_ARGUMENT",
                    )
            if resolved.suffix.lower() != ".pbip":
                raise InvalidArgumentError(
                    f"Workspace must be a .pbip file: {resolved}",
                    code="INVALID_ARGUMENT",
                )
            if not self._is_allowed(resolved):
                raise WorkspacePermissionError(
                    f"Workspace is outside configured roots: {resolved}",
                    code="PERMISSION_DENIED",
                    details={"workspace": str(resolved), "roots": [str(r) for r in self.roots]},
                )
            return resolved

        q = raw.casefold()
        # 1. Exact match on workspace_id
        id_matches = [
            Path(x["pbip_file"])
            for x in self._workspaces
            if x["workspace_id"].casefold() == q
        ]
        if len(id_matches) == 1:
            return id_matches[0]

        # 2. Exact match on name or filename
        exact_matches = [
            x
            for x in self._workspaces
            if q in {x["name"].casefold(), Path(x["pbip_file"]).name.casefold()}
        ]
        if len(exact_matches) == 1:
            return Path(exact_matches[0]["pbip_file"])
        if len(exact_matches) > 1:
            candidates = [
                {"workspace_id": x["workspace_id"], "name": x["name"], "pbip_file": x["pbip_file"]}
                for x in exact_matches
            ]
            raise AmbiguousWorkspaceError(
                f"Workspace {workspace!r} is ambiguous. Use workspace_id or full path:\n"
                + "\n".join(f"{x['workspace_id']}: {x['pbip_file']}" for x in exact_matches),
                code="AMBIGUOUS_WORKSPACE",
                details={"workspace": workspace, "candidates": candidates},
                remediation="Pass one of the candidate workspace_id values returned by list_workspaces.",
            )

        # 3. Partial match fallback (only if unambiguous)
        partial_matches = [
            x
            for x in self._workspaces
            if q in x["name"].casefold() or q in x["pbip_file"].casefold()
        ]
        if len(partial_matches) == 1:
            return Path(partial_matches[0]["pbip_file"])
        if len(partial_matches) > 1:
            candidates = [
                {"workspace_id": x["workspace_id"], "name": x["name"], "pbip_file": x["pbip_file"]}
                for x in partial_matches
            ]
            raise AmbiguousWorkspaceError(
                f"Workspace query {workspace!r} is ambiguous and matched {len(partial_matches)} workspaces:\n"
                + "\n".join(f"{x['workspace_id']}: {x['pbip_file']}" for x in partial_matches),
                code="AMBIGUOUS_WORKSPACE",
                details={"workspace": workspace, "candidates": candidates},
                remediation="Pass an exact workspace_id from list_workspaces.",
            )

        raise WorkspaceNotFoundError(
            f"No PBIP workspace matching {workspace!r} was found. Call list_workspaces or refresh_workspaces.",
            code="WORKSPACE_NOT_FOUND",
            details={"workspace": workspace},
            remediation="Call list_workspaces() or refresh_workspaces() to see available workspace IDs.",
        )

    def get_service(self, workspace: str) -> PbipService:
        pbip_path = self.resolve_workspace(workspace)
        return PbipService(pbip_path, create_backups=self.create_backups)

    def _is_allowed(self, path: Path) -> bool:
        return any(_within(path.resolve(), root) for root in self.roots)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
