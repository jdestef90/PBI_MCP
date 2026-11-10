from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _walk(value: Any, trail: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, trail + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, trail + (str(index),))
    elif isinstance(value, str):
        yield trail, value


def _candidate_kind(trail: tuple[str, ...], value: str) -> str | None:
    text = " ".join(trail).casefold()
    low = value.casefold()
    if "semanticmodel" in low or "semantic model" in text or "dataset" in text:
        return "semantic_model"
    if low.endswith(".report") or "report" in text:
        return "report"
    return None


def _resolve_candidate(pbip_file: Path, value: str, suffix: str) -> Path | None:
    normalized = value.replace("\\", "/").strip()
    candidate = Path(normalized)
    probes: list[Path] = []
    if candidate.is_absolute():
        probes.append(candidate)
    else:
        probes.extend([pbip_file.parent / candidate, pbip_file.parent / candidate.name])
    if not candidate.name.casefold().endswith(suffix.casefold()):
        probes.extend([p.with_name(p.name + suffix) for p in list(probes)])
    for probe in probes:
        resolved = probe.resolve()
        if resolved.exists() and resolved.is_dir() and resolved.name.casefold().endswith(suffix.casefold()):
            return resolved
    name = candidate.name
    if not name.casefold().endswith(suffix.casefold()):
        name += suffix
    matches = list(pbip_file.parent.glob(name))
    return matches[0].resolve() if len(matches) == 1 else None


def read_pbip_references(pbip_file: Path) -> dict[str, Any]:
    """Parse explicit local artifact references from a .pbip JSON document."""
    result: dict[str, Any] = {
        "reference_source": "pbip",
        "semantic_model_dir": None,
        "report_dir": None,
        "raw_references": [],
        "warnings": [],
    }
    try:
        payload = json.loads(pbip_file.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        result["reference_source"] = "unreadable"
        result["warnings"].append(f"PBIP reference document could not be parsed: {exc}")
        return result

    for trail, value in _walk(payload):
        kind = _candidate_kind(trail, value)
        if not kind:
            continue
        result["raw_references"].append({"kind": kind, "json_path": ".".join(trail), "value": value})
        suffix = ".SemanticModel" if kind == "semantic_model" else ".Report"
        resolved = _resolve_candidate(pbip_file, value, suffix)
        key = "semantic_model_dir" if kind == "semantic_model" else "report_dir"
        if resolved and result[key] is None:
            result[key] = str(resolved)

    if result["raw_references"] and result["semantic_model_dir"] is None:
        result["warnings"].append("No explicit semantic-model reference could be resolved to a local directory.")
    if result["raw_references"] and result["report_dir"] is None:
        result["warnings"].append("No explicit report reference could be resolved to a local directory.")
    return result
