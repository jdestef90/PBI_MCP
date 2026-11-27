"""Reusable Partition SQL edit helpers, guardrails, clean-SQL diffs, and plain-text formatting."""

from __future__ import annotations

import difflib
import re
from collections import Counter
from typing import Any

from .errors import InvalidArgumentError, PreconditionFailedError
from .m_codec import (
    MSerializationDescriptor,
    decode_m_sql_expression,
    scan_m_placeholder_end,
    split_clean_sql_segments,
)
from .models import MutationDiff

_PIPE_PREFIX_RE = re.compile(r"^[ \t]*\d+[ \t]*\|(?: |$)")
_COLON_PREFIX_RE = re.compile(r"^[ \t]*\d+:(?: |$)")


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
    old_norm = _normalize_newlines(old_text)
    new_norm = _normalize_newlines(new_text)

    exact_count = clean_sql.count(old_norm)
    if exact_count > 0:
        return clean_sql.replace(old_norm, new_norm), exact_count, "exact"

    sql_lines = clean_sql.split("\n")
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
        return clean_sql, 0, "none"

    old_stripped = [ln.strip(" \t") for ln in old_lines]
    sql_stripped = [ln.strip(" \t") for ln in sql_lines]

    match_starts: list[int] = []
    idx = 0
    limit = len(sql_lines) - span_len
    while idx <= limit:
        if sql_stripped[idx : idx + span_len] == old_stripped:
            match_starts.append(idx)
            idx += span_len
        else:
            idx += 1

    if not match_starts:
        return clean_sql, 0, "none"

    if len(match_starts) > 1 and expected_replacements is None:
        raise PreconditionFailedError(
            f"Ambiguous whitespace-normalized match: found {len(match_starts)} matching "
            "locations. Provide more context, exact indentation, or pass "
            f"expected_replacements={len(match_starts)}.",
            code="PRECONDITION_FAILED",
            expected=1,
            actual=len(match_starts),
        )

    out_lines: list[str] = []
    cursor = 0
    for start in match_starts:
        out_lines.extend(sql_lines[cursor:start])
        matched_block = sql_lines[start : start + span_len]
        out_lines.extend(_reindent_replacement(matched_block, old_lines, new_lines))
        cursor = start + span_len
    out_lines.extend(sql_lines[cursor:])

    return "\n".join(out_lines), len(match_starts), "whitespace_normalized"


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
    if start_line is None:
        raise InvalidArgumentError(
            "start_line is required when end_line is provided.",
            code="INVALID_ARGUMENT",
        )
    eff_end = start_line if end_line is None else end_line
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

    norm_new = new_text.replace("\r\n", "\n").replace("\r", "\n")
    replacement_lines = [] if norm_new == "" else norm_new.splitlines()
    updated_lines = lines[: start_line - 1] + replacement_lines + lines[eff_end:]
    updated_sql = "\n".join(updated_lines)
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
    before_norm = before_sql.replace("\r\n", "\n")
    after_norm = after_sql.replace("\r\n", "\n")
    if before_norm and not before_norm.endswith("\n"):
        before_norm += "\n"
    if after_norm and not after_norm.endswith("\n"):
        after_norm += "\n"
    diff_text = "".join(
        difflib.unified_diff(
            before_norm.splitlines(keepends=True),
            after_norm.splitlines(keepends=True),
            fromfile=f"a/{rel_file}",
            tofile=f"b/{rel_file}",
            n=context_lines,
        )
    )
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
