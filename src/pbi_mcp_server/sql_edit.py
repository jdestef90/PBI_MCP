"""Reusable Partition SQL edit helpers, guardrails, clean-SQL diffs, and plain-text formatting."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .errors import InvalidArgumentError, ObjectNotFoundError, PreconditionFailedError
from .m_codec import (
    MSerializationDescriptor,
    decode_m_sql_expression,
    scan_m_placeholder_end,
    split_clean_sql_segments,
)
from .models import MutationDiff
from .sql_sections import (
    LineIndex,
    SqlSection,
    SqlStructure,
    extract_table_refs,
    find_matching_rparen_token,
    scan_sql_structure,
    tokenize_sql,
    unquote_sql_identifier,
)

_PIPE_PREFIX_RE = re.compile(r"^[ \t]*\d+[ \t]*\|(?: |$)")
_COLON_PREFIX_RE = re.compile(r"^[ \t]*\d+:(?: |$)")
_CTE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_BATCH_EDIT_ALLOWED_KEYS = frozenset({
    "old_text",
    "new_text",
    "cte",
    "start_line",
    "end_line",
    "after_cte",
    "before_cte",
    "delete_cte",
})


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def verify_sql_hash_precondition(
    actual_hash: str,
    expected_sql_hash: str | None,
) -> None:
    """Raise PreconditionFailedError with the current sql_hash when hashes differ."""
    if expected_sql_hash is None:
        return
    expected_norm = expected_sql_hash.strip()
    if expected_norm.casefold() != actual_hash.casefold():
        raise PreconditionFailedError(
            f"Precondition failed: expected sql_hash {expected_norm!r}, "
            f"but current sql_hash is {actual_hash!r}. "
            "Re-read the query with get_table_sql before retrying.",
            code="PRECONDITION_FAILED",
            expected=expected_norm,
            actual=actual_hash,
            sql_hash=actual_hash,
        )


def strip_line_number_prefixes(
    text: str, *, reference_sql: str | None = None
) -> tuple[str, bool]:
    """Strip uniform `42 | ` or `42: ` line-number prefixes copied from get_table_sql."""
    norm = _normalize_newlines(text)
    if not norm:
        return text, False
    if reference_sql is not None and norm in reference_sql:
        return text, False

    lines = norm.split("\n")
    non_empty = [ln for ln in lines if ln.strip() != ""]
    if not non_empty:
        return text, False

    for pattern in (_PIPE_PREFIX_RE, _COLON_PREFIX_RE):
        if all(pattern.match(ln) for ln in non_empty):
            stripped = [pattern.sub("", ln, count=1) if ln.strip() != "" else "" for ln in lines]
            return "\n".join(stripped), True

    return text, False


def _common_leading_ws(lines: list[str]) -> str:
    non_empty = [ln for ln in lines if ln.strip(" \t") != ""]
    if not non_empty:
        return ""
    prefixes = [ln[: len(ln) - len(ln.lstrip(" \t"))] for ln in non_empty]
    common = prefixes[0]
    for cand in prefixes[1:]:
        limit = min(len(common), len(cand))
        idx = 0
        while idx < limit and common[idx] == cand[idx]:
            idx += 1
        common = common[:idx]
        if not common:
            break
    return common


def _reindent_replacement(
    matched_lines: list[str],
    old_lines: list[str],
    new_lines: list[str],
) -> list[str]:
    if not new_lines:
        return []
    matched_common = _common_leading_ws(matched_lines)
    old_common = _common_leading_ws(old_lines)
    new_common = _common_leading_ws(new_lines)

    out: list[str] = []
    for ln in new_lines:
        if ln.strip(" \t") == "":
            out.append("")
        elif old_common and ln.startswith(old_common):
            out.append(matched_common + ln[len(old_common) :])
        elif new_common and ln.startswith(new_common):
            out.append(matched_common + ln[len(new_common) :])
        else:
            out.append(matched_common + ln)
    return out


def apply_text_edit(
    clean_sql: str,
    *,
    old_text: str,
    new_text: str,
    expected_replacements: int | None = None,
) -> tuple[str, int, str]:
    """Apply exact substring replacement, falling back to whitespace-normalized matching."""
    patches, count, match_mode = _find_text_edit_patches(
        clean_sql,
        0,
        len(clean_sql),
        old_text=old_text,
        new_text=new_text,
        expected_replacements=expected_replacements,
    )
    if not patches:
        return clean_sql, 0, match_mode
    return apply_text_patches(clean_sql, patches), count, match_mode


@dataclass(frozen=True, slots=True)
class TextPatch:
    """Character-level splice against a pre-edit Clean SQL View snapshot."""

    start: int
    end: int
    replacement: str
    edit_index: int = 0
    label: str = ""

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(
                f"Invalid TextPatch span ({self.start}, {self.end}) for {self.label!r}: "
                "must satisfy 0 <= start <= end."
            )


def _resolve_sql_section(
    structure: SqlStructure, cte: str, *, role: str = "SQL Section"
) -> SqlSection:
    cleaned = unquote_sql_identifier(cte.strip())
    if not cleaned:
        raise InvalidArgumentError(
            f"{role} name must be a non-empty string.",
            code="INVALID_ARGUMENT",
        )
    by_cf = {s.name.casefold(): s for s in structure.sections}
    found = by_cf.get(cleaned.casefold())
    if found is None:
        available = [s.name for s in structure.sections]
        avail_str = ", ".join(available) if available else "(none)"
        raise ObjectNotFoundError(
            f"Unknown {role} {cte!r}. Available SQL Sections: {avail_str}.",
            code="OBJECT_NOT_FOUND",
            details={"requested": cte, "available_sections": available},
        )
    return found


def _line_start_if_ws_only(text: str, offset: int) -> int:
    line_start = text.rfind("\n", 0, offset) + 1
    if text[line_start:offset].strip(" \t") == "":
        return line_start
    return offset


def _section_text_scope(clean_sql: str, section: SqlSection) -> tuple[int, int]:
    if section.kind == "cte":
        return section.inner_span
    return _line_start_if_ws_only(clean_sql, section.outer_span[0]), len(clean_sql)


def _find_text_edit_patches(
    clean_sql: str,
    scope_start: int,
    scope_end: int,
    *,
    old_text: str,
    new_text: str,
    expected_replacements: int | None = None,
    edit_index: int = 0,
    label: str = "text replacement",
) -> tuple[list[TextPatch], int, str]:
    scoped = clean_sql[scope_start:scope_end]
    eff_old, old_stripped = strip_line_number_prefixes(old_text, reference_sql=scoped)
    eff_new, _ = strip_line_number_prefixes(
        new_text,
        reference_sql=None if old_stripped else scoped,
    )
    old_norm = _normalize_newlines(eff_old)
    new_norm = _normalize_newlines(eff_new)
    if not old_norm:
        raise InvalidArgumentError("old_text must not be empty.", code="INVALID_ARGUMENT")

    patches: list[TextPatch] = []
    pos = 0
    step = len(old_norm)
    while True:
        found = scoped.find(old_norm, pos)
        if found < 0:
            break
        patches.append(
            TextPatch(
                start=scope_start + found,
                end=scope_start + found + step,
                replacement=new_norm,
                edit_index=edit_index,
                label=label,
            )
        )
        pos = found + step
    if patches:
        return patches, len(patches), "exact"

    # Whitespace-normalized line-block fallback within scope
    line_starts = LineIndex(scoped).line_starts
    sql_lines = scoped.split("\n")

    old_for_lines = (
        old_norm[:-1] if old_norm.endswith("\n") and not old_norm.endswith("\n\n") else old_norm
    )
    new_for_lines = (
        new_norm[:-1] if new_norm.endswith("\n") and not new_norm.endswith("\n\n") else new_norm
    )
    old_lines = old_for_lines.split("\n")
    new_lines = [] if new_for_lines == "" else new_for_lines.split("\n")

    span_len = len(old_lines)
    if span_len == 0 or span_len > len(sql_lines):
        return [], 0, "none"

    old_stripped_lines = [ln.strip(" \t") for ln in old_lines]
    sql_stripped_lines = [ln.strip(" \t") for ln in sql_lines]

    match_starts: list[int] = []
    idx = 0
    limit = len(sql_lines) - span_len
    while idx <= limit:
        if sql_stripped_lines[idx : idx + span_len] == old_stripped_lines:
            match_starts.append(idx)
            idx += span_len
        else:
            idx += 1

    if not match_starts:
        return [], 0, "none"

    if len(match_starts) > 1 and expected_replacements is None:
        raise PreconditionFailedError(
            f"Ambiguous whitespace-normalized match: found {len(match_starts)} matching "
            "locations. Provide more context, exact indentation, or pass "
            f"expected_replacements={len(match_starts)}.",
            code="PRECONDITION_FAILED",
            expected=1,
            actual=len(match_starts),
        )

    deleted_line_set = (
        {ln for s in match_starts for ln in range(s, s + span_len)} if not new_lines else set()
    )
    for start_idx in match_starts:
        end_idx = start_idx + span_len - 1
        matched_block = sql_lines[start_idx : end_idx + 1]
        reindented = _reindent_replacement(matched_block, old_lines, new_lines)
        line_end = line_starts[end_idx + 1] - 1 if end_idx + 1 < len(sql_lines) else len(scoped)
        if reindented:
            rel_start = line_starts[start_idx]
            rel_end = line_end
            rep_str = "\n".join(reindented)
        else:
            has_surviving_after = any(
                j not in deleted_line_set for j in range(end_idx + 1, len(sql_lines))
            )
            if has_surviving_after:
                rel_start = line_starts[start_idx]
                rel_end = line_starts[end_idx + 1]
            elif start_idx > 0:
                rel_start = line_starts[start_idx] - 1
                rel_end = line_end
            else:
                rel_start = 0
                rel_end = line_end
            rep_str = ""
        patches.append(
            TextPatch(
                start=scope_start + rel_start,
                end=scope_start + rel_end,
                replacement=rep_str,
                edit_index=edit_index,
                label=label,
            )
        )

    return patches, len(match_starts), "whitespace_normalized"


def apply_text_patches(clean_sql: str, patches: list[TextPatch]) -> str:
    """Apply non-overlapping TextPatches in descending offset order."""
    updated = clean_sql
    for patch in sorted(patches, key=lambda p: (p.start, p.end), reverse=True):
        updated = updated[: patch.start] + patch.replacement + updated[patch.end :]
    return updated


def apply_cte_scoped_text_edit(
    clean_sql: str,
    *,
    cte: str,
    old_text: str,
    new_text: str,
    expected_replacements: int | None = None,
    structure: SqlStructure | None = None,
) -> tuple[str, int, str, str]:
    """Apply forgiving text replacement strictly inside a named SQL Section."""
    struct = structure if structure is not None else scan_sql_structure(clean_sql)
    section = _resolve_sql_section(struct, cte)
    scope_start, scope_end = _section_text_scope(clean_sql, section)
    patches, count, match_mode = _find_text_edit_patches(
        clean_sql,
        scope_start,
        scope_end,
        old_text=old_text,
        new_text=new_text,
        expected_replacements=expected_replacements,
        label=f"replace in {section.name!r}",
    )
    if not patches:
        return clean_sql, 0, match_mode, section.name
    return apply_text_patches(clean_sql, patches), count, match_mode, section.name


def _detect_indent_unit(clean_sql: str) -> str:
    min_spaces: int | None = None
    for line in clean_sql.split("\n"):
        if not line.strip(" \t"):
            continue
        if line.startswith("\t"):
            return "\t"
        lead = len(line) - len(line.lstrip(" "))
        if lead > 0 and (min_spaces is None or lead < min_spaces):
            min_spaces = lead
    if min_spaces == 2:
        return "  "
    return "    "


def _strip_outer_blank_lines(text: str) -> list[str]:
    lines = _normalize_newlines(text).split("\n")
    while lines and lines[0].strip(" \t") == "":
        lines.pop(0)
    while lines and lines[-1].strip(" \t") == "":
        lines.pop()
    return lines


def _format_cte_body_lines(raw_body: str, *, target_indent: str) -> list[str]:
    lines = _strip_outer_blank_lines(raw_body)
    if not lines:
        raise InvalidArgumentError("CTE body must not be empty.", code="INVALID_ARGUMENT")
    if not target_indent:
        return lines
    if _common_leading_ws(lines) == "":
        return [(target_indent + ln) if ln.strip(" \t") != "" else "" for ln in lines]
    return lines


def _unwrap_cte_body(raw_body: str, cte_name: str) -> tuple[str, str | None]:
    """Unwrap accidental `[WITH] <cte> AS (...) [,]` or outer `(...) [,]` wrappers."""
    norm = _normalize_newlines(raw_body)
    tokens = tokenize_sql(norm)
    if not tokens:
        return norm, None

    target_cf = unquote_sql_identifier(cte_name).casefold()
    n = len(tokens)
    idx = 0
    if tokens[idx].kind == "word" and tokens[idx].upper == "WITH":
        idx += 1
        if idx < n and tokens[idx].kind == "word" and tokens[idx].upper == "RECURSIVE":
            idx += 1

    if (
        idx < n
        and tokens[idx].kind in {"word", "ident"}
        and unquote_sql_identifier(tokens[idx].text).casefold() == target_cf
    ):
        j = idx + 1
        if j < n and tokens[j].kind == "lparen":
            col_close = find_matching_rparen_token(tokens, j)
            if col_close is not None:
                j = col_close + 1
        if j < n and tokens[j].kind == "word" and tokens[j].upper == "AS":
            j += 1
            if (
                j + 1 < n
                and tokens[j].kind == "word"
                and tokens[j].upper == "NOT"
                and tokens[j + 1].kind == "word"
                and tokens[j + 1].upper == "MATERIALIZED"
            ):
                j += 2
            elif j < n and tokens[j].kind == "word" and tokens[j].upper == "MATERIALIZED":
                j += 1
            if j < n and tokens[j].kind == "lparen":
                close_idx = find_matching_rparen_token(tokens, j)
                if close_idx is not None and (
                    close_idx == n - 1
                    or (close_idx == n - 2 and tokens[-1].kind == "comma")
                ):
                    inner = norm[tokens[j].end : tokens[close_idx].start]
                    note = (
                        f"Auto-unwrapped '{cte_name} AS (...)' wrapper from new_text "
                        f"for CTE '{cte_name}'."
                    )
                    return inner, note

    if tokens[0].kind == "lparen":
        close_idx = find_matching_rparen_token(tokens, 0)
        if close_idx is not None and (
            close_idx == n - 1 or (close_idx == n - 2 and tokens[-1].kind == "comma")
        ):
            inner = norm[tokens[0].end : tokens[close_idx].start]
            note = (
                f"Auto-unwrapped outer parentheses '(...)' from new_text "
                f"for CTE '{cte_name}'."
            )
            return inner, note

    return norm, None


def build_whole_cte_replace_patch(
    clean_sql: str,
    structure: SqlStructure,
    *,
    cte: str,
    new_text: str,
    edit_index: int = 0,
) -> tuple[TextPatch, str, list[str]]:
    """Build a TextPatch replacing the inner body of an existing CTE or `main`."""
    section = _resolve_sql_section(structure, cte)
    eff_new, _ = strip_line_number_prefixes(new_text)
    notes: list[str] = []

    if section.kind == "main":
        body_lines = _strip_outer_blank_lines(eff_new)
        if not body_lines:
            raise InvalidArgumentError(
                "Replacement SQL for 'main' must not be empty.",
                code="INVALID_ARGUMENT",
            )
        start = _line_start_if_ws_only(clean_sql, section.outer_span[0])
        end = section.outer_span[1]
        main_indent = clean_sql[start : section.outer_span[0]]
        formatted_lines = _format_cte_body_lines(
            "\n".join(body_lines), target_indent=main_indent
        )
        return (
            TextPatch(
                start=start,
                end=end,
                replacement="\n".join(formatted_lines),
                edit_index=edit_index,
                label="replace section 'main'",
            ),
            section.name,
            notes,
        )

    unwrapped, note = _unwrap_cte_body(eff_new, section.name)
    if note:
        notes.append(note)

    inner_start, inner_end = section.inner_span
    orig_inner = clean_sql[inner_start:inner_end]
    closing_line_start = clean_sql.rfind("\n", 0, inner_end) + 1
    raw_closing_prefix = clean_sql[closing_line_start:inner_end]
    closing_indent = raw_closing_prefix if raw_closing_prefix.strip(" \t") == "" else ""

    is_multiline_orig = "\n" in orig_inner
    orig_lines = orig_inner.split("\n")
    orig_body_indent = _common_leading_ws(
        orig_lines[1:-1] if len(orig_lines) > 2 else orig_lines
    )
    if not orig_body_indent:
        orig_body_indent = closing_indent + _detect_indent_unit(clean_sql)

    body_lines = _format_cte_body_lines(
        unwrapped,
        target_indent=orig_body_indent if (is_multiline_orig or "\n" in unwrapped.strip()) else "",
    )
    if is_multiline_orig or len(body_lines) > 1:
        replacement = "\n" + "\n".join(body_lines) + "\n" + closing_indent
    else:
        replacement = body_lines[0].strip()

    return (
        TextPatch(
            start=inner_start,
            end=inner_end,
            replacement=replacement,
            edit_index=edit_index,
            label=f"replace CTE {section.name!r}",
        ),
        section.name,
        notes,
    )


def apply_whole_cte_replace_edit(
    clean_sql: str,
    *,
    cte: str,
    new_text: str,
    structure: SqlStructure | None = None,
) -> tuple[str, int, str, list[str]]:
    """Replace the inner body of an existing CTE (or `main`) while preserving surrounding syntax."""
    struct = structure if structure is not None else scan_sql_structure(clean_sql)
    patch, sec_name, notes = build_whole_cte_replace_patch(
        clean_sql,
        struct,
        cte=cte,
        new_text=new_text,
    )
    updated = apply_text_patches(clean_sql, [patch])
    count = 0 if updated == clean_sql else 1
    return updated, count, sec_name, notes


def _validate_new_cte_name(cte: str, structure: SqlStructure) -> tuple[str, str]:
    """Return `(raw_sql_name, unquoted_name)` or raise InvalidArgumentError."""
    raw = cte.strip()
    if not raw:
        raise InvalidArgumentError("cte name must not be empty.", code="INVALID_ARGUMENT")
    unquoted = unquote_sql_identifier(raw)
    if not unquoted:
        raise InvalidArgumentError("cte name must not be empty.", code="INVALID_ARGUMENT")
    if unquoted.casefold() == "main":
        raise InvalidArgumentError(
            "Cannot insert a CTE named 'main'; 'main' is reserved for the final SELECT section.",
            code="INVALID_ARGUMENT",
        )
    if raw == unquoted and not _CTE_IDENT_RE.match(raw):
        raise InvalidArgumentError(
            f"Invalid CTE name {cte!r}. Provide a valid SQL identifier.",
            code="INVALID_ARGUMENT",
        )
    existing_cf = {s.name.casefold(): s.name for s in structure.sections if s.kind == "cte"}
    if unquoted.casefold() in existing_cf:
        raise InvalidArgumentError(
            f"CTE {existing_cf[unquoted.casefold()]!r} already exists. "
            f"Omit after_cte/before_cte to replace its body instead.",
            code="INVALID_ARGUMENT",
        )
    return raw, unquoted


def _cte_header_indent(clean_sql: str, section: SqlSection) -> str:
    start = section.outer_span[0]
    line_start = clean_sql.rfind("\n", 0, start) + 1
    prefix = clean_sql[line_start:start]
    if prefix.strip(" \t") == "":
        return prefix
    # CTE starts on the same line as `WITH` — use the leading whitespace of the `WITH` line
    return prefix[: len(prefix) - len(prefix.lstrip(" \t"))]


def _cte_body_indent(clean_sql: str, section: SqlSection, fallback_cte_indent: str) -> str:
    orig_inner = clean_sql[section.inner_span[0] : section.inner_span[1]]
    orig_lines = orig_inner.split("\n")
    detected = _common_leading_ws(orig_lines[1:-1] if len(orig_lines) > 2 else orig_lines)
    if detected:
        return detected
    return fallback_cte_indent + _detect_indent_unit(clean_sql)


def build_cte_insert_patch(
    clean_sql: str,
    structure: SqlStructure,
    *,
    cte: str,
    new_text: str,
    after_cte: str | None = None,
    before_cte: str | None = None,
    edit_index: int = 0,
) -> tuple[TextPatch, str, list[str]]:
    """Build a zero-width TextPatch inserting a new CTE at the requested anchor position."""
    raw_name, unquoted_name = _validate_new_cte_name(cte, structure)
    if after_cte is None and before_cte is None:
        raise InvalidArgumentError(
            "Provide after_cte and/or before_cte to insert a new CTE.",
            code="INVALID_ARGUMENT",
        )

    sec_idx_by_cf = {s.name.casefold(): i for i, s in enumerate(structure.sections)}
    after_idx: int | None = None
    before_idx: int | None = None

    if after_cte is not None:
        after_sec = _resolve_sql_section(structure, after_cte, role="after_cte anchor")
        if after_sec.kind == "main":
            raise PreconditionFailedError(
                "Cannot insert a CTE after 'main'; CTEs must precede the final 'main' SELECT.",
                code="PRECONDITION_FAILED",
            )
        after_idx = sec_idx_by_cf[after_sec.name.casefold()]

    if before_cte is not None:
        before_sec = _resolve_sql_section(structure, before_cte, role="before_cte anchor")
        before_idx = sec_idx_by_cf[before_sec.name.casefold()]

    if after_idx is not None and before_idx is not None and after_idx >= before_idx:
        after_label = structure.sections[after_idx].name
        before_label = structure.sections[before_idx].name
        raise PreconditionFailedError(
            f"Invalid CTE insertion order: after_cte={after_label!r} must precede "
            f"before_cte={before_label!r}.",
            code="PRECONDITION_FAILED",
        )

    eff_new, _ = strip_line_number_prefixes(new_text)
    unwrapped, note = _unwrap_cte_body(eff_new, unquoted_name)
    notes: list[str] = [note] if note else []

    cte_sections = [s for s in structure.sections if s.kind == "cte"]

    # Case 1: Non-CTE query — bootstrap `WITH <cte> AS (...)` right before `main`
    if not structure.has_ctes:
        main_sec = next((s for s in structure.sections if s.kind == "main"), None)
        if main_sec is None:
            raise InvalidArgumentError(
                "Cannot insert a CTE into an empty SQL query.",
                code="INVALID_ARGUMENT",
            )
        splice_pos = _line_start_if_ws_only(clean_sql, main_sec.outer_span[0])
        base_indent = clean_sql[splice_pos : main_sec.outer_span[0]]
        body_indent = base_indent + _detect_indent_unit(clean_sql)
        body_lines = _format_cte_body_lines(unwrapped, target_indent=body_indent)
        replacement = (
            f"{base_indent}WITH {raw_name} AS (\n"
            + "\n".join(body_lines)
            + f"\n{base_indent})\n"
        )
        return (
            TextPatch(
                start=splice_pos,
                end=splice_pos,
                replacement=replacement,
                edit_index=edit_index,
                label=f"insert CTE {unquoted_name!r}",
            ),
            unquoted_name,
            notes,
        )

    assert after_idx is not None or before_idx is not None
    insert_slot = (after_idx + 1) if after_idx is not None else before_idx
    assert insert_slot is not None

    # Case 2: Insert before the first CTE (slot == 0)
    if insert_slot == 0:
        first_cte = cte_sections[0]
        cte_indent = _cte_header_indent(clean_sql, first_cte)
        body_indent = _cte_body_indent(clean_sql, first_cte, cte_indent)
        body_lines = _format_cte_body_lines(unwrapped, target_indent=body_indent)
        replacement = (
            f"{raw_name} AS (\n"
            + "\n".join(body_lines)
            + f"\n{cte_indent}),\n{cte_indent}"
        )
        return (
            TextPatch(
                start=first_cte.outer_span[0],
                end=first_cte.outer_span[0],
                replacement=replacement,
                edit_index=edit_index,
                label=f"insert CTE {unquoted_name!r}",
            ),
            unquoted_name,
            notes,
        )

    # Case 3: Insert after an existing CTE (between CTEs or before `main`)
    prev_cte = cte_sections[insert_slot - 1]
    cte_indent = _cte_header_indent(clean_sql, prev_cte)
    body_indent = _cte_body_indent(clean_sql, prev_cte, cte_indent)
    body_lines = _format_cte_body_lines(unwrapped, target_indent=body_indent)
    replacement = (
        f",\n{cte_indent}{raw_name} AS (\n"
        + "\n".join(body_lines)
        + f"\n{cte_indent})"
    )
    return (
        TextPatch(
            start=prev_cte.outer_span[1],
            end=prev_cte.outer_span[1],
            replacement=replacement,
            edit_index=edit_index,
            label=f"insert CTE {unquoted_name!r}",
        ),
        unquoted_name,
        notes,
    )


def apply_cte_insert_edit(
    clean_sql: str,
    *,
    cte: str,
    new_text: str,
    after_cte: str | None = None,
    before_cte: str | None = None,
    structure: SqlStructure | None = None,
) -> tuple[str, int, str, list[str]]:
    """Insert a new CTE at the requested position while managing WITH and commas."""
    struct = structure if structure is not None else scan_sql_structure(clean_sql)
    patch, inserted_name, notes = build_cte_insert_patch(
        clean_sql,
        struct,
        cte=cte,
        new_text=new_text,
        after_cte=after_cte,
        before_cte=before_cte,
    )
    updated = apply_text_patches(clean_sql, [patch])
    return updated, 1, inserted_name, notes


def build_cte_delete_patches(
    clean_sql: str,
    structure: SqlStructure,
    deletions: list[tuple[int, str]],
    *,
    insert_slots: set[int] | None = None,
) -> tuple[list[TextPatch], list[str]]:
    """Build TextPatches removing one or more CTEs while fixing adjacent commas and WITH."""
    cte_sections = [s for s in structure.sections if s.kind == "cte"]
    cte_idx_by_cf = {s.name.casefold(): i for i, s in enumerate(cte_sections)}

    resolved: list[tuple[int, int, SqlSection]] = []
    for edit_index, cte_arg in deletions:
        cleaned = unquote_sql_identifier(cte_arg.strip())
        if not cleaned:
            raise InvalidArgumentError("cte name must not be empty.", code="INVALID_ARGUMENT")
        if cleaned.casefold() == "main":
            raise InvalidArgumentError(
                "Cannot delete the 'main' SQL Section; only named CTEs can be deleted.",
                code="INVALID_ARGUMENT",
            )
        section = _resolve_sql_section(structure, cte_arg)
        idx = cte_idx_by_cf[section.name.casefold()]
        resolved.append((edit_index, idx, section))

    deleted_idx_set = {idx for _, idx, _ in resolved}
    deleted_names = [sec.name for _, _, sec in resolved]
    has_insertions = bool(insert_slots)

    # Deleting ALL existing CTEs removes the entire WITH clause (unless insertions also occur)
    if len(deleted_idx_set) == len(cte_sections):
        if has_insertions:
            raise InvalidArgumentError(
                "Conflicting batch edits: cannot delete all existing CTEs while inserting a "
                "new CTE in the same batch; replace an existing CTE or apply in separate calls.",
                code="INVALID_ARGUMENT",
            )
        tokens = tokenize_sql(clean_sql)
        with_start = tokens[0].start if tokens else 0
        del_start = _line_start_if_ws_only(clean_sql, with_start)
        last_cte = cte_sections[-1]
        main_sec = next((s for s in structure.sections if s.kind == "main"), None)
        if main_sec is not None:
            between = clean_sql[last_cte.outer_span[1] : main_sec.outer_span[0]]
            if between.strip() == "":
                del_end = _line_start_if_ws_only(clean_sql, main_sec.outer_span[0])
            else:
                pos = last_cte.outer_span[1]
                nl = clean_sql.find("\n", pos, main_sec.outer_span[0])
                del_end = (nl + 1) if nl != -1 else main_sec.outer_span[0]
        else:
            del_end = len(clean_sql)
        label = (
            f"delete CTE {deleted_names[0]!r}"
            if len(deleted_names) == 1
            else f"delete CTEs {', '.join(deleted_names)}"
        )
        return (
            [
                TextPatch(
                    start=del_start,
                    end=del_end,
                    replacement="",
                    edit_index=resolved[0][0],
                    label=label,
                )
            ],
            deleted_names,
        )

    patches: list[TextPatch] = []
    for edit_index, idx, sec in resolved:
        has_surviving_after = any(
            j not in deleted_idx_set for j in range(idx + 1, len(cte_sections))
        ) or bool(insert_slots and any(slot > idx for slot in insert_slots))

        if has_surviving_after and idx + 1 < len(cte_sections):
            next_sec = cte_sections[idx + 1]
            between = clean_sql[sec.outer_span[1] : next_sec.outer_span[0]]
            between_tokens = tokenize_sql(between)
            comma_tok = next((t for t in between_tokens if t.kind == "comma"), None)
            if comma_tok is not None and between[comma_tok.end :].strip() != "":
                # Preserve comments between comma and next_sec
                after_comma = sec.outer_span[1] + comma_tok.end
                nl = clean_sql.find("\n", after_comma, next_sec.outer_span[0])
                del_end = (nl + 1) if nl != -1 else next_sec.outer_span[0]
                del_start = (
                    _line_start_if_ws_only(clean_sql, sec.outer_span[0])
                    if idx > 0
                    else sec.outer_span[0]
                )
            else:
                del_start = sec.outer_span[0]
                del_end = next_sec.outer_span[0]
        elif idx > 0:
            prev_sec = cte_sections[idx - 1]
            del_start = prev_sec.outer_span[1]
            del_end = sec.outer_span[1]
        else:
            raise InvalidArgumentError(
                f"Conflicting batch edits: cannot delete first CTE {sec.name!r} without a "
                "surviving subsequent CTE while inserting in the same batch.",
                code="INVALID_ARGUMENT",
            )

        patches.append(
            TextPatch(
                start=del_start,
                end=del_end,
                replacement="",
                edit_index=edit_index,
                label=f"delete CTE {sec.name!r}",
            )
        )

    return patches, deleted_names


def detect_deleted_cte_reference_warnings(
    updated_clean_sql: str,
    deleted_cte_names: list[str],
) -> list[str]:
    """Warn if any remaining SQL Section in updated_clean_sql still references a deleted CTE."""
    if not deleted_cte_names:
        return []
    after_struct = scan_sql_structure(updated_clean_sql)
    existing_cte_cfs = {s.name.casefold() for s in after_struct.sections if s.kind == "cte"}
    warnings: list[str] = []
    seen_cf: set[str] = set()
    for del_name in deleted_cte_names:
        del_cf = del_name.casefold()
        if del_cf in seen_cf or del_cf in existing_cte_cfs:
            continue
        seen_cf.add(del_cf)
        referencing: list[str] = []
        for sec in after_struct.sections:
            span = sec.inner_span if sec.kind == "cte" else sec.outer_span
            sec_tokens = tokenize_sql(updated_clean_sql[span[0] : span[1]])
            refs, _ = extract_table_refs(
                sec_tokens,
                cte_names_by_cf={del_cf: del_name},
                self_name_cf=None,
            )
            if del_name in refs:
                referencing.append(sec.name)
        if referencing:
            warnings.append(
                f"Deleted CTE '{del_name}' is still referenced by downstream SQL Section(s): "
                f"{', '.join(referencing)}."
            )
    return warnings


def apply_cte_delete_edit(
    clean_sql: str,
    *,
    cte: str,
    structure: SqlStructure | None = None,
) -> tuple[str, int, str, list[str]]:
    """Delete a named CTE, fix adjacent commas/WITH, and check downstream references."""
    struct = structure if structure is not None else scan_sql_structure(clean_sql)
    patches, deleted_names = build_cte_delete_patches(clean_sql, struct, [(0, cte)])
    updated = apply_text_patches(clean_sql, patches)
    ref_warnings = detect_deleted_cte_reference_warnings(updated, deleted_names)
    return updated, 1, deleted_names[0], ref_warnings


def build_line_range_patch(
    clean_sql: str,
    *,
    start_line: int | None,
    end_line: int | None,
    new_text: str,
    edit_index: int = 0,
) -> TextPatch:
    """Build a TextPatch replacing an inclusive 1-based line range in Clean SQL View."""
    if start_line is None or isinstance(start_line, bool):
        raise InvalidArgumentError(
            "start_line is required when end_line is provided.",
            code="INVALID_ARGUMENT",
        )
    eff_end = start_line if end_line is None else end_line
    if isinstance(eff_end, bool):
        raise InvalidArgumentError(
            f"end_line must be an integer (received {end_line!r}).",
            code="INVALID_ARGUMENT",
        )
    if start_line < 1:
        raise InvalidArgumentError(
            f"start_line must be >= 1 (received {start_line}).",
            code="INVALID_ARGUMENT",
        )
    if eff_end < 1:
        raise InvalidArgumentError(
            f"end_line must be >= 1 (received {eff_end}).",
            code="INVALID_ARGUMENT",
        )
    if eff_end < start_line:
        raise InvalidArgumentError(
            f"end_line ({eff_end}) must be >= start_line ({start_line}).",
            code="INVALID_ARGUMENT",
        )

    lines = clean_sql.split("\n") if clean_sql != "" else []
    line_count = len(lines)
    if start_line > line_count or eff_end > line_count:
        raise InvalidArgumentError(
            f"Line range {start_line}-{eff_end} is out of bounds for query with "
            f"{line_count} line(s).",
            code="INVALID_ARGUMENT",
            details={
                "start_line": start_line,
                "end_line": eff_end,
                "line_count": line_count,
            },
        )

    line_starts = LineIndex(clean_sql).line_starts

    eff_new, _ = strip_line_number_prefixes(new_text)
    norm_new = _normalize_newlines(eff_new)
    replacement_lines = [] if norm_new == "" else norm_new.splitlines()

    if replacement_lines:
        span_start = line_starts[start_line - 1]
        span_end = line_starts[eff_end] - 1 if eff_end < line_count else len(clean_sql)
        rep = "\n".join(replacement_lines)
    else:
        if eff_end < line_count:
            span_start = line_starts[start_line - 1]
            span_end = line_starts[eff_end]
        elif start_line > 1:
            span_start = line_starts[start_line - 1] - 1
            span_end = len(clean_sql)
        else:
            span_start = 0
            span_end = len(clean_sql)
        rep = ""

    return TextPatch(
        start=span_start,
        end=span_end,
        replacement=rep,
        edit_index=edit_index,
        label=f"replace lines {start_line}-{eff_end}",
    )


def _validate_non_overlapping_patches(patches: list[TextPatch]) -> None:
    ordered = sorted(patches, key=lambda p: (p.start, p.end, p.edit_index))
    for i in range(1, len(ordered)):
        prev = ordered[i - 1]
        curr = ordered[i]
        if prev.edit_index == curr.edit_index:
            continue
        if prev.start == prev.end == curr.start == curr.end:
            raise InvalidArgumentError(
                f"Conflicting batch edits: edit #{prev.edit_index + 1} ({prev.label}) and "
                f"edit #{curr.edit_index + 1} ({curr.label}) target the exact same position.",
                code="INVALID_ARGUMENT",
            )
        if curr.start < prev.end:
            raise InvalidArgumentError(
                f"Overlapping batch edits: edit #{prev.edit_index + 1} ({prev.label}) "
                f"overlaps with edit #{curr.edit_index + 1} ({curr.label}).",
                code="INVALID_ARGUMENT",
            )


def apply_batch_edits(
    clean_sql: str,
    edits: list[dict[str, Any]],
    *,
    structure: SqlStructure | None = None,
) -> tuple[str, int, list[str]]:
    """Resolve and atomically apply a batch of edits against one pre-edit Clean SQL View."""
    if not isinstance(edits, list) or not edits:
        raise InvalidArgumentError(
            "edits must be a non-empty list of edit objects.",
            code="INVALID_ARGUMENT",
        )

    struct = structure if structure is not None else scan_sql_structure(clean_sql)
    sec_idx_by_cf = {s.name.casefold(): i for i, s in enumerate(struct.sections)}

    parsed_entries: list[tuple[int, str, dict[str, Any]]] = []
    deleted_cte_cfs: dict[str, int] = {}
    inserted_cte_cfs: dict[str, int] = {}
    whole_replaced_cfs: dict[str, int] = {}
    scoped_cfs: dict[str, list[int]] = {}
    anchor_refs: list[tuple[str, int, str]] = []
    insert_slots: set[int] = set()
    delete_entries: list[tuple[int, str]] = []

    for idx, raw_entry in enumerate(edits):
        if not isinstance(raw_entry, dict):
            raise InvalidArgumentError(
                f"Batch edit #{idx + 1} must be a dict.",
                code="INVALID_ARGUMENT",
            )
        unknown_keys = sorted(set(raw_entry) - _BATCH_EDIT_ALLOWED_KEYS)
        if unknown_keys:
            raise InvalidArgumentError(
                f"Batch edit #{idx + 1} contains unknown field(s): {', '.join(unknown_keys)}. "
                f"Allowed fields: {', '.join(sorted(_BATCH_EDIT_ALLOWED_KEYS))}.",
                code="INVALID_ARGUMENT",
            )

        old_text = raw_entry.get("old_text")
        new_text = raw_entry.get("new_text")
        cte = raw_entry.get("cte")
        start_line = raw_entry.get("start_line")
        end_line = raw_entry.get("end_line")
        after_cte = raw_entry.get("after_cte")
        before_cte = raw_entry.get("before_cte")
        delete_cte = bool(raw_entry.get("delete_cte", False))
        has_line_range = start_line is not None or end_line is not None
        has_insert_anchor = after_cte is not None or before_cte is not None

        if delete_cte:
            if cte is None or not isinstance(cte, str):
                raise InvalidArgumentError(
                    f"Batch edit #{idx + 1}: cte is required when delete_cte=True.",
                    code="INVALID_ARGUMENT",
                )
            if (
                old_text is not None
                or new_text is not None
                or has_line_range
                or has_insert_anchor
            ):
                raise InvalidArgumentError(
                    f"Batch edit #{idx + 1}: cannot combine delete_cte=True with old_text, "
                    "new_text, start_line/end_line, or after_cte/before_cte.",
                    code="INVALID_ARGUMENT",
                )
            cleaned = unquote_sql_identifier(cte.strip())
            if cleaned.casefold() == "main":
                raise InvalidArgumentError(
                    f"Batch edit #{idx + 1}: cannot delete the 'main' SQL Section.",
                    code="INVALID_ARGUMENT",
                )
            sec = _resolve_sql_section(struct, cte)
            cf = sec.name.casefold()
            if cf in deleted_cte_cfs:
                raise InvalidArgumentError(
                    f"Conflicting batch edits: CTE {sec.name!r} is deleted more than once "
                    f"(edit #{deleted_cte_cfs[cf] + 1} and edit #{idx + 1}).",
                    code="INVALID_ARGUMENT",
                )
            deleted_cte_cfs[cf] = idx
            delete_entries.append((idx, cte))
            parsed_entries.append((idx, "delete_cte", raw_entry))
            continue

        if has_insert_anchor:
            if cte is None or not isinstance(cte, str):
                raise InvalidArgumentError(
                    f"Batch edit #{idx + 1}: cte is required when using after_cte or before_cte.",
                    code="INVALID_ARGUMENT",
                )
            if new_text is None or not isinstance(new_text, str):
                raise InvalidArgumentError(
                    f"Batch edit #{idx + 1}: new_text is required for CTE insertion.",
                    code="INVALID_ARGUMENT",
                )
            if old_text is not None or has_line_range:
                raise InvalidArgumentError(
                    f"Batch edit #{idx + 1}: cannot combine after_cte/before_cte with "
                    "old_text or start_line/end_line.",
                    code="INVALID_ARGUMENT",
                )
            _, unquoted = _validate_new_cte_name(cte, struct)
            cf = unquoted.casefold()
            if cf in inserted_cte_cfs:
                raise InvalidArgumentError(
                    f"Conflicting batch edits: CTE {unquoted!r} is inserted more than once "
                    f"(edit #{inserted_cte_cfs[cf] + 1} and edit #{idx + 1}).",
                    code="INVALID_ARGUMENT",
                )
            inserted_cte_cfs[cf] = idx

            after_idx_val: int | None = None
            before_idx_val: int | None = None
            if after_cte is not None:
                after_sec = _resolve_sql_section(struct, after_cte, role="after_cte anchor")
                if after_sec.kind == "main":
                    raise PreconditionFailedError(
                        "Cannot insert a CTE after 'main'; CTEs must precede the final "
                        "'main' SELECT.",
                        code="PRECONDITION_FAILED",
                    )
                after_idx_val = sec_idx_by_cf[after_sec.name.casefold()]
                anchor_refs.append((after_sec.name.casefold(), idx, after_sec.name))
            if before_cte is not None:
                before_sec = _resolve_sql_section(struct, before_cte, role="before_cte anchor")
                before_idx_val = sec_idx_by_cf[before_sec.name.casefold()]
                if before_sec.kind == "cte":
                    anchor_refs.append((before_sec.name.casefold(), idx, before_sec.name))
            if (
                after_idx_val is not None
                and before_idx_val is not None
                and after_idx_val >= before_idx_val
            ):
                raise PreconditionFailedError(
                    f"Invalid CTE insertion order in edit #{idx + 1}: "
                    f"after_cte={struct.sections[after_idx_val].name!r} must precede "
                    f"before_cte={struct.sections[before_idx_val].name!r}.",
                    code="PRECONDITION_FAILED",
                )
            slot = (after_idx_val + 1) if after_idx_val is not None else before_idx_val
            if slot is not None:
                insert_slots.add(slot)
            parsed_entries.append((idx, "insert_cte", raw_entry))
            continue

        if has_line_range:
            if old_text is not None or cte is not None:
                raise InvalidArgumentError(
                    f"Batch edit #{idx + 1}: cannot combine start_line/end_line with "
                    "old_text or cte.",
                    code="INVALID_ARGUMENT",
                )
            if new_text is None or not isinstance(new_text, str):
                raise InvalidArgumentError(
                    f"Batch edit #{idx + 1}: new_text is required for line-range replacement.",
                    code="INVALID_ARGUMENT",
                )
            parsed_entries.append((idx, "line_range", raw_entry))
            continue

        if old_text is not None:
            if not isinstance(old_text, str) or not old_text:
                raise InvalidArgumentError(
                    f"Batch edit #{idx + 1}: old_text must be a non-empty string.",
                    code="INVALID_ARGUMENT",
                )
            if new_text is None or not isinstance(new_text, str):
                raise InvalidArgumentError(
                    f"Batch edit #{idx + 1}: new_text is required.",
                    code="INVALID_ARGUMENT",
                )
            if cte is not None:
                sec = _resolve_sql_section(struct, cte)
                scoped_cfs.setdefault(sec.name.casefold(), []).append(idx)
                parsed_entries.append((idx, "cte_text", raw_entry))
            else:
                parsed_entries.append((idx, "global_text", raw_entry))
            continue

        if cte is not None:
            if new_text is None or not isinstance(new_text, str):
                raise InvalidArgumentError(
                    f"Batch edit #{idx + 1}: new_text is required for whole-CTE replacement.",
                    code="INVALID_ARGUMENT",
                )
            sec = _resolve_sql_section(struct, cte)
            cf = sec.name.casefold()
            if cf in whole_replaced_cfs:
                raise InvalidArgumentError(
                    f"Conflicting batch edits: SQL Section {sec.name!r} is replaced more than "
                    f"once (edit #{whole_replaced_cfs[cf] + 1} and edit #{idx + 1}).",
                    code="INVALID_ARGUMENT",
                )
            whole_replaced_cfs[cf] = idx
            parsed_entries.append((idx, "whole_cte", raw_entry))
            continue

        raise InvalidArgumentError(
            f"Batch edit #{idx + 1}: specify old_text, cte, start_line/end_line, "
            "after_cte/before_cte, or delete_cte=True.",
            code="INVALID_ARGUMENT",
        )

    # Cross-edit semantic conflict validation
    cte_sections = [s for s in struct.sections if s.kind == "cte"]
    for cf, del_idx in deleted_cte_cfs.items():
        if cf in whole_replaced_cfs:
            raise InvalidArgumentError(
                f"Conflicting batch edits: cannot delete and replace CTE {cf!r} "
                f"(edit #{del_idx + 1} and edit #{whole_replaced_cfs[cf] + 1}).",
                code="INVALID_ARGUMENT",
            )
        if cf in scoped_cfs:
            raise InvalidArgumentError(
                f"Conflicting batch edits: cannot delete and edit CTE {cf!r} "
                f"(edit #{del_idx + 1} and edit #{scoped_cfs[cf][0] + 1}).",
                code="INVALID_ARGUMENT",
            )
    for anchor_cf, ins_idx, anchor_label in anchor_refs:
        if anchor_cf in deleted_cte_cfs:
            raise InvalidArgumentError(
                f"Conflicting batch edits: edit #{ins_idx + 1} uses deleted CTE "
                f"{anchor_label!r} as an insertion anchor "
                f"(deleted in edit #{deleted_cte_cfs[anchor_cf] + 1}).",
                code="INVALID_ARGUMENT",
            )
    if cte_sections and len(deleted_cte_cfs) == len(cte_sections) and inserted_cte_cfs:
        raise InvalidArgumentError(
            "Conflicting batch edits: cannot delete all existing CTEs while inserting a "
            "new CTE in the same batch; replace an existing CTE or apply in separate calls.",
            code="INVALID_ARGUMENT",
        )
    for cf, rep_idx in whole_replaced_cfs.items():
        if cf in scoped_cfs:
            raise InvalidArgumentError(
                f"Conflicting batch edits: cannot combine whole-section replacement and "
                f"scoped replacement on SQL Section {cf!r} "
                f"(edit #{rep_idx + 1} and edit #{scoped_cfs[cf][0] + 1}).",
                code="INVALID_ARGUMENT",
            )

    # Resolve all entries against the single pre-edit snapshot
    all_patches: list[TextPatch] = []
    notes_and_warnings: list[str] = []
    total_replacements = 0

    for idx, mode, entry in parsed_entries:
        if mode == "delete_cte":
            continue
        if mode == "line_range":
            patch = build_line_range_patch(
                clean_sql,
                start_line=entry.get("start_line"),
                end_line=entry.get("end_line"),
                new_text=entry["new_text"],
                edit_index=idx,
            )
            all_patches.append(patch)
            total_replacements += 1
        elif mode == "whole_cte":
            patch, _, notes = build_whole_cte_replace_patch(
                clean_sql,
                struct,
                cte=entry["cte"],
                new_text=entry["new_text"],
                edit_index=idx,
            )
            all_patches.append(patch)
            notes_and_warnings.extend(notes)
            total_replacements += 1
        elif mode == "insert_cte":
            patch, _, notes = build_cte_insert_patch(
                clean_sql,
                struct,
                cte=entry["cte"],
                new_text=entry["new_text"],
                after_cte=entry.get("after_cte"),
                before_cte=entry.get("before_cte"),
                edit_index=idx,
            )
            all_patches.append(patch)
            notes_and_warnings.extend(notes)
            total_replacements += 1
        elif mode in {"cte_text", "global_text"}:
            if mode == "cte_text":
                sec = _resolve_sql_section(struct, entry["cte"])
                scope_start, scope_end = _section_text_scope(clean_sql, sec)
                label = f"replace in {sec.name!r}"
                scope_desc = f" in SQL Section {sec.name!r}"
            else:
                scope_start, scope_end = 0, len(clean_sql)
                label = "replace text"
                scope_desc = ""
            patches, count, _ = _find_text_edit_patches(
                clean_sql,
                scope_start,
                scope_end,
                old_text=entry["old_text"],
                new_text=entry["new_text"],
                edit_index=idx,
                label=label,
            )
            if count == 0:
                raise PreconditionFailedError(
                    f"Batch edit #{idx + 1}: matched 0 occurrences of old_text"
                    f"{scope_desc}; no edits were applied.",
                    code="PRECONDITION_FAILED",
                    expected=1,
                    actual=0,
                )
            all_patches.extend(patches)
            total_replacements += count

    deleted_names: list[str] = []
    if delete_entries:
        del_patches, deleted_names = build_cte_delete_patches(
            clean_sql,
            struct,
            delete_entries,
            insert_slots=insert_slots or None,
        )
        all_patches.extend(del_patches)
        total_replacements += len(delete_entries)

    _validate_non_overlapping_patches(all_patches)
    updated_clean = apply_text_patches(clean_sql, all_patches)
    notes_and_warnings.extend(
        detect_deleted_cte_reference_warnings(updated_clean, deleted_names)
    )
    if updated_clean == clean_sql:
        return clean_sql, 0, notes_and_warnings
    return updated_clean, total_replacements, notes_and_warnings


def _validate_m_expr_balance(expr: str) -> None:
    stripped = expr.strip()
    if not stripped:
        raise InvalidArgumentError(
            "Empty Dynamic M Parameter placeholder '{{m:}}' is not allowed.",
            code="INVALID_ARGUMENT",
        )
    depth_paren = 0
    depth_brack = 0
    depth_brace = 0
    i = 0
    n = len(stripped)
    while i < n:
        if stripped.startswith("//", i):
            nl = stripped.find("\n", i)
            i = n if nl < 0 else nl + 1
            continue
        if stripped.startswith("/*", i):
            close = stripped.find("*/", i + 2)
            if close < 0:
                raise InvalidArgumentError(
                    f"Invalid M expression inside {{{{m:{stripped}}}}}: unclosed block comment.",
                    code="INVALID_ARGUMENT",
                )
            i = close + 2
            continue
        ch = stripped[i]
        if ch == '"':
            i += 1
            closed = False
            while i < n:
                if stripped.startswith('""', i):
                    i += 2
                    continue
                if stripped[i] == '"':
                    closed = True
                    i += 1
                    break
                i += 1
            if not closed:
                raise InvalidArgumentError(
                    f"Invalid M expression inside {{{{m:{stripped}}}}}: "
                    'unbalanced double quote (").',
                    code="INVALID_ARGUMENT",
                )
            continue
        if ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren -= 1
            if depth_paren < 0:
                raise InvalidArgumentError(
                    f"Invalid M expression inside {{{{m:{stripped}}}}}: "
                    "unbalanced closing parenthesis ')'.",
                    code="INVALID_ARGUMENT",
                )
        elif ch == "[":
            depth_brack += 1
        elif ch == "]":
            depth_brack -= 1
            if depth_brack < 0:
                raise InvalidArgumentError(
                    f"Invalid M expression inside {{{{m:{stripped}}}}}: "
                    "unbalanced closing bracket ']'.",
                    code="INVALID_ARGUMENT",
                )
        elif ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace -= 1
            if depth_brace < 0:
                raise InvalidArgumentError(
                    f"Invalid M expression inside {{{{m:{stripped}}}}}: "
                    "unbalanced closing brace '}'.",
                    code="INVALID_ARGUMENT",
                )
        i += 1
    if depth_paren != 0:
        raise InvalidArgumentError(
            f"Invalid M expression inside {{{{m:{stripped}}}}}: "
            "unbalanced parentheses.",
            code="INVALID_ARGUMENT",
        )
    if depth_brack != 0:
        raise InvalidArgumentError(
            f"Invalid M expression inside {{{{m:{stripped}}}}}: "
            "unbalanced square brackets.",
            code="INVALID_ARGUMENT",
        )
    if depth_brace != 0:
        raise InvalidArgumentError(
            f"Invalid M expression inside {{{{m:{stripped}}}}}: "
            "unbalanced curly braces.",
            code="INVALID_ARGUMENT",
        )


def _scan_sql_quoted(
    text: str,
    start: int,
    *,
    quote: str,
    label: str,
    allow_backslash: bool,
) -> int:
    n = len(text)
    i = start + 1
    while i < n:
        if text.startswith("{{m:", i):
            end = scan_m_placeholder_end(text, i)
            if end < 0:
                raise InvalidArgumentError(
                    "Unclosed Dynamic M Parameter placeholder '{{m:...}}' in updated SQL.",
                    code="INVALID_ARGUMENT",
                )
            _validate_m_expr_balance(text[i + 4 : end - 2])
            i = end
            continue
        ch = text[i]
        if allow_backslash and ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == quote:
            if i + 1 < n and text[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    raise InvalidArgumentError(
        f"Unbalanced {label} ({quote}) in updated SQL.",
        code="INVALID_ARGUMENT",
    )


def validate_clean_sql_delimiters(clean_sql: str) -> None:
    """Verify balanced SQL quotes, backticks, parentheses, comments, and {{m:...}} placeholders."""
    n = len(clean_sql)
    i = 0
    paren_depth = 0
    while i < n:
        if clean_sql.startswith("--", i):
            nl = clean_sql.find("\n", i + 2)
            end = n if nl < 0 else nl + 1
            # Validate {{m:...}} inside line comment while ignoring SQL quotes/parens
            pos = i + 2
            while pos < end:
                m_idx = clean_sql.find("{{m:", pos, end)
                if m_idx < 0:
                    break
                m_end = scan_m_placeholder_end(clean_sql, m_idx)
                if m_end < 0 or m_end > end:
                    raise InvalidArgumentError(
                        "Unclosed Dynamic M Parameter placeholder '{{m:...}}' in updated SQL.",
                        code="INVALID_ARGUMENT",
                    )
                _validate_m_expr_balance(clean_sql[m_idx + 4 : m_end - 2])
                pos = m_end
            i = end
            continue
        if clean_sql.startswith("/*", i):
            close = clean_sql.find("*/", i + 2)
            if close < 0:
                raise InvalidArgumentError(
                    "Unclosed SQL block comment '/* ...' in updated SQL.",
                    code="INVALID_ARGUMENT",
                )
            pos = i + 2
            while pos < close:
                m_idx = clean_sql.find("{{m:", pos, close)
                if m_idx < 0:
                    break
                m_end = scan_m_placeholder_end(clean_sql, m_idx)
                if m_end < 0 or m_end > close:
                    raise InvalidArgumentError(
                        "Unclosed Dynamic M Parameter placeholder '{{m:...}}' in updated SQL.",
                        code="INVALID_ARGUMENT",
                    )
                _validate_m_expr_balance(clean_sql[m_idx + 4 : m_end - 2])
                pos = m_end
            i = close + 2
            continue
        if clean_sql.startswith("{{m:", i):
            end = scan_m_placeholder_end(clean_sql, i)
            if end < 0:
                raise InvalidArgumentError(
                    "Unclosed Dynamic M Parameter placeholder '{{m:...}}' in updated SQL.",
                    code="INVALID_ARGUMENT",
                )
            _validate_m_expr_balance(clean_sql[i + 4 : end - 2])
            i = end
            continue
        ch = clean_sql[i]
        if ch == "'":
            i = _scan_sql_quoted(
                clean_sql, i, quote="'", label="single quote", allow_backslash=True
            )
            continue
        if ch == '"':
            i = _scan_sql_quoted(
                clean_sql, i, quote='"', label="double quote", allow_backslash=True
            )
            continue
        if ch == "`":
            i = _scan_sql_quoted(
                clean_sql, i, quote="`", label="backtick", allow_backslash=False
            )
            continue
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
            if paren_depth < 0:
                raise InvalidArgumentError(
                    "Unbalanced closing parenthesis ')' in updated SQL.",
                    code="INVALID_ARGUMENT",
                )
        i += 1
    if paren_depth > 0:
        raise InvalidArgumentError(
            f"Unbalanced opening parenthesis '(' in updated SQL ({paren_depth} unclosed).",
            code="INVALID_ARGUMENT",
        )


def validate_m_reencoding(
    clean_sql: str,
    encoded_m: str,
    descriptor: MSerializationDescriptor,
) -> None:
    """Verify encoded M expression decodes cleanly back to the expected Clean SQL View."""
    try:
        redecoded, _, _ = decode_m_sql_expression(
            encoded_m,
            connector=descriptor.connector,
            fence_style=descriptor.fence_style,
            base_tmdl_indent=descriptor.base_tmdl_indent,
        )
    except ValueError as exc:
        raise InvalidArgumentError(
            f"Failed to re-encode updated SQL into valid Power Query M: {exc}",
            code="INVALID_ARGUMENT",
        ) from exc
    if redecoded != clean_sql:
        raise InvalidArgumentError(
            "Re-encoded Power Query M SQL failed round-trip verification against Clean SQL View.",
            code="INVALID_ARGUMENT",
        )


def detect_dynamic_param_warnings(before_sql: str, after_sql: str) -> list[str]:
    """Emit explicit warnings when an edit removes or modifies existing {{m:...}} parameters."""
    before_params = [val for kind, val in split_clean_sql_segments(before_sql) if kind == "expr"]
    if not before_params:
        return []
    after_params = [val for kind, val in split_clean_sql_segments(after_sql) if kind == "expr"]
    before_counts = Counter(before_params)
    after_counts = Counter(after_params)

    removed_completely: list[str] = []
    reduced_count: list[str] = []
    seen: set[str] = set()
    for param in before_params:
        if param in seen:
            continue
        seen.add(param)
        b_cnt = before_counts[param]
        a_cnt = after_counts.get(param, 0)
        if a_cnt == 0:
            removed_completely.append(param)
        elif a_cnt < b_cnt:
            reduced_count.append(param)

    warnings: list[str] = []
    if removed_completely:
        formatted = ", ".join(f"{{{{m:{p}}}}}" for p in removed_completely)
        warnings.append(
            f"Dynamic M Parameter(s) removed or modified: {formatted}. "
            "Verify this change is intentional before applying."
        )
    for param in reduced_count:
        warnings.append(
            f"Dynamic M Parameter {{{{m:{param}}}}} occurrence count decreased "
            f"({before_counts[param]} -> {after_counts[param]})."
        )
    return warnings


def apply_line_range_edit(
    clean_sql: str,
    *,
    start_line: int | None,
    end_line: int | None,
    new_text: str,
) -> tuple[str, int]:
    """Replace an inclusive 1-based line range in Clean SQL View."""
    patch = build_line_range_patch(
        clean_sql,
        start_line=start_line,
        end_line=end_line,
        new_text=new_text,
    )
    updated_sql = apply_text_patches(clean_sql, [patch])
    replacements = 0 if updated_sql == clean_sql else 1
    return updated_sql, replacements


def build_clean_sql_diff(
    rel_file: str,
    before_sql: str,
    after_sql: str,
    *,
    context_lines: int = 3,
) -> MutationDiff:
    """Build a unified diff over Clean SQL View without glued trailing lines."""
    from .mutation import build_mutation_diff

    before_norm = before_sql.replace("\r\n", "\n")
    after_norm = after_sql.replace("\r\n", "\n")
    if before_norm and not before_norm.endswith("\n"):
        before_norm += "\n"
    if after_norm and not after_norm.endswith("\n"):
        after_norm += "\n"
    return build_mutation_diff(
        rel_file,
        before_norm,
        after_norm,
        context_lines=context_lines,
    )


def _format_impact_counts(impact_summary: dict[str, Any] | None) -> str | None:
    if not impact_summary:
        return None
    parts = [f"{key}={val}" for key, val in impact_summary.items()]
    return ", ".join(parts) if parts else None


def format_update_table_sql_text(plan: dict[str, Any]) -> str:
    """Render compact, unescaped plain text for MCP update_table_sql."""
    status = str(plan.get("status") or ("dry_run" if plan.get("dry_run") else "applied"))
    target = str(plan.get("target_object") or "<unknown>")
    changed_files: list[str] = list(plan.get("changed_files") or [])
    replacements = int(plan.get("replacement_count", 0))
    hash_before = plan.get("sql_hash_before")
    hash_after = plan.get("sql_hash_after")

    header = [f"Status: {status}", f"Target: {target}"]
    if changed_files:
        header.append("File: " + ", ".join(changed_files))
    elif plan.get("file"):
        header.append(f"File: {plan['file']}")

    summary_line = f"Replacements: {replacements}"
    if hash_before and hash_after and hash_before != hash_after:
        summary_line += f" | sql_hash: {hash_before} -> {hash_after}"
    elif hash_before:
        summary_line += f" | sql_hash: {hash_before}"
    header.append(summary_line)

    blocks: list[str] = ["\n".join(header)]

    diffs: list[dict[str, Any]] = list(plan.get("diffs") or [])
    diff_chunks: list[str] = []
    for diff in diffs:
        unified = (diff.get("unified_diff") or "").rstrip("\n")
        if unified:
            diff_chunks.append(unified)
    if diff_chunks:
        blocks.append("Diff:\n" + "\n\n".join(diff_chunks))

    impact_line = _format_impact_counts(plan.get("impact_summary"))
    if impact_line:
        blocks.append(f"Impact: {impact_line}")

    warnings: list[str] = list(plan.get("warnings") or [])
    if warnings:
        blocks.append("Warnings:\n" + "\n".join(f"- {w}" for w in warnings))

    workflow = plan.get("workflow") or {}
    next_step = workflow.get("next_step")
    if status == "no_op":
        next_step = (
            "No matching SQL was changed; verify old_text or line range with get_table_sql."
        )
    elif not next_step:
        if status == "dry_run":
            next_step = (
                "Review the diff and warnings above, then re-invoke with dry_run=False to apply."
            )
        elif status == "applied":
            next_step = "Mutation applied and validated."
        elif status == "rolled_back":
            next_step = "Inspect validation errors above and retry with dry_run=True."
        else:
            next_step = "No changes were required."
    blocks.append(f"Next: {next_step}")

    return "\n\n".join(blocks)
