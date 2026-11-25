from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

NewlineEscape = Literal["#(lf)", "#(cr)#(lf)", "#(cr,lf)", "\n", "\r\n"]
TabEscape = Literal["#(tab)", "\t"]
FenceStyle = Literal["fenced", "unfenced"]
QuoteStyle = Literal['"', '""']
ConcatSpacing = Literal["spaced", "compact"]

_SENTINEL_PREFIX = "\x00PBI_M_EXPR_"
_SENTINEL_SUFFIX = "\x00"
_M_PLACEHOLDER_RE = re.compile(r"\{\{m:")


@dataclass(frozen=True)
class MSerializationDescriptor:
    """Lossless descriptor of how a Partition SQL string is serialized inside M."""

    connector: str
    fence_style: FenceStyle
    base_tmdl_indent: str
    common_sql_indent: str
    newline_escape: NewlineEscape
    tab_escape: TabEscape
    quote_style: QuoteStyle
    splice_quote: QuoteStyle
    concat_spacing: ConcatSpacing
    leading_newline_indent: str | None = None
    trailing_newline_indent: str | None = None
    indent_blank_lines: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_sql_hash(clean_sql: str) -> str:
    """Return a deterministic 12-char SHA-256 hex digest over Clean SQL View."""
    return hashlib.sha256(clean_sql.encode("utf-8")).hexdigest()[:12]


def format_numbered_sql(clean_sql: str, *, start_line: int = 1) -> str:
    """Format SQL with right-aligned canonical 1-based line numbers (`42 | ...`)."""
    lines = clean_sql.splitlines()
    if not lines:
        return ""
    max_line = start_line + len(lines) - 1
    width = len(str(max_line))
    return "\n".join(f"{idx:>{width}} | {line}" for idx, line in enumerate(lines, start_line))


def format_get_table_sql_text(data: dict[str, Any]) -> str:
    """Render compact, unescaped plain text for MCP get_table_sql."""
    table = data.get("table") or "<unknown>"
    partition = data.get("partition") or "<unknown>"
    mode = data.get("mode") or "unknown"
    connector = data.get("connector") or "Value.NativeQuery"
    rel_file = data.get("file") or ""
    line_count = data.get("line_count", 0)
    sql_hash = data.get("sql_hash", "")
    meta = data.get("sql_metadata") or {}
    dyn_params: list[str] = list(meta.get("dynamic_parameters") or [])
    src_tables: list[str] = list(meta.get("source_tables") or [])
    clean_sql: str = data.get("sql", "")

    header = [
        f"Table: {table}",
        f"Partition: {partition} (mode: {mode}, connector: {connector})",
        f"File: {rel_file}",
        f"Lines: {line_count} | sql_hash: {sql_hash}",
        "Dynamic M Parameters: " + (", ".join(dyn_params) if dyn_params else "none"),
    ]
    if src_tables:
        header.append("Source Tables: " + ", ".join(src_tables))

    numbered = format_numbered_sql(clean_sql)
    if numbered:
        return "\n".join(header) + "\n\n" + numbered
    return "\n".join(header)


def format_get_table_sql_json(data: dict[str, Any]) -> dict[str, Any]:
    """Return a lean structured dict for MCP get_table_sql(..., format='json')."""
    meta = data.get("sql_metadata") or {}
    return {
        "table": data.get("table"),
        "partition": data.get("partition"),
        "file": data.get("file"),
        "mode": data.get("mode"),
        "connector": data.get("connector"),
        "line_count": data.get("line_count", 0),
        "sql_hash": data.get("sql_hash", ""),
        "dynamic_parameters": list(meta.get("dynamic_parameters") or []),
        "source_tables": list(meta.get("source_tables") or []),
        "selected_fields": list(meta.get("selected_fields") or []),
        "sql": data.get("sql", ""),
    }


def _skip_ws_and_comments(text: str, i: int, limit: int | None = None) -> int:
    end = len(text) if limit is None else min(limit, len(text))
    while i < end:
        if text[i].isspace():
            i += 1
        elif text.startswith("//", i):
            nl = text.find("\n", i, end)
            i = end if nl < 0 else nl + 1
        elif text.startswith("/*", i):
            close = text.find("*/", i + 2, end)
            i = end if close < 0 else close + 2
        else:
            break
    return i


def _is_doubled_outer_string(text: str, pos: int) -> bool:
    """Detect legacy `""...""` outer-quoted M SQL literal at `pos`."""
    if not text.startswith('""', pos) or text.startswith('"""', pos):
        return False
    after = pos + 2
    while after < len(text) and text[after].isspace():
        after += 1
    if after >= len(text):
        return False
    # In standard M, `"" & expr` or `"" ,` or `"" )` is an empty string literal.
    return text[after] not in {"&", ",", ")", "]", "}"}


def _scan_m_string_token(text: str, start: int) -> int:
    """Return index immediately after an M string literal starting at `start`."""
    if _is_doubled_outer_string(text, start):
        return _scan_doubled_outer_string_end(text, start)
    i = start + 1
    while i < len(text):
        if text.startswith('""', i):
            i += 2
            continue
        if text[i] == '"':
            return i + 1
        i += 1
    raise ValueError("Unterminated M string literal.")


def _find_splice_closer_in_doubled(text: str, start: int) -> tuple[int, int] | None:
    """Scan balanced M expression inside `""...""` splice until `& "` or `& ""`."""
    depth_paren = 0
    depth_brack = 0
    depth_brace = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == '"':
            # Inside M expression (e.g. DateTime.ToText(RangeStart, "yyyy-MM-dd"))
            i = _scan_std_string_end(text, i)
            continue
        if ch == "(":
            depth_paren += 1
        elif ch == ")":
            if depth_paren == 0:
                return None
            depth_paren -= 1
        elif ch == "[":
            depth_brack += 1
        elif ch == "]":
            if depth_brack == 0:
                return None
            depth_brack -= 1
        elif ch == "{":
            depth_brace += 1
        elif ch == "}":
            if depth_brace == 0:
                return None
            depth_brace -= 1
        elif ch == "&" and depth_paren == 0 and depth_brack == 0 and depth_brace == 0:
            j = i + 1
            while j < len(text) and text[j] in " \t":
                j += 1
            if text.startswith('""', j):
                return i, j + 2
            if j < len(text) and text[j] == '"':
                return i, j + 1
        elif ch == "\n" and depth_paren == 0 and depth_brack == 0 and depth_brace == 0:
            return None
        i += 1
    return None


def _scan_std_string_end(text: str, start: int) -> int:
    i = start + 1
    while i < len(text):
        if text.startswith('""', i):
            i += 2
            continue
        if text[i] == '"':
            return i + 1
        i += 1
    raise ValueError("Unterminated M string literal.")


def _scan_doubled_outer_string_end(text: str, start: int) -> int:
    i = start + 2
    while i < len(text):
        if text.startswith('""', i) and not text.startswith('"""', i):
            return i + 2
        if text[i] == '"':
            # Check for `"&Expr&"` or `" & Expr & "` splice inside `""...""`
            j = i + 1
            while j < len(text) and text[j] in " \t":
                j += 1
            if j < len(text) and text[j] == "&":
                closer = _find_splice_closer_in_doubled(text, j + 1)
                if closer is not None:
                    _, resume = closer
                    i = resume
                    continue
        i += 1
    raise ValueError("Unterminated doubled-quote M string literal.")


def _scan_m_expr_end(
    text: str,
    start: int,
    *,
    stop_chars: set[str],
) -> int:
    """Scan an M expression from `start` until a top-level char in `stop_chars`."""
    depth_paren = 0
    depth_brack = 0
    depth_brace = 0
    i = start
    while i < len(text):
        if text.startswith("//", i):
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl + 1
            continue
        if text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = len(text) if close < 0 else close + 2
            continue
        if text[i] == '"':
            i = _scan_m_string_token(text, i)
            continue
        ch = text[i]
        if ch == "(":
            depth_paren += 1
        elif ch == ")":
            if depth_paren == 0 and depth_brack == 0 and depth_brace == 0 and ")" in stop_chars:
                return i
            depth_paren -= 1
            if depth_paren < 0:
                raise ValueError("Unbalanced closing ')' while scanning M expression.")
        elif ch == "[":
            depth_brack += 1
        elif ch == "]":
            if depth_paren == 0 and depth_brack == 0 and depth_brace == 0 and "]" in stop_chars:
                return i
            depth_brack -= 1
            if depth_brack < 0:
                raise ValueError("Unbalanced closing ']' while scanning M expression.")
        elif ch == "{":
            depth_brace += 1
        elif ch == "}":
            if depth_paren == 0 and depth_brack == 0 and depth_brace == 0 and "}" in stop_chars:
                return i
            depth_brace -= 1
            if depth_brace < 0:
                raise ValueError("Unbalanced closing '}' while scanning M expression.")
        elif (
            ch in stop_chars
            and depth_paren == 0
            and depth_brack == 0
            and depth_brace == 0
        ):
            return i
        i += 1
    raise ValueError("Could not locate delimiter while scanning M expression.")


def _parse_call_arguments(text: str, open_paren: int) -> list[tuple[int, int]]:
    """Return trimmed `(start, end)` spans for top-level arguments inside `(...)`."""
    args: list[tuple[int, int]] = []
    pos = open_paren + 1
    while pos < len(text):
        arg_start = _skip_ws_and_comments(text, pos)
        if arg_start < len(text) and text[arg_start] == ")":
            break
        arg_end = _scan_m_expr_end(text, arg_start, stop_chars={",", ")"})
        trimmed_end = arg_end
        while trimmed_end > arg_start and text[trimmed_end - 1].isspace():
            trimmed_end -= 1
        args.append((arg_start, trimmed_end))
        if text[arg_end] == ")":
            break
        pos = arg_end + 1
    return args


def _find_record_query_value_span(
    text: str, rec_start: int, rec_end: int
) -> tuple[int, int] | None:
    """Inside `[ ... ]`, find the value span of top-level `Query = <expr>`."""
    inner_start = _skip_ws_and_comments(text, rec_start + 1, rec_end)
    pos = inner_start
    while pos < rec_end - 1:
        pos = _skip_ws_and_comments(text, pos, rec_end - 1)
        if pos >= rec_end - 1:
            break
        field_match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*", text[pos : rec_end - 1])
        if field_match:
            val_start = pos + field_match.end()
            val_end = _scan_m_expr_end(text, val_start, stop_chars={",", "]"})
            trimmed_end = val_end
            while trimmed_end > val_start and text[trimmed_end - 1].isspace():
                trimmed_end -= 1
            if field_match.group(1) == "Query":
                return val_start, trimmed_end
            pos = val_end + 1
        else:
            val_end = _scan_m_expr_end(text, pos, stop_chars={",", "]"})
            pos = val_end + 1
    return None


def _expr_contains_string_literal(text: str, start: int, end: int) -> bool:
    """Return True if an M expression span contains a quoted string literal."""
    i = start
    while i < end:
        if text.startswith("//", i):
            nl = text.find("\n", i, end)
            i = end if nl < 0 else nl + 1
            continue
        if text.startswith("/*", i):
            close = text.find("*/", i + 2, end)
            i = end if close < 0 else close + 2
            continue
        if text[i] == '"':
            return True
        i += 1
    return False


def _find_m_call_sites(source: str, func_names: set[str]) -> list[tuple[str, int]]:
    """Find `(func_name, open_paren_index)` outside strings/comments."""
    sites: list[tuple[str, int]] = []
    i = 0
    while i < len(source):
        if source.startswith("//", i):
            nl = source.find("\n", i)
            i = len(source) if nl < 0 else nl + 1
            continue
        if source.startswith("/*", i):
            close = source.find("*/", i + 2)
            i = len(source) if close < 0 else close + 2
            continue
        if source[i] == '"':
            i = _scan_m_string_token(source, i)
            continue
        m = re.match(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", source[i:])
        if m:
            ident = m.group(0)
            after = _skip_ws_and_comments(source, i + len(ident))
            if ident in func_names and after < len(source) and source[after] == "(":
                sites.append((ident, after))
            i += len(ident)
            continue
        i += 1
    return sites


def _find_generic_query_record_call(source: str) -> tuple[str, int, int] | None:
    """Find `Connector.Fn(..., [..., Query = <sql>, ...])` in M source."""
    i = 0
    last_call: str | None = None
    paren_stack: list[str] = []
    while i < len(source):
        if source.startswith("//", i):
            nl = source.find("\n", i)
            i = len(source) if nl < 0 else nl + 1
            continue
        if source.startswith("/*", i):
            close = source.find("*/", i + 2)
            i = len(source) if close < 0 else close + 2
            continue
        if source[i] == '"':
            i = _scan_m_string_token(source, i)
            continue
        m = re.match(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", source[i:])
        if m:
            ident = m.group(0)
            after = _skip_ws_and_comments(source, i + len(ident))
            if after < len(source) and source[after] == "(":
                last_call = ident
                paren_stack.append(ident)
                i = after + 1
                continue
            i += len(ident)
            continue
        ch = source[i]
        if ch == "(":
            paren_stack.append(last_call or "M.Query")
        elif ch == ")" and paren_stack:
            paren_stack.pop()
        elif ch == "[":
            rec_end = _scan_m_expr_end(source, i + 1, stop_chars={"]"}) + 1
            q_span = _find_record_query_value_span(source, i, rec_end)
            if q_span is not None and _expr_contains_string_literal(source, q_span[0], q_span[1]):
                connector = paren_stack[-1] if paren_stack else (last_call or "M.Query")
                return connector, q_span[0], q_span[1]
            i = rec_end
            continue
        i += 1
    return None


def extract_m_sql_target(
    source: str, absolute_start: int
) -> tuple[str, int, int] | None:
    """Locate connector name and absolute `(sql_start, sql_end)` inside an M `source` block."""
    # 1. Value.NativeQuery(target, sql, ...)
    for _, open_paren in _find_m_call_sites(source, {"Value.NativeQuery"}):
        args = _parse_call_arguments(source, open_paren)
        if len(args) >= 2 and _expr_contains_string_literal(source, args[1][0], args[1][1]):
            return "Value.NativeQuery", absolute_start + args[1][0], absolute_start + args[1][1]

    # 2. Databricks.Query(host, httpPath, sql_or_options, [options])
    for _, open_paren in _find_m_call_sites(source, {"Databricks.Query"}):
        args = _parse_call_arguments(source, open_paren)
        for a_start, a_end in args:
            if source[a_start] == "[":
                q_span = _find_record_query_value_span(source, a_start, a_end)
                if q_span is not None and _expr_contains_string_literal(
                    source, q_span[0], q_span[1]
                ):
                    return (
                        "Databricks.Query",
                        absolute_start + q_span[0],
                        absolute_start + q_span[1],
                    )
        if (
            len(args) >= 3
            and not source[args[2][0]].startswith("[")
            and _expr_contains_string_literal(source, args[2][0], args[2][1])
        ):
            return "Databricks.Query", absolute_start + args[2][0], absolute_start + args[2][1]

    # 3. Odbc.Query / OleDb.Query(conn, sql_or_options, [options])
    for conn_name, open_paren in _find_m_call_sites(source, {"Odbc.Query", "OleDb.Query"}):
        args = _parse_call_arguments(source, open_paren)
        for a_start, a_end in args:
            if source[a_start] == "[":
                q_span = _find_record_query_value_span(source, a_start, a_end)
                if q_span is not None and _expr_contains_string_literal(
                    source, q_span[0], q_span[1]
                ):
                    return (
                        conn_name,
                        absolute_start + q_span[0],
                        absolute_start + q_span[1],
                    )
        if (
            len(args) >= 2
            and not source[args[1][0]].startswith("[")
            and _expr_contains_string_literal(source, args[1][0], args[1][1])
        ):
            return conn_name, absolute_start + args[1][0], absolute_start + args[1][1]

    # 4. Generic connector options record `[..., Query = <sql>, ...]` (e.g. Sql.Database)
    generic = _find_generic_query_record_call(source)
    if generic is not None:
        conn_name, rel_start, rel_end = generic
        return conn_name, absolute_start + rel_start, absolute_start + rel_end

    return None


def _tokenize_doubled_outer_expr(
    m_expr: str,
) -> tuple[list[tuple[str, str]], QuoteStyle, ConcatSpacing]:
    """Tokenize `""...""` outer-quoted M SQL with optional `"&Expr&"` splices."""
    inner = m_expr[2:-2]
    tokens: list[tuple[str, str]] = []
    splice_quote: QuoteStyle = '""'
    concat_spacing: ConcatSpacing = "compact"
    saw_splice = False

    buf: list[str] = []
    i = 0
    while i < len(inner):
        if inner[i] == '"' and not inner.startswith('""', i):
            j = i + 1
            had_space_before = j < len(inner) and inner[j] in " \t"
            while j < len(inner) and inner[j] in " \t":
                j += 1
            if j < len(inner) and inner[j] == "&":
                k = j + 1
                while k < len(inner) and inner[k] in " \t":
                    k += 1
                closer = _find_splice_closer_in_doubled(inner, j + 1)
                if closer is not None:
                    amp_end, resume = closer
                    if buf:
                        tokens.append(("str", "".join(buf)))
                        buf = []
                    expr_text = inner[j + 1 : amp_end].strip()
                    tokens.append(("expr", expr_text))
                    if not saw_splice:
                        saw_splice = True
                        splice_quote = '""' if inner.startswith('""', resume - 2) else '"'
                        concat_spacing = (
                            "spaced"
                            if (had_space_before or (k > j + 1))
                            else "compact"
                        )
                    i = resume
                    continue
        buf.append(inner[i])
        i += 1
    if buf:
        tokens.append(("str", "".join(buf)))
    return tokens, splice_quote, concat_spacing


def _tokenize_standard_m_expr(m_expr: str) -> tuple[list[tuple[str, str]], ConcatSpacing]:
    """Tokenize standard `"..." & Expr & "..."` M string concatenation."""
    tokens: list[tuple[str, str]] = []
    concat_spacing: ConcatSpacing = "spaced"
    saw_concat = False

    i = 0
    n = len(m_expr)
    while i < n:
        i = _skip_ws_and_comments(m_expr, i)
        if i >= n:
            break
        if m_expr[i] == '"':
            end = _scan_std_string_end(m_expr, i)
            seg = m_expr[i + 1 : end - 1]
            if tokens and tokens[-1][0] == "str":
                tokens[-1] = ("str", tokens[-1][1] + seg)
            else:
                tokens.append(("str", seg))
            i = end
        else:
            expr_end = _scan_m_expr_end_or_eof(m_expr, i)
            expr_text = m_expr[i:expr_end].strip()
            if expr_text:
                tokens.append(("expr", expr_text))
            i = expr_end

        after = _skip_ws_and_comments(m_expr, i)
        if after < n and m_expr[after] == "&":
            ws_before = after > i
            after_amp = after + 1
            ws_after = after_amp < n and m_expr[after_amp] in " \t"
            if not saw_concat:
                saw_concat = True
                concat_spacing = "spaced" if (ws_before or ws_after) else "compact"
            i = after_amp
        else:
            i = after

    return tokens, concat_spacing


def _scan_m_expr_end_or_eof(text: str, start: int) -> int:
    depth_paren = 0
    depth_brack = 0
    depth_brace = 0
    i = start
    while i < len(text):
        if text.startswith("//", i):
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl + 1
            continue
        if text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = len(text) if close < 0 else close + 2
            continue
        if text[i] == '"':
            i = _scan_std_string_end(text, i)
            continue
        ch = text[i]
        if ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren = max(0, depth_paren - 1)
        elif ch == "[":
            depth_brack += 1
        elif ch == "]":
            depth_brack = max(0, depth_brack - 1)
        elif ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace = max(0, depth_brace - 1)
        elif ch == "&" and depth_paren == 0 and depth_brack == 0 and depth_brace == 0:
            return i
        i += 1
    return len(text)


def _longest_common_ws_prefix(lines: list[str]) -> str:
    if not lines:
        return ""
    ws_prefixes: list[str] = []
    for line in lines:
        stripped = line.lstrip(" \t")
        ws_prefixes.append(line[: len(line) - len(stripped)])
    prefix = ws_prefixes[0]
    for candidate in ws_prefixes[1:]:
        limit = min(len(prefix), len(candidate))
        idx = 0
        while idx < limit and prefix[idx] == candidate[idx]:
            idx += 1
        prefix = prefix[:idx]
        if not prefix:
            break
    return prefix


def _unescape_m_string_body(raw: str) -> str:
    """Decode M string escapes (`#(lf)`, `#(cr)#(lf)`, `#(tab)`, `#(#)`, `""`) in one pass."""
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        if raw.startswith("\r\n", i):
            out.append("\n")
            i += 2
            continue
        if raw.startswith('""', i):
            out.append('"')
            i += 2
            continue
        if raw.startswith("#(", i):
            close = raw.find(")", i + 2, min(n, i + 40))
            if close > i + 2 and "\n" not in raw[i + 2 : close]:
                codes = [c.strip() for c in raw[i + 2 : close].split(",")]
                decoded_chars: list[str] = []
                valid = True
                for code in codes:
                    low = code.lower()
                    if low == "lf":
                        decoded_chars.append("\n")
                    elif low == "cr":
                        decoded_chars.append("\r")
                    elif low == "tab":
                        decoded_chars.append("\t")
                    elif low == "#":
                        decoded_chars.append("#")
                    elif re.fullmatch(r"[0-9a-fA-F]{4}|[0-9a-fA-F]{8}", code):
                        decoded_chars.append(chr(int(code, 16)))
                    else:
                        valid = False
                        break
                if valid:
                    joined = "".join(decoded_chars).replace("\r\n", "\n")
                    out.append(joined)
                    i = close + 1
                    continue
        out.append(raw[i])
        i += 1
    return "".join(out).replace("\r\n", "\n")


def _detect_newline_escape(raw: str, fence_style: FenceStyle) -> NewlineEscape:
    if "#(cr)#(lf)" in raw:
        return "#(cr)#(lf)"
    if "#(cr,lf)" in raw:
        return "#(cr,lf)"
    if "#(lf)" in raw:
        return "#(lf)"
    if "\r\n" in raw:
        return "\r\n"
    if "\n" in raw:
        return "\n"
    return "#(lf)" if fence_style == "unfenced" else "#(lf)"


def _detect_tab_escape(raw: str) -> TabEscape:
    return "#(tab)" if "#(tab)" in raw else "\t"


def decode_m_sql_expression(
    m_expr: str,
    *,
    connector: str,
    fence_style: FenceStyle,
    base_tmdl_indent: str,
) -> tuple[str, MSerializationDescriptor, list[str]]:
    """Decode an M SQL string/concat expression into `(clean_sql, descriptor, dynamic_params)`."""
    trimmed = m_expr.strip()
    doubled_outer = _is_doubled_outer_string(trimmed, 0) and trimmed.endswith('""')
    if doubled_outer and not re.search(r'&\s*""$', trimmed):
        quote_style: QuoteStyle = '""'
        tokens, splice_quote, concat_spacing = _tokenize_doubled_outer_expr(trimmed)
    else:
        quote_style = '"'
        splice_quote = '"'
        tokens, concat_spacing = _tokenize_standard_m_expr(trimmed)

    dynamic_params: list[str] = []
    raw_parts: list[str] = []
    expr_list: list[str] = []
    for kind, value in tokens:
        if kind == "str":
            raw_parts.append(value)
        else:
            idx = len(expr_list)
            expr_list.append(value)
            if value not in dynamic_params:
                dynamic_params.append(value)
            raw_parts.append(f"{_SENTINEL_PREFIX}{idx}{_SENTINEL_SUFFIX}")

    combined_raw = "".join(raw_parts)
    newline_escape = _detect_newline_escape(combined_raw, fence_style)
    tab_escape = _detect_tab_escape(combined_raw)

    leading_newline_indent: str | None = None
    trailing_newline_indent: str | None = None
    common_sql_indent = ""
    indent_blank_lines = False

    if "\n" in combined_raw:
        raw_eol = "\r\n" if "\r\n" in combined_raw else "\n"
        phys_lines = combined_raw.replace("\r\n", "\n").split("\n")

        # Determine non-empty content lines in phys_lines[1:]
        candidate_lines = phys_lines[1:]
        non_ws_tail = [ln for ln in candidate_lines if ln.strip(" \t") != ""]
        common_sql_indent = _longest_common_ws_prefix(non_ws_tail)

        # Check if opening quote was followed immediately by newline
        if phys_lines[0] == "" and len(phys_lines) >= 2:
            leading_ws = phys_lines[1][: len(phys_lines[1]) - len(phys_lines[1].lstrip(" \t"))]
            # Only strip the common indent portion from line 1
            leading_newline_indent = raw_eol + common_sql_indent
            if leading_ws.startswith(common_sql_indent):
                phys_lines = phys_lines[1:]

        # Check if closing quote sits on its own whitespace-only line
        if len(phys_lines) >= 2 and phys_lines[-1].strip(" \t") == "":
            trailing_newline_indent = raw_eol + phys_lines[-1]
            phys_lines = phys_lines[:-1]

        # Strip common_sql_indent from phys_lines[1:] (or all if leading_newline_indent stripped)
        start_strip_idx = 0 if leading_newline_indent is not None else 1
        stripped_lines: list[str] = []
        for idx, line in enumerate(phys_lines):
            if idx < start_strip_idx:
                stripped_lines.append(line)
                continue
            if line.strip(" \t") == "":
                if common_sql_indent and line.startswith(common_sql_indent):
                    indent_blank_lines = True
                stripped_lines.append("")
            elif common_sql_indent and line.startswith(common_sql_indent):
                stripped_lines.append(line[len(common_sql_indent) :])
            else:
                stripped_lines.append(line)
        combined_raw = "\n".join(stripped_lines)

    decoded = _unescape_m_string_body(combined_raw)
    for idx, expr_text in enumerate(expr_list):
        sentinel = f"{_SENTINEL_PREFIX}{idx}{_SENTINEL_SUFFIX}"
        decoded = decoded.replace(sentinel, f"{{{{m:{expr_text}}}}}")

    descriptor = MSerializationDescriptor(
        connector=connector,
        fence_style=fence_style,
        base_tmdl_indent=base_tmdl_indent,
        common_sql_indent=common_sql_indent,
        newline_escape=newline_escape,
        tab_escape=tab_escape,
        quote_style=quote_style,
        splice_quote=splice_quote,
        concat_spacing=concat_spacing,
        leading_newline_indent=leading_newline_indent,
        trailing_newline_indent=trailing_newline_indent,
        indent_blank_lines=indent_blank_lines,
    )
    return decoded, descriptor, dynamic_params


def split_clean_sql_segments(clean_sql: str) -> list[tuple[str, str]]:
    """Split Clean SQL View into `[('text', ...), ('expr', ...)]` segments."""
    segments: list[tuple[str, str]] = []
    pos = 0
    n = len(clean_sql)
    while pos < n:
        match = _M_PLACEHOLDER_RE.search(clean_sql, pos)
        if not match:
            segments.append(("text", clean_sql[pos:]))
            break
        if match.start() > pos:
            segments.append(("text", clean_sql[pos : match.start()]))
        expr_start = match.end()
        brace_depth = 0
        in_str = False
        i = expr_start
        closed_at: int | None = None
        while i < n:
            ch = clean_sql[i]
            if in_str:
                if clean_sql.startswith('""', i):
                    i += 2
                    continue
                if ch == '"':
                    in_str = False
                i += 1
                continue
            if ch == '"':
                in_str = True
                i += 1
                continue
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                if brace_depth == 0 and i + 1 < n and clean_sql[i + 1] == "}":
                    closed_at = i
                    break
                brace_depth = max(0, brace_depth - 1)
            i += 1
        if closed_at is None:
            # Unclosed placeholder: keep remainder as literal text
            segments.append(("text", clean_sql[match.start() :]))
            break
        segments.append(("expr", clean_sql[expr_start:closed_at].strip()))
        pos = closed_at + 2
    return segments


def _encode_text_chunk(
    text: str,
    descriptor: MSerializationDescriptor,
    *,
    is_first_line_at_start: bool,
) -> str:
    # Escape `#(` -> `#(#)(` and `"` -> `""`
    escaped = text.replace("#(", "#(#)(").replace('"', '""')
    if descriptor.tab_escape == "#(tab)":
        escaped = escaped.replace("\t", "#(tab)")

    nl_esc = descriptor.newline_escape
    if nl_esc in {"#(lf)", "#(cr)#(lf)", "#(cr,lf)"}:
        return escaped.replace("\n", nl_esc)

    # Physical newline inside M string literal
    lines = escaped.split("\n")
    if len(lines) == 1:
        return escaped
    out_lines: list[str] = [lines[0]]
    for line in lines[1:]:
        if line == "" and not descriptor.indent_blank_lines:
            out_lines.append("")
        else:
            out_lines.append(descriptor.common_sql_indent + line)
    return nl_esc.join(out_lines)


def encode_sql_to_m(clean_sql: str, descriptor: MSerializationDescriptor) -> str:
    """Re-encode Clean SQL View back into the partition's original M serialization format."""
    segments = split_clean_sql_segments(clean_sql)
    sep = " " if descriptor.concat_spacing == "spaced" else ""
    lead = descriptor.leading_newline_indent or ""
    trail = descriptor.trailing_newline_indent or ""

    if descriptor.quote_style == '""':
        parts: list[str] = ['""', lead]
        sq = descriptor.splice_quote
        for kind, val in segments:
            if kind == "text":
                parts.append(
                    _encode_text_chunk(val, descriptor, is_first_line_at_start=(len(parts) == 2))
                )
            else:
                parts.append(f"{sq}{sep}&{sep}{val}{sep}&{sep}{sq}")
        parts.append(trail)
        parts.append('""')
        return "".join(parts)

    # Standard M `"..."` + `& Expr &` serialization
    if not segments:
        return f'"{lead}{trail}"'

    if len(segments) == 1 and segments[0][0] == "expr":
        return segments[0][1]

    m_parts: list[str] = []
    for idx, (kind, val) in enumerate(segments):
        if kind == "text":
            if val == "" and len(segments) > 1:
                continue
            body = _encode_text_chunk(val, descriptor, is_first_line_at_start=(idx == 0))
            if idx == 0:
                body = lead + body
            if idx == len(segments) - 1:
                body = body + trail
            m_parts.append(f'"{body}"')
        else:
            m_parts.append(val)

    if not m_parts:
        return f'"{lead}{trail}"'

    joined = f"{sep}&{sep}".join(m_parts)
    if segments[-1][0] == "expr" and trail:
        joined += f'{sep}&{sep}"{trail}"'
    if segments[0][0] == "expr" and lead:
        joined = f'"{lead}"{sep}&{sep}' + joined
    return joined
