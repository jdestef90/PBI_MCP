"""PBIP-403: Power BI Structure Validation Safety Net Tests."""

import os
import shutil
from mcp_server.integrity.validator import StructureValidator


def test_validator_on_healthy_project():
    validator = StructureValidator("sample_project.pbip")
    res = validator.validate()
    assert res.is_valid is True
    assert len(res.errors) == 0
    assert len(res.checked_files) > 0


def test_validator_catches_corrupted_json(tmp_path):
    shutil.copy("sample_project.pbip", tmp_path / "sample_project.pbip")
    shutil.copytree("sample_project.SemanticModel", tmp_path / "sample_project.SemanticModel")
    shutil.copytree("sample_project.Report", tmp_path / "sample_project.Report")

    # Corrupt report.json
    corrupted_report = tmp_path / "sample_project.Report" / "report.json"
    with open(corrupted_report, "w") as f:
        f.write("{ invalid json")

    validator = StructureValidator(str(tmp_path / "sample_project.pbip"))
    res = validator.validate()
    assert res.is_valid is False
    assert any("report.json" in err for err in res.errors)
