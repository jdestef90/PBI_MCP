from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..errors import AmbiguousObjectError
from ..models import TmdlObject, make_object_id
from ..tmdl_sql import discover_partition_metadata, discover_tmdl_sql_sources
from ..workspace import compute_workspace_id, relative

DECLARATION = re.compile(
    r"^(?P<indent>\s*)(?P<kind>table|column|measure|calculatedColumn|relationship|partition)\s+"
    r"(?P<name>'(?:[^']|'')+'|\"[^\"]+\"|[^=\s]+)(?:\s*=\s*(?P<expr>.*))?$",
    re.IGNORECASE,
)

PROPERTY_RE = re.compile(
    r"^(?P<indent>\s*)(?P<prop>dataType|formatString|summarizeBy|sourceColumn|fromColumn|toColumn|fromCardinality|toCardinality|crossFilteringBehavior|isActive|mode)\s*:\s*(?P<val>.+?)\s*$",
    re.IGNORECASE,
)

FLAG_RE = re.compile(
    r"^(?P<indent>\s*)(?P<flag>isHidden)\s*$",
    re.IGNORECASE,
)


def unquote(value: str) -> str:
    value = value.strip()
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def quote_name(value: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        return value
    return "'" + value.replace("'", "''") + "'"


def tmdl_files(semantic_dir: Path | None) -> list[Path]:
    if not semantic_dir or not semantic_dir.exists():
        return []
    return sorted(semantic_dir.rglob("*.tmdl"))


def _compute_qualified_name(kind: str, name: str, table: str | None) -> str:
    if table and kind in {"column", "calculated_column", "measure"}:
        return f"{table}[{name}]"
    if table and kind == "partition":
        return f"{table}.{name}"
    return name


def parse_file(
    path: Path,
    root: Path,
    workspace_id: str | None = None,
) -> list[TmdlObject]:
    ws_id = workspace_id or compute_workspace_id(root)
    objects: list[TmdlObject] = []
    current_table: str | None = None
    current_obj: TmdlObject | None = None
    current_obj_indent: int = 0
    doc_lines: list[str] = []

    in_fenced_expr: bool = False
    fenced_lines: list[str] = []
    in_multiline_dax: bool = False
    multiline_dax_lines: list[str] = []

    def _finalize_multiline() -> None:
        nonlocal in_multiline_dax, multiline_dax_lines, current_obj
        if in_multiline_dax and current_obj is not None:
            combined = "\n".join(multiline_dax_lines).strip()
            if combined:
                if current_obj.expression:
                    current_obj.expression = f"{current_obj.expression}\n{combined}".strip()
                else:
                    current_obj.expression = combined
        in_multiline_dax = False
        multiline_dax_lines = []

    lines = path.read_text(encoding="utf-8-sig").splitlines()
    for number, line in enumerate(lines, 1):
        stripped = line.strip()

        if in_fenced_expr:
            if stripped == "```":
                if current_obj is not None:
                    current_obj.expression = "\n".join(fenced_lines).strip()
                in_fenced_expr = False
                fenced_lines = []
            else:
                fenced_lines.append(line)
            continue

        if stripped.startswith("///"):
            _finalize_multiline()
            doc_lines.append(stripped[3:].strip())
            continue

        match = DECLARATION.match(line)
        if match:
            _finalize_multiline()
            raw_kind = match.group("kind").lower()
            kind_map = {"calculatedcolumn": "calculated_column"}
            kind = kind_map.get(raw_kind, raw_kind)
            name = unquote(match.group("name"))
            indent = len(match.group("indent").expandtabs(4))

            if kind == "table":
                current_table = name

            table = (
                current_table
                if kind in {"column", "calculated_column", "measure", "partition"} and indent
                else None
            )
            expr = match.group("expr")
            if expr is not None:
                expr = expr.strip()
                if expr == "```":
                    expr = ""
                    in_fenced_expr = True
                    fenced_lines = []
                elif expr == "" and kind in {"column", "calculated_column"}:
                    kind = "calculated_column"
                    in_multiline_dax = True
                    multiline_dax_lines = []
                elif expr == "" and kind == "measure":
                    in_multiline_dax = True
                    multiline_dax_lines = []
                elif expr != "" and kind == "column":
                    kind = "calculated_column"
                elif expr != "" and kind in {"measure", "calculated_column"}:
                    # Also allow continuation lines before properties
                    in_multiline_dax = True
                    multiline_dax_lines = []

            description = "\n".join(doc_lines) if doc_lines else None
            doc_lines = []
            qname = _compute_qualified_name(kind, name, table)
            obj_id = make_object_id(ws_id, kind, qname)

            obj = TmdlObject(
                kind=kind,  # type: ignore[arg-type]
                name=name,
                table=table,
                expression=expr,
                description=description,
                file=relative(path, root),
                line=number,
                object_id=obj_id,
                qualified_name=qname,
            )
            objects.append(obj)
            current_obj = obj
            current_obj_indent = indent
            continue

        if stripped == "":
            if in_multiline_dax and multiline_dax_lines:
                multiline_dax_lines.append("")
            continue

        doc_lines = []

        if current_obj is not None:
            line_indent = len(line[: len(line) - len(line.lstrip())].expandtabs(4))
            if line_indent > current_obj_indent:
                prop_match = PROPERTY_RE.match(line)
                if prop_match:
                    _finalize_multiline()
                    prop_name = prop_match.group("prop").lower()
                    val = prop_match.group("val").strip()
                    if prop_name == "datatype":
                        current_obj.data_type = val
                    elif prop_name == "formatstring":
                        current_obj.format_string = val
                    elif prop_name == "summarizeby":
                        current_obj.summarize_by = val
                    elif prop_name == "sourcecolumn":
                        current_obj.source_column = unquote(val)
                    elif prop_name == "fromcolumn":
                        current_obj.from_column = val
                    elif prop_name == "tocolumn":
                        current_obj.to_column = val
                    elif prop_name == "fromcardinality":
                        current_obj.from_cardinality = val
                    elif prop_name == "tocardinality":
                        current_obj.to_cardinality = val
                    elif prop_name == "crossfilteringbehavior":
                        current_obj.cross_filtering = val
                    elif prop_name == "isactive":
                        current_obj.is_active = val.lower() not in {"false", "0"}
                    elif prop_name == "mode":
                        current_obj.partition_mode = val
                    continue

                flag_match = FLAG_RE.match(line)
                if flag_match and flag_match.group("flag").lower() == "ishidden":
                    _finalize_multiline()
                    current_obj.is_hidden = True
                    continue

                if stripped.lower().startswith("annotation ") or stripped.lower().startswith("lineageTag:"):
                    _finalize_multiline()
                    continue

                if stripped.lower().startswith("source =") and current_obj.kind == "partition":
                    if stripped.endswith("```"):
                        in_fenced_expr = True
                        fenced_lines = []
                    continue

                if in_multiline_dax and current_obj.kind in {"measure", "calculated_column"}:
                    multiline_dax_lines.append(stripped)
            else:
                _finalize_multiline()

    _finalize_multiline()
    return objects


def scan_model(
    semantic_dir: Path | None,
    root: Path,
    workspace_id: str | None = None,
) -> list[TmdlObject]:
    ws_id = workspace_id or compute_workspace_id(root)
    result: list[TmdlObject] = []
    for path in tmdl_files(semantic_dir):
        result.extend(parse_file(path, root, workspace_id=ws_id))
    return result


def get_table_schema(
    semantic_dir: Path | None,
    root: Path,
    table_name: str,
    workspace_id: str | None = None,
) -> dict[str, Any] | None:
    ws_id = workspace_id or compute_workspace_id(root)
    objects = scan_model(semantic_dir, root, workspace_id=ws_id)
    tables = [x for x in objects if x.kind == "table"]
    target_cf = table_name.strip().casefold()

    exact_matches = [x for x in tables if x.name.casefold() == target_cf]
    if len(exact_matches) == 1:
        table_obj = exact_matches[0]
    elif len(exact_matches) > 1:
        raise AmbiguousObjectError(
            f"Multiple tables match {table_name!r}.",
            candidates=[{"name": t.name, "file": t.file, "object_id": t.object_id} for t in exact_matches],
        )
    else:
        partial_matches = [x for x in tables if target_cf in x.name.casefold()]
        if len(partial_matches) > 1:
            raise AmbiguousObjectError(
                f"Ambiguous table selector {table_name!r}; matched multiple tables: "
                + ", ".join(t.name for t in partial_matches),
                candidates=[{"name": t.name, "file": t.file, "object_id": t.object_id} for t in partial_matches],
            )
        if not partial_matches:
            return None
        table_obj = partial_matches[0]

    canonical = table_obj.name
    columns = [
        x.model_dump()
        for x in objects
        if x.kind in {"column", "calculated_column"} and (x.table or "").casefold() == canonical.casefold()
    ]
    measures = [
        x.model_dump()
        for x in objects
        if x.kind == "measure" and (x.table or "").casefold() == canonical.casefold()
    ]
    relationships = [
        x.model_dump()
        for x in objects
        if x.kind == "relationship"
        and (
            (x.from_column or "").lower().startswith(f"{canonical.lower()}.")
            or (x.from_column or "").lower().startswith(f"'{canonical.lower()}'.")
            or (x.to_column or "").lower().startswith(f"{canonical.lower()}.")
            or (x.to_column or "").lower().startswith(f"'{canonical.lower()}'.")
        )
    ]

    partitions: list[dict[str, Any]] = []
    if semantic_dir and semantic_dir.exists():
        partitions = [
            p
            for p in discover_partition_metadata(semantic_dir, root)
            if str(p.get("table", "")).casefold() == canonical.casefold()
        ]
        sql_sources = [
            s.to_summary()
            for s in discover_tmdl_sql_sources(semantic_dir)
            if s.table.casefold() == canonical.casefold()
        ]
    else:
        sql_sources = []

    return {
        "table": canonical,
        "object_id": table_obj.object_id,
        "qualified_name": table_obj.qualified_name,
        "file": table_obj.file,
        "line": table_obj.line,
        "description": table_obj.description,
        "column_count": len(columns),
        "measure_count": len(measures),
        "partition_count": len(partitions),
        "columns": columns,
        "measures": measures,
        "relationships": relationships,
        "partitions": partitions,
        "sql_sources": sql_sources,
    }
