from __future__ import annotations

import json
import re
from collections import Counter

from ..engine_a.parser import scan_model, tmdl_files
from ..engine_b.parser import json_files
from ..models import ValidationIssue
from ..workspace import WorkspaceInfo, relative

_FENCE_RE = re.compile(r"(?m)^[ \t]*(?:[^\r\n=]+=\s*)?```[ \t]*$")


def validate(info: WorkspaceInfo) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not info.semantic_model_dir:
        issues.append(
            ValidationIssue(
                severity="error",
                code="SEMANTIC_MODEL_MISSING",
                message="Semantic model directory was not found.",
            )
        )
    if not info.report_dir:
        issues.append(
            ValidationIssue(
                severity="warning",
                code="REPORT_MISSING",
                message="Report directory was not found.",
            )
        )

    for path in json_files(info.report_dir):
        rel = relative(path, info.root)
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="INVALID_JSON",
                    message=str(exc),
                    file=rel,
                )
            )
            continue

        # If legacy report.json, also validate embedded JSON strings inside visualContainers
        if path.name.lower() == "report.json" and isinstance(payload, dict):
            for sec in payload.get("sections", []):
                if not isinstance(sec, dict):
                    continue
                for container in sec.get("visualContainers", []):
                    if not isinstance(container, dict):
                        continue
                    for key in ("config", "query", "dataTransforms", "filters"):
                        val = container.get(key)
                        if isinstance(val, str) and val.strip():
                            try:
                                json.loads(val)
                            except json.JSONDecodeError as exc:
                                issues.append(
                                    ValidationIssue(
                                        severity="error",
                                        code="INVALID_EMBEDDED_JSON",
                                        message=f"Embedded {key} JSON invalid: {exc}",
                                        file=rel,
                                    )
                                )

    for path in tmdl_files(info.semantic_model_dir):
        rel = relative(path, info.root)
        text = path.read_text(encoding="utf-8-sig")
        fences = _FENCE_RE.findall(text)
        if len(fences) % 2 != 0:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="UNBALANCED_TMDL_FENCE",
                    message="TMDL file has an odd number of ``` fence markers.",
                    file=rel,
                )
            )

    objects = scan_model(info.semantic_model_dir, info.root)
    keys = [(item.table, item.name) for item in objects if item.kind == "measure"]
    for key, count in Counter(keys).items():
        if count > 1:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="DUPLICATE_MEASURE",
                    message=f"Duplicate measure {key[1]!r} in table {key[0]!r}.",
                )
            )

    for item in objects:
        if item.kind == "measure" and not (item.expression or "").strip():
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="EMPTY_MEASURE_EXPRESSION",
                    message=f"Measure {item.name!r} in table {item.table!r} has an empty DAX expression.",
                    file=item.file,
                )
            )

    if not issues:
        issues.append(
            ValidationIssue(
                severity="info",
                code="VALIDATION_OK",
                message="No structural problems detected by local validation.",
            )
        )
    return issues


def has_validation_errors(info: WorkspaceInfo) -> list[ValidationIssue]:
    return [issue for issue in validate(info) if issue.severity == "error"]
