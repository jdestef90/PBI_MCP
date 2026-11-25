from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from pydantic.fields import FieldInfo

from .errors import InvalidArgumentError

_TOOL_CATEGORIES: list[tuple[str, list[str]]] = [
    ("Workspace", ["list_workspaces", "get_workspace", "refresh_workspaces"]),
    (
        "Semantic Model",
        ["list_tables", "get_table", "search_measures", "get_table_sql", "analyze_impact"],
    ),
    ("Report Layout", ["list_report_pages", "list_visuals", "get_visual"]),
    (
        "Safe Mutations (dry_run=True default)",
        ["update_measure_dax", "update_table_sql", "update_visual_fields", "rename_measure"],
    ),
    (
        "Analyst & Validation",
        [
            "find_unused_objects",
            "format_dax",
            "generate_measure_docs",
            "check_model_bpa",
            "validate_workspace",
        ],
    ),
]


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """Compact parameter metadata extracted from a tool function."""

    name: str
    type_label: str
    required: bool
    default: Any
    description: str


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Metadata for a single PBI MCP tool."""

    name: str
    description: str
    params: list[ParamSpec]

    def compact_signature(self) -> str:
        """Render a concise one-line tool signature."""
        parts: list[str] = []
        for p in self.params:
            suffix = "" if p.required else "?"
            parts.append(f"{p.name}{suffix}: {p.type_label}")
        return f"{self.name}({', '.join(parts)})"


def _format_type(annotation: Any) -> str:
    """Convert a Python type annotation into a short, readable type label."""
    if annotation is inspect.Parameter.empty or annotation is Any:
        return "any"
    origin = get_origin(annotation)
    if origin is not None:
        if origin is Literal:
            return "|".join(repr(a) for a in get_args(annotation))
        args = [a for a in get_args(annotation) if a is not type(None)]
        if origin in (Union, UnionType):
            if len(args) == 1:
                return _format_type(args[0])
            return "|".join(_format_type(a) for a in args)
        if origin is list:
            return "list"
        if origin is dict:
            return "dict"
        return getattr(origin, "__name__", str(origin))
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation).replace("typing.", "")


def _extract_field_description(annotated_hint: Any) -> str:
    """Extract Pydantic Field(description=...) from an Annotated type hint."""
    for meta in get_args(annotated_hint)[1:]:
        if isinstance(meta, FieldInfo) and meta.description:
            return meta.description
    return ""


def _unwrap_annotated(hint: Any) -> Any:
    """Return the underlying type from Annotated[T, ...] if present."""
    if get_origin(hint) is not None and getattr(hint, "__metadata__", None) is not None:
        args = get_args(hint)
        if args:
            return args[0]
    return hint


def extract_tool_specs(tools_map: dict[str, Callable[..., Any]]) -> list[ToolSpec]:
    """Introspect registered tool callables into ordered ToolSpec objects."""
    specs: list[ToolSpec] = []
    for name, fn in tools_map.items():
        sig = inspect.signature(fn)
        try:
            hints = get_type_hints(fn, include_extras=True)
        except Exception:
            hints = {}

        params: list[ParamSpec] = []
        for param_name, param in sig.parameters.items():
            raw_hint = hints.get(param_name, param.annotation)
            desc = _extract_field_description(raw_hint)
            base_type = _unwrap_annotated(raw_hint)
            required = param.default is inspect.Parameter.empty
            default = None if required else param.default
            params.append(
                ParamSpec(
                    name=param_name,
                    type_label=_format_type(base_type),
                    required=required,
                    default=default,
                    description=desc,
                )
            )

        doc = inspect.getdoc(fn) or ""
        first_line = doc.splitlines()[0].strip() if doc else ""
        specs.append(ToolSpec(name=name, description=first_line, params=params))
    return specs


def format_compact_catalog(specs: list[ToolSpec]) -> str:
    """Format all 20 PBI tools as an ultra-compact (~400 token) system prompt block."""
    by_name = {s.name: s for s in specs}
    seen: set[str] = set()

    lines: list[str] = [
        "## Power BI (PBI_MCP) Tools",
        (
            "Invoke any tool below via `pbi_call(tool_name, arguments)`. "
            "Call `pbi_help(tool_name)` or `pbi_help(query)` for full parameter docs/defaults."
        ),
        (
            "Note: Mutation tools default to `dry_run=true` (preview ChangePlan); "
            "pass `dry_run=false` to apply."
        ),
    ]

    for category, names in _TOOL_CATEGORIES:
        cat_specs = [by_name[n] for n in names if n in by_name]
        if not cat_specs:
            continue
        lines.append(f"\n### {category}")
        for spec in cat_specs:
            seen.add(spec.name)
            lines.append(f"- {spec.compact_signature()}: {spec.description}")

    remaining = [s for s in specs if s.name not in seen]
    if remaining:
        lines.append("\n### Other")
        for spec in remaining:
            lines.append(f"- {spec.compact_signature()}: {spec.description}")

    return "\n".join(lines)


def build_pbi_help(
    specs: list[ToolSpec],
    tool_name: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """Return full parameter documentation for a tool or search matching tools."""
    by_name = {s.name: s for s in specs}

    if tool_name is not None:
        spec = by_name.get(tool_name)
        if spec is None:
            suggestions = [
                s.name for s in specs if tool_name.casefold() in s.name.casefold()
            ]
            raise InvalidArgumentError(
                f"Unknown tool {tool_name!r}. "
                + (
                    f"Did you mean: {', '.join(suggestions)}?"
                    if suggestions
                    else "Call pbi_help() with no arguments to list all available tools."
                )
            )
        return {
            "tool_name": spec.name,
            "signature": spec.compact_signature(),
            "description": spec.description,
            "parameters": [
                {
                    "name": p.name,
                    "type": p.type_label,
                    "required": p.required,
                    **({"default": p.default} if not p.required else {}),
                    **({"description": p.description} if p.description else {}),
                }
                for p in spec.params
            ],
        }

    q = query.strip().casefold() if query else ""
    matches = [
        s
        for s in specs
        if not q
        or q in s.name.casefold()
        or q in s.description.casefold()
        or any(q in p.name.casefold() or q in p.description.casefold() for p in s.params)
    ]
    return {
        "count": len(matches),
        "tools": [
            {
                "tool_name": s.name,
                "signature": s.compact_signature(),
                "description": s.description,
            }
            for s in matches
        ],
    }


def dispatch_pbi_call(
    tools_map: dict[str, Callable[..., Any]],
    specs: list[ToolSpec],
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> Any:
    """Validate and invoke a PBI tool by name with the provided arguments dict."""
    fn = tools_map.get(tool_name)
    if fn is None:
        suggestions = [
            name for name in tools_map if tool_name.casefold() in name.casefold()
        ]
        raise InvalidArgumentError(
            f"Unknown PBI tool {tool_name!r}. "
            + (
                f"Did you mean: {', '.join(suggestions)}? "
                if suggestions
                else ""
            )
            + "Call pbi_help() to inspect available tools."
        )

    kwargs = dict(arguments or {})
    sig = inspect.signature(fn)
    try:
        bound = sig.bind(**kwargs)
        bound.apply_defaults()
    except TypeError as exc:
        by_name = {s.name: s for s in specs}
        spec = by_name.get(tool_name)
        hint = f" Expected signature: {spec.compact_signature()}." if spec else ""
        raise InvalidArgumentError(
            f"Invalid arguments for {tool_name!r}: {exc}.{hint} "
            f"Call pbi_help(tool_name={tool_name!r}) for details."
        ) from exc

    return fn(**bound.arguments)
