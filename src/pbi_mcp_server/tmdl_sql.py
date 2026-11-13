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

_PARTITION_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)partition\s+(?P<name>'(?:''|[^'])*'|[^=\r\n]+?)\s*=\s*(?P<ptype>m|calculated)\s*$"
)
_MODE_RE = re.compile(r"(?m)^[ \t]*mode\s*:\s*(?P<mode>[A-Za-z0-9_]+)\s*$")
_SOURCE_RE = re.compile(r"(?m)^(?P<indent>[ \t]*)source\s*=\s*```[ \t]*\r?\n")
_SOURCE_UNFENCED_RE = re.compile(r"(?m)^(?P<indent>[ \t]*)source\s*=[ \t]*\r?\n")
_TABLE_RE = re.compile(r"(?m)^table\s+(?P<name>'(?:''|[^'])*'|[^\r\n]+?)\s*$")
_NATIVE_QUERY = "Value.NativeQuery"

_M_PARAM_RE = re.compile(r'"\s*&\s*([A-Za-z_][A-Za-z0-9_]*)\s*&\s*"')
_SQL_DB_RE = re.compile(r'Sql\.Database\(\s*"([^"]+)"\s*,\s*"([^"]+)"')
_M_ITEM_RE = re.compile(r'Schema\s*=\s*"([^"]+)"\s*,\s*Item\s*=\s*"([^"]+)"')
_SQL_FROM_JOIN_RE = re.compile(
    r"(?i)\b(?:FROM|JOIN)\s+([A-Za-z0-9_.\"&\{\}\[\]]+)(?:\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*))?"
)


def _add_line_numbers(text: str) -> str:
    return "\n".join(f"{idx}: {line}" for idx, line in enumerate(text.splitlines(), 1))


def extract_sql_metadata(sql_expression: str) -> dict[str, Any]:
    """Extract projected SQL fields, source tables, and dynamic M parameters from a SQL expression."""
    dynamic_params: list[str] = []
    for m in _M_PARAM_RE.finditer(sql_expression):
        param = m.group(1)
        if param not in dynamic_params:
            dynamic_params.append(param)

    # Clean M string concatenation and SQL comments for structural analysis
    cleaned = _M_PARAM_RE.sub(r"__\1__", sql_expression)
    cleaned = cleaned.strip()
    if cleaned.startswith('""') and cleaned.endswith('""'):
        cleaned = cleaned[2:-2]
    elif cleaned.startswith('"') and cleaned.endswith('"'):
        cleaned = cleaned[1:-1]

    # Remove single-line and block SQL comments
    no_comments = re.sub(r"--[^\r\n]*", "", cleaned)
    no_comments = re.sub(r"/\*.*?\*/", "", no_comments, flags=re.DOTALL)

    # Extract source tables in FROM / JOIN clauses
    source_tables: list[str] = []
    for tm in _SQL_FROM_JOIN_RE.finditer(no_comments):
        raw_tbl = tm.group(1).strip().strip('",')
        if raw_tbl.upper() not in {"(", "SELECT", "LATERAL", "UNNEST"} and raw_tbl not in source_tables:
            source_tables.append(raw_tbl)

    # Extract projected columns from SELECT ... FROM
    selected_fields: list[str] = []
    select_match = re.search(r"(?is)\bSELECT\b(?:\s+DISTINCT|\s+TOP\s+\d+)?(.*?)\bFROM\b", no_comments)
    if select_match:
        select_body = select_match.group(1)
        # Split top-level commas
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
            # Check explicit AS alias
            as_match = re.search(r"(?i)\bAS\s+(?:\[([^\]]+)\]|\"([^\"]+)\"|'([^']+)'|([A-Za-z_][A-Za-z0-9_]*))\s*$", item)
            if as_match:
                alias = next(g for g in as_match.groups() if g is not None)
                if alias not in selected_fields:
                    selected_fields.append(alias)
                continue
            # Otherwise take trailing identifier (e.g. p.SSN -> SSN)
            ident_match = re.search(r"(?:\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_]*))\s*$", item)
            if ident_match:
                col_name = ident_match.group(1) or ident_match.group(2)
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

    def as_dict(self, root: Path | None = None, include_sql: bool = True) -> dict[str, Any]:
        file_value = str(self.file.relative_to(root)).replace("\\", "/") if root else str(self.file).replace("\\", "/")
        meta = extract_sql_metadata(self.sql_expression)
        result: dict[str, Any] = {
            "file": file_value,
            "table": self.table,
            "partition": self.partition,
            "mode": self.mode,
            "source_span": {"start": self.source_start, "end": self.source_end},
            "sql_span": {"start": self.sql_start, "end": self.sql_end},
            "sql_metadata": meta,
        }
        if include_sql:
            result["sql_m_expression"] = self.sql_expression
            result["sql_with_line_numbers"] = _add_line_numbers(self.sql_expression)
            result["source_m_expression"] = self.m_expression
        return result


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


def _scan_m_argument_end(text: str, start: int) -> int:
    """Find the next top-level argument comma in serialized Power Query M."""
    depth = 0
    in_string = False
    i = start

    while i < len(text):
        if text.startswith('""', i):
            in_string = not in_string
            i += 2
            continue

        char = text[i]
        if char == '"':
            in_string = not in_string
            i += 1
            continue

        if in_string:
            i += 1
            continue

        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth < 0:
                raise ValueError("Unbalanced closing delimiter while parsing Value.NativeQuery.")
        elif char == "," and depth == 0:
            return i
        i += 1

    raise ValueError("Could not locate the next top-level Value.NativeQuery argument.")


def _extract_native_query_second_argument(source: str, absolute_start: int) -> tuple[int, int]:
    marker = source.find(_NATIVE_QUERY)
    if marker < 0:
        raise ValueError("The TMDL M source does not contain Value.NativeQuery.")
    open_paren = source.find("(", marker + len(_NATIVE_QUERY))
    if open_paren < 0:
        raise ValueError("Value.NativeQuery has no opening parenthesis.")
    first_arg_end = _scan_m_argument_end(source, open_paren + 1)
    second_start = first_arg_end + 1
    while second_start < len(source) and source[second_start].isspace():
        second_start += 1
    second_end = _scan_m_argument_end(source, second_start)
    while second_end > second_start and source[second_end - 1].isspace():
        second_end -= 1
    return absolute_start + second_start, absolute_start + second_end


def parse_tmdl_sql_sources(path: Path) -> list[SqlSource]:
    text = path.read_text(encoding="utf-8-sig")
    table_match = _TABLE_RE.search(text)
    table_name = _unquote_tmdl_name(table_match.group("name")) if table_match else None
    partitions = list(_PARTITION_RE.finditer(text))
    results: list[SqlSource] = []
    for index, partition_match in enumerate(partitions):
        limit = partitions[index + 1].start() if index + 1 < len(partitions) else len(text)
        block_text = text[partition_match.end() : limit]
        mode_m = _MODE_RE.search(block_text)
        mode = mode_m.group("mode") if mode_m else None
        source_match = _SOURCE_RE.search(text, partition_match.end(), limit)
        if not source_match:
            continue
        source_start = source_match.end()
        source_end = _find_fence_end(text, source_start)
        source = text[source_start:source_end]
        if _NATIVE_QUERY not in source:
            continue
        sql_start, sql_end = _extract_native_query_second_argument(source, source_start)
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
                text[sql_start:sql_end],
                mode=mode,
            )
        )
    return results


def discover_partition_metadata(semantic_model_dir: Path | None, root: Path) -> list[dict[str, Any]]:
    """Discover all table partitions (both Value.NativeQuery and standard M/Sql.Database partitions)."""
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
                meta = extract_sql_metadata(ns.sql_expression)
                partitions_out.append({
                    "name": p_name,
                    "table": table_name,
                    "partition_type": p_type,
                    "mode": mode,
                    "has_native_sql": True,
                    "source_tables": meta["source_tables"],
                    "sql_fields": meta["selected_fields"],
                    "has_dynamic_parameters": meta["has_dynamic_parameters"],
                    "dynamic_parameters": meta["dynamic_parameters"],
                    "file": rel_file,
                    "line": line_num,
                })
            else:
                # Check standard M Sql.Database / Schema / Item pattern
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
                    "table": table_name,
                    "partition_type": p_type,
                    "mode": mode,
                    "has_native_sql": False,
                    "source_tables": source_tables,
                    "sql_fields": [],
                    "has_dynamic_parameters": False,
                    "dynamic_parameters": [],
                    "file": rel_file,
                    "line": line_num,
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
        if relative_file is not None and source.file.resolve() != (semantic_model_dir / relative_file).resolve():
            continue
        selected.append(source)
    return selected


def list_table_sql_sources(semantic_model_dir: Path) -> list[dict[str, Any]]:
    return [s.as_dict(semantic_model_dir, include_sql=False) for s in discover_tmdl_sql_sources(semantic_model_dir)]


def get_table_sql(
    semantic_model_dir: Path,
    *,
    table: str | None = None,
    partition: str | None = None,
    relative_file: str | None = None,
) -> dict[str, Any]:
    matches = _select_sources(
        semantic_model_dir,
        table=table,
        partition=partition,
        relative_file=relative_file,
    )
    if not matches:
        raise ObjectNotFoundError(
            "No Value.NativeQuery SQL source matched the supplied table, partition, or file selector.",
            code="OBJECT_NOT_FOUND",
            details={"table": table, "partition": partition, "relative_file": relative_file},
        )
    if len(matches) > 1:
        raise AmbiguousObjectError(
            "The selector matched multiple SQL sources. Supply an exact table, partition, or relative_file.",
            code="AMBIGUOUS_OBJECT",
            details={
                "candidates": [
                    {"table": m.table, "partition": m.partition, "file": str(m.file)}
                    for m in matches
                ]
            },
        )
    return matches[0].as_dict(semantic_model_dir, include_sql=True)


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
    compiled = re.compile(pattern if regex else re.escape(pattern), flags | re.MULTILINE | re.DOTALL)
    results = []
    for source in discover_tmdl_sql_sources(semantic_model_dir):
        matches = list(compiled.finditer(source.sql_expression))
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
            "No Value.NativeQuery SQL source matched the supplied selector.",
            code="OBJECT_NOT_FOUND",
            details={"table": table, "partition": partition, "relative_file": relative_file},
        )
    changes = []
    total = 0
    per_file: dict[Path, list[tuple[SqlSource, int]]] = {}
    for source in sources:
        count = source.sql_expression.count(old_text)
        if count:
            total += count
            per_file.setdefault(source.file, []).append((source, count))
            changes.append({
                **source.as_dict(semantic_model_dir, include_sql=False),
                "replacements": count,
                "old_text": old_text,
                "new_text": new_text,
                "reason": f"replace SQL fragment in partition {source.partition!r}",
            })
    if expected_replacements is not None and total != expected_replacements:
        raise PreconditionFailedError(
            f"Expected {expected_replacements} replacements but found {total}; no files were written.",
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
    if dry_run:
        return plan
    for path, file_sources in per_file.items():
        raw = path.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
        updated = text
        for source, count in sorted(file_sources, key=lambda item: item[0].sql_start, reverse=True):
            fragment = updated[source.sql_start : source.sql_end]
            if fragment.count(old_text) != count:
                raise RuntimeError(f"SQL source changed before write: {path}")
            updated = (
                updated[: source.sql_start]
                + fragment.replace(old_text, new_text)
                + updated[source.sql_end :]
            )
        if create_backups:
            path.with_suffix(path.suffix + ".bak").write_bytes(raw)
        payload = updated.encode("utf-8")
        payload = (b"\xef\xbb\xbf" + payload) if bom else payload
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_bytes(payload)
        temp.replace(path)
    return plan
