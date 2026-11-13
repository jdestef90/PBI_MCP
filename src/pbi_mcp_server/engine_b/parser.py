from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import jmespath

from ..engine_a.writer import atomic_write
from ..errors import AmbiguousObjectError, InvalidArgumentError, ObjectNotFoundError
from ..models import (
    ChangePlan,
    FieldBinding,
    FileChange,
    JsonValue,
    VisualSummary,
    make_object_id,
)
from ..workspace import compute_workspace_id, relative

_DAX_FIELD_RE = re.compile(r"^'?(?P<table>[^'\[\].]+)'?\[(?P<prop>[^\]]+)\]$")


def json_files(report_dir: Path | None) -> list[Path]:
    if not report_dir or not report_dir.exists():
        return []
    return sorted(report_dir.rglob("*.json"))


def load_json(path: Path) -> JsonValue:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def safe_json_loads(val: Any) -> Any:
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    return val


def query_json(path: Path, expression: str) -> Any:
    return jmespath.search(expression, load_json(path))


def parse_field_ref(raw: str) -> tuple[str, str, str]:
    """Parse 'Table[Field]' or 'Table.Field' into (table, property, query_ref)."""
    cleaned = raw.strip()
    m = _DAX_FIELD_RE.match(cleaned)
    if m:
        table = m.group("table").strip()
        prop = m.group("prop").strip()
        return table, prop, f"{table}.{prop}"
    if "." in cleaned:
        table, prop = cleaned.split(".", 1)
        table = table.strip("'\" ")
        prop = prop.strip("[]'\" ")
        return table, prop, f"{table}.{prop}"
    raise InvalidArgumentError(
        f"Field reference {raw!r} must include table and field name, e.g. 'Sales[Total Revenue]' or 'Sales.Total Revenue'."
    )


def _split_query_ref(qref: str) -> tuple[str | None, str | None]:
    if "." in qref:
        tbl, prop = qref.split(".", 1)
        return tbl.strip("'\" "), prop.strip("'\"[] ")
    m = _DAX_FIELD_RE.match(qref.strip())
    if m:
        return m.group("table").strip(), m.group("prop").strip()
    return None, qref.strip() or None


def _extract_filter_fields(filter_config: Any) -> list[str]:
    """Extract queryRef / Table.Property strings from PBIR or legacy filter containers."""
    fields: set[str] = set()
    parsed = safe_json_loads(filter_config)
    filters_list: list[Any] = []
    if isinstance(parsed, dict):
        filters_list = parsed.get("filters", []) if isinstance(parsed.get("filters"), list) else []
    elif isinstance(parsed, list):
        filters_list = parsed

    for flt in filters_list:
        if not isinstance(flt, dict):
            continue
        field_obj = flt.get("field") or flt.get("expression")
        if isinstance(field_obj, dict):
            for kind_key in ("Column", "Measure", "Aggregation", "HierarchyLevel"):
                inner = field_obj.get(kind_key)
                if isinstance(inner, dict):
                    prop = inner.get("Property")
                    expr = inner.get("Expression", {})
                    src = expr.get("SourceRef", {}) if isinstance(expr, dict) else {}
                    entity = src.get("Entity") or src.get("Source")
                    if entity and prop:
                        fields.add(f"{entity}.{prop}")
                    elif prop:
                        fields.add(str(prop))
        name_val = flt.get("name") or flt.get("queryRef")
        if isinstance(name_val, str) and "." in name_val and not name_val.lower().startswith("filter"):
            fields.add(name_val)
    return sorted(fields)


def _extract_title_from_objects(objects: dict[str, Any]) -> str | None:
    if not isinstance(objects, dict):
        return None
    for key in ("general", "title"):
        items = objects.get(key)
        if isinstance(items, list) and items:
            props = items[0].get("properties", {}) if isinstance(items[0], dict) else {}
            for prop_key in ("title", "text"):
                title_obj = props.get(prop_key, {})
                if isinstance(title_obj, dict):
                    val = title_obj.get("expr", {}).get("Literal", {}).get("Value")
                    if isinstance(val, str) and val.strip("'\""):
                        return val.strip("'\"")
    return None


def _extract_pbir_visual_info(
    data: dict[str, Any],
    ws_id: str,
) -> tuple[str | None, str | None, dict[str, list[str]], list[str], list[FieldBinding], list[str]]:
    """Extract (visual_type, title, role_bindings, bound_fields, field_bindings, filter_fields) from a PBIR visual.json dict."""
    visual = data.get("visual") if isinstance(data.get("visual"), dict) else data
    if not isinstance(visual, dict):
        return None, None, {}, [], [], []

    v_type = visual.get("visualType")
    visual_type = str(v_type) if v_type else None

    title = _extract_title_from_objects(visual.get("objects", {})) or _extract_title_from_objects(
        visual.get("visualContainerObjects", {})
    )

    role_bindings: dict[str, list[str]] = {}
    fields_set: set[str] = set()
    field_bindings: list[FieldBinding] = []
    seen_bindings: set[tuple[str, str]] = set()

    # Check singleVisual.projections or visual.projections
    projections = visual.get("projections")
    if isinstance(projections, dict):
        for role, items in projections.items():
            if isinstance(items, list):
                refs = [
                    str(item["queryRef"])
                    for item in items
                    if isinstance(item, dict) and "queryRef" in item
                ]
                if refs:
                    role_bindings[str(role)] = refs
                    fields_set.update(refs)
                    for qref in refs:
                        key = (str(role), qref)
                        if key not in seen_bindings:
                            seen_bindings.add(key)
                            tbl, prop = _split_query_ref(qref)
                            field_bindings.append(
                                FieldBinding(
                                    role=str(role),
                                    query_ref=qref,
                                    table=tbl,
                                    property_name=prop,
                                    field_kind="unknown",
                                )
                            )

    # Check PBIR enhanced query.queryState.<role>.projections
    query_state = visual.get("query", {}).get("queryState", {}) if isinstance(visual.get("query"), dict) else {}
    if isinstance(query_state, dict):
        for role, role_data in query_state.items():
            if isinstance(role_data, dict) and isinstance(role_data.get("projections"), list):
                refs = []
                for item in role_data["projections"]:
                    if isinstance(item, dict):
                        qref = item.get("queryRef")
                        if qref:
                            qref_str = str(qref)
                            refs.append(qref_str)
                            field_dict = item.get("field", {}) if isinstance(item.get("field"), dict) else {}
                            f_kind: str = "unknown"
                            tbl, prop = _split_query_ref(qref_str)
                            if "Measure" in field_dict and isinstance(field_dict["Measure"], dict):
                                f_kind = "measure"
                                m_inner = field_dict["Measure"]
                                prop = str(m_inner.get("Property") or prop or "")
                                ent = m_inner.get("Expression", {}).get("SourceRef", {}).get("Entity")
                                if ent:
                                    tbl = str(ent)
                            elif "Column" in field_dict and isinstance(field_dict["Column"], dict):
                                f_kind = "column"
                                c_inner = field_dict["Column"]
                                prop = str(c_inner.get("Property") or prop or "")
                                ent = c_inner.get("Expression", {}).get("SourceRef", {}).get("Entity")
                                if ent:
                                    tbl = str(ent)
                            obj_id = (
                                make_object_id(ws_id, f_kind, f"{tbl}[{prop}]")
                                if f_kind in {"measure", "column"} and tbl and prop
                                else None
                            )
                            key = (str(role), qref_str)
                            if key not in seen_bindings:
                                seen_bindings.add(key)
                                field_bindings.append(
                                    FieldBinding(
                                        role=str(role),
                                        query_ref=qref_str,
                                        table=tbl,
                                        property_name=prop,
                                        field_kind=f_kind,  # type: ignore[arg-type]
                                        object_id=obj_id,
                                    )
                                )
                if refs:
                    role_bindings[str(role)] = refs
                    fields_set.update(refs)

    filter_fields = _extract_filter_fields(data.get("filterConfig") or visual.get("filterConfig"))
    return visual_type, title, role_bindings, sorted(fields_set), field_bindings, filter_fields


def _extract_legacy_container_info(
    container: dict[str, Any],
    index: int,
    ws_id: str,
) -> tuple[str, str | None, str | None, dict[str, list[str]], list[str], list[FieldBinding], list[str]]:
    """Extract (visual_id, visual_type, title, role_bindings, bound_fields, field_bindings, filter_fields) from a report.json visualContainer."""
    config = safe_json_loads(container.get("config", "{}"))
    query = safe_json_loads(container.get("query", "{}"))
    if not isinstance(config, dict):
        config = {}
    if not isinstance(query, dict):
        query = {}

    v_id = str(config.get("name") or f"vis_{index}")
    single_visual = config.get("singleVisual", {})
    if not isinstance(single_visual, dict):
        single_visual = {}

    v_type = single_visual.get("visualType")
    visual_type = str(v_type) if v_type else None
    title = _extract_title_from_objects(single_visual.get("objects", {}))

    role_bindings: dict[str, list[str]] = {}
    fields_set: set[str] = set()
    field_bindings: list[FieldBinding] = []
    seen_bindings: set[tuple[str, str]] = set()

    # Build a map of queryRef -> (kind, table, prop) from Query.Select and From
    select_meta: dict[str, tuple[str, str | None, str | None]] = {}
    for cmd in query.get("Commands", []):
        if not isinstance(cmd, dict):
            continue
        q_obj = cmd.get("SemanticQueryDataShapeCommand", {}).get("Query", {})
        if not isinstance(q_obj, dict):
            continue
        alias_map = {
            str(f.get("Name")): str(f.get("Entity"))
            for f in q_obj.get("From", [])
            if isinstance(f, dict) and f.get("Name") and f.get("Entity")
        }
        for s in q_obj.get("Select", []):
            if not isinstance(s, dict) or "Name" not in s:
                continue
            qname = str(s["Name"])
            fields_set.add(qname)
            if "Measure" in s and isinstance(s["Measure"], dict):
                m_inner = s["Measure"]
                src = m_inner.get("Expression", {}).get("SourceRef", {}).get("Source")
                tbl = alias_map.get(str(src)) if src else _split_query_ref(qname)[0]
                prop = str(m_inner.get("Property") or _split_query_ref(qname)[1] or "")
                select_meta[qname.casefold()] = ("measure", tbl, prop)
            elif "Column" in s and isinstance(s["Column"], dict):
                c_inner = s["Column"]
                src = c_inner.get("Expression", {}).get("SourceRef", {}).get("Source")
                tbl = alias_map.get(str(src)) if src else _split_query_ref(qname)[0]
                prop = str(c_inner.get("Property") or _split_query_ref(qname)[1] or "")
                select_meta[qname.casefold()] = ("column", tbl, prop)

    projections = single_visual.get("projections", {})
    if isinstance(projections, dict):
        for role, items in projections.items():
            if isinstance(items, list):
                refs = [
                    str(item["queryRef"])
                    for item in items
                    if isinstance(item, dict) and "queryRef" in item
                ]
                if refs:
                    role_bindings[str(role)] = refs
                    fields_set.update(refs)
                    for qref in refs:
                        key = (str(role), qref)
                        if key not in seen_bindings:
                            seen_bindings.add(key)
                            f_kind, tbl, prop = select_meta.get(
                                qref.casefold(),
                                ("unknown", *_split_query_ref(qref)),
                            )
                            obj_id = (
                                make_object_id(ws_id, f_kind, f"{tbl}[{prop}]")
                                if f_kind in {"measure", "column"} and tbl and prop
                                else None
                            )
                            field_bindings.append(
                                FieldBinding(
                                    role=str(role),
                                    query_ref=qref,
                                    table=tbl,
                                    property_name=prop,
                                    field_kind=f_kind,  # type: ignore[arg-type]
                                    object_id=obj_id,
                                )
                            )

    filter_fields = _extract_filter_fields(container.get("filters"))
    return v_id, visual_type, title, role_bindings, sorted(fields_set), field_bindings, filter_fields


def list_pages(
    report_dir: Path | None,
    root: Path,
    *,
    limit: int | None = None,
    offset: int = 0,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    if not report_dir or not report_dir.exists():
        return []
    ws_id = workspace_id or compute_workspace_id(root)
    pages: list[dict[str, Any]] = []

    # 1. PBIR Enhanced format (page.json files)
    page_files = sorted(report_dir.rglob("page.json"))
    for path in page_files:
        data = load_json(path)
        if isinstance(data, dict):
            visuals_dir = path.parent / "visuals"
            v_count = len(list(visuals_dir.rglob("visual.json"))) if visuals_dir.exists() else 0
            p_name = str(data.get("name", path.parent.name))
            p_disp = str(data.get("displayName", p_name))
            filter_fields = _extract_filter_fields(data.get("filterConfig"))
            pages.append({
                "name": p_name,
                "display_name": p_disp,
                "object_id": make_object_id(ws_id, "page", p_name),
                "qualified_name": p_disp,
                "visual_count": v_count,
                "filter_fields": filter_fields,
                "file": relative(path, root),
            })

    # 2. PBIR-Legacy format (report.json with sections)
    report_json = report_dir / "report.json"
    if not pages and report_json.exists():
        data = load_json(report_json)
        if isinstance(data, dict):
            for idx, sec in enumerate(data.get("sections", [])):
                if isinstance(sec, dict):
                    containers = sec.get("visualContainers", [])
                    p_name = str(sec.get("name", f"Section_{idx}"))
                    p_disp = str(sec.get("displayName", p_name))
                    filter_fields = _extract_filter_fields(sec.get("filters"))
                    pages.append({
                        "name": p_name,
                        "display_name": p_disp,
                        "object_id": make_object_id(ws_id, "page", p_name),
                        "qualified_name": p_disp,
                        "ordinal": sec.get("ordinal", idx),
                        "visual_count": len(containers) if isinstance(containers, list) else 0,
                        "filter_fields": filter_fields,
                        "file": relative(report_json, root),
                    })

    if offset > 0:
        pages = pages[offset:]
    if limit is not None and limit >= 0:
        pages = pages[:limit]
    return pages


def list_visuals(
    report_dir: Path | None,
    root: Path,
    page: str | None = None,
    query: str | None = None,
    *,
    limit: int | None = None,
    offset: int = 0,
    workspace_id: str | None = None,
) -> list[VisualSummary]:
    if not report_dir or not report_dir.exists():
        return []
    ws_id = workspace_id or compute_workspace_id(root)
    result: list[VisualSummary] = []

    # Map page folder/id to display_name
    page_display_map: dict[str, str] = {}
    for p in list_pages(report_dir, root, workspace_id=ws_id):
        page_display_map[str(p["name"]).casefold()] = str(p["display_name"])

    # 1. PBIR Enhanced format (visual.json files)
    visual_files = sorted(report_dir.rglob("visual.json"))
    for path in visual_files:
        data = load_json(path)
        if not isinstance(data, dict):
            continue
        page_id = path.parents[2].name if len(path.parents) > 2 else "unknown"
        page_disp = page_display_map.get(page_id.casefold(), page_id)
        v_id = str(data.get("name", path.parent.name))
        v_type, title, role_bindings, bound_fields, field_bindings, filter_fields = _extract_pbir_visual_info(
            data, ws_id
        )
        qname = f"{page_id}/{v_id}"
        result.append(
            VisualSummary(
                page=page_id,
                page_display_name=page_disp,
                visual_id=v_id,
                visual_type=v_type,
                title=title,
                role_bindings=role_bindings,
                bound_fields=bound_fields,
                field_bindings=field_bindings,
                filter_fields=filter_fields,
                file=relative(path, root),
                object_id=make_object_id(ws_id, "visual", qname),
                qualified_name=qname,
            )
        )

    # 2. PBIR-Legacy format (report.json)
    report_json = report_dir / "report.json"
    if not result and report_json.exists():
        data = load_json(report_json)
        if isinstance(data, dict):
            for idx, sec in enumerate(data.get("sections", [])):
                if not isinstance(sec, dict):
                    continue
                sec_id = str(sec.get("name", f"Section_{idx}"))
                sec_disp = str(sec.get("displayName", sec_id))
                containers = sec.get("visualContainers", [])
                if not isinstance(containers, list):
                    continue
                for c_idx, container in enumerate(containers):
                    if not isinstance(container, dict):
                        continue
                    v_id, v_type, title, role_bindings, bound_fields, field_bindings, filter_fields = (
                        _extract_legacy_container_info(container, c_idx, ws_id)
                    )
                    qname = f"{sec_id}/{v_id}"
                    result.append(
                        VisualSummary(
                            page=sec_id,
                            page_display_name=sec_disp,
                            visual_id=v_id,
                            visual_type=v_type,
                            title=title,
                            role_bindings=role_bindings,
                            bound_fields=bound_fields,
                            field_bindings=field_bindings,
                            filter_fields=filter_fields,
                            file=relative(report_json, root),
                            object_id=make_object_id(ws_id, "visual", qname),
                            qualified_name=qname,
                        )
                    )

    if page:
        target_page = page.strip().casefold()
        result = [
            v
            for v in result
            if v.page.casefold() == target_page
            or (v.page_display_name or "").casefold() == target_page
        ]

    if query:
        q = query.strip().casefold()
        result = [
            v
            for v in result
            if q in v.visual_id.casefold()
            or q in (v.title or "").casefold()
            or q in (v.visual_type or "").casefold()
            or any(q in f.casefold() for f in v.bound_fields)
            or any(q in f.casefold() for f in v.filter_fields)
        ]

    if offset > 0:
        result = result[offset:]
    if limit is not None and limit >= 0:
        result = result[:limit]
    return result


def resolve_visual(
    report_dir: Path | None,
    root: Path,
    visual_id_or_title: str,
    page: str | None = None,
    workspace_id: str | None = None,
) -> VisualSummary | None:
    visuals = list_visuals(report_dir, root, page=page, workspace_id=workspace_id)
    target = visual_id_or_title.strip().casefold()

    # 0. Exact object_id or qualified_name match
    exact_obj = [
        v
        for v in visuals
        if (v.object_id or "").casefold() == target
        or (v.qualified_name or "").casefold() == target
    ]
    if len(exact_obj) == 1:
        return exact_obj[0]

    # 1. Exact visual_id match
    exact_id = [v for v in visuals if v.visual_id.casefold() == target]
    if len(exact_id) == 1:
        return exact_id[0]
    if len(exact_id) > 1:
        raise AmbiguousObjectError(
            f"Multiple visuals match ID {visual_id_or_title!r}; specify page: "
            + ", ".join(f"{v.visual_id} (page {v.page_display_name})" for v in exact_id),
            candidates=[
                {"visual_id": v.visual_id, "page": v.page, "page_display_name": v.page_display_name, "object_id": v.object_id}
                for v in exact_id
            ],
        )

    # 2. Exact title match
    exact_title = [v for v in visuals if (v.title or "").casefold() == target]
    if len(exact_title) == 1:
        return exact_title[0]
    if len(exact_title) > 1:
        raise AmbiguousObjectError(
            f"Multiple visuals match title {visual_id_or_title!r}; specify page or exact visual_id: "
            + ", ".join(f"{v.visual_id} (page {v.page_display_name})" for v in exact_title),
            candidates=[
                {"visual_id": v.visual_id, "title": v.title, "page": v.page, "page_display_name": v.page_display_name, "object_id": v.object_id}
                for v in exact_title
            ],
        )

    # 3. Partial match on visual_id or title
    partial = [
        v
        for v in visuals
        if target in v.visual_id.casefold() or target in (v.title or "").casefold()
    ]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise AmbiguousObjectError(
            f"Ambiguous visual selector {visual_id_or_title!r}; matched: "
            + ", ".join(f"{v.visual_id} ({v.title or v.visual_type})" for v in partial),
            candidates=[
                {"visual_id": v.visual_id, "title": v.title, "page": v.page, "page_display_name": v.page_display_name, "object_id": v.object_id}
                for v in partial
            ],
        )
    return None


def _default_role_for_visual(
    visual_type: str | None,
    existing_roles: dict[str, Any],
    is_measure: bool,
) -> str:
    vtype = (visual_type or "").lower()
    if is_measure:
        if "Y" in existing_roles or vtype in {"barchart", "columnchart", "linechart", "areachart", "clusteredbarchart", "clusteredcolumnchart"}:
            return "Y" if "Y" in existing_roles or "Values" not in existing_roles else "Values"
        return "Values"
    else:
        if "Category" in existing_roles or vtype in {"barchart", "columnchart", "linechart", "piechart", "donutchart"}:
            return "Category"
        return "Values"


def update_visual_fields(
    report_dir: Path | None,
    root: Path,
    visual_id_or_title: str,
    *,
    add_fields: list[dict[str, str] | str] | None = None,
    remove_fields: list[str] | None = None,
    known_measures: set[str] | None = None,
    page: str | None = None,
    dry_run: bool = True,
    create_backups: bool = True,
) -> dict[str, Any]:
    target = resolve_visual(report_dir, root, visual_id_or_title, page=page)
    if not target:
        raise ObjectNotFoundError(f"Visual {visual_id_or_title!r} was not found in report.")

    known_measures_cf = {m.casefold() for m in (known_measures or set())}
    add_specs: list[tuple[str, str, str, str | None]] = []
    for item in add_fields or []:
        if isinstance(item, str):
            table, prop, qref = parse_field_ref(item)
            add_specs.append((table, prop, qref, None))
        elif isinstance(item, dict):
            raw_field = item.get("field") or item.get("queryRef") or ""
            role = item.get("role")
            table, prop, qref = parse_field_ref(raw_field)
            add_specs.append((table, prop, qref, role))

    remove_qrefs: set[str] = set()
    for item in remove_fields or []:
        if "." in item or "[" in item:
            _, _, qref = parse_field_ref(item)
            remove_qrefs.add(qref.casefold())
        else:
            remove_qrefs.add(item.strip().casefold())

    target_path = (root / target.file).resolve()
    plan = ChangePlan(
        operation=f"update visual fields on {target.visual_id!r}",
        dry_run=dry_run,
        target_object=target.object_id or target.visual_id,
    )

    if target_path.name.lower() == "report.json":
        return _update_legacy_report_visual(
            target_path,
            root,
            target.visual_id,
            add_specs,
            remove_qrefs,
            known_measures_cf,
            plan,
            dry_run=dry_run,
            create_backups=create_backups,
        )
    else:
        return _update_pbir_visual_file(
            target_path,
            root,
            target.visual_id,
            add_specs,
            remove_qrefs,
            known_measures_cf,
            plan,
            dry_run=dry_run,
            create_backups=create_backups,
        )


def _update_legacy_report_visual(
    report_path: Path,
    root: Path,
    visual_id: str,
    add_specs: list[tuple[str, str, str, str | None]],
    remove_qrefs: set[str],
    known_measures_cf: set[str],
    plan: ChangePlan,
    *,
    dry_run: bool,
    create_backups: bool,
) -> dict[str, Any]:
    raw_text = report_path.read_text(encoding="utf-8-sig")
    data = json.loads(raw_text)
    modifications = 0

    for sec in data.get("sections", []):
        for container in sec.get("visualContainers", []):
            config = safe_json_loads(container.get("config", "{}"))
            if not isinstance(config, dict) or config.get("name") != visual_id:
                continue

            query = safe_json_loads(container.get("query", "{}"))
            if not isinstance(query, dict):
                query = {}
            transforms = safe_json_loads(container.get("dataTransforms", "{}"))
            if not isinstance(transforms, dict):
                transforms = {}

            single_visual = config.setdefault("singleVisual", {})
            projections: dict[str, list[dict[str, Any]]] = single_visual.setdefault("projections", {})
            vtype = single_visual.get("visualType")

            commands = query.setdefault("Commands", [{"SemanticQueryDataShapeCommand": {"Query": {"Version": 2, "From": [], "Select": []}}}])
            sqdc = commands[0].setdefault("SemanticQueryDataShapeCommand", {})
            q_obj = sqdc.setdefault("Query", {"Version": 2, "From": [], "Select": []})
            from_list: list[dict[str, Any]] = q_obj.setdefault("From", [])
            select_list: list[dict[str, Any]] = q_obj.setdefault("Select", [])
            meta_select: list[dict[str, Any]] = transforms.setdefault("queryMetadata", {}).setdefault("Select", [])

            # 1. Remove fields
            if remove_qrefs:
                for role in list(projections.keys()):
                    before = len(projections[role])
                    projections[role] = [
                        p for p in projections[role]
                        if str(p.get("queryRef", "")).casefold() not in remove_qrefs
                        and str(p.get("queryRef", "")).split(".")[-1].casefold() not in remove_qrefs
                    ]
                    modifications += before - len(projections[role])

                select_list[:] = [
                    s for s in select_list
                    if str(s.get("Name", "")).casefold() not in remove_qrefs
                    and str(s.get("NativeReferenceName", "")).casefold() not in remove_qrefs
                ]
                meta_select[:] = [
                    m for m in meta_select
                    if str(m.get("Name", "")).casefold() not in remove_qrefs
                    and str(m.get("Restatement", "")).casefold() not in remove_qrefs
                ]

            # 2. Add fields
            for table, prop, qref, explicit_role in add_specs:
                is_measure = prop.casefold() in known_measures_cf or f"{table}.{prop}".casefold() in known_measures_cf
                role = explicit_role or _default_role_for_visual(vtype, projections, is_measure)
                role_list = projections.setdefault(role, [])
                if not any(str(p.get("queryRef", "")).casefold() == qref.casefold() for p in role_list):
                    role_list.append({"queryRef": qref})
                    modifications += 1

                # Ensure From alias exists
                alias = next((f["Name"] for f in from_list if f.get("Entity") == table), None)
                if not alias:
                    base_alias = table[0].lower() if table else "t"
                    alias = base_alias
                    counter = 1
                    used = {f.get("Name") for f in from_list}
                    while alias in used:
                        alias = f"{base_alias}{counter}"
                        counter += 1
                    from_list.append({"Name": alias, "Entity": table, "Type": 0})

                # Ensure Select entry exists
                if not any(str(s.get("Name", "")).casefold() == qref.casefold() for s in select_list):
                    kind_key = "Measure" if is_measure else "Column"
                    select_list.append({
                        kind_key: {
                            "Expression": {"SourceRef": {"Source": alias}},
                            "Property": prop,
                        },
                        "Name": qref,
                        "NativeReferenceName": prop,
                    })

                if not any(str(m.get("Name", "")).casefold() == qref.casefold() for m in meta_select):
                    meta_select.append({
                        "Restatement": prop,
                        "Name": qref,
                        "Type": 1 if is_measure else 2048,
                    })

            container["config"] = json.dumps(config, separators=(",", ":"))
            container["query"] = json.dumps(query, separators=(",", ":"))
            container["dataTransforms"] = json.dumps(transforms, separators=(",", ":"))

    if modifications:
        rel_file = relative(report_path, root)
        plan.changes.append(
            FileChange(
                file=rel_file,
                replacements=modifications,
                reason=f"visual field bindings updated on {visual_id}",
            )
        )
        plan.changed_files = [rel_file]
        plan.replacement_count = modifications
        plan.status = "dry_run" if dry_run else "applied"
        if not dry_run:
            updated_text = json.dumps(data, indent=2) + "\n"
            atomic_write(report_path, updated_text, create_backups)
    else:
        plan.status = "no_op"
        plan.warnings.append("No visual field binding changes were needed.")

    return plan.model_dump()


def _update_pbir_visual_file(
    visual_path: Path,
    root: Path,
    visual_id: str,
    add_specs: list[tuple[str, str, str, str | None]],
    remove_qrefs: set[str],
    known_measures_cf: set[str],
    plan: ChangePlan,
    *,
    dry_run: bool,
    create_backups: bool,
) -> dict[str, Any]:
    raw_text = visual_path.read_text(encoding="utf-8-sig")
    data = json.loads(raw_text)
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected JSON structure in {visual_path}")

    visual = data.setdefault("visual", data)
    vtype = visual.get("visualType")
    modifications = 0

    # Check if visual uses query.queryState or direct projections
    if "query" in visual and isinstance(visual["query"], dict) and "queryState" in visual["query"]:
        qstate: dict[str, Any] = visual["query"]["queryState"]
        if remove_qrefs:
            for role, rdata in list(qstate.items()):
                if isinstance(rdata, dict) and isinstance(rdata.get("projections"), list):
                    before = len(rdata["projections"])
                    rdata["projections"] = [
                        p for p in rdata["projections"]
                        if str(p.get("queryRef", "")).casefold() not in remove_qrefs
                        and str(p.get("nativeQueryRef", "")).casefold() not in remove_qrefs
                    ]
                    modifications += before - len(rdata["projections"])

        for table, prop, qref, explicit_role in add_specs:
            is_measure = prop.casefold() in known_measures_cf or qref.casefold() in known_measures_cf
            role = explicit_role or _default_role_for_visual(vtype, qstate, is_measure)
            role_obj = qstate.setdefault(role, {"projections": []})
            proj_list = role_obj.setdefault("projections", [])
            if not any(str(p.get("queryRef", "")).casefold() == qref.casefold() for p in proj_list):
                kind_key = "Measure" if is_measure else "Column"
                proj_list.append({
                    "field": {
                        kind_key: {
                            "Expression": {"SourceRef": {"Entity": table}},
                            "Property": prop,
                        }
                    },
                    "queryRef": qref,
                    "nativeQueryRef": prop,
                })
                modifications += 1
    else:
        projections: dict[str, list[dict[str, Any]]] = visual.setdefault("projections", {})
        if remove_qrefs:
            for role in list(projections.keys()):
                before = len(projections[role])
                projections[role] = [
                    p for p in projections[role]
                    if str(p.get("queryRef", "")).casefold() not in remove_qrefs
                    and str(p.get("queryRef", "")).split(".")[-1].casefold() not in remove_qrefs
                ]
                modifications += before - len(projections[role])

        for table, prop, qref, explicit_role in add_specs:
            is_measure = prop.casefold() in known_measures_cf or qref.casefold() in known_measures_cf
            role = explicit_role or _default_role_for_visual(vtype, projections, is_measure)
            role_list = projections.setdefault(role, [])
            if not any(str(p.get("queryRef", "")).casefold() == qref.casefold() for p in role_list):
                role_list.append({"queryRef": qref})
                modifications += 1

    if modifications:
        rel_file = relative(visual_path, root)
        plan.changes.append(
            FileChange(
                file=rel_file,
                replacements=modifications,
                reason=f"visual field bindings updated on {visual_id}",
            )
        )
        plan.changed_files = [rel_file]
        plan.replacement_count = modifications
        plan.status = "dry_run" if dry_run else "applied"
        if not dry_run:
            updated_text = json.dumps(data, indent=2) + "\n"
            atomic_write(visual_path, updated_text, create_backups)
    else:
        plan.status = "no_op"
        plan.warnings.append("No visual field binding changes were needed.")

    return plan.model_dump()
