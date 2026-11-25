# Partition SQL Codec, Plain-Text MCP Boundary, and Progressive CTE Editing

**Status:** Accepted

## Decision

To make large Databricks and Power BI **Partition SQL** queries reliable for sub-30B open-weight agents without expanding the 20-tool MCP surface (ADR-0002, ADR-0005), we redesign `get_table_sql` and `update_table_sql` around four deliberate choices:

1. **Plain-text MCP responses by default:** Both `get_table_sql` and `update_table_sql` return compact, unescaped plain text at the MCP boundary (with opt-in `format="json"` and structured dicts preserved inside `PbipService`). Returning JSON dicts previously forced `\n`/`\t`/`\"` escaping across every line and duplicated the SQL four times (`sql`, `sql_m_expression`, `sql_with_line_numbers`, `source_m_expression`). `get_table` is trimmed to summary-only `sql_source` metadata (no SQL body).
2. **Bidirectional M–SQL codec (`Clean SQL View`):** Partition SQL across fenced and unfenced `source =` blocks (`Value.NativeQuery`, `Databricks.Query`, and `[Query=...]` connectors like `Sql.Database`) is decoded into a single 1-based line-numbered **Clean SQL View** (unescaping `#(lf)`/`#(cr)#(lf)`/`#(tab)` and `""`, stripping common TMDL indentation, and rendering **Dynamic M Parameters** as `{{m:<expr>}}`). On write, edits re-encode back into each partition's original M serialization style and indentation so git diffs stay minimal and Power BI Desktop compatibility is preserved.
3. **Outline-first progressive disclosure & `SQL Section` granularity:** Queries `<= 120` lines return full numbered SQL in one call; larger queries default to an outline (CTEs with canonical line ranges and inter-CTE references, or top-level clause landmarks for non-CTE queries, plus source tables, Dynamic M Parameters, and `sql_hash`). Agents can then fetch specific **SQL Sections** (`ctes=[...]` including `"main"`, up to a 400-line ceiling), line ranges (`start_line`/`end_line`, 1-based inclusive), or scoped `search` hits annotated with enclosing CTE and context lines.
4. **Multi-mode, atomic CTE editing with small-model guardrails:** `update_table_sql` operates on the **Clean SQL View** and supports forgiving `old_text`/`new_text`, `start_line`/`end_line`, CTE-scoped replacement (with auto-unwrapping of accidental `cte AS (...)`), positional CTE insertion (`after_cte`/`before_cte`, including bootstrapping `WITH` via `before_cte="main"`), CTE deletion (`delete_cte=True` with downstream-reference warnings), and atomic batch `edits` evaluated against a single pre-edit snapshot. Pre-write checks strip accidental `42 | ` line prefixes, verify balanced delimiters and intact `{{m:...}}` placeholders, and support optional `expected_sql_hash` before running the **Mutation Pipeline**'s clean-SQL unified diff, post-write validation, and automatic rollback.

## Considered Options

- **Keep JSON dicts with a `lines` array:** Avoids `\n` blobs, but still escapes every quote (`\"`), increases token overhead, and leaves unified diffs JSON-escaped for sub-30B models.
- **Add dedicated `search_table_sql` / `get_sql_outline` tools:** Rejected to preserve the 20-tool ceiling (ADR-0002, ADR-0005) and keep baked-in `--meta-tools` Jinja catalogs compact.
- **Use a third-party SQL AST rewriter (e.g., `sqlglot`):** Rejected because Databricks M partitions embed non-SQL **Dynamic M Parameters** (`"&Catalog&"`, M function calls) and comments that strict SQL rewriters frequently fail on or reformat aggressively, inflating diffs.
- **Always normalize TMDL partitions to fenced multiline on write:** Rejected to avoid noisy git diffs and unnecessary re-serialization changes for Power BI Desktop.

## Consequences

- **Positive:** Eliminates 4× payload duplication, JSON escape noise, and context truncation on large queries; lets sub-30B models inspect and atomically edit/insert 2–3 CTEs in 2–3 focused tool calls; fixes **Dependency Graph** SQL lineage (`extract_sql_metadata` reads projected columns from `main` instead of CTE #1 and excludes internal CTE names from external `source_tables`); and fixes the empty `dry_run=True` diff in `update_table_sql`.
- **Negative:** Two of the 20 MCP tools (`get_table_sql`, `update_table_sql`) return plain text instead of JSON by default (callers requiring JSON pass `format="json"`), and `templates/*.jinja` presets must be regenerated whenever tool signatures change.
