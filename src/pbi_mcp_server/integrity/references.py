from __future__ import annotations

from pathlib import Path

from ..engine_a.parser import tmdl_files
from ..engine_b.parser import json_files
from ..workspace import relative


def find_references(
    root: Path,
    semantic_dir: Path | None,
    report_dir: Path | None,
    term: str,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for path in [*tmdl_files(semantic_dir), *json_files(report_dir)]:
        text = path.read_text(encoding="utf-8-sig")
        for number, line in enumerate(text.splitlines(), 1):
            if term.casefold() in line.casefold():
                results.append({
                    "file": relative(path, root),
                    "line": number,
                    "text": line.strip()[:500],
                })
    return results
