from __future__ import annotations

import argparse
import os
from pathlib import Path

from . import __version__
from .jinja_templates import (
    PRESET_NAMES,
    inject_into_base_template,
    render_preset_template,
    write_preset_templates,
)
from .logging_utils import configure_logging
from .meta_tools import format_compact_catalog
from .server import create_server, get_tool_specs
from .service import PbipService
from .workspace_registry import WorkspaceRegistry


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Local MCP server for Power BI PBIP projects"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    group = parser.add_mutually_exclusive_group(required=False)

    group.add_argument(
        "--workspace",
        help="Path to a single .pbip file or project directory",
    )

    group.add_argument(
        "--workspace-root",
        action="append",
        dest="workspace_roots",
        help="Directory containing PBIP projects. May be supplied multiple times.",
    )

    parser.add_argument(
        "--meta-tools",
        action="store_true",
        default=False,
        help=(
            "Expose only 2 meta-tools (pbi_call and pbi_help) instead of all 20 tools. "
            "Pair with a llama-server Jinja template containing the PBI tool catalog."
        ),
    )

    parser.add_argument(
        "--export-jinja",
        nargs="?",
        const="all",
        choices=("all", *PRESET_NAMES),
        help="Export llama-server Jinja template(s) with baked-in PBI_MCP tool catalog and exit.",
    )
    parser.add_argument(
        "--base-template",
        help="Path to an existing GGUF Jinja template to inject the PBI_MCP catalog into.",
    )
    parser.add_argument(
        "--output",
        help="Output file path when exporting a single preset or --base-template.",
    )
    parser.add_argument(
        "--output-dir",
        default="templates",
        help="Output directory when exporting multiple presets (default: templates).",
    )

    args = parser.parse_args(argv)
    if (
        args.export_jinja is None
        and not args.base_template
        and not args.workspace
        and not args.workspace_roots
    ):
        env_ws = os.getenv("PBI_WORKSPACE_PATH")
        if env_ws:
            args.workspace = env_ws
        else:
            parser.error(
                "one of the arguments --workspace --workspace-root "
                "(or PBI_WORKSPACE_PATH) is required"
            )
    return args


def create_backups_enabled() -> bool:
    return os.getenv("PBI_MCP_CREATE_BACKUPS", "true").lower() in {
        "1",
        "true",
        "yes",
    }


def meta_tools_enabled(flag: bool) -> bool:
    return flag or os.getenv("PBI_MCP_META_TOOLS", "false").lower() in {
        "1",
        "true",
        "yes",
    }


def _run_export_jinja(args: argparse.Namespace) -> None:
    catalog_text = format_compact_catalog(get_tool_specs())

    if args.base_template:
        base_path = Path(args.base_template).expanduser().resolve()
        rendered = inject_into_base_template(base_path.read_text(encoding="utf-8"), catalog_text)
        if args.output:
            out_path = Path(args.output).expanduser().resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(rendered, encoding="utf-8")
            print(f"Wrote {out_path}")
        else:
            print(rendered, end="")
        return

    preset = args.export_jinja or "all"
    if preset == "all":
        written = write_preset_templates(Path(args.output_dir), catalog_text)
        for path in written:
            print(f"Wrote {path}")
        return

    rendered = render_preset_template(preset, catalog_text)
    if args.output:
        out_path = Path(args.output).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
        print(f"Wrote {out_path}")
    else:
        print(rendered, end="")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if args.export_jinja is not None or args.base_template:
        _run_export_jinja(args)
        return

    configure_logging()

    create_backups = create_backups_enabled()
    use_meta_tools = meta_tools_enabled(args.meta_tools)

    if args.workspace:
        service = PbipService(
            Path(args.workspace).expanduser().resolve(),
            create_backups=create_backups,
        )
        mcp = create_server(service, meta_tools=use_meta_tools)
    else:
        registry = WorkspaceRegistry(
            roots=[Path(root) for root in args.workspace_roots],
            create_backups=create_backups,
        )
        mcp = create_server(registry, meta_tools=use_meta_tools)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
