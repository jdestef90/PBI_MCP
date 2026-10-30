"""PBIP-102: TMDL Round-Trip Fidelity Spike Tests.

Verifies that loading a .tmdl table file, parsing it into the AST,
and serializing it back to disk preserves indentation, whitespace,
and developer comments with zero unintended diff.
"""

import os
import shutil
import tempfile
import difflib
from mcp_server.engine_a.tmdl_document import TMDLDocument


def test_tmdl_roundtrip_unchanged_fidelity():
    """Verify that serializing an unmodified document produces an exact byte-for-byte match."""
    sample_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "sample_project.SemanticModel",
        "definition",
        "tables",
        "Sales.tmdl",
    )
    sample_path = os.path.abspath(sample_path)

    with open(sample_path, "r", encoding="utf-8") as f:
        original_content = f.read()

    doc = TMDLDocument.from_file(sample_path)
    serialized = doc.to_string()

    diff = list(
        difflib.unified_diff(
            original_content.splitlines(keepends=True),
            serialized.splitlines(keepends=True),
            fromfile="original",
            tofile="serialized",
        )
    )
    assert not diff, f"Round-trip produced unexpected diff:\n{''.join(diff)}"


def test_tmdl_measure_dax_update_diff_cleanliness():
    """Verify that updating DAX only changes the targeted measure header line."""
    sample_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "sample_project.SemanticModel",
        "definition",
        "tables",
        "Sales.tmdl",
    )
    sample_path = os.path.abspath(sample_path)

    with open(sample_path, "r", encoding="utf-8") as f:
        original_content = f.read()

    doc = TMDLDocument.from_file(sample_path)
    new_dax = "CALCULATE(SUM('Sales'[Quantity]) * 100)"
    success = doc.update_measure_dax("Total Revenue", new_dax)
    assert success is True

    serialized = doc.to_string()
    diff = list(
        difflib.unified_diff(
            original_content.splitlines(keepends=True),
            serialized.splitlines(keepends=True),
            fromfile="original",
            tofile="modified",
        )
    )

    # Verify diff only contains 1 removed and 1 added line
    removed_lines = [line for line in diff if line.startswith("-") and not line.startswith("---")]
    added_lines = [line for line in diff if line.startswith("+") and not line.startswith("+++")]

    assert len(removed_lines) == 1
    assert len(added_lines) == 1
    assert "measure 'Total Revenue' =" in removed_lines[0]
    assert f"measure 'Total Revenue' = {new_dax}" in added_lines[0]


def test_tmdl_measure_rename_diff_cleanliness():
    """Verify that renaming a measure preserves comments, whitespace, and properties."""
    sample_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "sample_project.SemanticModel",
        "definition",
        "tables",
        "Sales.tmdl",
    )
    sample_path = os.path.abspath(sample_path)

    with open(sample_path, "r", encoding="utf-8") as f:
        original_content = f.read()

    doc = TMDLDocument.from_file(sample_path)
    success = doc.rename_measure("Total Revenue", "Gross Revenue")
    assert success is True

    serialized = doc.to_string()
    diff = list(
        difflib.unified_diff(
            original_content.splitlines(keepends=True),
            serialized.splitlines(keepends=True),
            fromfile="original",
            tofile="renamed",
        )
    )

    removed_lines = [line for line in diff if line.startswith("-") and not line.startswith("---")]
    added_lines = [line for line in diff if line.startswith("+") and not line.startswith("+++")]

    assert len(removed_lines) == 1
    assert len(added_lines) == 1
    assert "measure 'Total Revenue' =" in removed_lines[0]
    assert "measure 'Gross Revenue' =" in added_lines[0]

    # Verify comments are preserved
    assert "/// Total revenue calculated from sales transactions" in serialized
