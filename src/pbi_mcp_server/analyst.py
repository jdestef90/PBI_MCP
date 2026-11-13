from __future__ import annotations

import re
from typing import Any

from .engine_a.parser import scan_model
from .engine_b.parser import list_pages, list_visuals
from .lineage import find_unused_model_objects
from .models import BpaIssue, TmdlObject, WorkspaceInfo
from .workspace import compute_workspace_id

_DAX_FUNCTIONS = {
    "abs", "acos", "acosh", "addcolumns", "addmissingitems", "all", "allcrossfiltered",
    "allexcept", "allnoblankrow", "allselected", "and", "approximatedistinctcount",
    "asin", "asinh", "atan", "atanh", "average", "averagea", "averagex", "betadist",
    "betainv", "blank", "calculate", "calculatetable", "calendar", "calendarauto",
    "ceiling", "chisqdist", "chisqinv", "CLOSINGBALANCEMONTH", "closingbalancequarter",
    "closingbalanceyear", "coalesce", "columnstatistics", "combin", "combina",
    "concatenate", "concatenatex", "confidence.norm", "confidence.t", "contains",
    "containsrow", "containsstring", "containsstringexact", "convert", "cos", "cosh",
    "count", "counta", "countax", "countblank", "countrows", "countx", "crossfilter",
    "crossjoin", "currency", "currentgroup", "customdata", "datatable", "date",
    "dateadd", "datediff", "datesbetween", "datesinperiod", "datesmtd", "datesqtd",
    "datesytd", "datevalue", "day", "degrees", "detailrows", "distinct",
    "distinctcount", "distinctcountnoblank", "divide", "earlier", "earliest",
    "edate", "endofmonth", "endofquarter", "endofyear", "eomonth", "error", "even",
    "exact", "except", "exp", "expondist", "fact", "false", "filter", "filters",
    "find", "firstdate", "firstnonblank", "firstnonblankvalue", "fixed", "floor",
    "format", "gcd", "generate", "generateall", "generateseries", "grouping",
    "hasonefilter", "hasonevalue", "hour", "if", "if.eager", "iferror", "ignore",
    "in", "index", "int", "intersect", "isafter", "isblank", "iscrossfiltered",
    "isempty", "iserror", "iseven", "isfiltered", "isinsubtotal", "islogical",
    "isnontext", "isnumber", "iso.ceiling", "isodd", "isonorafter", "isselectedmeasure",
    "issubtotal", "istext", "keepfilters", "keywordmatch", "lastdate", "lastnonblank",
    "lastnonblankvalue", "lcm", "left", "len", "ln", "log", "log10", "lookupvalue",
    "lower", "max", "maxa", "maxx", "median", "medianx", "mid", "min", "mina",
    "minute", "minx", "mod", "month", "mround", "naturalinnerjoin",
    "naturalleftouterjoin", "networkdays", "nextday", "nextmonth", "nextquarter",
    "nextyear", "nonvisual", "norm.dist", "norm.inv", "norm.s.dist", "norm.s.inv",
    "not", "now", "odd", "offset", "openingbalancemonth", "openingbalancequarter",
    "openingbalanceyear", "or", "orderby", "parallelperiod", "partitionby", "path",
    "pathcontains", "pathitem", "pathitemreverse", "pathlength", "percentile.exc",
    "percentile.inc", "percentilex.exc", "percentilex.inc", "permut", "pi",
    "poisson.dist", "power", "previousday", "previousmonth", "previousquarter",
    "previousyear", "product", "productx", "quarter", "quotient", "radians", "rand",
    "randbetween", "rank", "rank.eq", "rankx", "related", "relatedtable",
    "removefilters", "replace", "rept", "return", "right", "rollup",
    "rollupaddissubtotal", "rollupgroup", "rollupissubtotal", "round", "rounddown",
    "roundup", "row", "rownumber", "sameperiodlastyear", "sample", "search",
    "second", "selectcolumns", "selectedmeasure", "selectedmeasureformatstring",
    "selectedmeasurename", "selectedvalue", "sign", "sin", "sinh", "sqrt", "sqrtpi",
    "startofmonth", "startofquarter", "startofyear", "stdev.p", "stdev.s",
    "stdevx.p", "stdevx.s", "substitute", "substitutewithindex", "sum", "summarize",
    "summarizecolumns", "sumx", "switch", "t.dist", "t.dist.2t", "t.dist.rt",
    "t.inv", "t.inv.2t", "tan", "tanh", "time", "timevalue", "today", "topn",
    "topnperlevel", "topnskip", "totalmtd", "totalqtd", "totalytd", "treatas",
    "trim", "true", "trunc", "unichar", "unicode", "union", "upper",
    "userculture", "userelationship", "username", "userobjectid",
    "userprincipalname", "utcNow", "utctoday", "value", "values", "var", "var.p",
    "var.s", "varx.p", "varx.s", "weekday", "weeknum", "window", "xirr", "xnpv",
    "year", "yearfrac",
}
_DAX_KEYWORDS_LOWER = {k.lower() for k in _DAX_FUNCTIONS}

_TOKEN_PROTECT_RE = re.compile(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\[[^\]]*\])")
_WORD_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.]*)\b")
_BRACKET_REF_RE = re.compile(r"(?:'(?P<qtable>[^']+)'|(?P<table_bare>[A-Za-z_][A-Za-z0-9_]*))?\s*\[(?P<ref>[^\]]+)\]")


def format_dax_string(dax: str, *, multiline: bool = False) -> str:
    """Format a DAX formula to clean SQLBI-style casing and operator spacing."""
    raw = dax.strip()
    if not raw:
        return raw

    placeholders: list[str] = []

    def _protect(match: re.Match[str]) -> str:
        idx = len(placeholders)
        placeholders.append(match.group(0))
        return f"__DAX_PROTECT_{idx}__"

    protected = _TOKEN_PROTECT_RE.sub(_protect, raw)

    # Collapse redundant whitespace into single spaces
    protected = re.sub(r"\s+", " ", protected).strip()

    # Uppercase DAX functions and keywords
    def _upper_kw(match: re.Match[str]) -> str:
        word = match.group(1)
        if word.lower() in _DAX_KEYWORDS_LOWER:
            return word.upper()
        return word

    protected = _WORD_RE.sub(_upper_kw, protected)

    # Normalize spacing around commas and binary operators
    protected = re.sub(r"\s*,\s*", ", ", protected)
    protected = re.sub(r"\s*(<>|<=|>=|&&|\|\||[=+\-*/<>])\s*", r" \1 ", protected)
    protected = re.sub(r"\(\s+", "(", protected)
    protected = re.sub(r"\s+\)", ")", protected)
    protected = re.sub(r"\s+", " ", protected).strip()

    if multiline and ("VAR " in protected or "RETURN " in protected):
        protected = re.sub(r"\bVAR\s+", "\nVAR ", protected)
        protected = re.sub(r"\bRETURN\s+", "\nRETURN\n    ", protected)
        protected = protected.strip()

    # Restore protected tokens (removing accidental space between Table and [Col])
    for idx, token in enumerate(placeholders):
        protected = protected.replace(f"__DAX_PROTECT_{idx}__", token)

    protected = re.sub(r"([A-Za-z0-9_'])\s+(\[[^\]]+\])", r"\1\2", protected)
    return protected


def generate_measure_docstring(
    measure_name: str,
    table_name: str | None,
    dax: str,
    *,
    dependent_visual_count: int = 0,
    dependent_measure_count: int = 0,
) -> str:
    """Synthesize a concise TMDL description comment from a measure's DAX formula and lineage."""
    expr = dax.strip()
    refs: list[str] = []
    for m in _BRACKET_REF_RE.finditer(expr):
        tbl = m.group("qtable") or m.group("table_bare")
        ref = m.group("ref")
        formatted_ref = f"{tbl}[{ref}]" if tbl else f"[{ref}]"
        if formatted_ref not in refs:
            refs.append(formatted_ref)

    upper = expr.upper()
    operations: list[str] = []
    for fn, label in [
        ("CALCULATE(", "filtered context calculation"),
        ("SUMX(", "row-by-row sum iteration"),
        ("SUM(", "sum aggregation"),
        ("AVERAGEX(", "row-by-row average iteration"),
        ("AVERAGE(", "average aggregation"),
        ("DIVIDE(", "safe ratio division"),
        ("DISTINCTCOUNT(", "distinct count"),
        ("COUNTROWS(", "row count"),
        ("SAMEPERIODLASTYEAR(", "year-over-year time intelligence"),
        ("DATESYTD(", "year-to-date time intelligence"),
        ("TOTALYTD(", "year-to-date aggregation"),
        ("DATEADD(", "date-shifted time intelligence"),
        ("FILTER(", "table filtering"),
    ]:
        if fn in upper:
            operations.append(label)

    table_clause = f" in {table_name}" if table_name else ""
    op_clause = f" using {', '.join(operations[:3])}" if operations else ""
    ref_clause = f" (references: {', '.join(refs[:5])})" if refs else ""
    usage_parts: list[str] = []
    if dependent_measure_count > 0:
        usage_parts.append(f"{dependent_measure_count} measure(s)")
    if dependent_visual_count > 0:
        usage_parts.append(f"{dependent_visual_count} visual(s)")
    usage_clause = f" Used by {' and '.join(usage_parts)}." if usage_parts else ""
    return f"Computes {measure_name}{table_clause}{op_clause}{ref_clause}.{usage_clause}"


def _normalize_rel_endpoint(endpoint: str | None) -> tuple[str, str] | None:
    if not endpoint or "." not in endpoint:
        return None
    tbl, col = endpoint.split(".", 1)
    return tbl.strip("'\" "), col.strip("'\"[] ")


def find_unused_objects(info: WorkspaceInfo) -> dict[str, Any]:
    """Identify unreferenced measures and columns across the Semantic Model and Report Layout."""
    return find_unused_model_objects(info)


def check_model_bpa(info: WorkspaceInfo) -> dict[str, Any]:
    """Run Best Practice Analyzer rules against the Semantic Model and Report Layout."""
    ws_id = info.workspace_id or compute_workspace_id(info.root, info.pbip_file)
    objects = scan_model(info.semantic_model_dir, info.root, workspace_id=ws_id)
    issues: list[BpaIssue] = []

    columns_by_key: dict[tuple[str, str], TmdlObject] = {}
    measures_by_name_cf: dict[str, TmdlObject] = {}
    measures_by_key: dict[tuple[str, str], TmdlObject] = {}

    for obj in objects:
        if obj.kind in {"column", "calculated_column"} and obj.table:
            columns_by_key[(obj.table.casefold(), obj.name.casefold())] = obj
        elif obj.kind == "measure":
            measures_by_name_cf[obj.name.casefold()] = obj
            if obj.table:
                measures_by_key[(obj.table.casefold(), obj.name.casefold())] = obj

    # Rule 1, 2, 7, 8: Relationship checks
    for rel in [x for x in objects if x.kind == "relationship"]:
        if (rel.cross_filtering or "").casefold() == "bothdirections":
            issues.append(
                BpaIssue(
                    rule_id="BPA_BIDIRECTIONAL_RELATIONSHIP",
                    severity="warning",
                    category="Relationships",
                    object_type="relationship",
                    object_name=rel.name,
                    object_id=rel.object_id,
                    message=f"Relationship {rel.name!r} uses bidirectional cross-filtering.",
                    recommendation="Prefer single-direction filtering and use CROSSFILTER inside specific measures when needed.",
                    file=rel.file,
                )
            )
        if (
            (rel.from_cardinality or "").casefold() == "many"
            and (rel.to_cardinality or "").casefold() == "many"
        ):
            issues.append(
                BpaIssue(
                    rule_id="BPA_MANY_TO_MANY_RELATIONSHIP",
                    severity="warning",
                    category="Relationships",
                    object_type="relationship",
                    object_name=rel.name,
                    object_id=rel.object_id,
                    message=f"Relationship {rel.name!r} uses many-to-many cardinality.",
                    recommendation="Introduce a bridging dimension table to avoid ambiguous filter propagation.",
                    file=rel.file,
                )
            )

        from_ep = _normalize_rel_endpoint(rel.from_column)
        to_ep = _normalize_rel_endpoint(rel.to_column)
        for ep_name, ep in (("fromColumn", from_ep), ("toColumn", to_ep)):
            if not ep:
                continue
            col_obj = columns_by_key.get((ep[0].casefold(), ep[1].casefold()))
            if not col_obj:
                issues.append(
                    BpaIssue(
                        rule_id="BPA_BROKEN_RELATIONSHIP_ENDPOINT",
                        severity="error",
                        category="Relationships",
                        object_type="relationship",
                        object_name=rel.name,
                        object_id=rel.object_id,
                        message=f"Relationship {rel.name!r} {ep_name} references missing column '{ep[0]}[{ep[1]}]'.",
                        recommendation="Update or remove the broken relationship endpoint in relationships.tmdl.",
                        file=rel.file,
                    )
                )
            elif ep_name == "fromColumn" and not col_obj.is_hidden:
                issues.append(
                    BpaIssue(
                        rule_id="BPA_UNHIDDEN_FOREIGN_KEY",
                        severity="warning",
                        category="Maintenance",
                        object_type="column",
                        object_name=col_obj.name,
                        table=col_obj.table,
                        object_id=col_obj.object_id,
                        message=f"Foreign key column '{col_obj.table}[{col_obj.name}]' used in relationship {rel.name!r} is visible.",
                        recommendation="Hide foreign key columns on the many-side of relationships so users filter via dimension tables.",
                        file=col_obj.file,
                    )
                )

    # Rule 3, 4, 6: Measure checks
    for m in [x for x in objects if x.kind == "measure"]:
        if not (m.description or "").strip():
            issues.append(
                BpaIssue(
                    rule_id="BPA_MISSING_MEASURE_DESCRIPTION",
                    severity="info",
                    category="Maintenance",
                    object_type="measure",
                    object_name=m.name,
                    table=m.table,
                    object_id=m.object_id,
                    message=f"Measure '{m.name}' has no /// documentation description.",
                    recommendation="Use generate_measure_docs to add a TMDL /// description comment.",
                    file=m.file,
                )
            )
        if not (m.format_string or "").strip():
            issues.append(
                BpaIssue(
                    rule_id="BPA_MISSING_FORMAT_STRING",
                    severity="warning",
                    category="Formatting",
                    object_type="measure",
                    object_name=m.name,
                    table=m.table,
                    object_id=m.object_id,
                    message=f"Measure '{m.name}' is missing an explicit formatString.",
                    recommendation="Specify a formatString (e.g. '#,##0' or '$#,##0.00') for consistent visual rendering.",
                    file=m.file,
                )
            )
        if m.expression and "/" in m.expression and "DIVIDE(" not in m.expression.upper():
            issues.append(
                BpaIssue(
                    rule_id="BPA_DIRECT_DIVISION_OPERATOR",
                    severity="info",
                    category="DAX",
                    object_type="measure",
                    object_name=m.name,
                    table=m.table,
                    object_id=m.object_id,
                    message=f"Measure '{m.name}' uses the '/' division operator directly.",
                    recommendation="Use DIVIDE(numerator, denominator) to handle division-by-zero safely.",
                    file=m.file,
                )
            )

    # Rule 5 & 9: Column data types and calculated column complexity
    for col in [x for x in objects if x.kind in {"column", "calculated_column"}]:
        if (col.data_type or "").casefold() == "double":
            issues.append(
                BpaIssue(
                    rule_id="BPA_FLOAT_DATA_TYPE",
                    severity="warning",
                    category="Performance",
                    object_type=col.kind,
                    object_name=col.name,
                    table=col.table,
                    object_id=col.object_id,
                    message=f"Column '{col.table}[{col.name}]' uses floating-point 'double' dataType.",
                    recommendation="Use 'decimal' (Fixed Decimal Currency) or 'int64' to prevent floating-point rounding inaccuracies.",
                    file=col.file,
                )
            )
        if col.kind == "calculated_column" and col.expression:
            upper_expr = col.expression.upper()
            if any(fn in upper_expr for fn in ("SUMX(", "FILTER(", "CALCULATE(", "RELATEDTABLE(", "LOOKUPVALUE(")):
                issues.append(
                    BpaIssue(
                        rule_id="BPA_CALCULATED_COLUMN_COMPLEXITY",
                        severity="warning",
                        category="Performance",
                        object_type="calculated_column",
                        object_name=col.name,
                        table=col.table,
                        object_id=col.object_id,
                        message=f"Calculated column '{col.table}[{col.name}]' uses context transition or table scan functions.",
                        recommendation="Push calculated column logic upstream to SQL/Power Query or convert to a DAX measure.",
                        file=col.file,
                    )
                )

    # Rule 10, 11, 12: Report Layout checks (broken visual bindings, empty pages, duplicate visuals)
    if info.report_dir and info.report_dir.exists() and objects:
        pages = list_pages(info.report_dir, info.root, workspace_id=ws_id)
        visuals = list_visuals(info.report_dir, info.root, workspace_id=ws_id)

        for p in pages:
            if p.get("visual_count", 0) == 0:
                issues.append(
                    BpaIssue(
                        rule_id="BPA_EMPTY_REPORT_PAGE",
                        severity="info",
                        category="Report",
                        object_type="page",
                        object_name=str(p.get("display_name") or p.get("name")),
                        object_id=p.get("object_id"),
                        message=f"Report page '{p.get('display_name') or p.get('name')}' contains 0 visuals.",
                        recommendation="Remove unused blank pages or populate with visuals before publishing.",
                        file=p.get("file"),
                    )
                )

        seen_visual_signatures: dict[tuple[str, str, tuple[str, ...]], str] = {}
        for v in visuals:
            # Check for broken bindings
            for qref in v.bound_fields:
                if "." not in qref:
                    continue
                tbl, prop = qref.split(".", 1)
                t_cf = tbl.strip("'\" ").casefold()
                p_cf = prop.strip("'\"[] ").casefold()
                has_col = (t_cf, p_cf) in columns_by_key
                has_meas = (t_cf, p_cf) in measures_by_key or p_cf in measures_by_name_cf
                if not has_col and not has_meas:
                    issues.append(
                        BpaIssue(
                            rule_id="BPA_BROKEN_VISUAL_BINDING",
                            severity="error",
                            category="Report",
                            object_type="visual",
                            object_name=v.visual_id,
                            object_id=v.object_id,
                            message=f"Visual '{v.title or v.visual_id}' on page '{v.page_display_name}' binds to missing model field '{qref}'.",
                            recommendation="Use update_visual_fields to remove the stale field binding or bind the replacement field.",
                            file=v.file,
                        )
                    )

            # Check for duplicate visuals on the same page with identical type and non-empty bound fields
            if v.visual_type and v.bound_fields:
                sig = (v.page.casefold(), v.visual_type.casefold(), tuple(sorted(f.casefold() for f in v.bound_fields)))
                if sig in seen_visual_signatures:
                    first_vis_id = seen_visual_signatures[sig]
                    issues.append(
                        BpaIssue(
                            rule_id="BPA_DUPLICATE_VISUAL",
                            severity="info",
                            category="Report",
                            object_type="visual",
                            object_name=v.visual_id,
                            object_id=v.object_id,
                            message=(
                                f"Visual '{v.title or v.visual_id}' on page '{v.page_display_name}' has identical "
                                f"type ({v.visual_type}) and field bindings as visual '{first_vis_id}'."
                            ),
                            recommendation="Consolidate duplicate visuals on the same report page.",
                            file=v.file,
                        )
                    )
                else:
                    seen_visual_signatures[sig] = v.visual_id

    by_severity = {
        "error": sum(i.severity == "error" for i in issues),
        "warning": sum(i.severity == "warning" for i in issues),
        "info": sum(i.severity == "info" for i in issues),
    }
    by_category: dict[str, int] = {}
    for i in issues:
        by_category[i.category] = by_category.get(i.category, 0) + 1

    health_status = (
        "critical"
        if by_severity["error"] > 0
        else ("needs_attention" if by_severity["warning"] > 0 else "healthy")
    )

    return {
        "issue_count": len(issues),
        "by_severity": by_severity,
        "by_category": by_category,
        "health_summary": {
            "status": health_status,
            "error_count": by_severity["error"],
            "warning_count": by_severity["warning"],
            "info_count": by_severity["info"],
            "top_recommendations": [i.recommendation for i in issues[:3]],
        },
        "issues": [i.model_dump() for i in issues],
    }
