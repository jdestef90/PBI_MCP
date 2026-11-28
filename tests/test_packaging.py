from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from pbi_mcp_server import __version__
from pbi_mcp_server.main import parse_args

REPO_ROOT = Path(__file__).parents[1]


def test_pyproject_package_and_npm_versions_stay_in_sync() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    npm_pkg = json.loads((REPO_ROOT / "npm" / "package.json").read_text(encoding="utf-8"))

    assert project["name"] == "pbi-mcp-server"
    assert project["version"] == __version__
    assert npm_pkg["name"] == "pbi-mcp-server"
    assert npm_pkg["version"] == __version__
    assert project["license"] == "GPL-3.0-or-later"
    assert npm_pkg["license"] == "GPL-3.0-or-later"
    assert "Repository" in project["urls"]
    assert project["scripts"]["pbi-mcp-server"] == "pbi_mcp_server.main:main"


def test_npm_wrapper_assets_and_license_parity() -> None:
    npm_dir = REPO_ROOT / "npm"
    npm_pkg = json.loads((npm_dir / "package.json").read_text(encoding="utf-8"))
    bin_rel = npm_pkg["bin"]["pbi-mcp-server"]
    bin_file = npm_dir / bin_rel

    assert bin_file.is_file()
    assert bin_file.read_text(encoding="utf-8").startswith("#!/usr/bin/env node\n")
    assert (npm_dir / "LICENSE").read_text(encoding="utf-8") == (
        REPO_ROOT / "LICENSE"
    ).read_text(encoding="utf-8")


def test_cli_version_flag_outputs_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert __version__ in captured.out
