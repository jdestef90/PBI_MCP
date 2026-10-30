"""PBIP-401: Reference-Aware Rename Engine.

Coordinates cross-layer renames between the Semantic Model (Engine A TMDL)
and the Report Layout (Engine B JSON), ensuring all bound visual references,
queries, projections, and DAX formulas are updated without destructive naive
substring replacement.
"""

from __future__ import annotations
import os
import re
import json
from typing import Dict, List, Any, Optional, Tuple
from mcp_server.engine_a.parser_wrapper import SemanticModelEngine
from mcp_server.engine_a.tmdl_document import TMDLDocument, unquote_identifier, quote_identifier
from mcp_server.engine_b.layout_flattener import ReportLayoutEngine, safe_json_loads
from mcp_server.integrity.validator import StructureValidator, ValidationResult


class RenameResult:
    """Detailed summary of cross-layer rename operation."""

    def __init__(
        self,
        success: bool,
        old_name: str,
        new_name: str,
        table_name: str,
        tmdl_files_updated: List[str],
        visual_references_updated: int,
        validation: ValidationResult,
        error_message: Optional[str] = None,
    ):
        self.success = success
        self.old_name = old_name
        self.new_name = new_name
        self.table_name = table_name
        self.tmdl_files_updated = tmdl_files_updated
        self.visual_references_updated = visual_references_updated
        self.validation = validation
        self.error_message = error_message

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "old_name": self.old_name,
            "new_name": self.new_name,
            "table_name": self.table_name,
            "tmdl_files_updated": self.tmdl_files_updated,
            "visual_references_updated": self.visual_references_updated,
            "validation_passed": self.validation.is_valid,
            "validation_errors": self.validation.errors,
            "error_message": self.error_message,
        }


class RenameEngine:
    """Coordinates reference-aware renames across TMDL semantic models and JSON layouts."""

    def __init__(
        self,
        workspace_path: str,
        semantic_engine: Optional[SemanticModelEngine] = None,
        layout_engine: Optional[ReportLayoutEngine] = None,
    ):
        self.workspace_path = os.path.abspath(workspace_path)
        self.semantic_engine = semantic_engine or SemanticModelEngine(self.workspace_path)
        self.layout_engine = layout_engine or ReportLayoutEngine(self.workspace_path)
        self.validator = StructureValidator(self.workspace_path)

    def rename_measure(
        self,
        table_name: str,
        old_measure_name: str,
        new_measure_name: str,
    ) -> RenameResult:
        """Execute atomic cross-layer rename workflow for a measure."""
        clean_table = unquote_identifier(table_name)
        clean_old = unquote_identifier(old_measure_name)
        clean_new = unquote_identifier(new_measure_name)

        # 1. Verify existence in Semantic Model
        measure = self.semantic_engine.get_measure(clean_table, clean_old)
        if not measure:
            return RenameResult(
                success=False,
                old_name=clean_old,
                new_name=clean_new,
                table_name=clean_table,
                tmdl_files_updated=[],
                visual_references_updated=0,
                validation=ValidationResult(is_valid=False, errors=[f"Measure '{clean_old}' not found in table '{clean_table}'"]),
                error_message=f"Measure '{clean_old}' not found in table '{clean_table}'",
            )

        tmdl_updated: List[str] = []

        # 2. Update definition in target table TMDL file
        target_tmdl_path = self.semantic_engine._get_table_filepath(clean_table)
        target_doc = TMDLDocument.from_file(target_tmdl_path)
        target_doc.rename_measure(clean_old, clean_new)
        target_doc.write_to_file(target_tmdl_path)
        tmdl_updated.append(target_tmdl_path)

        # 3. Update DAX formula references across ALL table TMDL files
        tables_dir = self.semantic_engine.tables_dir
        for fname in os.listdir(tables_dir):
            if not fname.endswith(".tmdl"):
                continue
            fpath = os.path.join(tables_dir, fname)
            doc = TMDLDocument.from_file(fpath)
            doc_modified = False

            # Pattern for: 'TableName'[OldMeasure] or [OldMeasure]
            table_meas_pat = re.compile(
                r"('?" + re.escape(clean_table) + r"'?\[)" + re.escape(clean_old) + r"(\])"
            )
            bare_meas_pat = re.compile(r"(\[)" + re.escape(clean_old) + r"(\])")

            for m in doc.get_measures():
                orig_dax = m.expression
                if orig_dax:
                    new_dax = table_meas_pat.sub(r"\g<1>" + clean_new + r"\2", orig_dax)
                    new_dax = bare_meas_pat.sub(r"\g<1>" + clean_new + r"\2", new_dax)
                    if new_dax != orig_dax:
                        doc.update_measure_dax(m.name, new_dax)
                        doc_modified = True

            for c in doc.get_columns():
                orig_expr = c.expression
                if orig_expr:
                    new_expr = table_meas_pat.sub(r"\g<1>" + clean_new + r"\2", orig_expr)
                    new_expr = bare_meas_pat.sub(r"\g<1>" + clean_new + r"\2", new_expr)
                    if new_expr != orig_expr:
                        c.expression = new_expr
                        doc_modified = True

            if doc_modified:
                doc.write_to_file(fpath)
                if fpath not in tmdl_updated:
                    tmdl_updated.append(fpath)

        # 4. Invalidate semantic cache
        self.semantic_engine.cache.invalidate()

        # 5. Update Report Layout JSON references safely
        layout_raw = self.layout_engine.load_layout()
        old_full_ref = f"{clean_table}.{clean_old}"
        new_full_ref = f"{clean_table}.{clean_new}"

        ref_count = 0

        def update_references(obj: Any) -> Any:
            nonlocal ref_count
            if isinstance(obj, dict):
                # Check queryRef
                if "queryRef" in obj and obj["queryRef"] == old_full_ref:
                    obj["queryRef"] = new_full_ref
                    ref_count += 1
                elif "queryRef" in obj and obj["queryRef"] == clean_old:
                    obj["queryRef"] = clean_new
                    ref_count += 1

                # Check Name
                if "Name" in obj and obj["Name"] == old_full_ref:
                    obj["Name"] = new_full_ref
                    ref_count += 1

                # Check NativeReferenceName
                if "NativeReferenceName" in obj and obj["NativeReferenceName"] == clean_old:
                    obj["NativeReferenceName"] = clean_new
                    ref_count += 1

                # Check Restatement
                if "Restatement" in obj and obj["Restatement"] == clean_old:
                    obj["Restatement"] = clean_new
                    ref_count += 1

                # Check Measure.Property
                if "Measure" in obj and isinstance(obj["Measure"], dict):
                    m_dict = obj["Measure"]
                    if m_dict.get("Property") == clean_old:
                        m_dict["Property"] = clean_new
                        ref_count += 1

                # Check title literal value
                if "Literal" in obj and isinstance(obj["Literal"], dict):
                    lit_val = obj["Literal"].get("Value")
                    if isinstance(lit_val, str) and (lit_val == f"'{clean_old}'" or lit_val == clean_old):
                        obj["Literal"]["Value"] = f"'{clean_new}'"
                        ref_count += 1

                # Recurse all keys
                for k, v in obj.items():
                    obj[k] = update_references(v)
                return obj

            elif isinstance(obj, list):
                return [update_references(elem) for elem in obj]
            return obj

        # Process report sections & containers
        sections = layout_raw.get("sections", [])
        for sec in sections:
            containers = sec.get("visualContainers", [])
            for container in containers:
                for field in ("config", "query", "dataTransforms", "filters"):
                    if field in container and isinstance(container[field], str):
                        raw_str = container[field]
                        decoded = safe_json_loads(raw_str)
                        if isinstance(decoded, (dict, list)):
                            updated_decoded = update_references(decoded)
                            # Re-encode cleanly without extra spaces
                            container[field] = json.dumps(updated_decoded, separators=(',', ':'))

        # Write updated report layout to disk
        self.layout_engine.write_layout(layout_raw)

        # 6. Safety Net: Validate modified project
        val_result = self.validator.validate()
        if not val_result.is_valid:
            return RenameResult(
                success=False,
                old_name=clean_old,
                new_name=clean_new,
                table_name=clean_table,
                tmdl_files_updated=tmdl_updated,
                visual_references_updated=ref_count,
                validation=val_result,
                error_message=f"Post-write validation failed: {', '.join(val_result.errors)}",
            )

        return RenameResult(
            success=True,
            old_name=clean_old,
            new_name=clean_new,
            table_name=clean_table,
            tmdl_files_updated=tmdl_updated,
            visual_references_updated=ref_count,
            validation=val_result,
        )
