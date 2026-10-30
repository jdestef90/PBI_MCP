"""PBIP-301: Report Layout Flattener and Shadow Model Engine (Engine B).

Ingests deeply nested Power BI report layout JSON, decodes embedded JSON strings,
and provides a flat, path-based addressing scheme along with JMESPath querying.
"""

from __future__ import annotations
import os
import json
import glob
from typing import Any, Dict, List, Optional, Tuple, Union
import jmespath
from mcp_server.cache.state_cache import StateCache
from mcp_server.engine_b.models import (
    VisualPosition,
    VisualShadow,
    PageShadow,
    ReportShadow,
)


def safe_json_loads(val: Any) -> Any:
    """Safely parse a JSON string if string, else return as-is."""
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    return val


class ReportLayoutEngine:
    """Engine B: Manages Report Layout JSON parsing, Shadow Model abstraction, and querying."""

    def __init__(self, workspace_path: str, cache: Optional[StateCache] = None):
        self.workspace_path = os.path.abspath(workspace_path)
        self.cache = cache or StateCache()
        self.report_dir = self._locate_report_dir(self.workspace_path)
        self.report_json_path = os.path.join(self.report_dir, "report.json")
        self._raw_layout: Optional[Dict[str, Any]] = None
        self._shadow_model: Optional[ReportShadow] = None

    def _locate_report_dir(self, root_path: str) -> str:
        """Find the .Report directory given a pbip file or root folder."""
        if root_path.endswith(".pbip") and os.path.isfile(root_path):
            dir_name = os.path.dirname(root_path)
            base_name = os.path.splitext(os.path.basename(root_path))[0]
            candidate = os.path.join(dir_name, f"{base_name}.Report")
            if os.path.isdir(candidate):
                return candidate

        if root_path.endswith(".Report") and os.path.isdir(root_path):
            return root_path

        if os.path.isdir(root_path):
            candidates = glob.glob(os.path.join(root_path, "*.Report"))
            if candidates:
                return candidates[0]
            # Check if report.json is directly inside
            if os.path.isfile(os.path.join(root_path, "report.json")):
                return root_path

        raise ValueError(f"Could not locate Power BI Report directory in: {root_path}")

    def load_layout(self) -> Dict[str, Any]:
        """Load and return the raw report layout JSON using cache."""
        if not os.path.isfile(self.report_json_path):
            raise FileNotFoundError(f"report.json not found in {self.report_dir}")

        def loader(path: str) -> Dict[str, Any]:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)

        raw = self.cache.get_or_load(self.report_json_path, loader)
        self._raw_layout = raw
        self._shadow_model = self._build_shadow_model(raw)
        return raw

    def get_shadow_model(self) -> ReportShadow:
        """Get or build the Shadow Model representation."""
        if self._shadow_model is None:
            self.load_layout()
        assert self._shadow_model is not None
        return self._shadow_model

    def _extract_bound_fields(self, config_dict: Dict[str, Any], query_dict: Dict[str, Any]) -> List[str]:
        """Extract all model fields (Table.Measure, Table.Column) bound to a visual."""
        fields = set()

        # 1. From projections in config
        single_visual = config_dict.get("singleVisual", {})
        projections = single_visual.get("projections", {})
        for channel, items in projections.items():
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and "queryRef" in item:
                        fields.add(item["queryRef"])

        # 2. From query selects
        commands = query_dict.get("Commands", [])
        for cmd in commands:
            sqdc = cmd.get("SemanticQueryDataShapeCommand", {})
            query = sqdc.get("Query", {})
            selects = query.get("Select", [])
            for s in selects:
                if isinstance(s, dict) and "Name" in s:
                    fields.add(s["Name"])
                if isinstance(s, dict) and "Measure" in s:
                    prop = s["Measure"].get("Property")
                    if prop:
                        fields.add(prop)
                if isinstance(s, dict) and "Column" in s:
                    prop = s["Column"].get("Property")
                    if prop:
                        fields.add(prop)

        return sorted(list(fields))

    def _extract_title(self, config_dict: Dict[str, Any]) -> Optional[str]:
        """Extract title if defined in visual objects."""
        single_visual = config_dict.get("singleVisual", {})
        objects = single_visual.get("objects", {})
        general = objects.get("general", [])
        if isinstance(general, list) and general:
            title_obj = general[0].get("properties", {}).get("title", {})
            expr = title_obj.get("expr", {})
            val = expr.get("Literal", {}).get("Value")
            if val and isinstance(val, str):
                return val.strip("'\"")
        return None

    def _build_shadow_model(self, raw_layout: Dict[str, Any]) -> ReportShadow:
        """Transform raw verbose layout JSON into flat Shadow Model."""
        sections = raw_layout.get("sections", [])
        pages: List[PageShadow] = []
        total_visuals = 0

        for s_idx, sec in enumerate(sections):
            sec_id = sec.get("name", f"Section_{s_idx}")
            sec_display = sec.get("displayName", sec_id)
            ordinal = sec.get("ordinal", s_idx)
            width = sec.get("width", 1280)
            height = sec.get("height", 720)

            containers = sec.get("visualContainers", [])
            visuals: List[VisualShadow] = []

            for c_idx, container in enumerate(containers):
                x = container.get("x", 0)
                y = container.get("y", 0)
                z = container.get("z", 0)
                w = container.get("width", 0)
                h = container.get("height", 0)

                config_decoded = safe_json_loads(container.get("config", "{}"))
                query_decoded = safe_json_loads(container.get("query", "{}"))

                v_id = config_decoded.get("name") if isinstance(config_decoded, dict) else None
                if not v_id:
                    v_id = f"vis_{c_idx}"

                single_visual = config_decoded.get("singleVisual", {}) if isinstance(config_decoded, dict) else {}
                v_type = single_visual.get("visualType", "unknown")
                is_slicer = (v_type == "slicer")
                title = self._extract_title(config_decoded) if isinstance(config_decoded, dict) else None

                bound_fields = self._extract_bound_fields(
                    config_decoded if isinstance(config_decoded, dict) else {},
                    query_decoded if isinstance(query_decoded, dict) else {},
                )

                projs: Dict[str, List[str]] = {}
                raw_projs = single_visual.get("projections", {})
                for k, v in raw_projs.items():
                    if isinstance(v, list):
                        projs[k] = [item.get("queryRef", "") for item in v if isinstance(item, dict) and "queryRef" in item]

                v_shadow = VisualShadow(
                    id=v_id,
                    visual_type=v_type,
                    title=title,
                    position=VisualPosition(x=x, y=y, z=z, width=w, height=h),
                    bound_fields=bound_fields,
                    projections=projs,
                    is_slicer=is_slicer,
                    raw_path=f"pages['{sec_display}'].visuals['{v_id}']",
                )
                visuals.append(v_shadow)

            total_visuals += len(visuals)
            pages.append(
                PageShadow(
                    id=sec_id,
                    display_name=sec_display,
                    ordinal=ordinal,
                    width=width,
                    height=height,
                    visual_count=len(visuals),
                    visuals=visuals,
                )
            )

        return ReportShadow(pages=pages, total_visuals=total_visuals)

    def list_pages(self) -> List[Dict[str, Any]]:
        """List summary of all pages in the report."""
        shadow = self.get_shadow_model()
        return [
            {
                "id": p.id,
                "display_name": p.display_name,
                "ordinal": p.ordinal,
                "visual_count": p.visual_count,
                "dimensions": f"{p.width}x{p.height}",
            }
            for p in shadow.pages
        ]

    def find_page(self, page_name_or_id: str) -> Optional[PageShadow]:
        """Find page by display name or id."""
        shadow = self.get_shadow_model()
        target = page_name_or_id.strip().lower()
        for p in shadow.pages:
            if p.id.lower() == target or p.display_name.lower() == target:
                return p
        return None

    def get_page_visuals(self, page_name_or_id: str) -> List[VisualShadow]:
        """Get all visuals on a page."""
        page = self.find_page(page_name_or_id)
        if not page:
            raise ValueError(f"Page '{page_name_or_id}' not found in report.")
        return page.visuals

    def find_visual(self, page_name_or_id: str, visual_id: str) -> Optional[VisualShadow]:
        """Find a visual on a page by id."""
        page = self.find_page(page_name_or_id)
        if not page:
            return None
        target = visual_id.strip().lower()
        for v in page.visuals:
            if v.id.lower() == target:
                return v
        return None

    def get_property(self, path: str) -> Any:
        """PBIP-302: Path-based visual property lookup.

        Syntax: "<page>/<visual>/<property>"
        Example: "Executive Summary/vis_card_revenue/visual_type"
                 "Executive Summary/vis_card_revenue/position"
                 "Executive Summary/vis_card_revenue/projections"
                 "Executive Summary/vis_card_revenue/bound_fields"
        """
        parts = [p.strip() for p in path.split("/") if p.strip()]
        if len(parts) < 2:
            raise ValueError(
                f"Path '{path}' must contain at least '<page>/<visual>' or '<page>/<visual>/<prop>'"
            )

        page_name = parts[0]
        visual_name = parts[1]
        visual = self.find_visual(page_name, visual_name)
        if not visual:
            raise ValueError(f"Visual '{visual_name}' on page '{page_name}' not found.")

        if len(parts) == 2:
            return visual.model_dump()

        prop_name = parts[2]
        vis_dict = visual.model_dump()
        if prop_name in vis_dict:
            res = vis_dict[prop_name]
            # If deeper nested path requested (e.g. position/width)
            for sub_prop in parts[3:]:
                if isinstance(res, dict) and sub_prop in res:
                    res = res[sub_prop]
                else:
                    return None
            return res

        # Check raw decoded container config
        raw_val = self.get_raw_visual_property(page_name, visual_name, "/".join(parts[2:]))
        if raw_val is not None:
            return raw_val

        raise KeyError(f"Property '{prop_name}' not found on visual '{visual_name}'")

    def get_raw_visual_property(self, page_name_or_id: str, visual_id: str, inner_path: str) -> Any:
        """Access raw nested JSON properties inside a visual container."""
        if self._raw_layout is None:
            self.load_layout()
        assert self._raw_layout is not None

        page = self.find_page(page_name_or_id)
        if not page:
            return None

        # Locate raw container
        for sec in self._raw_layout.get("sections", []):
            if sec.get("name") == page.id or sec.get("displayName") == page.display_name:
                for c in sec.get("visualContainers", []):
                    c_conf = safe_json_loads(c.get("config", "{}"))
                    v_name = c_conf.get("name") if isinstance(c_conf, dict) else None
                    if v_name == visual_id:
                        # Query inner path
                        parts = inner_path.split("/")
                        curr = c_conf
                        for p in parts:
                            if isinstance(curr, dict) and p in curr:
                                curr = curr[p]
                            else:
                                return None
                        return curr
        return None

    def query(self, jmespath_expression: str) -> Any:
        """PBIP-302: Query the Shadow Model using JMESPath expression."""
        shadow = self.get_shadow_model()
        data = shadow.model_dump()
        return jmespath.search(jmespath_expression, data)

    def write_layout(self, raw_layout: Dict[str, Any]) -> None:
        """Save raw layout back to report.json and update state cache."""
        with open(self.report_json_path, "w", encoding="utf-8") as f:
            json.dump(raw_layout, f, indent=2)
        self.cache.invalidate(self.report_json_path)
        self._raw_layout = raw_layout
        self._shadow_model = self._build_shadow_model(raw_layout)
