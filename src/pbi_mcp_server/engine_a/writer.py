from __future__ import annotations

import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from .parser import DECLARATION, FLAG_RE, PROPERTY_RE, quote_name, unquote


def atomic_write(path: Path, text: str, create_backup: bool = True) -> None:
    if create_backup and path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    fd, temp_name = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def restore_backup(path: Path) -> bool:
    backup = path.with_suffix(path.suffix + ".bak")
    if backup.exists():
        shutil.copy2(backup, path)
        return True
    return False


def replace_measure_declaration(text: str, old_name: str, new_name: str) -> tuple[str, int]:
    lines = text.splitlines(keepends=True)
    count = 0
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        match = DECLARATION.match(body)
        if not match or match.group("kind").lower() != "measure":
            continue
        if unquote(match.group("name")) != old_name:
            continue
        start, end = match.span("name")
        lines[index] = body[:start] + quote_name(new_name) + body[end:] + ending
        count += 1
    return "".join(lines), count


def _is_property_or_meta_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if PROPERTY_RE.match(line) or FLAG_RE.match(line):
        return True
    lower = stripped.lower()
    return (
        lower.startswith("annotation ")
        or lower.startswith("lineagetag:")
        or lower.startswith("changedproperty")
        or lower.startswith("formatstringdefinition")
        or lower.startswith("///")
        or DECLARATION.match(line) is not None
    )


def update_measure_expression(
    text: str,
    measure_name: str,
    expression: str,
    *,
    format_string: str | None = None,
    display_folder: str | None = None,
) -> tuple[str, int]:
    lines = text.splitlines(keepends=True)
    count = 0
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        body = line.rstrip("\r\n")
        ending = line[len(body) :] or "\n"
        match = DECLARATION.match(body)
        if not match or match.group("kind").lower() != "measure" or unquote(match.group("name")) != measure_name:
            out.append(line)
            i += 1
            continue

        indent_str = match.group("indent")
        decl_indent = len(indent_str.expandtabs(4))
        prop_indent = indent_str + ("\t" if "\t" in indent_str else "    ")
        prefix = body[: match.start("name")]
        old_expr = (match.group("expr") or "").strip()

        expr_lines = expression.strip().splitlines()
        if len(expr_lines) <= 1:
            single_expr = expr_lines[0] if expr_lines else ""
            out.append(f"{prefix}{match.group('name')} = {single_expr}{ending}")
        else:
            out.append(f"{prefix}{match.group('name')} = ```{ending}")
            for eline in expr_lines:
                out.append(f"{prop_indent}{eline}{ending}")
            out.append(f"{indent_str}```{ending}")

        count += 1
        i += 1

        # Skip old continuation lines if the measure had a fenced or multiline block
        if old_expr == "```":
            while i < len(lines):
                if lines[i].strip() == "```":
                    i += 1
                    break
                i += 1
        else:
            while i < len(lines):
                next_line = lines[i]
                next_stripped = next_line.strip()
                if not next_stripped:
                    break
                next_indent = len(next_line[: len(next_line) - len(next_line.lstrip())].expandtabs(4))
                if next_indent <= decl_indent or _is_property_or_meta_line(next_line):
                    break
                i += 1

        # Optionally update or insert formatString / displayFolder inside the measure's property block
        if format_string is not None or display_folder is not None:
            saw_format = False
            saw_folder = False
            while i < len(lines):
                p_line = lines[i]
                p_stripped = p_line.strip()
                if not p_stripped:
                    break
                p_indent = len(p_line[: len(p_line) - len(p_line.lstrip())].expandtabs(4))
                if p_indent <= decl_indent or DECLARATION.match(p_line.rstrip("\r\n")):
                    break
                pm = PROPERTY_RE.match(p_line.rstrip("\r\n"))
                if pm and pm.group("prop").lower() == "formatstring" and format_string is not None:
                    out.append(f"{prop_indent}formatString: {format_string}{ending}")
                    saw_format = True
                    i += 1
                    continue
                if pm and pm.group("prop").lower() == "displayfolder" and display_folder is not None:
                    out.append(f"{prop_indent}displayFolder: {display_folder}{ending}")
                    saw_folder = True
                    i += 1
                    continue
                out.append(p_line)
                i += 1

            if format_string is not None and not saw_format:
                out.append(f"{prop_indent}formatString: {format_string}{ending}")
            if display_folder is not None and not saw_folder:
                out.append(f"{prop_indent}displayFolder: {display_folder}{ending}")

    return "".join(out), count


def create_measure_declaration(
    text: str,
    measure_name: str,
    expression: str,
    *,
    format_string: str | None = None,
    display_folder: str | None = None,
    description: str | None = None,
    lineage_tag: str | None = None,
) -> tuple[str, int]:
    """Insert a brand-new measure declaration into a table's TMDL text before its partition block."""
    lines = text.splitlines(keepends=True)
    ending = "\r\n" if text.endswith("\r\n") else "\n"
    indent_unit = "\t"
    for line in lines:
        if line.startswith("    ") and not line.startswith("\t"):
            indent_unit = "    "
            break

    m_indent = indent_unit
    p_indent = indent_unit * 2
    tag = lineage_tag or str(uuid.uuid4())
    qname = quote_name(measure_name)

    block_lines: list[str] = [ending]
    if description:
        for dline in description.strip().splitlines():
            block_lines.append(f"{m_indent}/// {dline.strip()}{ending}")

    expr_lines = expression.strip().splitlines()
    if len(expr_lines) <= 1:
        single_expr = expr_lines[0] if expr_lines else ""
        block_lines.append(f"{m_indent}measure {qname} = {single_expr}{ending}")
    else:
        block_lines.append(f"{m_indent}measure {qname} = ```{ending}")
        for eline in expr_lines:
            block_lines.append(f"{p_indent}{eline}{ending}")
        block_lines.append(f"{m_indent}```{ending}")

    if format_string:
        block_lines.append(f"{p_indent}formatString: {format_string}{ending}")
    if display_folder:
        block_lines.append(f"{p_indent}displayFolder: {display_folder}{ending}")
    block_lines.append(f"{p_indent}lineageTag: {tag}{ending}")

    # Find insertion point: immediately before the first `partition ` or table-level `annotation PBI_ResultType`
    insert_idx = len(lines)
    for idx, line in enumerate(lines):
        stripped = line.strip().lower()
        if stripped.startswith("partition ") or stripped.startswith("annotation pbi_resulttype"):
            insert_idx = idx
            break

    new_lines = lines[:insert_idx] + block_lines + [ending] + lines[insert_idx:]
    return "".join(new_lines), 1


def update_measure_description(text: str, measure_name: str, description: str) -> tuple[str, int]:
    lines = text.splitlines(keepends=True)
    count = 0
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        body = line.rstrip("\r\n")
        ending = line[len(body) :] or "\n"
        match = DECLARATION.match(body)
        if match and match.group("kind").lower() == "measure" and unquote(match.group("name")) == measure_name:
            indent = match.group("indent")
            while out and out[-1].strip().startswith("///"):
                out.pop()
            for doc_line in description.strip().splitlines():
                out.append(f"{indent}/// {doc_line.strip()}{ending}")
            out.append(line)
            count += 1
        else:
            out.append(line)
        i += 1
    return "".join(out), count


def replace_exact_references(text: str, old_name: str, new_name: str) -> tuple[str, int]:
    patterns = [
        (re.compile(rf"\[{re.escape(old_name)}\]"), f"[{new_name}]"),
        (re.compile(rf"(?<![\w]){re.escape(old_name)}(?![\w])"), new_name),
    ]
    total = 0
    output = text
    for pattern, replacement in patterns:
        output, count = pattern.subn(replacement, output)
        total += count
    return output, total


def replace_object_declaration(text: str, kind: str, old_name: str, new_name: str) -> tuple[str, int]:
    lines = text.splitlines(keepends=True)
    count = 0
    normalized = kind.replace("_", "").casefold()
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        m = DECLARATION.match(body)
        if (
            not m
            or m.group("kind").replace("_", "").casefold() != normalized
            or unquote(m.group("name")) != old_name
        ):
            continue
        a, b = m.span("name")
        lines[i] = body[:a] + quote_name(new_name) + body[b:] + ending
        count += 1
    return "".join(lines), count


def delete_measure_declaration(text: str, measure_name: str) -> tuple[str, int]:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    count = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        body = line.rstrip("\r\n")
        m = DECLARATION.match(body)
        if m and m.group("kind").casefold() == "measure" and unquote(m.group("name")) == measure_name:
            # Remove preceding /// doc lines
            while out and out[-1].strip().startswith("///"):
                out.pop()
            decl_indent = len(m.group("indent").expandtabs(4))
            old_expr = (m.group("expr") or "").strip()
            count += 1
            i += 1
            if old_expr == "```":
                while i < len(lines):
                    if lines[i].strip() == "```":
                        i += 1
                        break
                    i += 1
            # Skip indented property/annotation lines belonging to this measure
            while i < len(lines):
                nline = lines[i]
                nstripped = nline.strip()
                if not nstripped:
                    i += 1
                    break
                nindent = len(nline[: len(nline) - len(nline.lstrip())].expandtabs(4))
                if nindent <= decl_indent or DECLARATION.match(nline.rstrip("\r\n")):
                    break
                i += 1
            continue
        out.append(line)
        i += 1
    return "".join(out), count


def upsert_relationship_declaration(
    text: str,
    relationship_name: str,
    *,
    from_column: str | None = None,
    to_column: str | None = None,
    cross_filtering: str | None = None,
    is_active: bool | None = None,
    from_cardinality: str | None = None,
    to_cardinality: str | None = None,
    create_if_missing: bool = False,
) -> tuple[str, int]:
    """Update an existing relationship in relationships.tmdl or append a new one if create_if_missing=True."""
    lines = text.splitlines(keepends=True)
    ending = "\r\n" if text.endswith("\r\n") else "\n"
    indent_unit = "\t"
    count = 0
    out: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        body = line.rstrip("\r\n")
        m = DECLARATION.match(body)
        if not m or m.group("kind").lower() != "relationship" or unquote(m.group("name")) != relationship_name:
            out.append(line)
            i += 1
            continue

        decl_indent = len(m.group("indent").expandtabs(4))
        prop_indent = m.group("indent") + indent_unit
        out.append(line)
        count += 1
        i += 1

        props: dict[str, str] = {}
        other_lines: list[str] = []
        while i < len(lines):
            p_line = lines[i]
            p_stripped = p_line.strip()
            if not p_stripped:
                break
            p_indent = len(p_line[: len(p_line) - len(p_line.lstrip())].expandtabs(4))
            if p_indent <= decl_indent or DECLARATION.match(p_line.rstrip("\r\n")):
                break
            pm = PROPERTY_RE.match(p_line.rstrip("\r\n"))
            if pm:
                props[pm.group("prop").lower()] = pm.group("val").strip()
            else:
                other_lines.append(p_line)
            i += 1

        if from_column is not None:
            props["fromcolumn"] = from_column
        if to_column is not None:
            props["tocolumn"] = to_column
        if cross_filtering is not None:
            props["crossfilteringbehavior"] = cross_filtering
        if is_active is not None:
            props["isactive"] = "true" if is_active else "false"
        if from_cardinality is not None:
            props["fromcardinality"] = from_cardinality
        if to_cardinality is not None:
            props["tocardinality"] = to_cardinality

        canonical_keys = [
            ("fromcolumn", "fromColumn"),
            ("tocolumn", "toColumn"),
            ("fromcardinality", "fromCardinality"),
            ("tocardinality", "toCardinality"),
            ("crossfilteringbehavior", "crossFilteringBehavior"),
            ("isactive", "isActive"),
        ]
        for low_key, display_key in canonical_keys:
            if low_key in props:
                out.append(f"{prop_indent}{display_key}: {props[low_key]}{ending}")
        out.extend(other_lines)

    if count == 0 and create_if_missing:
        if not from_column or not to_column:
            raise ValueError("from_column and to_column are required when creating a new relationship.")
        qname = quote_name(relationship_name)
        block: list[str] = []
        if out and out[-1].strip():
            block.append(ending)
        block.append(f"relationship {qname}{ending}")
        block.append(f"{indent_unit}fromColumn: {from_column}{ending}")
        block.append(f"{indent_unit}toColumn: {to_column}{ending}")
        if from_cardinality:
            block.append(f"{indent_unit}fromCardinality: {from_cardinality}{ending}")
        if to_cardinality:
            block.append(f"{indent_unit}toCardinality: {to_cardinality}{ending}")
        if cross_filtering:
            block.append(f"{indent_unit}crossFilteringBehavior: {cross_filtering}{ending}")
        if is_active is not None and not is_active:
            block.append(f"{indent_unit}isActive: false{ending}")
        out.extend(block)
        count = 1

    return "".join(out), count
