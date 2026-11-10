from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from .parser import DECLARATION, quote_name, unquote


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


def update_measure_expression(text: str, measure_name: str, expression: str) -> tuple[str, int]:
    lines = text.splitlines(keepends=True)
    count = 0
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        match = DECLARATION.match(body)
        if not match or match.group("kind").lower() != "measure":
            continue
        if unquote(match.group("name")) != measure_name:
            continue
        prefix = body[: match.start("name")]
        lines[index] = f"{prefix}{match.group('name')} = {expression}{ending}"
        count += 1
    return "".join(lines), count


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
            # Remove any immediately preceding /// lines in `out`
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
    out = []
    count = 0
    for line in lines:
        m = DECLARATION.match(line.rstrip("\r\n"))
        if m and m.group("kind").casefold() == "measure" and unquote(m.group("name")) == measure_name:
            count += 1
            continue
        out.append(line)
    return "".join(out), count
