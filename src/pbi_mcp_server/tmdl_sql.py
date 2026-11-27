from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import (
    AmbiguousObjectError,
    InvalidArgumentError,
    ObjectNotFoundError,
    PreconditionFailedError,
)
from .m_codec import (
    FenceStyle,
    MSerializationDescriptor,
    compute_sql_hash,
    decode_m_sql_expression,
    encode_sql_to_m,
    extract_m_sql_target,
    split_clean_sql_segments,
)
from .sql_edit import (
    apply_line_range_edit,
    apply_text_edit,
    detect_dynamic_param_warnings,
    strip_line_number_prefixes,
    validate_clean_sql_delimiters,
    validate_m_reencoding,
    verify_sql_hash_precondition,
)
from .sql_sections import SqlSection, SqlStructure, scan_sql_structure, unquote_sql_identifier

_PARTITION_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)partition\s+"
    r"(?P<name>'(?:''|[^'])*'|[^=\r\n]+?)\s*=\s*(?P<ptype>m|calculated)\s*$"
)
_MODE_RE = re.compile(r"(?m)^[ \t]*mode\s*:\s*(?P<mode>[A-Za-z0-9_]+)\s*$")
_SOURCE_FENCED_RE = re.compile(r"(?m)^(?P<indent>[ \t]*)source\s*=\s*```[ \t]*\r?\n")
_SOURCE_UNFENCED_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)source\s*=(?![ \t]*```)[ \t]*(?P<inline>[^\r\n]*)(?:\r?\n|$)"
)
_TABLE_RE = re.compile(r"(?m)^table\s+(?P<name>'(?:''|[^'])*'|[^\r\n]+?)\s*$")

_M_PARAM_RE = re.compile(r'"\s*&\s*([A-Za-z_][A-Za-z0-9_]*)\s*&\s*"')
_SQL_DB_RE = re.compile(r'Sql\.Database\(\s*"([^"]+)"\s*,\s*"([^"]+)"')
_M_ITEM_RE = re.compile(r'Schema\s*=\s*"([^"]+)"\s*,\s*Item\s*=\s*"([^"]+)"')

OUTLINE_LINE_THRESHOLD = 120
DEFAULT_LINE_WINDOW = 120
CTE_MAX_LINES = 400


def _add_line_numbers(text: str) -> str:
    return "\n".join(f"{idx}: {line}" for idx, line in enumerate(text.splitlines(), 1))


def extract_sql_metadata(
    sql_expression: str, *, structure: SqlStructure | None = None
) -> dict[str, Any]:
    """Extract projected fields, source tables, and dynamic M parameters from SQL."""
    dynamic_params: list[str] = []
    if "{{m:" in sql_expression:
        for kind, val in split_clean_sql_segments(sql_expression):
            if kind == "expr" and val not in dynamic_params:
                dynamic_params.append(val)
    for m in _M_PARAM_RE.finditer(sql_expression):
        param = m.group(1)
        if param not in dynamic_params:
            dynamic_params.append(param)

    if structure is None:
        cleaned = _M_PARAM_RE.sub(lambda m: f"{{{{m:{m.group(1)}}}}}", sql_expression)
        cleaned = cleaned.strip()
        if cleaned.startswith('""') and cleaned.endswith('""'):
            cleaned = cleaned[2:-2]
        elif cleaned.startswith('"') and cleaned.endswith('"'):
            cleaned = cleaned[1:-1]
        structure = scan_sql_structure(cleaned)

    return {
        "selected_fields": list(structure.selected_fields),
        "source_tables": list(structure.source_tables),
        "has_dynamic_parameters": bool(dynamic_params),
        "dynamic_parameters": dynamic_params,
        "has_ctes": structure.has_ctes,
        "sql_sections": [s.name for s in structure.sections],
        "sql_section_count": len(structure.sections),
    }


@dataclass(frozen=True)
class SqlSource:
    file: Path
    table: str | None
    partition: str
    source_start: int
    source_end: int
    sql_start: int
    sql_end: int
    m_expression: str
    sql_expression: str
    mode: str | None = None
    connector: str = "Value.NativeQuery"
    clean_sql: str = ""
    sql_hash: str = ""
    descriptor: MSerializationDescriptor | None = None
    dynamic_parameters: tuple[str, ...] = ()

    def as_dict(
        self,
        root: Path | None = None,
        include_sql: bool = True,
        *,
        structure: SqlStructure | None = None,
    ) -> dict[str, Any]:
        if root:
            file_value = str(self.file.relative_to(root)).replace("\\", "/")
        else:
            file_value = str(self.file).replace("\\", "/")
        clean = self.clean_sql or self.sql_expression
        meta = extract_sql_metadata(clean, structure=structure)
        if self.dynamic_parameters:
            merged_params = list(self.dynamic_parameters)
            for p in meta["dynamic_parameters"]:
                if p not in merged_params:
                    merged_params.append(p)
            meta["dynamic_parameters"] = merged_params
            meta["has_dynamic_parameters"] = bool(merged_params)
        line_count = len(clean.splitlines())
        sql_hash = self.sql_hash or compute_sql_hash(clean)
        result: dict[str, Any] = {
            "file": file_value,
            "table": self.table,
            "partition": self.partition,
            "mode": self.mode,
            "connector": self.connector,
            "line_count": line_count,
            "sql_hash": sql_hash,
            "has_ctes": bool(meta.get("has_ctes", False)),
            "sql_sections": list(meta.get("sql_sections") or []),
            "sql_section_count": int(meta.get("sql_section_count", 0)),
            "source_span": {"start": self.source_start, "end": self.source_end},
            "sql_span": {"start": self.sql_start, "end": self.sql_end},
            "sql_metadata": meta,
        }
        if include_sql:
            result["sql"] = clean
            result["sql_m_expression"] = self.sql_expression
            result["sql_with_line_numbers"] = _add_line_numbers(clean)
            result["source_m_expression"] = self.m_expression
            if self.descriptor is not None:
                result["serialization"] = self.descriptor.to_dict()
        return result

    def to_summary(self, root: Path | None = None) -> dict[str, Any]:
        return self.as_dict(root=root, include_sql=False)


def _unquote_tmdl_name(value: str) -> str:
    value = value.strip()
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value


def _find_fence_end(text: str, start: int) -> int:
    match = re.search(r"(?m)^[ \t]*```[ \t]*(?:\r?\n|$)", text[start:])
    if not match:
        raise ValueError("TMDL source block starts with ``` but has no closing fence.")
    return start + match.start()


def _extract_partition_source_block(
    text: str, part_end: int, limit: int
) -> tuple[int, int, FenceStyle, str, int] | None:
    """Return `(source_start, source_end, fence_style, base_indent, header_start)`."""
    fenced = _SOURCE_FENCED_RE.search(text, part_end, limit)
    unfenced = _SOURCE_UNFENCED_RE.search(text, part_end, limit)

    if fenced and (not unfenced or fenced.start() <= unfenced.start()):
        source_start = fenced.end()
        source_end = _find_fence_end(text, source_start)
        return source_start, source_end, "fenced", fenced.group("indent"), fenced.start()

    if not unfenced:
        return None

    base_indent = unfenced.group("indent")
    base_width = len(base_indent.expandtabs(4))
    inline = unfenced.group("inline").strip()
    source_start = unfenced.start("inline") if inline else unfenced.end()
    last_content_end = unfenced.end("inline") if inline else unfenced.end()

    pos = unfenced.end()
    while pos < limit:
        nl = text.find("\n", pos, limit)
        line_end = limit if nl < 0 else nl + 1
        raw_line = text[pos:line_end].rstrip("\r\n")
        if raw_line.strip() == "":
            pos = line_end
            continue
        indent_str = raw_line[: len(raw_line) - len(raw_line.lstrip(" \t"))]
        if len(indent_str.expandtabs(4)) <= base_width:
            break
        last_content_end = line_end
        pos = line_end

    return source_start, last_content_end, "unfenced", base_indent, unfenced.start()


def parse_tmdl_sql_sources(path: Path) -> list[SqlSource]:
    text = path.read_text(encoding="utf-8-sig")
    table_match = _TABLE_RE.search(text)
    table_name = _unquote_tmdl_name(table_match.group("name")) if table_match else None
    partitions = list(_PARTITION_RE.finditer(text))
    results: list[SqlSource] = []
    for index, partition_match in enumerate(partitions):
        limit = partitions[index + 1].start() if index + 1 < len(partitions) else len(text)
        extracted = _extract_partition_source_block(text, partition_match.end(), limit)
        if not extracted:
            continue
        source_start, source_end, fence_style, base_indent, header_start = extracted
        meta_block = text[partition_match.end() : header_start] + text[source_end:limit]
        mode_m = _MODE_RE.search(meta_block)
        mode = mode_m.group("mode") if mode_m else None

        source = text[source_start:source_end]
        target = extract_m_sql_target(source, source_start)
        if target is None:
            continue
        connector, sql_start, sql_end = target
        raw_sql_m = text[sql_start:sql_end]
        clean_sql, descriptor, dyn_params = decode_m_sql_expression(
            raw_sql_m,
            connector=connector,
            fence_style=fence_style,
            base_tmdl_indent=base_indent,
        )
        results.append(
            SqlSource(
                path,
                table_name,
                _unquote_tmdl_name(partition_match.group("name")),
                source_start,
                source_end,
                sql_start,
                sql_end,
                source,
                raw_sql_m,
                mode=mode,
                connector=connector,
                clean_sql=clean_sql,
                sql_hash=compute_sql_hash(clean_sql),
                descriptor=descriptor,
                dynamic_parameters=tuple(dyn_params),
            )
        )
    return results


def discover_partition_metadata(
    semantic_model_dir: Path | None, root: Path
) -> list[dict[str, Any]]:
    """Discover all table partitions (native SQL and standard M/Sql.Database)."""
    if not semantic_model_dir or not semantic_model_dir.exists():
        return []
    tables_dir = semantic_model_dir / "definition" / "tables"
    if not tables_dir.exists():
        return []

    partitions_out: list[dict[str, Any]] = []
    for path in sorted(tables_dir.rglob("*.tmdl"), key=lambda p: str(p).casefold()):
        text = path.read_text(encoding="utf-8-sig")
        table_match = _TABLE_RE.search(text)
        table_name = _unquote_tmdl_name(table_match.group("name")) if table_match else path.stem
        try:
            rel_file = str(path.relative_to(root)).replace("\\", "/")
        except ValueError:
            rel_file = str(path).replace("\\", "/")

        native_sources = {s.partition: s for s in parse_tmdl_sql_sources(path)}
        p_matches = list(_PARTITION_RE.finditer(text))
        for idx, pm in enumerate(p_matches):
            p_name = _unquote_tmdl_name(pm.group("name"))
            p_type = pm.group("ptype")
            line_num = text[: pm.start()].count("\n") + 1
            limit = p_matches[idx + 1].start() if idx + 1 < len(p_matches) else len(text)
            block = text[pm.end() : limit]
            mode_m = _MODE_RE.search(block)
            mode = mode_m.group("mode") if mode_m else "import"

            if p_name in native_sources:
                ns = native_sources[p_name]
                meta = extract_sql_metadata(ns.clean_sql or ns.sql_expression)
                sql_start_line = text[: ns.sql_start].count("\n") + 1
                partitions_out.append({
                    "name": p_name,
                    "partition": p_name,
                    "table": table_name,
                    "partition_type": p_type,
                    "source_type": "native_sql",
                    "connector": ns.connector,
                    "mode": mode,
                    "has_native_sql": True,
                    "source_tables": meta["source_tables"],
                    "sql_fields": meta["selected_fields"],
                    "has_dynamic_parameters": meta["has_dynamic_parameters"],
                    "dynamic_parameters": meta["dynamic_parameters"],
                    "m_parameters": meta["dynamic_parameters"],
                    "file": rel_file,
                    "line": line_num,
                    "sql_start_line": sql_start_line,
                })
            else:
                source_tables: list[str] = []
                db_m = _SQL_DB_RE.search(block)
                item_m = _M_ITEM_RE.search(block)
                if item_m:
                    schema_name, item_name = item_m.group(1), item_m.group(2)
                    if db_m:
                        server_name, db_name = db_m.group(1), db_m.group(2)
                        source_tables.append(f"{server_name}/{db_name}.{schema_name}.{item_name}")
                    source_tables.append(f"{schema_name}.{item_name}")
                partitions_out.append({
                    "name": p_name,
                    "partition": p_name,
                    "table": table_name,
                    "partition_type": p_type,
                    "source_type": "m_table" if source_tables else p_type,
                    "mode": mode,
                    "has_native_sql": False,
                    "source_tables": source_tables,
                    "sql_fields": [],
                    "has_dynamic_parameters": False,
                    "dynamic_parameters": [],
                    "m_parameters": [],
                    "file": rel_file,
                    "line": line_num,
                    "sql_start_line": line_num,
                })
    return partitions_out


def discover_tmdl_sql_sources(semantic_model_dir: Path) -> list[SqlSource]:
    tables_dir = semantic_model_dir / "definition" / "tables"
    if not tables_dir.exists():
        return []
    return [
        source
        for path in sorted(tables_dir.rglob("*.tmdl"), key=lambda p: str(p).casefold())
        for source in parse_tmdl_sql_sources(path)
    ]


def _select_sources(
    semantic_model_dir: Path,
    *,
    table: str | None = None,
    partition: str | None = None,
    relative_file: str | None = None,
) -> list[SqlSource]:
    selected = []
    for source in discover_tmdl_sql_sources(semantic_model_dir):
        if table is not None and (source.table or "").casefold() != table.strip().casefold():
            continue
        if partition is not None and source.partition.casefold() != partition.strip().casefold():
            continue
        if (
            relative_file is not None
            and source.file.resolve() != (semantic_model_dir / relative_file).resolve()
        ):
            continue
        selected.append(source)
    return selected


def list_table_sql_sources(semantic_model_dir: Path) -> list[dict[str, Any]]:
    return [
        s.as_dict(semantic_model_dir, include_sql=False)
        for s in discover_tmdl_sql_sources(semantic_model_dir)
    ]


@dataclass(frozen=True, slots=True)
class _CteSelection:
    chunks: list[dict[str, Any]]
    combined_sql: str
    truncated: bool
    returned_line_count: int
    total_requested_lines: int
    warnings: list[str]
    next_hints: list[str]


def _format_hint_call(
    table: str | None,
    partition: str | None,
    **selector_kwargs: Any,
) -> str:
    tbl = table or "<table_name>"
    parts = [f"table_name={tbl!r}"]
    if partition is not None:
        parts.append(f"partition={partition!r}")
    for key, val in selector_kwargs.items():
        parts.append(f"{key}={val!r}")
    return f"get_table_sql({', '.join(parts)})"


def _build_outline_hints(
    table: str | None,
    *,
    partition: str | None = None,
    has_ctes: bool,
    section_names: list[str],
    line_count: int,
) -> list[str]:
    hints: list[str] = []
    if has_ctes and section_names:
        sample = section_names[:2] if len(section_names) >= 2 else section_names
        if "main" in section_names and "main" not in sample:
            sample = [section_names[0], "main"]
        hints.append(_format_hint_call(table, partition, ctes=sample))
    window_end = min(line_count, DEFAULT_LINE_WINDOW) if line_count > 0 else 1
    hints.append(_format_hint_call(table, partition, start_line=1, end_line=window_end))
    return hints


def _normalize_cte_names(ctes: list[str] | str) -> list[str]:
    raw_items = [ctes] if isinstance(ctes, str) else list(ctes)
    normalized: list[str] = []
    seen_cf: set[str] = set()
    for item in raw_items:
        cleaned = unquote_sql_identifier(item.strip())
        if not cleaned:
            continue
        cf = cleaned.casefold()
        if cf not in seen_cf:
            seen_cf.add(cf)
            normalized.append(cleaned)
    if not normalized:
        raise InvalidArgumentError(
            "ctes must contain at least one non-empty SQL Section name.",
            code="INVALID_ARGUMENT",
        )
    return normalized


def _select_cte_sections(
    *,
    requested: list[str],
    sections: tuple[SqlSection, ...],
    lines: list[str],
    table: str | None,
    partition: str | None = None,
) -> _CteSelection:
    by_cf = {s.name.casefold(): s for s in sections}
    available = [s.name for s in sections]
    missing = [name for name in requested if name.casefold() not in by_cf]
    if missing:
        avail_str = ", ".join(available) if available else "(none)"
        raise ObjectNotFoundError(
            f"Unknown SQL Section(s): {', '.join(missing)}. "
            f"Available SQL Sections: {avail_str}.",
            code="OBJECT_NOT_FOUND",
            details={
                "requested": requested,
                "missing": missing,
                "available_sections": available,
            },
        )

    matched = [by_cf[name.casefold()] for name in requested]
    total_requested = sum(sec.line_count for sec in matched)
    remaining = CTE_MAX_LINES
    selected_chunks: list[dict[str, Any]] = []
    returned_parts: list[str] = []
    returned_line_count = 0
    truncated = False
    partial_continuation: tuple[int, int] | None = None
    unreturned_sections: list[SqlSection] = []

    for sec in matched:
        sec_lines = lines[sec.start_line - 1 : sec.end_line]
        if remaining <= 0:
            truncated = True
            unreturned_sections.append(sec)
            continue

        is_partial = len(sec_lines) > remaining
        kept = sec_lines[:remaining] if is_partial else sec_lines
        ret_end = sec.start_line + len(kept) - 1
        chunk_sql = "\n".join(kept)
        selected_chunks.append({
            "name": sec.name,
            "kind": sec.kind,
            "start_line": sec.start_line,
            "end_line": sec.end_line,
            "returned_start_line": sec.start_line,
            "returned_end_line": ret_end,
            "truncated": is_partial,
            "sql": chunk_sql,
        })
        returned_parts.append(chunk_sql)
        returned_line_count += len(kept)
        remaining -= len(kept)
        if is_partial:
            truncated = True
            partial_continuation = (ret_end + 1, sec.end_line)

    warnings: list[str] = []
    next_hints: list[str] = []
    if truncated:
        warnings.append(
            f"Requested SQL Sections span {total_requested} lines, exceeding the "
            f"{CTE_MAX_LINES}-line single-call safety ceiling; truncated to "
            f"{returned_line_count} lines."
        )
        if partial_continuation is not None:
            cont_start, sec_end = partial_continuation
            window_end = min(sec_end, cont_start + DEFAULT_LINE_WINDOW - 1)
            next_hints.append(
                _format_hint_call(
                    table, partition, start_line=cont_start, end_line=window_end
                )
            )
        if unreturned_sections:
            # If the next unreturned section is contiguous with a partial continuation,
            # still give an explicit ctes/line hint for remaining requested sections.
            if (
                len(unreturned_sections) == 1
                and unreturned_sections[0].line_count > CTE_MAX_LINES
            ):
                rem_sec = unreturned_sections[0]
                rem_end = min(rem_sec.end_line, rem_sec.start_line + DEFAULT_LINE_WINDOW - 1)
                next_hints.append(
                    _format_hint_call(
                        table,
                        partition,
                        start_line=rem_sec.start_line,
                        end_line=rem_end,
                    )
                )
            else:
                next_hints.append(
                    _format_hint_call(
                        table,
                        partition,
                        ctes=[s.name for s in unreturned_sections],
                    )
                )

    return _CteSelection(
        chunks=selected_chunks,
        combined_sql="\n".join(returned_parts),
        truncated=truncated,
        returned_line_count=returned_line_count,
        total_requested_lines=total_requested,
        warnings=warnings,
        next_hints=next_hints,
    )


def _slice_sql_lines(
    *,
    lines: list[str],
    start_line: int | None,
    end_line: int | None,
    table: str | None,
    partition: str | None = None,
) -> tuple[str, int, int, list[str]]:
    line_count = len(lines)
    if start_line is not None and start_line < 1:
        raise InvalidArgumentError(
            f"start_line must be >= 1 (received {start_line}).",
            code="INVALID_ARGUMENT",
        )
    if end_line is not None and end_line < 1:
        raise InvalidArgumentError(
            f"end_line must be >= 1 (received {end_line}).",
            code="INVALID_ARGUMENT",
        )
    eff_start = start_line if start_line is not None else 1
    if end_line is not None and end_line < eff_start:
        raise InvalidArgumentError(
            f"end_line ({end_line}) must be >= start_line ({eff_start}).",
            code="INVALID_ARGUMENT",
        )
    if line_count == 0:
        return "", 1, 0, []
    if eff_start > line_count:
        raise InvalidArgumentError(
            f"start_line ({eff_start}) exceeds total query line count ({line_count}).",
            code="INVALID_ARGUMENT",
        )
    if end_line is None:
        eff_end = min(line_count, eff_start + DEFAULT_LINE_WINDOW - 1)
    else:
        eff_end = min(line_count, end_line)

    sliced = "\n".join(lines[eff_start - 1 : eff_end])
    next_hints: list[str] = []
    if eff_end < line_count:
        next_start = eff_end + 1
        next_end = min(line_count, next_start + DEFAULT_LINE_WINDOW - 1)
        next_hints.append(
            _format_hint_call(table, partition, start_line=next_start, end_line=next_end)
        )
    return sliced, eff_start, eff_end, next_hints


def get_table_sql(
    semantic_model_dir: Path,
    *,
    table: str | None = None,
    partition: str | None = None,
    relative_file: str | None = None,
    outline: bool = False,
    ctes: list[str] | str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    include_full_sql: bool = False,
) -> dict[str, Any]:
    matches = _select_sources(
        semantic_model_dir,
        table=table,
        partition=partition,
        relative_file=relative_file,
    )
    if not matches:
        raise ObjectNotFoundError(
            "No Partition SQL source matched the supplied table, partition, or file selector.",
            code="OBJECT_NOT_FOUND",
            details={"table": table, "partition": partition, "relative_file": relative_file},
        )
    if len(matches) > 1:
        raise AmbiguousObjectError(
            "The selector matched multiple SQL sources. "
            "Supply an exact table, partition, or relative_file.",
            code="AMBIGUOUS_OBJECT",
            details={
                "candidates": [
                    {"table": m.table, "partition": m.partition, "file": str(m.file)}
                    for m in matches
                ]
            },
        )
    source = matches[0]
    hint_partition = source.partition if partition is not None else None
    clean = source.clean_sql or source.sql_expression
    lines = clean.splitlines()
    line_count = len(lines)
    structure = scan_sql_structure(clean)
    sections_dicts = [s.to_dict() for s in structure.sections]
    clauses_dicts = [c.to_dict() for c in structure.clauses]
    has_line_range = start_line is not None or end_line is not None

    if not include_full_sql and ctes is not None and has_line_range:
        raise InvalidArgumentError(
            "Cannot combine ctes with start_line/end_line; use one selector at a time.",
            code="INVALID_ARGUMENT",
        )

    if not include_full_sql and ctes is not None:
        if outline:
            raise InvalidArgumentError(
                "Cannot combine outline=True with ctes selector.",
                code="INVALID_ARGUMENT",
            )
        req_names = _normalize_cte_names(ctes)
        selection = _select_cte_sections(
            requested=req_names,
            sections=structure.sections,
            lines=lines,
            table=source.table,
            partition=hint_partition,
        )
        base = source.as_dict(semantic_model_dir, include_sql=False, structure=structure)
        base["view"] = "ctes"
        base["sql"] = selection.combined_sql
        base["sections"] = sections_dicts
        base["clauses"] = clauses_dicts
        base["selected_sections"] = selection.chunks
        base["truncated"] = selection.truncated
        base["returned_line_count"] = selection.returned_line_count
        base["total_requested_lines"] = selection.total_requested_lines
        base["warnings"] = selection.warnings
        base["next_hints"] = selection.next_hints
        return base

    if not include_full_sql and has_line_range:
        if outline:
            raise InvalidArgumentError(
                "Cannot combine outline=True with start_line/end_line.",
                code="INVALID_ARGUMENT",
            )
        sliced_sql, eff_start, eff_end, next_hints = _slice_sql_lines(
            lines=lines,
            start_line=start_line,
            end_line=end_line,
            table=source.table,
            partition=hint_partition,
        )
        base = source.as_dict(semantic_model_dir, include_sql=False, structure=structure)
        base["view"] = "lines"
        base["sql"] = sliced_sql
        base["start_line"] = eff_start
        base["end_line"] = eff_end
        base["sections"] = sections_dicts
        base["clauses"] = clauses_dicts
        base["warnings"] = []
        base["next_hints"] = next_hints
        return base

    show_outline = (
        not include_full_sql and (outline or line_count > OUTLINE_LINE_THRESHOLD)
    )
    if show_outline:
        base = source.as_dict(semantic_model_dir, include_sql=False, structure=structure)
        base["view"] = "outline"
        base["sql"] = None
        base["sections"] = sections_dicts
        base["clauses"] = clauses_dicts
        base["warnings"] = []
        base["next_hints"] = _build_outline_hints(
            source.table,
            partition=hint_partition,
            has_ctes=structure.has_ctes,
            section_names=[s.name for s in structure.sections],
            line_count=line_count,
        )
        return base

    full_res = source.as_dict(semantic_model_dir, include_sql=True, structure=structure)
    full_res["view"] = "full"
    full_res["sections"] = sections_dicts
    full_res["clauses"] = clauses_dicts
    full_res["warnings"] = []
    full_res["next_hints"] = []
    return full_res


def search_table_sql(
    semantic_model_dir: Path,
    pattern: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
) -> dict[str, Any]:
    if not pattern:
        raise InvalidArgumentError("pattern must not be empty.", code="INVALID_ARGUMENT")
    flags = 0 if case_sensitive else re.IGNORECASE
    compiled = re.compile(
        pattern if regex else re.escape(pattern), flags | re.MULTILINE | re.DOTALL
    )
    results = []
    for source in discover_tmdl_sql_sources(semantic_model_dir):
        target_sql = source.clean_sql or source.sql_expression
        matches = list(compiled.finditer(target_sql))
        if matches:
            results.append({
                **source.as_dict(semantic_model_dir, include_sql=False),
                "match_count": len(matches),
                "matches": [m.group(0) for m in matches],
            })
    return {
        "pattern": pattern,
        "regex": regex,
        "case_sensitive": case_sensitive,
        "total_matches": sum(x["match_count"] for x in results),
        "sources": results,
    }


def update_table_sql(
    semantic_model_dir: Path,
    old_text: str | None = None,
    new_text: str | None = None,
    *,
    table: str | None = None,
    partition: str | None = None,
    relative_file: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    expected_replacements: int | None = None,
    expected_sql_hash: str | None = None,
    dry_run: bool = True,
    create_backups: bool = True,
) -> dict[str, Any]:
    has_line_range = start_line is not None or end_line is not None
    if old_text is not None and has_line_range:
        raise InvalidArgumentError(
            "Cannot combine old_text with start_line/end_line; choose one edit target mode.",
            code="INVALID_ARGUMENT",
        )
    if old_text is None and not has_line_range:
        raise InvalidArgumentError(
            "Provide either old_text or start_line/end_line to target a SQL edit.",
            code="INVALID_ARGUMENT",
        )
    if new_text is None:
        raise InvalidArgumentError("new_text is required.", code="INVALID_ARGUMENT")
    if not has_line_range and not old_text:
        raise InvalidArgumentError("old_text must not be empty.", code="INVALID_ARGUMENT")

    sources = _select_sources(
        semantic_model_dir,
        table=table,
        partition=partition,
        relative_file=relative_file,
    )
    if not sources:
        raise ObjectNotFoundError(
            "No Partition SQL source matched the supplied selector.",
            code="OBJECT_NOT_FOUND",
            details={"table": table, "partition": partition, "relative_file": relative_file},
        )
    if (has_line_range or expected_sql_hash is not None) and len(sources) > 1:
        raise AmbiguousObjectError(
            "Targeted Partition SQL edits require a single unambiguous source. "
            "Supply an exact table and/or partition.",
            code="AMBIGUOUS_OBJECT",
            details={
                "candidates": [
                    {"table": m.table, "partition": m.partition, "file": str(m.file)}
                    for m in sources
                ]
            },
        )
    if expected_sql_hash is not None:
        target_src = sources[0]
        actual_hash = target_src.sql_hash or compute_sql_hash(
            target_src.clean_sql or target_src.sql_expression
        )
        verify_sql_hash_precondition(actual_hash, expected_sql_hash)

    changes = []
    warnings: list[str] = []
    total = 0
    per_file: dict[Path, list[tuple[SqlSource, str, str]]] = {}
    last_before_clean: str | None = None
    last_updated_clean: str | None = None
    last_updated_m: str | None = None
    for source in sources:
        clean = source.clean_sql or source.sql_expression
        if has_line_range:
            eff_new, _ = strip_line_number_prefixes(new_text)
            updated_clean, count = apply_line_range_edit(
                clean,
                start_line=start_line,
                end_line=end_line,
                new_text=eff_new,
            )
            if not count:
                continue
            validate_clean_sql_delimiters(updated_clean)
            if source.descriptor is not None:
                updated_m = encode_sql_to_m(updated_clean, source.descriptor)
                validate_m_reencoding(updated_clean, updated_m, source.descriptor)
            else:
                updated_m = updated_clean
            warnings.extend(detect_dynamic_param_warnings(clean, updated_clean))
            eff_end = start_line if end_line is None else end_line
            reason = f"replace lines {start_line}-{eff_end} in partition {source.partition!r}"
        else:
            assert old_text is not None
            eff_old, old_stripped = strip_line_number_prefixes(old_text, reference_sql=clean)
            eff_new, _ = strip_line_number_prefixes(
                new_text,
                reference_sql=None if old_stripped else clean,
            )
            updated_clean, count, match_mode = apply_text_edit(
                clean,
                old_text=eff_old,
                new_text=eff_new,
                expected_replacements=expected_replacements,
            )
            if count:
                validate_clean_sql_delimiters(updated_clean)
                if source.descriptor is not None:
                    updated_m = encode_sql_to_m(updated_clean, source.descriptor)
                    validate_m_reencoding(updated_clean, updated_m, source.descriptor)
                else:
                    updated_m = updated_clean
                warnings.extend(detect_dynamic_param_warnings(clean, updated_clean))
            else:
                count = source.sql_expression.count(eff_old)
                if not count:
                    continue
                updated_m = source.sql_expression.replace(eff_old, eff_new)
                updated_clean = updated_m
            reason = (
                f"replace SQL fragment ({match_mode}) in partition {source.partition!r}"
                if match_mode == "whitespace_normalized"
                else f"replace SQL fragment in partition {source.partition!r}"
            )

        total += count
        last_before_clean = clean
        last_updated_clean = updated_clean
        last_updated_m = updated_m
        per_file.setdefault(source.file, []).append((source, source.sql_expression, updated_m))
        changes.append({
            **source.as_dict(semantic_model_dir, include_sql=False),
            "replacements": count,
            "old_text": old_text,
            "new_text": new_text,
            "reason": reason,
        })
    if expected_replacements is not None and total != expected_replacements:
        raise PreconditionFailedError(
            f"Expected {expected_replacements} replacements but found {total}; "
            "no files were written.",
            code="PRECONDITION_FAILED",
            details={"expected_replacements": expected_replacements, "actual_replacements": total},
        )
    if total == 0:
        first_src = sources[0]
        first_clean = first_src.clean_sql or first_src.sql_expression
        return {
            "operation": "update TMDL Value.NativeQuery SQL",
            "status": "no_op",
            "dry_run": dry_run,
            "replacement_count": 0,
            "changes": [],
            "changed_files": [],
            "warnings": ["No exact old_text matches were found inside isolated SQL sources."],
            "rolled_back": False,
            "sql_hash_before": first_src.sql_hash or compute_sql_hash(first_clean),
            "sql_hash_after": first_src.sql_hash or compute_sql_hash(first_clean),
        }
    plan: dict[str, Any] = {
        "operation": "update TMDL Value.NativeQuery SQL",
        "status": "dry_run" if dry_run else "applied",
        "dry_run": dry_run,
        "replacement_count": total,
        "changes": changes,
        "changed_files": sorted({ch["file"] for ch in changes}),
        "warnings": warnings,
        "rolled_back": False,
    }
    if (
        len(changes) == 1
        and last_before_clean is not None
        and last_updated_clean is not None
        and last_updated_m is not None
    ):
        plan["before_sql"] = last_before_clean
        plan["updated_sql"] = last_updated_clean
        plan["updated_m_sql"] = last_updated_m
        plan["sql_hash_before"] = compute_sql_hash(last_before_clean)
        plan["sql_hash_after"] = compute_sql_hash(last_updated_clean)
    if dry_run:
        return plan
    for path, file_sources in per_file.items():
        raw = path.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        raw_text = raw.decode("utf-8-sig")
        has_crlf = "\r\n" in raw_text
        updated = raw_text.replace("\r\n", "\n")
        for source, expected_m, updated_m in sorted(
            file_sources, key=lambda item: item[0].sql_start, reverse=True
        ):
            fragment = updated[source.sql_start : source.sql_end]
            if fragment != expected_m:
                raise RuntimeError(f"SQL source changed before write: {path}")
            updated = updated[: source.sql_start] + updated_m + updated[source.sql_end :]
        if create_backups:
            path.with_suffix(path.suffix + ".bak").write_bytes(raw)
        if has_crlf:
            updated = updated.replace("\n", "\r\n")
        payload = updated.encode("utf-8")
        payload = (b"\xef\xbb\xbf" + payload) if bom else payload
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_bytes(payload)
        temp.replace(path)
    return plan
