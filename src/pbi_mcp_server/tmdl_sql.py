from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PARTITION_RE = re.compile(r"(?m)^(?P<indent>[ \t]*)partition\s+(?P<name>'(?:''|[^'])*'|[^=\r\n]+?)\s*=\s*m\s*$")
_SOURCE_RE = re.compile(r"(?m)^(?P<indent>[ \t]*)source\s*=\s*```[ \t]*\r?\n")
_SOURCE_UNFENCED_RE = re.compile(r"(?m)^(?P<indent>[ \t]*)source\s*=[ \t]*\r?\n")
_TABLE_RE = re.compile(r"(?m)^table\s+(?P<name>'(?:''|[^'])*'|[^\r\n]+?)\s*$")
_NATIVE_QUERY = "Value.NativeQuery"


def _add_line_numbers(text: str) -> str:
    return "\n".join(f"{idx}: {line}" for idx, line in enumerate(text.splitlines(), 1))


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

    def as_dict(self, root: Path | None = None, include_sql: bool = True) -> dict[str, Any]:
        file_value = str(self.file.relative_to(root)) if root else str(self.file)
        result: dict[str, Any] = {
            "file": file_value,
            "table": self.table,
            "partition": self.partition,
            "source_span": {"start": self.source_start, "end": self.source_end},
            "sql_span": {"start": self.sql_start, "end": self.sql_end},
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
            )
        )
    return results


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
        if table is not None and source.table != table:
            continue
        if partition is not None and source.partition != partition:
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
        raise FileNotFoundError(
            "No Value.NativeQuery SQL source matched the supplied table, partition, or file selector."
        )
    if len(matches) > 1:
        raise ValueError(
            "The selector matched multiple SQL sources. Supply an exact table, partition, or relative_file."
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
        raise ValueError("pattern must not be empty.")
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
        raise ValueError("old_text must not be empty.")
    sources = _select_sources(
        semantic_model_dir,
        table=table,
        partition=partition,
        relative_file=relative_file,
    )
    if not sources:
        raise FileNotFoundError("No Value.NativeQuery SQL source matched the supplied selector.")
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
            })
    if expected_replacements is not None and total != expected_replacements:
        raise ValueError(f"Expected {expected_replacements} replacements but found {total}; no files were written.")
    if total == 0:
        return {
            "operation": "update TMDL Value.NativeQuery SQL",
            "dry_run": dry_run,
            "replacement_count": 0,
            "changes": [],
            "warnings": ["No exact old_text matches were found inside isolated SQL sources."],
        }
    plan = {
        "operation": "update TMDL Value.NativeQuery SQL",
        "dry_run": dry_run,
        "replacement_count": total,
        "changes": changes,
        "warnings": [],
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
