"""PBIP-301: Shadow Model Pydantic structures for Power BI Report Layout."""

from __future__ import annotations
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field


class VisualPosition(BaseModel):
    """Placement coordinates and dimensions."""
    x: float
    y: float
    z: float = 0
    width: float
    height: float


class VisualShadow(BaseModel):
    """Simplified flat representation of a visual container."""
    id: str  # visual container config.name or index
    visual_type: str  # e.g., 'card', 'barChart', 'slicer', 'tableEx'
    title: Optional[str] = None
    position: VisualPosition
    bound_fields: List[str] = Field(default_factory=list)  # e.g. ['Sales.Total Revenue']
    projections: Dict[str, List[str]] = Field(default_factory=dict)
    is_slicer: bool = False
    raw_path: str = ""  # path like "pages['Summary'].visuals['vis_1']"


class PageShadow(BaseModel):
    """Simplified flat representation of a report page / section."""
    id: str  # internal section name (e.g., ReportSection1)
    display_name: str  # human-readable name (e.g., Executive Summary)
    ordinal: int
    width: float
    height: float
    visual_count: int
    visuals: List[VisualShadow] = Field(default_factory=list)


class ReportShadow(BaseModel):
    """Top-level shadow model of report layout."""
    pages: List[PageShadow] = Field(default_factory=list)
    total_visuals: int = 0
