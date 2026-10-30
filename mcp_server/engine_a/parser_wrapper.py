"""PBIP-201: TMDL Parser Wrapper and Semantic Model Engine.

Provides an abstraction layer over tmdl-parser and TMDLDocument to ingest
definition folders, expose typed Pydantic models, and execute structured
write operations for DAX updates and renames.
"""

from __future__ import annotations
import os
import glob
from typing import Dict, List, Optional, Tuple
from mcp_server.cache.state_cache import StateCache
from mcp_server.engine_a.tmdl_document import TMDLDocument, unquote_identifier, quote_identifier
from mcp_server.engine_a.models import (
    ColumnMetadata,
    MeasureMetadata,
    TableMetadata,
    TableSummary,
    SemanticModelSummary,
)


class SemanticModelEngine:
    """Engine A: Manages Semantic Model parsing, querying, and updating."""

    def __init__(self, workspace_path: str, cache: Optional[StateCache] = None):
        self.workspace_path = os.path.abspath(workspace_path)
        self.cache = cache or StateCache()
        self.semantic_model_path = self._locate_semantic_model(self.workspace_path)
        self.tables_dir = os.path.join(self.semantic_model_path, "definition", "tables")

    def _locate_semantic_model(self, root_path: str) -> str:
        """Find the .SemanticModel directory given a pbip file or root folder."""
        if root_path.endswith(".pbip") and os.path.isfile(root_path):
            dir_name = os.path.dirname(root_path)
            base_name = os.path.splitext(os.path.basename(root_path))[0]
            candidate = os.path.join(dir_name, f"{base_name}.SemanticModel")
            if os.path.isdir(candidate):
                return candidate

        # If pointing directly to .SemanticModel
        if root_path.endswith(".SemanticModel") and os.path.isdir(root_path):
            return root_path

        # If pointing to project directory, search for .SemanticModel inside
        if os.path.isdir(root_path):
            candidates = glob.glob(os.path.join(root_path, "*.SemanticModel"))
            if candidates:
                return candidates[0]
            # Check if definition folder is directly inside
            if os.path.isdir(os.path.join(root_path, "definition")):
                return root_path

        raise ValueError(f"Could not locate Power BI SemanticModel directory in: {root_path}")

    def _get_table_filepath(self, table_name: str) -> str:
        """Get the full file path for a table TMDL file."""
        clean_name = unquote_identifier(table_name)
        # Check direct filename
        target = os.path.join(self.tables_dir, f"{clean_name}.tmdl")
        if os.path.isfile(target):
            return target
        # Check case-insensitive match
        if os.path.isdir(self.tables_dir):
            for fname in os.listdir(self.tables_dir):
                if fname.lower() == f"{clean_name.lower()}.tmdl":
                    return os.path.join(self.tables_dir, fname)
        raise FileNotFoundError(f"Table '{clean_name}' TMDL file not found in {self.tables_dir}")

    def _load_tmdl_doc(self, filepath: str) -> TMDLDocument:
        """Load TMDLDocument through cache."""
        return self.cache.get_or_load(filepath, lambda p: TMDLDocument.from_file(p))

    def list_table_names(self) -> List[str]:
        """List all available table names from the semantic model."""
        if not os.path.isdir(self.tables_dir):
            return []
        table_files = [f for f in os.listdir(self.tables_dir) if f.endswith(".tmdl")]
        names = []
        for f in table_files:
            table_name = os.path.splitext(f)[0]
            names.append(table_name)
        return sorted(names)

    def get_table(self, table_name: str) -> TableMetadata:
        """Load and return full metadata for a table as a typed Pydantic model."""
        filepath = self._get_table_filepath(table_name)
        doc = self._load_tmdl_doc(filepath)

        table_node = doc.get_table()
        table_clean_name = table_node.name if table_node else unquote_identifier(table_name)
        lineage_tag = table_node.get_property("lineageTag") if table_node else None

        # Build column metadata
        columns: List[ColumnMetadata] = []
        for c in doc.get_columns():
            is_calc = bool(c.expression)
            columns.append(
                ColumnMetadata(
                    name=c.name,
                    data_type=c.get_property("dataType"),
                    format_string=c.get_property("formatString"),
                    summarize_by=c.get_property("summarizeBy"),
                    source_column=c.get_property("sourceColumn"),
                    is_calculated=is_calc,
                    expression=c.expression if is_calc else None,
                    description="\n".join([line.strip().lstrip("/ ") for line in c.leading_comments]) if c.leading_comments else None,
                    lineage_tag=c.get_property("lineageTag"),
                )
            )

        # Build measure metadata
        measures: List[MeasureMetadata] = []
        for m in doc.get_measures():
            measures.append(
                MeasureMetadata(
                    name=m.name,
                    expression=m.expression,
                    format_string=m.get_property("formatString"),
                    description="\n".join([line.strip().lstrip("/ ") for line in m.leading_comments]) if m.leading_comments else None,
                    lineage_tag=m.get_property("lineageTag"),
                    display_folder=m.get_property("displayFolder"),
                )
            )

        # Partitions
        partitions: List[str] = []
        if table_node:
            for child in table_node.children:
                if child.node_type == "partition":
                    partitions.append(child.name)

        return TableMetadata(
            name=table_clean_name,
            lineage_tag=lineage_tag,
            description="\n".join([line.strip().lstrip("/ ") for line in table_node.leading_comments]) if table_node and table_node.leading_comments else None,
            columns=columns,
            measures=measures,
            partitions=partitions,
        )

    def get_table_summary(self, table_name: str) -> TableSummary:
        """Return a lightweight summary of a table."""
        table_meta = self.get_table(table_name)
        return TableSummary(
            name=table_meta.name,
            column_count=len(table_meta.columns),
            measure_count=len(table_meta.measures),
            columns=[c.name for c in table_meta.columns],
            measures=[m.name for m in table_meta.measures],
        )

    def get_model_summary(self) -> SemanticModelSummary:
        """Return overall summary of the semantic model."""
        project_name = os.path.basename(self.semantic_model_path).replace(".SemanticModel", "")
        table_names = self.list_table_names()
        summaries = [self.get_table_summary(t) for t in table_names]

        # Count relationships if file exists
        rel_path = os.path.join(self.semantic_model_path, "definition", "relationships.tmdl")
        rel_count = 0
        if os.path.isfile(rel_path):
            doc = self._load_tmdl_doc(rel_path)
            rel_count = len([n for n in doc.nodes if n.node_type == "relationship"])

        return SemanticModelSummary(
            project_name=project_name,
            tables_count=len(table_names),
            tables=summaries,
            relationships_count=rel_count,
        )

    def get_measure(self, table_name: str, measure_name: str) -> Optional[MeasureMetadata]:
        """Get detailed metadata for a specific measure."""
        table_meta = self.get_table(table_name)
        target = unquote_identifier(measure_name)
        for m in table_meta.measures:
            if m.name == target:
                return m
        return None

    def update_measure_dax(self, table_name: str, measure_name: str, new_dax: str) -> bool:
        """PBIP-203: Update measure DAX expression cleanly on disk."""
        filepath = self._get_table_filepath(table_name)
        # Parse directly from file to ensure latest state
        doc = TMDLDocument.from_file(filepath)
        success = doc.update_measure_dax(measure_name, new_dax)
        if success:
            doc.write_to_file(filepath)
            # Invalidate and update cache
            self.cache.invalidate(filepath)
            return True
        return False

    def rename_measure(self, table_name: str, old_measure_name: str, new_measure_name: str) -> bool:
        """PBIP-203: Rename measure in TMDL table definition on disk."""
        filepath = self._get_table_filepath(table_name)
        doc = TMDLDocument.from_file(filepath)
        success = doc.rename_measure(old_measure_name, new_measure_name)
        if success:
            doc.write_to_file(filepath)
            self.cache.invalidate(filepath)
            return True
        return False
