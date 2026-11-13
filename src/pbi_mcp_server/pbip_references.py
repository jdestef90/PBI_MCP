from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_EXTENDED_WIN_UNC_RE = re.compile(r"^\\\\\?\\UNC\\", re.IGNORECASE)
_EXTENDED_WIN_DRIVE_RE = re.compile(r"^\\\\[\?\.]\\([A-Za-z]:)", re.IGNORECASE)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def normalize_path_str(raw: str | Path) -> str:
    """Normalize Windows, OneDrive, UNC, and POSIX path strings into a canonical form."""
    text = os.path.expandvars(str(raw).strip())
    text = _EXTENDED_WIN_UNC_RE.sub(r"\\\\", text)
    text = _EXTENDED_WIN_DRIVE_RE.sub(r"\1", text)
    if text.startswith("~"):
        text = str(Path(text).expanduser())
    # Normalize backslashes to forward slashes for cross-platform canonicalization
    normalized = text.replace("\\", "/")
    # Collapse duplicate slashes except leading UNC //
    if normalized.startswith("//"):
        prefix = "//"
        rest = re.sub(r"/+", "/", normalized[2:])
        normalized = prefix + rest
    else:
        normalized = re.sub(r"/+", "/", normalized)
    if len(normalized) > 1 and normalized.endswith("/") and not re.fullmatch(r"[A-Za-z]:/", normalized):
        normalized = normalized.rstrip("/")
    return normalized


def canonical_path_key(raw: str | Path) -> str:
    """Return a deterministic, case-folded canonical key for workspace ID hashing and comparison."""
    p = Path(normalize_path_str(raw))
    try:
        if p.exists():
            p = p.resolve()
    except OSError:
        pass
    norm = normalize_path_str(p)
    # Normalize drive letter and casefold for deterministic cross-platform IDs
    return norm.casefold()


def normalize_platform_path(raw: str | Path) -> Path:
    """Convert raw user/config path (including OneDrive or Windows syntax) into a resolved Path."""
    cleaned = normalize_path_str(raw)
    p = Path(cleaned).expanduser()
    try:
        return p.resolve()
    except OSError:
        return p


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


def _resolve_candidate(base_dir: Path, value: str, suffix: str) -> Path | None:
    normalized = normalize_path_str(value)
    candidate = Path(normalized)
    probes: list[Path] = []
    if candidate.is_absolute():
        probes.append(candidate)
    else:
        probes.extend([base_dir / candidate, base_dir / candidate.name])
    if not candidate.name.casefold().endswith(suffix.casefold()):
        probes.extend([p.with_name(p.name + suffix) for p in list(probes)])
    for probe in probes:
        try:
            resolved = probe.resolve()
            if resolved.exists() and resolved.is_dir() and resolved.name.casefold().endswith(suffix.casefold()):
                return resolved
        except OSError:
            continue
    name = candidate.name
    if not name.casefold().endswith(suffix.casefold()):
        name += suffix
    try:
        matches = list(base_dir.glob(name))
        return matches[0].resolve() if len(matches) == 1 else None
    except OSError:
        return None


def _read_pbir_dataset_reference(report_dir: Path) -> Path | None:
    """Resolve semantic model path referenced inside <Report>/definition.pbir if present."""
    pbir = report_dir / "definition.pbir"
    if not pbir.exists() or not pbir.is_file():
        return None
    try:
        payload = json.loads(pbir.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    by_path = payload.get("datasetReference", {})
    if isinstance(by_path, dict):
        by_path_obj = by_path.get("byPath")
        if isinstance(by_path_obj, dict) and isinstance(by_path_obj.get("path"), str):
            raw_rel = by_path_obj["path"]
            resolved = _resolve_candidate(report_dir, raw_rel, ".SemanticModel")
            if resolved:
                return resolved
    return None


def read_pbip_references(pbip_file: Path) -> dict[str, Any]:
    """Parse explicit local artifact references from a .pbip JSON document and paired definition.pbir."""
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

    had_explicit_semantic_ref = False
    had_explicit_report_ref = False

    for trail, value in _walk(payload):
        kind = _candidate_kind(trail, value)
        if not kind:
            continue
        result["raw_references"].append({"kind": kind, "json_path": ".".join(trail), "value": value})
        if kind == "semantic_model":
            had_explicit_semantic_ref = True
            resolved = _resolve_candidate(pbip_file.parent, value, ".SemanticModel")
            if resolved and result["semantic_model_dir"] is None:
                result["semantic_model_dir"] = str(resolved)
        else:
            had_explicit_report_ref = True
            resolved = _resolve_candidate(pbip_file.parent, value, ".Report")
            if resolved and result["report_dir"] is None:
                result["report_dir"] = str(resolved)

    # If report_dir was resolved and semantic_model_dir wasn't explicitly in .pbip, check definition.pbir
    if result["report_dir"] and not result["semantic_model_dir"]:
        pbir_sem = _read_pbir_dataset_reference(Path(result["report_dir"]))
        if pbir_sem:
            result["semantic_model_dir"] = str(pbir_sem)

    if had_explicit_semantic_ref and result["semantic_model_dir"] is None:
        result["warnings"].append("No explicit semantic-model reference could be resolved to a local directory.")
    if had_explicit_report_ref and result["report_dir"] is None:
        result["warnings"].append("No explicit report reference could be resolved to a local directory.")
    return result
