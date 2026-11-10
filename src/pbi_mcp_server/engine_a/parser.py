from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..models import TmdlObject
from ..workspace import relative

DECLARATION = re.compile(
    r"^(?P<indent>\s*)(?P<kind>table|column|measure|calculatedColumn|relationship)\s+"
    r"(?P<name>'(?:[^']|'')+'|\"[^\"]+\"|[^=\s]+)(?:\s*=\s*(?P<expr>.*))?$",
    re.IGNORECASE,
)

PROPERTY_RE = re.compile(
    r"^(?P<indent>\s*)(?P<prop>dataType|formatString|summarizeBy|fromColumn|toColumn|crossFilteringBehavior)\s*:\s*(?P<val>.+?)\s*$",
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


def parse_file(path: Path, root: Path) -> list[TmdlObject]:
    objects: list[TmdlObject] = []
    current_table: str | None = None
    current_obj: TmdlObject | None = None
    current_obj_indent: int = 0
    doc_lines: list[str] = []

    lines = path.read_text(encoding="utf-8-sig").splitlines()
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("///"):
            doc_lines.append(stripped[3:].strip())
            continue

        match = DECLARATION.match(line)
        if match:
            raw_kind = match.group("kind").lower()
            kind_map = {"calculatedcolumn": "calculated_column"}
            kind = kind_map.get(raw_kind, raw_kind)
            name = unquote(match.group("name"))
            indent = len(match.group("indent").expandtabs(4))

            if kind == "table":
                current_table = name

            table = current_table if kind in {"column", "calculated_column", "measure"} and indent else None
            expr = match.group("expr")
            if expr is not None:
                expr = expr.strip()
                if expr == "```":
                    expr = ""
                elif expr == "" and kind in {"column", "calculated_column"}:
                    kind = "calculated_column"
                elif expr != "" and kind == "column":
                    kind = "calculated_column"

            description = "\n".join(doc_lines) if doc_lines else None
            doc_lines = []

            obj = TmdlObject(
                kind=kind,  # type: ignore[arg-type]
                name=name,
                table=table,
                expression=expr,
                description=description,
                file=relative(path, root),
                line=number,
            )
            objects.append(obj)
            current_obj = obj
            current_obj_indent = indent
            continue

        if stripped == "":
            continue

        doc_lines = []

        if current_obj is not None:
            line_indent = len(line[: len(line) - len(line.lstrip())].expandtabs(4))
            if line_indent > current_obj_indent:
                prop_match = PROPERTY_RE.match(line)
                if prop_match:
                    prop_name = prop_match.group("prop").lower()
                    val = prop_match.group("val").strip()
                    if prop_name == "datatype":
                        current_obj.data_type = val
                    elif prop_name == "formatstring":
                        current_obj.format_string = val
                    elif prop_name == "summarizeby":
                        current_obj.summarize_by = val
                    elif prop_name == "fromcolumn":
                        current_obj.from_column = val
                    elif prop_name == "tocolumn":
                        current_obj.to_column = val
                    elif prop_name == "crossfilteringbehavior":
                        current_obj.cross_filtering = val
                    continue

                flag_match = FLAG_RE.match(line)
                if flag_match and flag_match.group("flag").lower() == "ishidden":
                    current_obj.is_hidden = True
                    continue

    return objects


def scan_model(semantic_dir: Path | None, root: Path) -> list[TmdlObject]:
    result: list[TmdlObject] = []
    for path in tmdl_files(semantic_dir):
        result.extend(parse_file(path, root))
    return result


def get_table_schema(semantic_dir: Path | None, root: Path, table_name: str) -> dict[str, Any] | None:
    objects = scan_model(semantic_dir, root)
    table_obj = next(
        (x for x in objects if x.kind == "table" and x.name.casefold() == table_name.casefold()),
        None,
    )
    if not table_obj:
        return None

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

    return {
        "table": canonical,
        "file": table_obj.file,
        "line": table_obj.line,
        "description": table_obj.description,
        "column_count": len(columns),
        "measure_count": len(measures),
        "columns": columns,
        "measures": measures,
        "relationships": relationships,
    }
