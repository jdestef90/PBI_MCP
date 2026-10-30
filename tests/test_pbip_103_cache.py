"""PBIP-103: In-Memory State Caching Layer Tests."""

import os
import time
import tempfile
from mcp_server.cache.state_cache import StateCache


def test_cache_hit_and_miss():
    cache = StateCache()

    with tempfile.NamedTemporaryFile("w+", delete=False) as f:
        f.write("initial content")
        f.flush()
        filepath = f.name

    try:
        load_count = 0

        def loader(path: str):
            nonlocal load_count
            load_count += 1
            with open(path, "r") as fp:
                return fp.read()

        # First load: Miss
        val1 = cache.get_or_load(filepath, loader)
        assert val1 == "initial content"
        assert load_count == 1
        assert cache.stats()["misses"] == 1
        assert cache.stats()["hits"] == 0

        # Second load without modifying file: Hit
        val2 = cache.get_or_load(filepath, loader)
        assert val2 == "initial content"
        assert load_count == 1
        assert cache.stats()["hits"] == 1

        # Modify file and update mtime
        time.sleep(0.05)  # Ensure mtime difference
        with open(filepath, "w") as fp:
            fp.write("updated content")

        # Third load: Miss because file mtime changed
        val3 = cache.get_or_load(filepath, loader)
        assert val3 == "updated content"
        assert load_count == 2
        assert cache.stats()["misses"] == 2
        assert cache.stats()["hits"] == 1
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)
