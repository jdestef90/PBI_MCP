from __future__ import annotations

import re

# SQL keywords that look like table names in FROM/JOIN clauses but aren't
_SQL_KEYWORDS = frozenset({
    "SELECT", "LATERAL", "UNNEST", "WHERE", "GROUP", "ORDER",
    "HAVING", "ON", "CROSS", "INNER", "LEFT", "RIGHT",
    "FULL", "OUTER", "NATURAL", "USING", "VALUES",
})

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
_IDENT_PART = (
    r"(?:\{\{m:(?:(?!\}\}).)+\}\}|`[^`]+`|\"[^\"]+\"|\[[^\]]+\]|[A-Za-z0-9_]+)"
)
_SQL_FROM_JOIN_RE = re.compile(
    rf"(?i)\b(?:FROM|JOIN)\s+({_IDENT_PART}(?:\.{_IDENT_PART})*)"
    r"(?:\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*))?"
)


def _add_line_numbers(text: str) -> str:
    """Format SQL with right-aligned canonical 1-based line numbers (``42 | ...``)."""
    lines = text.splitlines()
    if not lines:
        return ""
    width = len(str(len(lines)))
    return "\n".join(f"{idx:>{width}} | {line}" for idx, line in enumerate(lines, 1))


def extract_sql_metadata(sql_expression: str) -> dict[str, Any]:
    """Extract projected fields, source tables, and dynamic M parameters from SQL.

    For CTE-based queries, reads selected_fields from the ``main`` section (the
    final SELECT) rather than the first CTE.  Internal CTE names are excluded
    from ``source_tables``.
    """
    dynamic_params: list[str] = []
    if "{{m:" in sql_expression:
        for kind, val in split_clean_sql_segments(sql_expression):
            if kind == "expr" and val not in dynamic_params:
                dynamic_params.append(val)
    for m in _M_PARAM_RE.finditer(sql_expression):
        param = m.group(1)
        if param not in dynamic_params:
            dynamic_params.append(param)

    cleaned = _M_PARAM_RE.sub(lambda m: f"{{{{m:{m.group(1)}}}}}", sql_expression)
    cleaned = cleaned.strip()
    if cleaned.startswith('""') and cleaned.endswith('""'):
        cleaned = cleaned[2:-2]
    elif cleaned.startswith('"') and cleaned.endswith('"'):
        cleaned = cleaned[1:-1]

    no_comments = re.sub(r"--[^\r\n]*", "", cleaned)
    no_comments = re.sub(r"/\*.*?\*/", "", no_comments, flags=re.DOTALL)

    # Scan for SQL Sections to identify main section and internal CTE names
    sections = scan_sql_sections(cleaned)
    cte_names: set[str] = set()
    main_section: SqlSection | None = None
    for s in sections:
        if s.name != "main":
            cte_names.add(s.name)
        else:
            main_section = s

    # Extract source tables from ALL sections (CTEs + main), excluding internal CTE names
    source_tables: list[str] = []
    for sec in sections:
        if sec.start_offset > 0:
            sec_text = cleaned[sec.start_offset:]
        else:
            sec_lines = cleaned.splitlines()
            sec_text = "\n".join(sec_lines[sec.start_line - 1:sec.end_line])
        for tm in _SQL_FROM_JOIN_RE.finditer(sec_text):
            raw_tbl = tm.group(1).strip().strip('",')
            if (
                raw_tbl.upper() not in {"(", "SELECT", "LATERAL", "UNNEST"}
                and raw_tbl not in source_tables
                and raw_tbl not in cte_names
            ):
                source_tables.append(raw_tbl)

    # Extract selected_fields from main section (or first SELECT if no sections)
    selected_fields: list[str] = []
    if main_section:
        if main_section.start_offset > 0:
            select_body_text = cleaned[main_section.start_offset:]
        else:
            main_lines = cleaned.splitlines()
            select_body_text = "\n".join(main_lines[main_section.start_line - 1:main_section.end_line])
    else:
        select_body_text = no_comments

    select_match = re.search(
        r"(?is)\bSELECT\b(?:\s+DISTINCT|\s+TOP\s+\d+)?(.*?)\bFROM\b", select_body_text
    )
    if select_match:
        select_body = select_match.group(1)
        items: list[str] = []
        depth = 0
        in_str = False
        current: list[str] = []
        for ch in select_body:
            if ch == "'":
                in_str = not in_str
                current.append(ch)
            elif not in_str:
                if ch == "(":
                    depth += 1
                    current.append(ch)
                elif ch == ")":
                    depth = max(0, depth - 1)
                    current.append(ch)
                elif ch == "," and depth == 0:
                    items.append("".join(current).strip())
                    current = []
                else:
                    current.append(ch)
            else:
                current.append(ch)
        if current:
            items.append("".join(current).strip())

        for item in items:
            if not item:
                continue
            as_match = re.search(
                r"(?i)\bAS\s+(?:\[([^\]]+)\]|\"([^\"]+)\"|'([^']+)'|`([^`]+)`|"
                r"([A-Za-z_][A-Za-z0-9_]*))\s*$",
                item,
            )
            if as_match:
                alias = next(g for g in as_match.groups() if g is not None)
                if alias not in selected_fields:
                    selected_fields.append(alias)
                continue
            ident_match = re.search(
                r"(?:\[([^\]]+)\]|\"([^\"]+)\"|`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*))\s*$",
                item,
            )
            if ident_match:
                col_name = (
                    ident_match.group(1)
                    or ident_match.group(2)
                    or ident_match.group(3)
                    or ident_match.group(4)
                )
                if col_name and col_name not in selected_fields:
                    selected_fields.append(col_name)

    return {
        "selected_fields": selected_fields,
        "source_tables": source_tables,
        "has_dynamic_parameters": bool(dynamic_params),
        "dynamic_parameters": dynamic_params,
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

    def as_dict(self, root: Path | None = None, include_sql: bool = True) -> dict[str, Any]:
        if root:
            file_value = str(self.file.relative_to(root)).replace("\\", "/")
        else:
            file_value = str(self.file).replace("\\", "/")
        clean = self.clean_sql or self.sql_expression
        meta = extract_sql_metadata(clean)
        if self.dynamic_parameters:
            merged_params = list(self.dynamic_parameters)
            for p in meta["dynamic_parameters"]:
                if p not in merged_params:
                    merged_params.append(p)
            meta["dynamic_parameters"] = merged_params
            meta["has_dynamic_parameters"] = bool(merged_params)
        line_count = len(clean.splitlines())
        sql_hash = self.sql_hash or compute_sql_hash(clean)

        # Add SQL section names to metadata
        sections = scan_sql_sections(clean)
        section_names = [s.name for s in sections]
        meta["sql_section_names"] = section_names
        meta["sql_section_count"] = len(section_names)

        result: dict[str, Any] = {
            "file": file_value,
            "table": self.table,
            "partition": self.partition,
            "mode": self.mode,
            "connector": self.connector,
            "line_count": line_count,
            "sql_hash": sql_hash,
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


def get_table_sql(
    semantic_model_dir: Path,
    *,
    table: str | None = None,
    partition: str | None = None,
    relative_file: str | None = None,
    ctes: str | list[str] | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    outline: bool = False,
) -> dict[str, Any]:
    """Get Partition SQL for a table with outline-first progressive disclosure.

    For queries > 120 lines without selectors, returns an outline of SQL Sections.
    The ``ctes`` parameter fetches specific CTEs/main sections.
    ``start_line``/``end_line`` returns a line-range slice.
    ``outline=True`` forces outline mode for any query size.
    """
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
    clean_sql = source.clean_sql or source.sql_expression
    total_lines = len(clean_sql.splitlines())

    # Determine if we should return outline
    has_selector = ctes is not None or start_line is not None or end_line is not None
    if outline or (total_lines > 120 and not has_selector):
        # Return outline view
        sections = scan_sql_sections(clean_sql)
        section_names = [s.name for s in sections]
        result = source.as_dict(semantic_model_dir, include_sql=False)
        result["sql_sections"] = [
            {
                "name": s.name,
                "start_line": s.start_line,
                "end_line": s.end_line,
                "line_count": s.end_line - s.start_line + 1,
                "references": list(s.references),
                "external_tables": list(s.external_tables),
            }
            for s in sections
        ]
        result["outline"] = True
        result["total_lines"] = total_lines
        # Add next-call hints
        if sections:
            cte_names = [s.name for s in sections if s.name != "main"]
            if cte_names:
                result["next_call_hints"] = (
                    f"Fetch a specific CTE: ctes={cte_names[0]!r} "
                    f"or ctes={cte_names!r}"
                )
            else:
                result["next_call_hints"] = (
                    f"Fetch lines: start_line=1 or start_line=N"
                )
        return result

    # Handle ctes selector
    if ctes is not None:
        if isinstance(ctes, str):
            ctes = [ctes]
        sections = scan_sql_sections(clean_sql)
        section_map: dict[str, SqlSection] = {}
        for s in sections:
            section_map[s.name.casefold()] = s
        requested: list[SqlSection] = []
        for c in ctes:
            cf = c.casefold()
            if cf not in section_map:
                valid_names = [s.name for s in sections]
                raise ObjectNotFoundError(
                    f"Unknown SQL Section {c!r}. Valid sections: {valid_names}",
                    code="OBJECT_NOT_FOUND",
                    details={"requested_cte": c, "valid_sections": valid_names},
                )
            requested.append(section_map[cf])

        # Build SQL for requested sections
        requested_sql_parts: list[str] = []
        for i, sec in enumerate(requested):
            sec_lines = clean_sql.splitlines()
            section_text = "\n".join(
                f"{ln:>{len(str(sec.end_line))}} | {sec_lines[ln - 1]}"
                for ln in range(sec.start_line, sec.end_line + 1)
            )
            requested_sql_parts.append(section_text)
        requested_sql = "\n\n---\n\n".join(requested_sql_parts)

        result = source.as_dict(semantic_model_dir, include_sql=False)
        result["sql"] = requested_sql
        result["sql_with_line_numbers"] = requested_sql
        result["requested_ctes"] = ctes
        result["outline"] = False
        result["total_lines"] = total_lines

        # Check 400-line safety ceiling
        if len(requested_sql.splitlines()) > 400:
            result["warnings"] = [
                f"Requested sections exceed 400 lines. "
                f"Use start_line/end_line to fetch remaining lines."
            ]
        return result

    # Handle start_line/end_line selector
    if start_line is not None or end_line is not None:
        all_lines = clean_sql.splitlines()
        total = len(all_lines)

        if start_line is None:
            start_line = 1
        if end_line is None:
            end_line = min(start_line + 119, total)

        if start_line < 1 or end_line < 1:
            raise InvalidArgumentError(
                "start_line and end_line must be >= 1.",
                code="INVALID_ARGUMENT",
            )
        if start_line > end_line:
            raise InvalidArgumentError(
                f"start_line ({start_line}) exceeds end_line ({end_line}).",
                code="INVALID_ARGUMENT",
            )
        if start_line > total:
            raise InvalidArgumentError(
                f"start_line ({start_line}) exceeds total lines ({total}).",
                code="INVALID_ARGUMENT",
            )
        if end_line > total:
            end_line = total

        sliced = all_lines[start_line - 1:end_line]
        width = len(str(end_line))
        numbered = "\n".join(
            f"{ln:>{width}} | {line}" for ln, line in enumerate(sliced, start_line)
        )

        result = source.as_dict(semantic_model_dir, include_sql=False)
        result["sql"] = numbered
        result["sql_with_line_numbers"] = numbered
        result["start_line"] = start_line
        result["end_line"] = end_line
        result["outline"] = False
        result["total_lines"] = total

        if end_line < total:
            result["next_call_hints"] = (
                f"Continue from line {end_line + 1}: "
                f"start_line={end_line + 1}"
            )
        return result

    # Default: return full SQL for small queries (<= 120 lines)
    return source.as_dict(semantic_model_dir, include_sql=True)


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
    old_text: str,
    new_text: str,
    *,
    table: str | None = None,
    partition: str | None = None,
    relative_file: str | None = None,
    expected_replacements: int | None = None,
    dry_run: bool = True,
    create_backups: bool = True,
) -> dict[str, Any]:
    if not old_text:
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
    changes = []
    total = 0
    per_file: dict[Path, list[tuple[SqlSource, str, str]]] = {}
    last_updated_clean: str | None = None
    last_updated_m: str | None = None
    for source in sources:
        clean = source.clean_sql or source.sql_expression
        count = clean.count(old_text)
        if count:
            updated_clean = clean.replace(old_text, new_text)
            updated_m = (
                encode_sql_to_m(updated_clean, source.descriptor)
                if source.descriptor is not None
                else updated_clean
            )
        else:
            count = source.sql_expression.count(old_text)
            if not count:
                continue
            updated_m = source.sql_expression.replace(old_text, new_text)
            updated_clean = updated_m
        total += count
        last_updated_clean = updated_clean
        last_updated_m = updated_m
        per_file.setdefault(source.file, []).append((source, source.sql_expression, updated_m))
        changes.append({
            **source.as_dict(semantic_model_dir, include_sql=False),
            "replacements": count,
            "old_text": old_text,
            "new_text": new_text,
            "reason": f"replace SQL fragment in partition {source.partition!r}",
        })
    if expected_replacements is not None and total != expected_replacements:
        raise PreconditionFailedError(
            f"Expected {expected_replacements} replacements but found {total}; "
            "no files were written.",
            code="PRECONDITION_FAILED",
            details={"expected_replacements": expected_replacements, "actual_replacements": total},
        )
    if total == 0:
        return {
            "operation": "update TMDL Value.NativeQuery SQL",
            "status": "no_op",
            "dry_run": dry_run,
            "replacement_count": 0,
            "changes": [],
            "changed_files": [],
            "warnings": ["No exact old_text matches were found inside isolated SQL sources."],
            "rolled_back": False,
        }
    plan: dict[str, Any] = {
        "operation": "update TMDL Value.NativeQuery SQL",
        "status": "dry_run" if dry_run else "applied",
        "dry_run": dry_run,
        "replacement_count": total,
        "changes": changes,
        "changed_files": sorted({ch["file"] for ch in changes}),
        "warnings": [],
        "rolled_back": False,
    }
    if len(changes) == 1 and last_updated_clean is not None and last_updated_m is not None:
        plan["updated_sql"] = last_updated_clean
        plan["updated_m_sql"] = last_updated_m
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


# ─────────────────────────────────────────────────────────────────────────────
# SQL Section scanner (Issue #3)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SqlSection:
    """A named SQL Section: either a top-level CTE or the final `main` SELECT."""

    name: str
    start_line: int
    end_line: int
    references: tuple[str, ...] = ()
    external_tables: tuple[str, ...] = ()
    start_offset: int = 0  # Character offset in cleaned SQL (for precise extraction)


def _strip_comments(text: str) -> str:
    """Replace SQL comments with spaces (preserving line count and positions)."""
    result: list[str] = []
    i = 0
    length = len(text)
    in_single_quote = False
    in_double_quote = False
    in_backtick = False
    while i < length:
        ch = text[i]
        # Track string context
        if ch == "'" and not in_double_quote and not in_backtick:
            in_single_quote = not in_single_quote
            result.append(ch)
            i += 1
        elif ch == '"' and not in_single_quote and not in_backtick:
            in_double_quote = not in_double_quote
            result.append(ch)
            i += 1
        elif ch == '`' and not in_single_quote and not in_double_quote:
            in_backtick = not in_backtick
            result.append(ch)
            i += 1
        elif not in_single_quote and not in_double_quote and not in_backtick:
            # Block comment
            if ch == '/' and i + 1 < length and text[i + 1] == '*':
                end = text.find('*/', i + 2)
                if end == -1:
                    end = length
                result.append(' ' * (end - i))
                i = end + 2
            # Line comment
            elif ch == '-' and i + 1 < length and text[i + 1] == '-':
                end = text.find('\n', i)
                if end == -1:
                    end = length
                result.append(' ' * (end - i))
                i = end
            else:
                result.append(ch)
                i += 1
        else:
            result.append(ch)
            i += 1
    return ''.join(result)


def _find_matching_paren(text: str, start: int) -> int:
    """Find the matching closing parenthesis for the one at position `start`."""
    depth = 0
    i = start
    length = len(text)
    in_single_quote = False
    in_double_quote = False
    in_backtick = False
    while i < length:
        ch = text[i]
        if ch == "'" and not in_double_quote and not in_backtick:
            in_single_quote = not in_single_quote
        elif ch == '"' and not in_single_quote and not in_backtick:
            in_double_quote = not in_double_quote
        elif ch == '`' and not in_single_quote and not in_double_quote:
            in_backtick = not in_backtick
        elif not in_single_quote and not in_double_quote and not in_backtick:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return length - 1


def _extract_cte_name(text: str, pos: int) -> str | None:
    """Extract a CTE name starting at position `pos`."""
    length = len(text)
    # Skip whitespace
    while pos < length and text[pos] in ' \t':
        pos += 1
    if pos >= length:
        return None

    # Check for backtick-quoted identifier
    if text[pos] == '`':
        end = text.find('`', pos + 1)
        if end == -1:
            return None
        return text[pos:end + 1]

    # Check for double-quoted identifier
    if text[pos] == '"':
        end = text.find('"', pos + 1)
        if end == -1:
            return None
        return text[pos:end + 1]

    # Plain identifier
    start = pos
    while pos < length and (text[pos].isalnum() or text[pos] == '_'):
        pos += 1
    if pos == start:
        return None
    return text[start:pos]


def _scan_ctes(clean_sql: str, lines: list[str]) -> list[SqlSection]:
    """Scan top-level WITH ... CTEs and return ordered section descriptors."""
    stripped = _strip_comments(clean_sql)
    upper = stripped.upper()

    # Check if this is a WITH query
    with_match = re.search(r'\bWITH\s+(RECURSIVE\s+)?', upper)
    if not with_match:
        return []

    with_start = with_match.start()
    pos = with_match.end()

    sections: list[SqlSection] = []
    cte_names: set[str] = set()

    while True:
        # Skip whitespace
        while pos < len(stripped) and stripped[pos] in ' \t\n\r':
            pos += 1

        if pos >= len(stripped):
            break

        # Extract CTE name
        cte_name = _extract_cte_name(stripped, pos)
        if cte_name is None:
            break

        # Check for "AS (" after the name
        after_name = stripped[pos + len(cte_name):]
        as_match = re.match(r'\s*AS\s*\(', after_name, re.IGNORECASE)
        if not as_match:
            break

        # Find the opening paren of the CTE body
        paren_start = pos + len(cte_name) + as_match.start() + 2  # skip "AS ("
        paren_end = _find_matching_paren(stripped, paren_start)

        # Compute line numbers (1-based)
        cte_start_offset = pos
        cte_end_offset = paren_end + 1  # inclusive

        cte_start_line = stripped[:cte_start_offset].count('\n') + 1
        cte_end_line = stripped[:cte_end_offset].count('\n') + 1

        # Extract body for reference/table scanning
        cte_body = stripped[paren_start + 1:paren_end]

        # Find references to other CTEs and external tables in this CTE
        refs: set[str] = set()
        ext_tables: set[str] = set()

        # Look for FROM/JOIN references
        for tm in re.finditer(
            r'\b(?:FROM|JOIN)\s+(`[^`]+`|"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)',
            cte_body,
            re.IGNORECASE,
        ):
            ref = tm.group(1).strip('`,')
            ref_upper = ref.upper()
            if ref_upper in _SQL_KEYWORDS:
                continue
            if ref in cte_names and ref != cte_name:
                refs.add(ref)
            elif ref not in cte_names:
                ext_tables.add(ref)

        sections.append(SqlSection(
            name=cte_name,
            start_line=cte_start_line,
            end_line=cte_end_line,
            references=tuple(sorted(refs)),
            external_tables=tuple(sorted(ext_tables)),
        ))
        cte_names.add(cte_name)

        # Move past the CTE body closing paren
        pos = paren_end + 1

        # Skip whitespace and optional comma
        while pos < len(stripped) and stripped[pos] in ' \t\n\r':
            pos += 1
        if pos < len(stripped) and stripped[pos] == ',':
            pos += 1
            while pos < len(stripped) and stripped[pos] in ' \t\n\r':
                pos += 1

        # If we hit a non-comma, non-whitespace that's not a valid identifier start, stop
        if pos < len(stripped) and not (stripped[pos].isalpha() or stripped[pos] == '_' or stripped[pos] in ('"', '`')):
            break

    return sections


def _scan_main_section(clean_sql: str, lines: list[str], cte_sections: list[SqlSection]) -> SqlSection:
    """Find the main (trailing SELECT) section after all CTEs."""
    stripped = _strip_comments(clean_sql)

    if cte_sections:
        # Find the character position after the last CTE
        last_cte = cte_sections[-1]
        # We need to find where the last CTE actually ends in the text
        # by counting newlines up to end_line, then finding the closing paren
        line_count = 0
        pos_after_last_cte = 0
        for i, line in enumerate(lines):
            line_count += len(line) + 1  # +1 for newline
            if i + 1 >= last_cte.end_line:
                pos_after_last_cte = line_count
                break
        
        # If we're still on the same line as the CTE end, find the actual end
        # by looking for the closing paren of the CTE
        if last_cte.start_line == last_cte.end_line:
            # Single-line CTE: find the closing paren
            cte_start = stripped[:last_cte.start_line - 1].count('\n')
            # Find the AS ( and then the matching )
            as_idx = stripped[cte_start:].upper().find(' AS (')
            if as_idx >= 0:
                paren_start = cte_start + as_idx + 4
                paren_end = _find_matching_paren(stripped, paren_start)
                pos_after_last_cte = paren_end + 1
        
        main_text = stripped[pos_after_last_cte:]
    else:
        main_text = stripped

    # Find the main SELECT
    main_select_match = re.search(r'\bSELECT\b', main_text, re.IGNORECASE)
    if not main_select_match:
        main_select_match = re.search(r'\bUNION\b', main_text, re.IGNORECASE)

    if main_select_match:
        if cte_sections:
            main_start_offset = pos_after_last_cte + main_select_match.start()
        else:
            main_start_offset = main_select_match.start()
        main_start_line = stripped[:main_start_offset].count('\n') + 1
        main_end_line = stripped.count('\n') + 1
    else:
        main_start_line = 1
        main_end_line = len(lines)
        main_text = stripped

    # Build a case-insensitive lookup of CTE names
    cte_names_lower: dict[str, str] = {}
    for s in cte_sections:
        cte_names_lower[s.name.casefold()] = s.name

    # Extract references and external tables from main section
    refs: set[str] = set()
    ext_tables: set[str] = set()
    for tm in re.finditer(
        r'\b(?:FROM|JOIN)\s+(`[^`]+`|"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)',
        main_text,
        re.IGNORECASE,
    ):
        ref = tm.group(1).strip('`,')
        ref_upper = ref.upper()
        if ref_upper in _SQL_KEYWORDS:
            continue
        if ref.casefold() in cte_names_lower:
            refs.add(cte_names_lower[ref.casefold()])
        else:
            ext_tables.add(ref)

    return SqlSection(
        name='main',
        start_line=main_start_line,
        end_line=main_end_line,
        references=tuple(sorted(refs)),
        external_tables=tuple(sorted(ext_tables)),
        start_offset=main_start_offset if cte_sections else 0,
    )


def _scan_clause_landmarks(clean_sql: str, lines: list[str]) -> list[SqlSection]:
    """Scan non-CTE queries for top-level clause landmarks."""
    stripped = _strip_comments(clean_sql)
    upper = stripped.upper()
    sections: list[SqlSection] = []

    # Find top-level clause positions
    clause_patterns = [
        ('SELECT', r'\bSELECT\b'),
        ('FROM', r'\bFROM\b'),
        ('JOIN', r'\bJOIN\b'),
        ('WHERE', r'\bWHERE\b'),
        ('GROUP BY', r'\bGROUP\s+BY\b'),
        ('HAVING', r'\bHAVING\b'),
        ('QUALIFY', r'\bQUALIFY\b'),
        ('ORDER BY', r'\bORDER\s+BY\b'),
        ('UNION', r'\bUNION\b'),
    ]

    found: list[tuple[str, int, int]] = []
    for clause_name, pattern in clause_patterns:
        for m in re.finditer(pattern, upper):
            # Make sure this isn't inside a string
            start = m.start()
            # Check it's not preceded by SELECT/FROM/WHERE/JOIN etc. that would make it part of another clause
            before = upper[max(0, start - 20):start]
            if re.search(r'\b(SELECT|FROM|WHERE|GROUP|HAVING|ORDER|UNION)\b', before):
                continue
            line_num = stripped[:start].count('\n') + 1
            found.append((clause_name, line_num, m.end(), start))

    if not found:
        # Default: the whole query is one section
        return [SqlSection(name='main', start_line=1, end_line=len(lines))]

    # Sort by line number
    found.sort(key=lambda x: x[1])

    # Build sections
    for i, (name, start_line, end_pos, clause_start) in enumerate(found):
        # End is the start of the next clause, or end of file
        if i + 1 < len(found):
            next_line = found[i + 1][1]
            end_line = next_line - 1
        else:
            end_line = len(lines)

        # Extract external tables for FROM/JOIN clauses
        ext_tables: set[str] = set()
        if name in ('FROM', 'JOIN'):
            # Scan from this clause's start position up to the next clause
            clause_end_pos = stripped[:found[i + 1][1] - 1].count('\n') + found[i + 1][1] if i + 1 < len(found) else len(stripped)
            clause_text = stripped[clause_start:clause_end_pos]
            for tm in re.finditer(
                r'\b(?:FROM|JOIN)\s+(`[^`]+`|"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)',
                clause_text,
                re.IGNORECASE,
            ):
                ref = tm.group(1).strip('`,')
                ref_upper = ref.upper()
                if ref_upper not in _SQL_KEYWORDS:
                    ext_tables.add(ref)

        sections.append(SqlSection(
            name=name,
            start_line=start_line,
            end_line=end_line,
            external_tables=tuple(sorted(ext_tables)),
        ))

    return sections


def scan_sql_sections(clean_sql: str) -> list[SqlSection]:
    """Scan a Clean SQL View for SQL Sections (CTEs + main, or clause landmarks).

    Returns an ordered list of SqlSection descriptors. For CTE-based queries,
    returns every top-level CTE followed by ``main``. For plain SELECT/UNION
    queries without CTEs, returns clause landmarks.
    """
    lines = clean_sql.splitlines()
    cte_sections = _scan_ctes(clean_sql, lines)

    if cte_sections:
        main_section = _scan_main_section(clean_sql, lines, cte_sections)
        return cte_sections + [main_section]

    return _scan_clause_landmarks(clean_sql, lines)
