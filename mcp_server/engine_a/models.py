"""Pydantic models for Power BI Semantic Model (Engine A)."""

from __future__ import annotations
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class ColumnMetadata(BaseModel):
    """Metadata describing a table column."""
    name: str
    data_type: Optional[str] = None
    format_string: Optional[str] = None
    summarize_by: Optional[str] = None
    source_column: Optional[str] = None
    is_calculated: bool = False
    expression: Optional[str] = None
    description: Optional[str] = None
    lineage_tag: Optional[str] = None


class MeasureMetadata(BaseModel):
    """Metadata describing a measure."""
    name: str
    expression: str
    format_string: Optional[str] = None
    description: Optional[str] = None
    lineage_tag: Optional[str] = None
    display_folder: Optional[str] = None


class TableSummary(BaseModel):
    """Concise summary of a table designed for LLM context windows."""
    name: str
    column_count: int
    measure_count: int
    columns: List[str] = Field(default_factory=list)
    measures: List[str] = Field(default_factory=list)


class TableMetadata(BaseModel):
    """Full detail of a table including columns, measures, and partitions."""
    name: str
    lineage_tag: Optional[str] = None
    description: Optional[str] = None
    columns: List[ColumnMetadata] = Field(default_factory=list)
    measures: List[MeasureMetadata] = Field(default_factory=list)
    partitions: List[str] = Field(default_factory=list)


class SemanticModelSummary(BaseModel):
    """High-level summary of the entire semantic model."""
    project_name: str
    tables_count: int
    tables: List[TableSummary] = Field(default_factory=list)
    relationships_count: int = 0
