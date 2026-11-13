"""Standardized mutation pipeline with precondition checks, unified diffs, impact analysis, and transactional rollback."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .engine_a.writer import atomic_write, restore_backup
from .errors import PreconditionFailedError
from .integrity.validator import has_validation_errors, validate
from .models import ChangePlan, FileChange, MutationDiff, WorkspaceInfo
from .workspace import relative


@dataclass
class PendingFileEdit:
    path: Path
    original_text: str
    updated_text: str
    replacements: int
    reason: str


def build_mutation_diff(
    rel_file: str,
    original_text: str,
    updated_text: str,
    context_lines: int = 3,
) -> MutationDiff:
    orig_lines = original_text.splitlines(keepends=True)
    new_lines = updated_text.splitlines(keepends=True)
    diff_iter = difflib.unified_diff(
        orig_lines,
        new_lines,
        fromfile=f"a/{rel_file}",
        tofile=f"b/{rel_file}",
        n=context_lines,
    )
    diff_text = "".join(diff_iter)
    lines_added = 0
    lines_removed = 0
    for dline in diff_text.splitlines():
        if dline.startswith("+++") or dline.startswith("---"):
            continue
        if dline.startswith("+"):
            lines_added += 1
        elif dline.startswith("-"):
            lines_removed += 1
    return MutationDiff(
        file=rel_file,
        unified_diff=diff_text,
        lines_added=lines_added,
        lines_removed=lines_removed,
    )


def verify_expression_precondition(
    *,
    object_name: str,
    actual_expression: str | None,
    expected_expression: str | None,
) -> None:
    if expected_expression is None:
        return
    actual_norm = (actual_expression or "").strip()
    expected_norm = expected_expression.strip()
    if actual_norm != expected_norm:
        raise PreconditionFailedError(
            f"Precondition failed for {object_name!r}: expected current expression "
            f"{expected_norm!r}, but found {actual_norm!r}.",
            expected=expected_norm,
            actual=actual_norm,
        )


def verify_replacements_precondition(
    *,
    operation: str,
    actual_replacements: int,
    expected_replacements: int | None,
) -> None:
    if expected_replacements is None:
        return
    if actual_replacements != expected_replacements:
        raise PreconditionFailedError(
            f"Precondition failed for {operation}: expected {expected_replacements} "
            f"replacement(s), but matched {actual_replacements}.",
            expected=expected_replacements,
            actual=actual_replacements,
        )


def execute_mutation_pipeline(
    info: WorkspaceInfo,
    *,
    operation: str,
    target_object: str | None,
    edits: list[PendingFileEdit],
    dry_run: bool,
    create_backups: bool = True,
    expected_replacements: int | None = None,
    impact_summary: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    workflow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute the standardized mutation pipeline:
    1. Precondition check on total replacements
    2. Build ChangePlan with FileChanges and unified MutationDiffs
    3. If dry_run=True, return ChangePlan without touching disk
    4. If dry_run=False, apply edits atomically with backups, run post-write validation,
       and roll back all modified files if any validation error occurs.
    """
    total_replacements = sum(e.replacements for e in edits)
    verify_replacements_precondition(
        operation=operation,
        actual_replacements=total_replacements,
        expected_replacements=expected_replacements,
    )

    plan = ChangePlan(
        operation=operation,
        dry_run=dry_run,
        target_object=target_object,
        replacement_count=total_replacements,
        impact_summary=impact_summary,
        warnings=list(warnings or []),
        workflow=workflow,
    )

    changed_files_set: list[str] = []
    modified_paths: list[Path] = []
    in_memory_backups: dict[Path, str] = {}

    for edit in edits:
        if edit.replacements <= 0 and edit.updated_text == edit.original_text:
            continue
        rel_file = relative(edit.path, info.root)
        plan.changes.append(
            FileChange(
                file=rel_file,
                replacements=edit.replacements,
                reason=edit.reason,
            )
        )
        if rel_file not in changed_files_set:
            changed_files_set.append(rel_file)
        if edit.updated_text != edit.original_text:
            # Merge diffs per file if multiple edits targeted the same file
            existing_diff = next((d for d in plan.diffs if d.file == rel_file), None)
            if existing_diff is None:
                plan.diffs.append(
                    build_mutation_diff(rel_file, edit.original_text, edit.updated_text)
                )

    plan.changed_files = changed_files_set

    if not changed_files_set:
        plan.status = "no_op"
        return plan.model_dump()

    if dry_run:
        plan.status = "dry_run"
        return plan.model_dump()

    # Apply edits atomically; keep in-memory copy of original content per path so multi-file rollback
    # succeeds even if create_backups=False
    seen_paths: set[Path] = set()
    for edit in edits:
        if edit.updated_text == edit.original_text:
            continue
        resolved = edit.path.resolve()
        if resolved not in in_memory_backups:
            in_memory_backups[resolved] = edit.original_text
        atomic_write(resolved, edit.updated_text, create_backup=create_backups)
        if resolved not in seen_paths:
            seen_paths.add(resolved)
            modified_paths.append(resolved)

    errors = has_validation_errors(info)
    all_issues = [i.model_dump() for i in validate(info)]
    plan.validation = all_issues

    if errors:
        for path in modified_paths:
            if not restore_backup(path) and path in in_memory_backups:
                atomic_write(path, in_memory_backups[path], create_backup=False)
        plan.rolled_back = True
        plan.status = "rolled_back"
        plan.error_code = "ROLLBACK_COMPLETED"
        for err in errors:
            plan.warnings.append(
                f"Post-write validation failed ({err.code}): {err.message} — changes rolled back."
            )
    else:
        plan.status = "applied"

    return plan.model_dump()
