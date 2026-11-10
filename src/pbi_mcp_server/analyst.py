from __future__ import annotations

import re
from typing import Any

from .engine_a.parser import scan_model
from .engine_b.parser import json_files, list_visuals
from .models import BpaIssue, TmdlObject, WorkspaceInfo

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


def format_dax_string(dax: str) -> str:
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

    # Restore protected tokens (removing accidental space between Table and [Col])
    for idx, token in enumerate(placeholders):
        protected = protected.replace(f"__DAX_PROTECT_{idx}__", token)

    protected = re.sub(r"([A-Za-z0-9_'])\s+(\[[^\]]+\])", r"\1\2", protected)
    return protected


def generate_measure_docstring(measure_name: str, table_name: str | None, dax: str) -> str:
    """Synthesize a concise TMDL description comment from a measure's DAX formula."""
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
    return f"Computes {measure_name}{table_clause}{op_clause}{ref_clause}."


def _normalize_rel_endpoint(endpoint: str | None) -> tuple[str, str] | None:
    if not endpoint or "." not in endpoint:
        return None
    tbl, col = endpoint.split(".", 1)
    return tbl.strip("'\" "), col.strip("'\"[] ")


def find_unused_objects(info: WorkspaceInfo) -> dict[str, Any]:
    """Identify unreferenced measures and columns across the Semantic Model and Report Layout."""
    objects = scan_model(info.semantic_model_dir, info.root)
    measures = [x for x in objects if x.kind == "measure"]
    columns = [x for x in objects if x.kind in {"column", "calculated_column"}]
    relationships = [x for x in objects if x.kind == "relationship"]
    visuals = list_visuals(info.report_dir, info.root)

    # Collect all report JSON content for filter/reference fallback
    report_texts = [p.read_text(encoding="utf-8-sig").casefold() for p in json_files(info.report_dir)]
    visual_bound_cf = {f.casefold() for v in visuals for f in v.bound_fields}
    visual_prop_cf = {f.split(".")[-1].casefold() for f in visual_bound_cf}

    # Collect relationship columns: (table_cf, col_cf)
    rel_columns_cf: set[tuple[str, str]] = set()
    for rel in relationships:
        for ep in (rel.from_column, rel.to_column):
            parsed = _normalize_rel_endpoint(ep)
            if parsed:
                rel_columns_cf.add((parsed[0].casefold(), parsed[1].casefold()))

    # Collect DAX references from all measures and calculated columns
    # Each item is (owner_kind, owner_table_cf, owner_name_cf, referenced_table_cf_or_none, referenced_name_cf)
    dax_refs: list[tuple[str, str, str, str | None, str]] = []
    for obj in objects:
        if obj.kind in {"measure", "calculated_column"} and obj.expression:
            owner_tbl = (obj.table or "").casefold()
            owner_name = obj.name.casefold()
            for m in _BRACKET_REF_RE.finditer(obj.expression):
                ref_tbl = (m.group("qtable") or m.group("table_bare") or "").strip().casefold() or None
                ref_name = m.group("ref").strip().casefold()
                dax_refs.append((obj.kind, owner_tbl, owner_name, ref_tbl, ref_name))

    unused_measures: list[dict[str, Any]] = []
    for m_obj in measures:
        m_name_cf = m_obj.name.casefold()
        m_tbl_cf = (m_obj.table or "").casefold()
        qref_cf = f"{m_tbl_cf}.{m_name_cf}"

        in_visual = qref_cf in visual_bound_cf or m_name_cf in visual_prop_cf
        in_report_json = any(
            f'"{m_name_cf}"' in rtext or f".{m_name_cf}\"" in rtext for rtext in report_texts
        )
        in_other_dax = any(
            ref_name == m_name_cf and not (o_kind == "measure" and o_name == m_name_cf)
            for o_kind, _o_tbl, o_name, _ref_tbl, ref_name in dax_refs
        )

        if not (in_visual or in_report_json or in_other_dax):
            unused_measures.append({
                "name": m_obj.name,
                "table": m_obj.table,
                "file": m_obj.file,
                "line": m_obj.line,
            })

    unused_columns: list[dict[str, Any]] = []
    for c_obj in columns:
        c_name_cf = c_obj.name.casefold()
        c_tbl_cf = (c_obj.table or "").casefold()
        qref_cf = f"{c_tbl_cf}.{c_name_cf}"

        in_rel = (c_tbl_cf, c_name_cf) in rel_columns_cf
        in_visual = qref_cf in visual_bound_cf
        in_report_json = any(f'"{qref_cf}"' in rtext for rtext in report_texts)
        in_dax = any(
            ref_name == c_name_cf
            and (ref_tbl is None or ref_tbl == c_tbl_cf)
            and not (o_kind == c_obj.kind and o_tbl == c_tbl_cf and o_name == c_name_cf)
            for o_kind, o_tbl, o_name, ref_tbl, ref_name in dax_refs
        )

        if not (in_rel or in_visual or in_report_json or in_dax):
            unused_columns.append({
                "name": c_obj.name,
                "kind": c_obj.kind,
                "table": c_obj.table,
                "file": c_obj.file,
                "line": c_obj.line,
            })

    return {
        "counts": {
            "unused_measures": len(unused_measures),
            "unused_columns": len(unused_columns),
            "total_unused": len(unused_measures) + len(unused_columns),
        },
        "unused_measures": unused_measures,
        "unused_columns": unused_columns,
    }


def check_model_bpa(info: WorkspaceInfo) -> dict[str, Any]:
    """Run Best Practice Analyzer rules against the Semantic Model."""
    objects = scan_model(info.semantic_model_dir, info.root)
    issues: list[BpaIssue] = []

    columns_by_key: dict[tuple[str, str], TmdlObject] = {}
    for obj in objects:
        if obj.kind in {"column", "calculated_column"} and obj.table:
            columns_by_key[(obj.table.casefold(), obj.name.casefold())] = obj

    # Rule 1 & 2: Relationship checks
    for rel in [x for x in objects if x.kind == "relationship"]:
        if (rel.cross_filtering or "").casefold() == "bothdirections":
            issues.append(
                BpaIssue(
                    rule_id="BPA_BIDIRECTIONAL_RELATIONSHIP",
                    severity="warning",
                    category="Relationships",
                    object_type="relationship",
                    object_name=rel.name,
                    message=f"Relationship {rel.name!r} uses bidirectional cross-filtering.",
                    recommendation="Prefer single-direction filtering and use CROSSFILTER inside specific measures when needed.",
                    file=rel.file,
                )
            )
        from_ep = _normalize_rel_endpoint(rel.from_column)
        if from_ep:
            fk_col = columns_by_key.get((from_ep[0].casefold(), from_ep[1].casefold()))
            if fk_col and not fk_col.is_hidden:
                issues.append(
                    BpaIssue(
                        rule_id="BPA_UNHIDDEN_FOREIGN_KEY",
                        severity="warning",
                        category="Maintenance",
                        object_type="column",
                        object_name=fk_col.name,
                        table=fk_col.table,
                        message=f"Foreign key column '{fk_col.table}[{fk_col.name}]' used in relationship {rel.name!r} is visible.",
                        recommendation="Hide foreign key columns on the many-side of relationships so users filter via dimension tables.",
                        file=fk_col.file,
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
                    message=f"Measure '{m.name}' uses the '/' division operator directly.",
                    recommendation="Use DIVIDE(numerator, denominator) to handle division-by-zero safely.",
                    file=m.file,
                )
            )

    # Rule 5: Column data types
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
                    message=f"Column '{col.table}[{col.name}]' uses floating-point 'double' dataType.",
                    recommendation="Use 'decimal' (Fixed Decimal Currency) or 'int64' to prevent floating-point rounding inaccuracies.",
                    file=col.file,
                )
            )

    return {
        "issue_count": len(issues),
        "by_severity": {
            "error": sum(i.severity == "error" for i in issues),
            "warning": sum(i.severity == "warning" for i in issues),
            "info": sum(i.severity == "info" for i in issues),
        },
        "issues": [i.model_dump() for i in issues],
    }
