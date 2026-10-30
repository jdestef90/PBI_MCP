"""PBIP-103: In-Memory State Caching Layer.

Uses file modification timestamps (os.path.getmtime) to avoid redundant
parsing of TMDL and JSON layout files across agent turns.
"""

from __future__ import annotations
import os
import time
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar

T = TypeVar("T")


class CacheEntry:
    """Stores cached parsed object and timestamp."""

    def __init__(self, value: Any, mtime: float, loaded_at: float):
        self.value = value
        self.mtime = mtime
        self.loaded_at = loaded_at


class StateCache:
    """Thread-safe file modification timestamp-based cache."""

    def __init__(self):
        self._cache: Dict[str, CacheEntry] = {}
        self.hits: int = 0
        self.misses: int = 0

    def get_or_load(self, filepath: str, loader: Callable[[str], T]) -> T:
        """Get cached parsed object if file hasn't changed, else load and cache."""
        norm_path = os.path.abspath(filepath)

        if not os.path.exists(norm_path):
            # File doesn't exist on disk; if cached, invalidate
            if norm_path in self._cache:
                del self._cache[norm_path]
            raise FileNotFoundError(f"File not found: {norm_path}")

        current_mtime = os.path.getmtime(norm_path)
        entry = self._cache.get(norm_path)

        if entry is not None and entry.mtime == current_mtime:
            self.hits += 1
            return entry.value

        # Cache miss or file was modified
        self.misses += 1
        value = loader(norm_path)
        self._cache[norm_path] = CacheEntry(
            value=value,
            mtime=current_mtime,
            loaded_at=time.time(),
        )
        return value

    def set(self, filepath: str, value: Any) -> None:
        """Explicitly set cached value and current file mtime."""
        norm_path = os.path.abspath(filepath)
        current_mtime = os.path.getmtime(norm_path) if os.path.exists(norm_path) else time.time()
        self._cache[norm_path] = CacheEntry(
            value=value,
            mtime=current_mtime,
            loaded_at=time.time(),
        )

    def invalidate(self, filepath: Optional[str] = None) -> None:
        """Invalidate a specific file or clear the entire cache."""
        if filepath is None:
            self._cache.clear()
        else:
            norm_path = os.path.abspath(filepath)
            self._cache.pop(norm_path, None)

    def stats(self) -> Dict[str, Any]:
        """Return cache hit/miss statistics and cached entries count."""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "cached_entries": len(self._cache),
            "hit_ratio": (self.hits / (self.hits + self.misses)) if (self.hits + self.misses) > 0 else 0.0,
        }
