"""Integrity module: reference-aware rename engine and structure validation."""

from .validator import StructureValidator, ValidationResult
from .rename_engine import RenameEngine, RenameResult

__all__ = [
    "StructureValidator",
    "ValidationResult",
    "RenameEngine",
    "RenameResult",
]
