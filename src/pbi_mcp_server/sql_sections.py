"""Databricks-aware SQL Section scanner, clause landmark scanner, and metadata extractor."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Literal

from .m_codec import scan_m_placeholder_end

TokenKind = Literal[
    "word",
    "ident",
    "string",
    "placeholder",
    "lparen",
    "rparen",
    "comma",
    "dot",
    "star",
    "other",
]

_NON_TABLE_KEYWORDS: frozenset[str] = frozenset({
    "SELECT",
    "LATERAL",
    "UNNEST",
    "VALUES",
    "TABLE",
    "ONLY",
    "WHERE",
    "JOIN",
    "INNER",
    "LEFT",
    "RIGHT",
    "FULL",
    "CROSS",
    "OUTER",
    "ON",
    "USING",
    "GROUP",
    "ORDER",
    "HAVING",
    "QUALIFY",
    "UNION",
    "INTERSECT",
    "EXCEPT",
    "LIMIT",
    "FETCH",
    "SET",
})

# SQL functions whose argument syntax includes the keyword FROM (not a table source).
_FROM_KEYWORD_FUNCTIONS: frozenset[str] = frozenset({
    "EXTRACT",
    "TRIM",
    "SUBSTRING",
    "OVERLAY",
    "POSITION",
})

# Top-level single-word clause keywords that terminate a SELECT projection list.
_SELECT_TERMINATORS: frozenset[str] = frozenset({
    "FROM",
    "WHERE",
    "GROUP",
    "HAVING",
    "QUALIFY",
    "ORDER",
    "UNION",
    "INTERSECT",
    "EXCEPT",
    "LIMIT",
    "FETCH",
    "WINDOW",
})


@dataclass(frozen=True, slots=True)
class SqlToken:
    kind: TokenKind
    text: str
    start: int
    end: int

    @property
    def upper(self) -> str:
        return self.text.upper() if self.kind == "word" else ""


@dataclass(frozen=True, slots=True)
class SqlSection:
    """A named, addressable region of Partition SQL (top-level CTE or `main`)."""

    name: str
    kind: Literal["cte", "main"]
    start_line: int
    end_line: int
    line_count: int
    outer_span: tuple[int, int]
    inner_span: tuple[int, int]
    references_ctes: tuple[str, ...] = ()
    source_tables: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "line_count": self.line_count,
            "outer_span": {"start": self.outer_span[0], "end": self.outer_span[1]},
            "inner_span": {"start": self.inner_span[0], "end": self.inner_span[1]},
            "references_ctes": list(self.references_ctes),
            "source_tables": list(self.source_tables),
        }


@dataclass(frozen=True, slots=True)
class ClauseLandmark:
    """Top-level SQL clause landmark for non-CTE queries."""

    clause: str
    start_line: int
    end_line: int
    line_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "clause": self.clause,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "line_count": self.line_count,
        }


@dataclass(frozen=True, slots=True)
class SqlStructure:
    """Parsed structural overview of a Clean SQL View query."""

    has_ctes: bool
    sections: tuple[SqlSection, ...]
    clauses: tuple[ClauseLandmark, ...]
    source_tables: tuple[str, ...]
    selected_fields: tuple[str, ...]


class LineIndex:
    """Fast 1-based line lookup for character offsets in Clean SQL View."""

    def __init__(self, text: str) -> None:
        starts = [0]
        pos = text.find("\n")
        while pos != -1:
            starts.append(pos + 1)
            pos = text.find("\n", pos + 1)
        self._starts = starts

    @property
    def line_starts(self) -> list[int]:
        return self._starts

    def line_of(self, offset: int) -> int:
        if offset <= 0:
            return 1
        return bisect_right(self._starts, offset)


def unquote_sql_identifier(raw: str) -> str:
    """Strip outer SQL quoting delimiters (backticks, double quotes, brackets)."""
    s = raw.strip()
    if len(s) >= 2:
        if s[0] == "`" and s[-1] == "`":
            return s[1:-1].replace("``", "`")
        if s[0] == '"' and s[-1] == '"':
            return s[1:-1].replace('""', '"')
        if s[0] == "[" and s[-1] == "]":
            return s[1:-1].replace("]]", "]")
        if s[0] == "'" and s[-1] == "'":
            return s[1:-1].replace("''", "'")
    return s


@dataclass(frozen=True, slots=True)
class _RawCte:
    name: str
    outer_start: int
    outer_end: int
    inner_start: int
    inner_end: int
    body_tokens: list[SqlToken]


def _scan_quoted_end(sql: str, start: int, quote: str, *, allow_backslash: bool) -> int:
    """Scan a quoted SQL string/identifier starting at `start`."""
    n = len(sql)
    i = start + 1
    while i < n:
        ch = sql[i]
        if allow_backslash and ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return n


def tokenize_sql(sql: str) -> list[SqlToken]:
    """Tokenize Databricks/ANSI SQL while skipping whitespace and comments."""
    tokens: list[SqlToken] = []
    n = len(sql)
    i = 0
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        # Line comment: -- ...
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            nl = sql.find("\n", i + 2)
            i = n if nl == -1 else nl + 1
            continue
        # Block comment: /* ... */
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            close = sql.find("*/", i + 2)
            i = n if close == -1 else close + 2
            continue
        # Dynamic M Parameter placeholder: {{m:<expr>}}
        if sql.startswith("{{m:", i):
            found_end = scan_m_placeholder_end(sql, i)
            end = n if found_end < 0 else found_end
            tokens.append(SqlToken("placeholder", sql[i:end], i, end))
            i = end
            continue
        # Single-quoted string literal
        if ch == "'":
            end = _scan_quoted_end(sql, i, "'", allow_backslash=True)
            tokens.append(SqlToken("string", sql[i:end], i, end))
            i = end
            continue
        # Double-quoted identifier / literal
        if ch == '"':
            end = _scan_quoted_end(sql, i, '"', allow_backslash=True)
            tokens.append(SqlToken("ident", sql[i:end], i, end))
            i = end
            continue
        # Backtick identifier (Databricks/Spark SQL)
        if ch == "`":
            end = _scan_quoted_end(sql, i, "`", allow_backslash=False)
            tokens.append(SqlToken("ident", sql[i:end], i, end))
            i = end
            continue
        # Bracketed identifier (T-SQL)
        if ch == "[":
            end = i + 1
            while end < n:
                if sql[end] == "]":
                    if end + 1 < n and sql[end + 1] == "]":
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            tokens.append(SqlToken("ident", sql[i:end], i, end))
            i = end
            continue
        if ch == "(":
            tokens.append(SqlToken("lparen", ch, i, i + 1))
            i += 1
            continue
        if ch == ")":
            tokens.append(SqlToken("rparen", ch, i, i + 1))
            i += 1
            continue
        if ch == ",":
            tokens.append(SqlToken("comma", ch, i, i + 1))
            i += 1
            continue
        if ch == ".":
            tokens.append(SqlToken("dot", ch, i, i + 1))
            i += 1
            continue
        if ch == "*":
            tokens.append(SqlToken("star", ch, i, i + 1))
            i += 1
            continue
        if ch.isalpha() or ch == "_":
            end = i + 1
            while end < n and (sql[end].isalnum() or sql[end] in "_$"):
                end += 1
            tokens.append(SqlToken("word", sql[i:end], i, end))
            i = end
            continue
        tokens.append(SqlToken("other", ch, i, i + 1))
        i += 1
    return tokens


def find_matching_rparen_token(tokens: list[SqlToken], lparen_idx: int) -> int | None:
    """Return token index of the `)` matching `tokens[lparen_idx]`."""
    depth = 0
    for idx in range(lparen_idx, len(tokens)):
        kind = tokens[idx].kind
        if kind == "lparen":
            depth += 1
        elif kind == "rparen":
            depth -= 1
            if depth == 0:
                return idx
    return None


def extract_table_refs(
    tokens: list[SqlToken],
    *,
    cte_names_by_cf: dict[str, str],
    self_name_cf: str | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Extract `(references_ctes, external_source_tables)` from a token slice."""
    cte_refs: list[str] = []
    source_tables: list[str] = []
    paren_fn_stack: list[str] = []
    n = len(tokens)
    i = 0

    while i < n:
        tok = tokens[i]
        if tok.kind == "lparen":
            prev_fn = tokens[i - 1].upper if i > 0 and tokens[i - 1].kind == "word" else ""
            paren_fn_stack.append(prev_fn)
            i += 1
            continue
        if tok.kind == "rparen":
            if paren_fn_stack:
                paren_fn_stack.pop()
            i += 1
            continue

        if tok.kind == "word" and tok.upper in {"FROM", "JOIN"}:
            # Skip FROM inside EXTRACT(YEAR FROM col), TRIM(... FROM col), etc.
            if (
                tok.upper == "FROM"
                and paren_fn_stack
                and paren_fn_stack[-1] in _FROM_KEYWORD_FUNCTIONS
            ):
                i += 1
                continue

            j = i + 1
            if j >= n:
                break
            nxt = tokens[j]
            if nxt.kind not in {"word", "ident", "placeholder"}:
                i += 1
                continue
            if nxt.kind == "word" and nxt.upper in _NON_TABLE_KEYWORDS:
                i += 1
                continue

            # Consume dot-qualified name parts: catalog.schema.table or {{m:Cat}}.{{m:Sch}}.tbl
            parts: list[SqlToken] = [nxt]
            k = j + 1
            while k + 1 < n and tokens[k].kind == "dot":
                part_tok = tokens[k + 1]
                if part_tok.kind in {"word", "ident", "placeholder"}:
                    parts.append(part_tok)
                    k += 2
                else:
                    break

            # Table-valued function call like UNNEST(...) or TABLE(...) — skip if immediately `(`
            if k < n and tokens[k].kind == "lparen":
                i = k
                continue

            if len(parts) == 1 and parts[0].kind in {"word", "ident"}:
                unquoted = unquote_sql_identifier(parts[0].text)
                cf = unquoted.casefold()
                if cf in cte_names_by_cf:
                    canonical = cte_names_by_cf[cf]
                    if cf != self_name_cf and canonical not in cte_refs:
                        cte_refs.append(canonical)
                    i = k
                    continue

            raw_name = ".".join(p.text for p in parts)
            if raw_name not in source_tables:
                source_tables.append(raw_name)
            i = k
            continue

        i += 1

    return tuple(cte_refs), tuple(source_tables)


def _extract_selected_fields_from_tokens(tokens: list[SqlToken]) -> tuple[str, ...]:
    """Extract projected column/alias names from the top-level SELECT clause of `main`."""
    n = len(tokens)
    # Find the first top-level SELECT token (unwrapping outer parens if the query is `(SELECT ...)`)
    select_idx: int | None = None
    depth = 0
    for idx, tok in enumerate(tokens):
        if tok.kind == "lparen":
            depth += 1
        elif tok.kind == "rparen":
            depth = max(0, depth - 1)
        elif tok.kind == "word" and tok.upper == "SELECT" and depth == 0:
            select_idx = idx
            break
    if select_idx is None and tokens and tokens[0].kind == "lparen":
        end_p = find_matching_rparen_token(tokens, 0)
        if end_p is not None:
            return _extract_selected_fields_from_tokens(tokens[1:end_p])
    if select_idx is None:
        return ()

    i = select_idx + 1
    # Skip DISTINCT / ALL / TOP N
    while i < n and tokens[i].kind == "word" and tokens[i].upper in {"DISTINCT", "ALL"}:
        i += 1
    if i < n and tokens[i].kind == "word" and tokens[i].upper == "TOP":
        i += 1
        if i < n and tokens[i].kind == "lparen":
            close = find_matching_rparen_token(tokens, i)
            i = (close + 1) if close is not None else i + 1
        elif i < n:
            i += 1

    # Collect projection items up to top-level FROM (or another clause terminator / semicolon)
    items: list[list[SqlToken]] = []
    current: list[SqlToken] = []
    depth = 0
    while i < n:
        tok = tokens[i]
        if tok.kind == "lparen":
            depth += 1
            current.append(tok)
        elif tok.kind == "rparen":
            if depth == 0:
                break
            depth -= 1
            current.append(tok)
        elif depth == 0 and tok.kind == "comma":
            if current:
                items.append(current)
                current = []
        elif depth == 0 and (
            (tok.kind == "word" and tok.upper in _SELECT_TERMINATORS) or tok.text == ";"
        ):
            break
        else:
            current.append(tok)
        i += 1
    if current:
        items.append(current)

    fields: list[str] = []
    for item in items:
        if not item:
            continue
        last = item[-1]
        if last.kind == "star":
            if "*" not in fields:
                fields.append("*")
            continue
        # Check for explicit `... AS <alias>` at depth 0
        alias: str | None = None
        if len(item) >= 2 and item[-2].kind == "word" and item[-2].upper == "AS":
            if last.kind in {"word", "ident", "string"}:
                alias = unquote_sql_identifier(last.text)
        elif last.kind in {"word", "ident"}:
            alias = unquote_sql_identifier(last.text)

        if alias and alias not in fields:
            fields.append(alias)

    return tuple(fields)


def _scan_clause_landmarks(
    tokens: list[SqlToken], line_index: LineIndex
) -> tuple[ClauseLandmark, ...]:
    """Scan top-level SQL clause landmarks for non-CTE queries."""
    if not tokens:
        return ()

    # Collect (clause_label, start_tok_idx) at paren depth 0
    raw_landmarks: list[tuple[str, int]] = []
    depth = 0
    n = len(tokens)
    i = 0
    while i < n:
        tok = tokens[i]
        if tok.kind == "lparen":
            depth += 1
            i += 1
            continue
        if tok.kind == "rparen":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth > 0 or tok.kind != "word":
            i += 1
            continue

        word = tok.upper
        if word == "SELECT":
            raw_landmarks.append(("SELECT", i))
            i += 1
            continue
        if word == "FROM":
            raw_landmarks.append(("FROM", i))
            i += 1
            continue
        if word == "JOIN":
            # Upgrade the preceding FROM landmark to FROM/JOIN if it's the active clause
            if raw_landmarks and raw_landmarks[-1][0] == "FROM":
                raw_landmarks[-1] = ("FROM/JOIN", raw_landmarks[-1][1])
            elif not raw_landmarks or raw_landmarks[-1][0] != "FROM/JOIN":
                raw_landmarks.append(("FROM/JOIN", i))
            i += 1
            continue
        if word == "WHERE":
            raw_landmarks.append(("WHERE", i))
            i += 1
            continue
        if (
            word == "GROUP"
            and i + 1 < n
            and tokens[i + 1].kind == "word"
            and tokens[i + 1].upper == "BY"
        ):
            raw_landmarks.append(("GROUP BY", i))
            i += 2
            continue
        if word == "HAVING":
            raw_landmarks.append(("HAVING", i))
            i += 1
            continue
        if word == "QUALIFY":
            raw_landmarks.append(("QUALIFY", i))
            i += 1
            continue
        if (
            word == "ORDER"
            and i + 1 < n
            and tokens[i + 1].kind == "word"
            and tokens[i + 1].upper == "BY"
        ):
            raw_landmarks.append(("ORDER BY", i))
            i += 2
            continue
        if word == "UNION":
            raw_landmarks.append(("UNION", i))
            i += 1
            if i < n and tokens[i].kind == "word" and tokens[i].upper in {"ALL", "DISTINCT"}:
                i += 1
            continue
        i += 1

    landmarks: list[ClauseLandmark] = []
    for idx, (label, tok_idx) in enumerate(raw_landmarks):
        start_off = tokens[tok_idx].start
        next_tok_idx = raw_landmarks[idx + 1][1] if idx + 1 < len(raw_landmarks) else n
        end_tok_idx = max(tok_idx, next_tok_idx - 1)
        end_off = max(start_off, tokens[end_tok_idx].end - 1)
        start_line = line_index.line_of(start_off)
        end_line = max(start_line, line_index.line_of(end_off))
        landmarks.append(
            ClauseLandmark(
                clause=label,
                start_line=start_line,
                end_line=end_line,
                line_count=end_line - start_line + 1,
            )
        )
    return tuple(landmarks)


def scan_sql_structure(clean_sql: str) -> SqlStructure:
    """Scan Clean SQL View into SQL Sections (CTEs + `main`), clause landmarks, and metadata."""
    tokens = tokenize_sql(clean_sql)
    if not tokens:
        return SqlStructure(
            has_ctes=False,
            sections=(),
            clauses=(),
            source_tables=(),
            selected_fields=(),
        )

    line_index = LineIndex(clean_sql)
    n = len(tokens)

    # Parse top-level WITH / WITH RECURSIVE CTE definitions
    raw_ctes: list[_RawCte] = []
    main_start_tok_idx = 0

    if tokens[0].kind == "word" and tokens[0].upper == "WITH":
        i = 1
        if i < n and tokens[i].kind == "word" and tokens[i].upper == "RECURSIVE":
            i += 1

        while i < n:
            name_tok = tokens[i]
            if name_tok.kind not in {"word", "ident"}:
                break
            cte_name = unquote_sql_identifier(name_tok.text)
            outer_start = name_tok.start
            j = i + 1

            # Optional CTE column list: `cte_name (col1, col2)`
            if j < n and tokens[j].kind == "lparen":
                col_close = find_matching_rparen_token(tokens, j)
                if col_close is None:
                    break
                j = col_close + 1

            # Expect AS
            if j >= n or tokens[j].kind != "word" or tokens[j].upper != "AS":
                break
            j += 1

            # Optional Databricks/Postgres modifier: [NOT] MATERIALIZED
            if (
                j + 1 < n
                and tokens[j].kind == "word"
                and tokens[j].upper == "NOT"
                and tokens[j + 1].kind == "word"
                and tokens[j + 1].upper == "MATERIALIZED"
            ):
                j += 2
            elif j < n and tokens[j].kind == "word" and tokens[j].upper == "MATERIALIZED":
                j += 1

            if j >= n or tokens[j].kind != "lparen":
                break
            close_idx = find_matching_rparen_token(tokens, j)
            if close_idx is None:
                break

            inner_start = tokens[j].end
            inner_end = tokens[close_idx].start
            outer_end = tokens[close_idx].end
            body_tokens = tokens[j + 1 : close_idx]
            raw_ctes.append(
                _RawCte(
                    name=cte_name,
                    outer_start=outer_start,
                    outer_end=outer_end,
                    inner_start=inner_start,
                    inner_end=inner_end,
                    body_tokens=body_tokens,
                )
            )

            i = close_idx + 1
            if i < n and tokens[i].kind == "comma":
                i += 1
                continue
            main_start_tok_idx = i
            break
        else:
            main_start_tok_idx = i

    has_ctes = bool(raw_ctes)
    cte_names_by_cf: dict[str, str] = {cte.name.casefold(): cte.name for cte in raw_ctes}

    sections: list[SqlSection] = []
    all_source_tables: list[str] = []

    for cte in raw_ctes:
        refs, srcs = extract_table_refs(
            cte.body_tokens,
            cte_names_by_cf=cte_names_by_cf,
            self_name_cf=cte.name.casefold(),
        )
        for tbl in srcs:
            if tbl not in all_source_tables:
                all_source_tables.append(tbl)
        start_line = line_index.line_of(cte.outer_start)
        end_line = max(start_line, line_index.line_of(max(cte.outer_start, cte.outer_end - 1)))
        sections.append(
            SqlSection(
                name=cte.name,
                kind="cte",
                start_line=start_line,
                end_line=end_line,
                line_count=end_line - start_line + 1,
                outer_span=(cte.outer_start, cte.outer_end),
                inner_span=(cte.inner_start, cte.inner_end),
                references_ctes=refs,
                source_tables=srcs,
            )
        )

    main_tokens = tokens[main_start_tok_idx:] if has_ctes else tokens
    selected_fields: tuple[str, ...] = ()
    if main_tokens:
        main_refs, main_srcs = extract_table_refs(
            main_tokens,
            cte_names_by_cf=cte_names_by_cf,
            self_name_cf=None,
        )
        for tbl in main_srcs:
            if tbl not in all_source_tables:
                all_source_tables.append(tbl)
        m_start = main_tokens[0].start
        m_end = main_tokens[-1].end
        m_start_line = line_index.line_of(m_start)
        m_end_line = max(m_start_line, line_index.line_of(max(m_start, m_end - 1)))
        sections.append(
            SqlSection(
                name="main",
                kind="main",
                start_line=m_start_line,
                end_line=m_end_line,
                line_count=m_end_line - m_start_line + 1,
                outer_span=(m_start, m_end),
                inner_span=(m_start, m_end),
                references_ctes=main_refs,
                source_tables=main_srcs,
            )
        )
        selected_fields = _extract_selected_fields_from_tokens(main_tokens)

    clauses = () if has_ctes else _scan_clause_landmarks(tokens, line_index)
    return SqlStructure(
        has_ctes=has_ctes,
        sections=tuple(sections),
        clauses=clauses,
        source_tables=tuple(all_source_tables),
        selected_fields=selected_fields,
    )
