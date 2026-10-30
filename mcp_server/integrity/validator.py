"""PBIP-403: Power BI Structure Validation Safety Net.

Performs comprehensive post-write syntactic and structural validation on
.pbip projects (descriptor, semantic model TMDLs, and report layout JSON)
to guarantee file integrity before releasing control back to the user.
"""

from __future__ import annotations
import os
import json
import glob
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
from mcp_server.engine_a.tmdl_document import TMDLDocument


class ValidationResult(BaseModel):
    """Result of structural validation."""
    is_valid: bool
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    checked_files: List[str] = Field(default_factory=list)


class StructureValidator:
    """Post-write syntax validator checking modified .pbip directories."""

    def __init__(self, workspace_path: str):
        self.workspace_path = os.path.abspath(workspace_path)

    def _locate_pbip_file(self) -> Optional[str]:
        if self.workspace_path.endswith(".pbip") and os.path.isfile(self.workspace_path):
            return self.workspace_path
        if os.path.isdir(self.workspace_path):
            candidates = glob.glob(os.path.join(self.workspace_path, "*.pbip"))
            if candidates:
                return candidates[0]
        return None

    def validate(self) -> ValidationResult:
        """Run complete validation suite across semantic model and report layout."""
        errors: List[str] = []
        warnings: List[str] = []
        checked: List[str] = []

        # 1. Validate .pbip file if present
        pbip_file = self._locate_pbip_file()
        if pbip_file:
            checked.append(pbip_file)
            try:
                with open(pbip_file, "r", encoding="utf-8") as f:
                    pbip_data = json.load(f)
                if "artifacts" not in pbip_data:
                    errors.append(f"{pbip_file}: Missing 'artifacts' root property.")
            except Exception as e:
                errors.append(f"{pbip_file}: Invalid JSON syntax: {e}")

        # 2. Locate and validate SemanticModel
        sem_dirs = []
        if pbip_file:
            base_dir = os.path.dirname(pbip_file)
            proj_name = os.path.splitext(os.path.basename(pbip_file))[0]
            sem_candidate = os.path.join(base_dir, f"{proj_name}.SemanticModel")
            if os.path.isdir(sem_candidate):
                sem_dirs.append(sem_candidate)
        if not sem_dirs and os.path.isdir(self.workspace_path):
            sem_dirs.extend(glob.glob(os.path.join(self.workspace_path, "*.SemanticModel")))

        for sem_dir in sem_dirs:
            # Check definition.pbism
            pbism_path = os.path.join(sem_dir, "definition.pbism")
            if os.path.isfile(pbism_path):
                checked.append(pbism_path)
                try:
                    with open(pbism_path, "r", encoding="utf-8") as f:
                        json.load(f)
                except Exception as e:
                    errors.append(f"{pbism_path}: Invalid JSON: {e}")

            # Check definition/model.tmdl
            model_tmdl = os.path.join(sem_dir, "definition", "model.tmdl")
            if os.path.isfile(model_tmdl):
                checked.append(model_tmdl)
                try:
                    doc = TMDLDocument.from_file(model_tmdl)
                    if not any(n.node_type == "model" for n in doc.nodes):
                        warnings.append(f"{model_tmdl}: No top-level 'model' declaration found.")
                except Exception as e:
                    errors.append(f"{model_tmdl}: TMDL parse error: {e}")

            # Check all tables in definition/tables/
            tables_dir = os.path.join(sem_dir, "definition", "tables")
            if os.path.isdir(tables_dir):
                for tfile in glob.glob(os.path.join(tables_dir, "*.tmdl")):
                    checked.append(tfile)
                    try:
                        doc = TMDLDocument.from_file(tfile)
                        table_node = doc.get_table()
                        if not table_node:
                            errors.append(f"{tfile}: Missing 'table' declaration.")
                        # Check measure syntax
                        for m in doc.get_measures():
                            if not m.expression and not m.properties_lines:
                                warnings.append(f"{tfile}: Measure '{m.name}' has no expression or properties.")
                    except Exception as e:
                        errors.append(f"{tfile}: TMDL parse error: {e}")

        # 3. Locate and validate Report
        rep_dirs = []
        if pbip_file:
            base_dir = os.path.dirname(pbip_file)
            proj_name = os.path.splitext(os.path.basename(pbip_file))[0]
            rep_candidate = os.path.join(base_dir, f"{proj_name}.Report")
            if os.path.isdir(rep_candidate):
                rep_dirs.append(rep_candidate)
        if not rep_dirs and os.path.isdir(self.workspace_path):
            rep_dirs.extend(glob.glob(os.path.join(self.workspace_path, "*.Report")))

        for rep_dir in rep_dirs:
            # Check definition.pbir
            pbir_path = os.path.join(rep_dir, "definition.pbir")
            if os.path.isfile(pbir_path):
                checked.append(pbir_path)
                try:
                    with open(pbir_path, "r", encoding="utf-8") as f:
                        json.load(f)
                except Exception as e:
                    errors.append(f"{pbir_path}: Invalid JSON: {e}")

            # Check report.json
            rep_json = os.path.join(rep_dir, "report.json")
            if os.path.isfile(rep_json):
                checked.append(rep_json)
                try:
                    with open(rep_json, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if "sections" not in data or not isinstance(data["sections"], list):
                        errors.append(f"{rep_json}: 'sections' must be an array.")
                    else:
                        for s_idx, sec in enumerate(data["sections"]):
                            containers = sec.get("visualContainers", [])
                            for c_idx, container in enumerate(containers):
                                for field in ("config", "query", "dataTransforms", "filters"):
                                    if field in container and isinstance(container[field], str):
                                        val = container[field].strip()
                                        if val:
                                            try:
                                                json.loads(val)
                                            except Exception as je:
                                                errors.append(
                                                    f"{rep_json}: section[{s_idx}] visualContainer[{c_idx}] embedded JSON string in '{field}' is invalid: {je}"
                                                )
                except Exception as e:
                    errors.append(f"{rep_json}: Invalid report.json syntax: {e}")

        is_valid = (len(errors) == 0)
        return ValidationResult(
            is_valid=is_valid,
            errors=errors,
            warnings=warnings,
            checked_files=checked,
        )
