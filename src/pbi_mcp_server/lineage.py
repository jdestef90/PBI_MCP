"""Cross-layer dependency graph and impact analysis for PBIP workspaces.

Connects:
1. Partition SQL (`source_tables`, `sql_fields`) -> TMDL Table & Columns (`source_column`)
2. TMDL Columns & Measures -> DAX expressions in Calculated Columns & Measures, plus Relationships
3. TMDL Measures & Columns -> PBIR Visual Field Bindings & Filters -> Report Pages & Files
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .engine_a.parser import scan_model
from .engine_b.parser import json_files, list_pages, list_visuals
from .integrity.references import find_references
from .models import TmdlObject, VisualSummary, WorkspaceInfo
from .tmdl_sql import discover_partition_metadata
from .workspace import compute_workspace_id

# Matches 'Table Name'[Object Name] or TableName[Object Name] or [Measure Name]
_DAX_REF_RE = re.compile(
    r"(?:'(?P<qtable>(?:''|[^'])+)'|(?P<table_bare>[A-Za-z_][A-Za-z0-9_]*))?\s*\[(?P<ref>[^\]]+)\]"
)

# Matches table-level DAX functions like COUNTROWS('Sales'), ALL(Customer), RELATEDTABLE('Orders')
_TABLE_FN_RE = re.compile(
    r"\b(?:COUNTROWS|RELATEDTABLE|ALL|ALLEXCEPT|ALLNOBLANKROW|ALLSELECTED|VALUES|DISTINCT|FILTER|SUMX|AVERAGEX|MINX|MAXX|COUNTX|RANKX)\s*\(\s*"
    r"(?:'(?P<qtable>(?:''|[^'])+)'|(?P<table_bare>[A-Za-z_][A-Za-z0-9_]*))\b",
    re.IGNORECASE,
)

_DAX_STRING_RE = re.compile(r'"(?:""|[^"])*"')


def _strip_dax_strings_and_comments(dax: str) -> str:
    """Remove string literals and line/block comments before scanning for DAX references."""
    no_block = re.sub(r"/\*.*?\*/", " ", dax, flags=re.DOTALL)
    lines = []
    for line in no_block.splitlines():
        idx = line.find("//")
        lines.append(line[:idx] if idx >= 0 else line)
    joined = "\n".join(lines)
    return _DAX_STRING_RE.sub('""', joined)


def _normalize_rel_endpoint(endpoint: str | None) -> tuple[str, str] | None:
    if not endpoint or "." not in endpoint:
        return None
    tbl, col = endpoint.split(".", 1)
    return tbl.strip("'\" "), col.strip("'\"[] ")


@dataclass
class GraphNode:
    object_id: str
    kind: str  # sql_table, sql_field, partition, table, column, calculated_column, measure, relationship, visual, page
    name: str
    qualified_name: str
    table: str | None = None
    file: str | None = None
    line: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_summary(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "object_id": self.object_id,
            "kind": self.kind,
            "name": self.name,
            "qualified_name": self.qualified_name,
        }
        if self.table is not None:
            res["table"] = self.table
        if self.file is not None:
            res["file"] = self.file
        if self.line is not None:
            res["line"] = self.line
        if self.metadata:
            res.update(self.metadata)
        return res


class DependencyGraph:
    """Directed cross-layer dependency graph for a PBIP workspace.

    Edges point from `upstream -> downstream` (e.g. `Column -> Measure -> Visual -> Page`).
    """

    def __init__(self, info: WorkspaceInfo) -> None:
        self.info = info
        self.ws_id = info.workspace_id or compute_workspace_id(info.root, info.pbip_file)
        self.nodes: dict[str, GraphNode] = {}
        self.downstream: dict[str, set[str]] = {}
        self.upstream: dict[str, set[str]] = {}
        self.unresolved_references: list[dict[str, Any]] = []
        self.dynamic_warnings: list[str] = []

        self.objects: list[TmdlObject] = []
        self.visuals: list[VisualSummary] = []
        self.pages: list[dict[str, Any]] = []
        self.partitions: list[dict[str, Any]] = []

        self._build()

    def _add_node(self, node: GraphNode) -> GraphNode:
        if node.object_id not in self.nodes:
            self.nodes[node.object_id] = node
            self.downstream.setdefault(node.object_id, set())
            self.upstream.setdefault(node.object_id, set())
        return self.nodes[node.object_id]

    def _add_edge(self, src_id: str, dst_id: str) -> None:
        if src_id == dst_id:
            return
        self.downstream.setdefault(src_id, set()).add(dst_id)
        self.upstream.setdefault(dst_id, set()).add(src_id)

    def _build(self) -> None:
        self.objects = scan_model(self.info.semantic_model_dir, self.info.root, workspace_id=self.ws_id)
        self.pages = list_pages(self.info.report_dir, self.info.root, workspace_id=self.ws_id)
        self.visuals = list_visuals(self.info.report_dir, self.info.root, workspace_id=self.ws_id)
        if self.info.semantic_model_dir and self.info.semantic_model_dir.exists():
            self.partitions = discover_partition_metadata(self.info.semantic_model_dir, self.info.root)

        tables_by_cf: dict[str, GraphNode] = {}
        columns_by_key: dict[tuple[str, str], GraphNode] = {}
        columns_by_name_cf: dict[str, list[GraphNode]] = {}
        measures_by_key: dict[tuple[str, str], GraphNode] = {}
        measures_by_name_cf: dict[str, list[GraphNode]] = {}

        # 1. Register TMDL objects
        for obj in self.objects:
            obj_id = obj.object_id or f"{self.ws_id}::{obj.kind}::{obj.qualified_name or obj.name}"
            qname = obj.qualified_name or obj.name
            meta: dict[str, Any] = {}
            if obj.source_column:
                meta["source_column"] = obj.source_column
            if obj.data_type:
                meta["data_type"] = obj.data_type
            node = self._add_node(
                GraphNode(
                    object_id=obj_id,
                    kind=obj.kind,
                    name=obj.name,
                    qualified_name=qname,
                    table=obj.table,
                    file=obj.file,
                    line=obj.line,
                    metadata=meta,
                )
            )
            if obj.kind == "table":
                tables_by_cf[obj.name.casefold()] = node
            elif obj.kind in {"column", "calculated_column"} and obj.table:
                t_cf = obj.table.casefold()
                c_cf = obj.name.casefold()
                columns_by_key[(t_cf, c_cf)] = node
                columns_by_name_cf.setdefault(c_cf, []).append(node)
                if t_cf in tables_by_cf:
                    self._add_edge(tables_by_cf[t_cf].object_id, node.object_id)
            elif obj.kind == "measure":
                m_cf = obj.name.casefold()
                t_cf = (obj.table or "").casefold()
                measures_by_key[(t_cf, m_cf)] = node
                measures_by_name_cf.setdefault(m_cf, []).append(node)

        # Ensure table -> column edges even if table appeared after columns
        for (t_cf, _), col_node in columns_by_key.items():
            if t_cf in tables_by_cf:
                self._add_edge(tables_by_cf[t_cf].object_id, col_node.object_id)

        # 2. Register Partitions and SQL Lineage
        for part in self.partitions:
            t_name = str(part.get("table") or "")
            p_name = str(part.get("partition") or "")
            t_cf = t_name.casefold()
            part_id = f"{self.ws_id}::partition::{t_name}.{p_name}"
            part_node = self._add_node(
                GraphNode(
                    object_id=part_id,
                    kind="partition",
                    name=p_name,
                    qualified_name=f"{t_name}.{p_name}",
                    table=t_name,
                    file=part.get("file"),
                    line=part.get("sql_start_line"),
                    metadata={
                        "source_type": part.get("source_type"),
                        "source_tables": part.get("source_tables", []),
                        "sql_fields": part.get("sql_fields", []),
                    },
                )
            )
            if t_cf in tables_by_cf:
                self._add_edge(part_node.object_id, tables_by_cf[t_cf].object_id)

            for m_param in part.get("m_parameters", []):
                self.dynamic_warnings.append(
                    f"Partition '{t_name}.{p_name}' uses dynamic M parameter '{m_param}' in SQL query."
                )

            for src_tbl in part.get("source_tables", []):
                sql_tbl_id = f"{self.ws_id}::sql_table::{src_tbl}"
                sql_tbl_node = self._add_node(
                    GraphNode(
                        object_id=sql_tbl_id,
                        kind="sql_table",
                        name=src_tbl,
                        qualified_name=src_tbl,
                        table=t_name,
                        file=part.get("file"),
                    )
                )
                self._add_edge(sql_tbl_node.object_id, part_node.object_id)

            sql_fields_map = {str(f).casefold(): str(f) for f in part.get("sql_fields", [])}
            has_wildcard = "*" in sql_fields_map
            # Connect SQL fields -> TMDL columns via source_column or column name
            for obj in self.objects:
                if obj.kind == "column" and (obj.table or "").casefold() == t_cf:
                    col_node = columns_by_key.get((t_cf, obj.name.casefold()))
                    if not col_node:
                        continue
                    src_col_name = obj.source_column or obj.name
                    matched_sql_field = sql_fields_map.get(src_col_name.casefold())
                    if matched_sql_field or has_wildcard or part.get("source_type") == "native_sql":
                        field_label = matched_sql_field or src_col_name
                        sql_field_id = f"{self.ws_id}::sql_field::{t_name}.{field_label}"
                        sql_field_node = self._add_node(
                            GraphNode(
                                object_id=sql_field_id,
                                kind="sql_field",
                                name=field_label,
                                qualified_name=f"{t_name}.{field_label}",
                                table=t_name,
                                file=part.get("file"),
                                line=part.get("sql_start_line"),
                            )
                        )
                        self._add_edge(part_node.object_id, sql_field_node.object_id)
                        self._add_edge(sql_field_node.object_id, col_node.object_id)

        # 3. Relationships
        for obj in self.objects:
            if obj.kind != "relationship":
                continue
            rel_id = obj.object_id or f"{self.ws_id}::relationship::{obj.name}"
            rel_node = self.nodes.get(rel_id)
            if not rel_node:
                continue
            from_ep = _normalize_rel_endpoint(obj.from_column)
            to_ep = _normalize_rel_endpoint(obj.to_column)
            for ep_label, ep in (("fromColumn", from_ep), ("toColumn", to_ep)):
                if not ep:
                    continue
                col_node = columns_by_key.get((ep[0].casefold(), ep[1].casefold()))
                if col_node:
                    self._add_edge(col_node.object_id, rel_node.object_id)
                else:
                    self.unresolved_references.append({
                        "source_object": rel_node.qualified_name,
                        "source_kind": "relationship",
                        "property": ep_label,
                        "missing_reference": f"{ep[0]}[{ep[1]}]",
                        "file": obj.file,
                        "line": obj.line,
                    })

        # 4. DAX Expressions in Measures and Calculated Columns
        for obj in self.objects:
            if obj.kind not in {"measure", "calculated_column"} or not obj.expression:
                continue
            owner_t_cf = (obj.table or "").casefold()
            owner_n_cf = obj.name.casefold()
            owner_node = (
                measures_by_key.get((owner_t_cf, owner_n_cf))
                if obj.kind == "measure"
                else columns_by_key.get((owner_t_cf, owner_n_cf))
            )
            if not owner_node:
                continue

            cleaned_dax = _strip_dax_strings_and_comments(obj.expression)

            # Table references in DAX functions
            for tm in _TABLE_FN_RE.finditer(cleaned_dax):
                raw_tbl = (tm.group("qtable") or tm.group("table_bare") or "").replace("''", "'").strip()
                t_node = tables_by_cf.get(raw_tbl.casefold())
                if t_node:
                    self._add_edge(t_node.object_id, owner_node.object_id)

            # Bracketed references: 'Table'[Ref], Table[Ref], or [Ref]
            for bm in _DAX_REF_RE.finditer(cleaned_dax):
                raw_tbl = (bm.group("qtable") or bm.group("table_bare") or "").replace("''", "'").strip()
                ref_name = bm.group("ref").strip()
                ref_cf = ref_name.casefold()

                if raw_tbl:
                    t_cf = raw_tbl.casefold()
                    col_target = columns_by_key.get((t_cf, ref_cf))
                    meas_target = measures_by_key.get((t_cf, ref_cf))
                    if col_target:
                        self._add_edge(col_target.object_id, owner_node.object_id)
                    elif meas_target:
                        self._add_edge(meas_target.object_id, owner_node.object_id)
                    elif ref_cf in measures_by_name_cf and len(measures_by_name_cf[ref_cf]) == 1:
                        self._add_edge(measures_by_name_cf[ref_cf][0].object_id, owner_node.object_id)
                    else:
                        self.unresolved_references.append({
                            "source_object": owner_node.qualified_name,
                            "source_kind": obj.kind,
                            "missing_reference": f"{raw_tbl}[{ref_name}]",
                            "file": obj.file,
                            "line": obj.line,
                        })
                else:
                    # Unqualified [Ref]: in DAX, unqualified [Ref] resolves to a measure, or a column in the current table
                    meas_candidates = measures_by_name_cf.get(ref_cf, [])
                    same_table_col = columns_by_key.get((owner_t_cf, ref_cf))
                    if meas_candidates:
                        for mc in meas_candidates:
                            self._add_edge(mc.object_id, owner_node.object_id)
                    elif same_table_col:
                        self._add_edge(same_table_col.object_id, owner_node.object_id)
                    else:
                        self.unresolved_references.append({
                            "source_object": owner_node.qualified_name,
                            "source_kind": obj.kind,
                            "missing_reference": f"[{ref_name}]",
                            "file": obj.file,
                            "line": obj.line,
                        })

        # 5. Report Pages and Visuals
        pages_by_cf: dict[str, GraphNode] = {}
        for p in self.pages:
            p_name = str(p["name"])
            p_disp = str(p.get("display_name") or p_name)
            p_id = str(p.get("object_id") or f"{self.ws_id}::page::{p_name}")
            p_node = self._add_node(
                GraphNode(
                    object_id=p_id,
                    kind="page",
                    name=p_name,
                    qualified_name=p_disp,
                    file=p.get("file"),
                    metadata={"display_name": p_disp, "visual_count": p.get("visual_count", 0)},
                )
            )
            pages_by_cf[p_name.casefold()] = p_node
            for flt_ref in p.get("filter_fields", []):
                self._connect_field_ref_to_node(
                    flt_ref,
                    p_node,
                    columns_by_key,
                    measures_by_key,
                    measures_by_name_cf,
                    columns_by_name_cf,
                )

        for v in self.visuals:
            v_id = v.object_id or f"{self.ws_id}::visual::{v.page}/{v.visual_id}"
            v_node = self._add_node(
                GraphNode(
                    object_id=v_id,
                    kind="visual",
                    name=v.visual_id,
                    qualified_name=v.qualified_name or f"{v.page}/{v.visual_id}",
                    file=v.file,
                    metadata={
                        "page": v.page,
                        "page_display_name": v.page_display_name,
                        "visual_type": v.visual_type,
                        "title": v.title,
                        "bound_fields": v.bound_fields,
                        "role_bindings": v.role_bindings,
                    },
                )
            )
            p_node = pages_by_cf.get(v.page.casefold())
            if p_node:
                self._add_edge(v_node.object_id, p_node.object_id)

            all_v_refs = set(v.bound_fields) | set(v.filter_fields)
            for qref in sorted(all_v_refs):
                self._connect_field_ref_to_node(
                    qref,
                    v_node,
                    columns_by_key,
                    measures_by_key,
                    measures_by_name_cf,
                    columns_by_name_cf,
                )

    def _connect_field_ref_to_node(
        self,
        qref: str,
        target_node: GraphNode,
        columns_by_key: dict[tuple[str, str], GraphNode],
        measures_by_key: dict[tuple[str, str], GraphNode],
        measures_by_name_cf: dict[str, list[GraphNode]],
        columns_by_name_cf: dict[str, list[GraphNode]],
    ) -> None:
        cleaned = qref.strip()
        if "." in cleaned:
            tbl, prop = cleaned.split(".", 1)
            t_cf = tbl.strip("'\" ").casefold()
            p_cf = prop.strip("'\"[] ").casefold()
            col_node = columns_by_key.get((t_cf, p_cf))
            meas_node = measures_by_key.get((t_cf, p_cf))
            if meas_node:
                self._add_edge(meas_node.object_id, target_node.object_id)
                return
            if col_node:
                self._add_edge(col_node.object_id, target_node.object_id)
                return
            if p_cf in measures_by_name_cf and len(measures_by_name_cf[p_cf]) == 1:
                self._add_edge(measures_by_name_cf[p_cf][0].object_id, target_node.object_id)
                return
            self.unresolved_references.append({
                "source_object": target_node.qualified_name,
                "source_kind": target_node.kind,
                "missing_reference": cleaned,
                "file": target_node.file,
            })
        else:
            p_cf = cleaned.casefold()
            for mn in measures_by_name_cf.get(p_cf, []):
                self._add_edge(mn.object_id, target_node.object_id)
            for cn in columns_by_name_cf.get(p_cf, []):
                self._add_edge(cn.object_id, target_node.object_id)

    def resolve_nodes(self, term: str) -> list[GraphNode]:
        """Resolve a user search term (`Sales[Revenue]`, `Sales.Amount`, `Total Revenue`, or `object_id`) to graph nodes."""
        cleaned = term.strip()
        cf = cleaned.casefold()

        # 1. Exact object_id or qualified_name
        exact_q = [
            n
            for n in self.nodes.values()
            if n.object_id.casefold() == cf or n.qualified_name.casefold() == cf
        ]
        if exact_q:
            return exact_q

        # 2. Table[Field] or Table.Field syntax
        m = _DAX_REF_RE.fullmatch(cleaned)
        if m and (m.group("qtable") or m.group("table_bare")):
            tbl_cf = (m.group("qtable") or m.group("table_bare") or "").strip().casefold()
            ref_cf = m.group("ref").strip().casefold()
            matched = [
                n
                for n in self.nodes.values()
                if (n.table or "").casefold() == tbl_cf and n.name.casefold() == ref_cf
            ]
            if matched:
                return matched
        elif "." in cleaned and "::" not in cleaned:
            tbl, prop = cleaned.split(".", 1)
            tbl_cf = tbl.strip("'\" ").casefold()
            prop_cf = prop.strip("'\"[] ").casefold()
            matched = [
                n
                for n in self.nodes.values()
                if (n.table or "").casefold() == tbl_cf and n.name.casefold() == prop_cf
            ]
            if matched:
                return matched

        # 3. Exact name match (prefer measures/columns/tables over sql_field)
        exact_name = [
            n
            for n in self.nodes.values()
            if n.name.casefold() == cf and n.kind in {"measure", "column", "calculated_column", "table", "visual"}
        ]
        if exact_name:
            return exact_name

        # 4. Partial match
        return [
            n
            for n in self.nodes.values()
            if cf in n.name.casefold() or cf in n.qualified_name.casefold()
        ]

    def get_transitive_upstream(self, start_ids: list[str]) -> list[GraphNode]:
        visited: set[str] = set()
        queue = list(start_ids)
        result: list[GraphNode] = []
        while queue:
            curr = queue.pop(0)
            for parent_id in sorted(self.upstream.get(curr, set())):
                if parent_id not in visited and parent_id not in start_ids:
                    visited.add(parent_id)
                    if parent_id in self.nodes:
                        result.append(self.nodes[parent_id])
                    queue.append(parent_id)
        return result

    def get_transitive_downstream(self, start_ids: list[str]) -> list[GraphNode]:
        visited: set[str] = set()
        queue = list(start_ids)
        result: list[GraphNode] = []
        while queue:
            curr = queue.pop(0)
            for child_id in sorted(self.downstream.get(curr, set())):
                if child_id not in visited and child_id not in start_ids:
                    visited.add(child_id)
                    if child_id in self.nodes:
                        result.append(self.nodes[child_id])
                    queue.append(child_id)
        return result


def analyze_object_impact(info: WorkspaceInfo, term: str) -> dict[str, Any]:
    """Perform cross-layer lineage and impact analysis while preserving full backward compatibility."""
    graph = DependencyGraph(info)
    matched_nodes = graph.resolve_nodes(term)
    start_ids = [n.object_id for n in matched_nodes]

    upstream_nodes = graph.get_transitive_upstream(start_ids)
    downstream_nodes = graph.get_transitive_downstream(start_ids)

    # Textual references (legacy & complete file/line lookup)
    raw_refs = find_references(info.root, info.semantic_model_dir, info.report_dir, term)
    # If term was Table[Field], also search for Field name if raw_refs was empty
    if not raw_refs and matched_nodes:
        primary_name = matched_nodes[0].name
        if primary_name.casefold() != term.strip().casefold():
            raw_refs = find_references(info.root, info.semantic_model_dir, info.report_dir, primary_name)

    for ref in raw_refs:
        file = ref.get("file")
        if isinstance(file, str):
            ref["absolute_file"] = str((info.root / file).resolve())

    visual_refs = [r for r in raw_refs if str(r.get("file", "")).lower().endswith(".json")]
    tmdl_refs = [r for r in raw_refs if str(r.get("file", "")).lower().endswith(".tmdl")]

    # Combine graph-derived affected visuals with query-matched visuals
    graph_visual_ids = {n.name.casefold() for n in downstream_nodes if n.kind == "visual"}
    all_visuals = list_visuals(info.report_dir, info.root, workspace_id=graph.ws_id)
    query_visuals = list_visuals(info.report_dir, info.root, query=term, workspace_id=graph.ws_id)
    query_vis_ids = {v.visual_id.casefold() for v in query_visuals}

    affected_visuals = [
        v.model_dump()
        for v in all_visuals
        if v.visual_id.casefold() in graph_visual_ids or v.visual_id.casefold() in query_vis_ids
    ]

    affected_pages = [
        n.to_summary() for n in downstream_nodes if n.kind == "page"
    ]
    dependent_measures = [
        n.to_summary() for n in downstream_nodes if n.kind == "measure"
    ]
    dependent_columns = [
        n.to_summary() for n in downstream_nodes if n.kind in {"column", "calculated_column"}
    ]
    dependent_relationships = [
        n.to_summary() for n in downstream_nodes if n.kind == "relationship"
    ]

    upstream_sql = [
        n.to_summary() for n in upstream_nodes if n.kind in {"sql_table", "sql_field", "partition"}
    ]
    upstream_columns = [
        n.to_summary() for n in upstream_nodes if n.kind in {"column", "calculated_column"}
    ]
    upstream_measures = [
        n.to_summary() for n in upstream_nodes if n.kind == "measure"
    ]

    visual_files = sorted(
        {str(x["file"]) for x in visual_refs if "file" in x}
        | {str(v["file"]) for v in affected_visuals if v.get("file")}
    )
    all_affected_files = sorted(
        {str(x["file"]) for x in raw_refs if "file" in x}
        | {str(n.file) for n in [*matched_nodes, *downstream_nodes] if n.file}
    )

    warnings: list[str] = list(graph.dynamic_warnings)
    if len(matched_nodes) > 1:
        warnings.append(
            f"Term {term!r} matched {len(matched_nodes)} objects: "
            + ", ".join(n.qualified_name for n in matched_nodes[:5])
        )
    for ur in graph.unresolved_references:
        if term.strip().casefold() in str(ur.get("missing_reference", "")).casefold() or any(
            ur.get("source_object") == n.qualified_name for n in matched_nodes
        ):
            warnings.append(
                f"Unresolved reference {ur['missing_reference']!r} in {ur['source_kind']} {ur['source_object']!r}."
            )

    return {
        "term": term,
        "resolved_objects": [n.to_summary() for n in matched_nodes],
        "upstream": {
            "sql_and_partitions": upstream_sql,
            "columns": upstream_columns,
            "measures": upstream_measures,
        },
        "downstream": {
            "measures": dependent_measures,
            "columns": dependent_columns,
            "relationships": dependent_relationships,
            "visuals": affected_visuals,
            "pages": affected_pages,
        },
        "references": raw_refs,
        "tmdl_references": tmdl_refs,
        "visual_references": visual_refs,
        "affected_visuals": affected_visuals,
        "affected_pages": affected_pages,
        "affected_files": all_affected_files,
        "affected_visual_files": visual_files,
        "warnings": warnings,
        "counts": {
            "total": len(raw_refs),
            "tmdl": len(tmdl_refs),
            "visual": len(visual_refs),
            "affected_visual_objects": len(affected_visuals),
            "affected_pages": len(affected_pages),
            "dependent_measures": len(dependent_measures),
            "dependent_columns": len(dependent_columns),
            "upstream_dependencies": len(upstream_nodes),
        },
    }


def find_unused_model_objects(info: WorkspaceInfo) -> dict[str, Any]:
    """Identify unreferenced measures/columns and dead-branch (indirectly referenced only) objects."""
    graph = DependencyGraph(info)
    report_texts = [p.read_text(encoding="utf-8-sig").casefold() for p in json_files(info.report_dir)]

    unused_measures: list[dict[str, Any]] = []
    unused_columns: list[dict[str, Any]] = []
    indirectly_referenced_only: list[dict[str, Any]] = []

    for obj in graph.objects:
        if obj.kind not in {"measure", "column", "calculated_column"}:
            continue
        node_id = obj.object_id or f"{graph.ws_id}::{obj.kind}::{obj.qualified_name or obj.name}"
        direct_children = [graph.nodes[cid] for cid in graph.downstream.get(node_id, set()) if cid in graph.nodes]
        transitive_children = graph.get_transitive_downstream([node_id])

        name_cf = obj.name.casefold()
        tbl_cf = (obj.table or "").casefold()
        qref_cf = f"{tbl_cf}.{name_cf}"

        if obj.kind == "measure":
            in_report_json = any(
                f'"{name_cf}"' in rtext or f".{name_cf}\"" in rtext for rtext in report_texts
            )
        else:
            in_report_json = any(f'"{qref_cf}"' in rtext for rtext in report_texts)

        reaches_report_or_rel = (
            in_report_json
            or any(n.kind in {"visual", "page", "relationship"} for n in transitive_children)
        )

        if not direct_children and not in_report_json:
            entry = {
                "name": obj.name,
                "table": obj.table,
                "file": obj.file,
                "line": obj.line,
                "object_id": obj.object_id,
                "qualified_name": obj.qualified_name,
            }
            if obj.kind == "measure":
                unused_measures.append(entry)
            else:
                entry["kind"] = obj.kind
                unused_columns.append(entry)
        elif direct_children and not reaches_report_or_rel:
            indirectly_referenced_only.append({
                "name": obj.name,
                "kind": obj.kind,
                "table": obj.table,
                "file": obj.file,
                "line": obj.line,
                "object_id": obj.object_id,
                "qualified_name": obj.qualified_name,
                "referenced_by": [n.qualified_name for n in direct_children],
            })

    return {
        "counts": {
            "unused_measures": len(unused_measures),
            "unused_columns": len(unused_columns),
            "indirectly_referenced_only": len(indirectly_referenced_only),
            "total_unused": len(unused_measures) + len(unused_columns),
        },
        "unused_measures": unused_measures,
        "unused_columns": unused_columns,
        "indirectly_referenced_only": indirectly_referenced_only,
    }
