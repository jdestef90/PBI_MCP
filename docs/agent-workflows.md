# Agent Workflows Guide

This guide describes the four canonical multi-step workflows supported by the PBI MCP Server and its `WorkflowOrchestrator` (`src/pbi_mcp_server/workflows.py`). Every mutation tool response includes a `workflow` metadata block indicating `workflow_id`, `current_stage`, `stages`, and `next_step`.

---

## Core Operating Principles

1. **Resolve Ambiguity First**: Always resolve the target Workspace (`list_workspaces`) and target object (`search_measures`, `get_table`, `get_visual`) before attempting mutations. If multiple objects match, the server raises `AMBIGUOUS_WORKSPACE` or `AMBIGUOUS_OBJECT` with candidate details rather than guessing.
2. **Analyze Cross-Layer Impact**: Run `analyze_impact(term)` to inspect upstream Partition SQL/columns and downstream measures, visuals, and pages before changing DAX, Partition SQL, or measure names.
3. **Dry-Run Preview with Unified Diffs**: All mutation tools default to `dry_run=True` and preview unified diffs and impact summaries before writing. `get_table_sql` and `update_table_sql` return compact, unescaped plain text by default (`format="text"`, with opt-in `format="json"`), while the other mutation tools return a structured JSON `ChangePlan`.
4. **Enforce Preconditions on Apply**: Pass `expected_current_expression` (for `update_measure_dax`), `expected_sql_hash` and/or `expected_replacements` (for `update_table_sql`), or `expected_replacements` (for `update_visual_fields`, `rename_measure`) when calling with `dry_run=False`.
5. **Post-Write Validation & Auto-Rollback**: Every applied mutation automatically runs through the **Mutation Pipeline** (`validate_workspace`). If any structural error is detected (unbalanced TMDL fences, invalid JSON, empty measure expression, duplicate measure), all modified files are restored from backup and `status="rolled_back"` (`error_code="ROLLBACK_COMPLETED"`) is returned.

---

## Canonical Workflows

### 1. Measure Lookup and Safe DAX Update (`measure_lookup_and_update`)

1. `list_workspaces()` $\rightarrow$ select `workspace_id`.
2. `search_measures(query="Total Revenue", workspace=workspace_id)` $\rightarrow$ inspect `name`, `table`, `object_id`, and current `expression`.
3. `analyze_impact(term="Sales[Total Revenue]", workspace=workspace_id)` $\rightarrow$ review upstream columns and downstream measures/visuals.
4. `update_measure_dax(measure_name="Total Revenue", expression="...", table="Sales", expected_current_expression="...", workspace=workspace_id, dry_run=True)` $\rightarrow$ inspect `diffs`.
5. `update_measure_dax(..., dry_run=False)` $\rightarrow$ apply and verify `status == "applied"`.

### 2. Partition SQL Progressive Inspection and CTE Editing (`sql_inspection_and_replacement`)

`get_table_sql` and `update_table_sql` operate on the **Clean SQL View** (M-decoded, 1-based line-numbered SQL with explicit `{{m:<expr>}}` placeholders for **Dynamic M Parameters**) and return compact, unescaped plain text by default (`format="text"`; pass `format="json"` if structured JSON is needed).

1. **Discover or outline first**:
   - Call `get_table_sql(table_name="Sales", workspace=workspace_id)` (or pass `outline=True` for any query size).
   - Queries `<= 120` lines return the full numbered **Clean SQL View** in one call; queries `> 120` lines default to a structural outline listing each **SQL Section** (every top-level CTE plus `main`, or top-level clause landmarks for non-CTE queries) with canonical 1-based line ranges, inter-CTE references, external source tables, **Dynamic M Parameters**, and `sql_hash`.
   - To locate a column, table, or filter across the **Workspace**, a table, or specific CTEs, call `get_table_sql(search="status", context_lines=2)` (optionally scoped by `table_name`, `partition`, and `ctes`).
2. **Fetch only the relevant SQL Sections or line ranges**:
   - Call `get_table_sql(table_name="Sales", ctes=["filtered_orders", "main"], workspace=workspace_id)` to read only the **SQL Sections** you plan to change (up to a 400-line single-call ceiling), or `start_line`/`end_line` for a 1-based inclusive window.
3. **Check downstream lineage**:
   - Call `analyze_impact(term="Sales", workspace=workspace_id)` to inspect downstream Semantic Model columns, measures, and Report Layout visuals (upstream SQL projections are extracted from `main`, excluding internal CTE names).
4. **Preview the edit (`dry_run=True`) with clean-SQL unified diffs**:
   - **Targeted text or line-range edit**: `update_table_sql(table_name="Sales", cte="filtered_orders", old_text="...", new_text="...", expected_sql_hash="<sql_hash>", workspace=workspace_id, dry_run=True)` (or `start_line`/`end_line` + `new_text`).
   - **Whole-CTE rewrite**: `update_table_sql(table_name="Sales", cte="filtered_orders", new_text="SELECT ...", expected_sql_hash="<sql_hash>", dry_run=True)` (preserves `WITH`, `name AS (...)`, and commas).
   - **Positional CTE insertion or deletion**: Pass `after_cte` and/or `before_cte` (e.g. `before_cte="main"` even on a non-CTE query to bootstrap `WITH`) to insert a new CTE, or `delete_cte=True` to remove one (with downstream-reference warnings).
   - **Atomic multi-CTE batch edit**: Pass `edits=[...]` (e.g., inserting a new CTE between two existing CTEs and rewiring the downstream CTE in one snapshot) to preview a single unified diff without line-number drift.
   - Review the returned clean-SQL unified diff and any warnings (such as modified or removed `{{m:...}}` **Dynamic M Parameters** or dangling CTE references).
5. **Apply through the Mutation Pipeline (`dry_run=False`)**:
   - Re-run `update_table_sql(..., expected_sql_hash="<sql_hash>", dry_run=False)` to re-encode the edited **Clean SQL View** back into the partition's original M serialization style, write atomically with backup, and run post-write TMDL validation with automatic rollback on error.

### 3. Visual Field Binding Update (`visual_field_binding_update`)

1. `list_visuals(page="Executive Summary", workspace=workspace_id)` or `get_visual(visual_id_or_title="Revenue by Region", workspace=workspace_id)` $\rightarrow$ inspect `visual_id`, `role_bindings`, and `field_bindings`.
2. `update_visual_fields(visual_id_or_title="vis_bar_revenue_region", add_fields=[{"field": "Sales[Total Cost]", "role": "Y"}], remove_fields=None, workspace=workspace_id, dry_run=True)` $\rightarrow$ preview changes.
3. `update_visual_fields(..., dry_run=False)` $\rightarrow$ apply and validate report JSON.

### 4. Cross-Layer Atomic Measure Rename (`cross_layer_measure_rename`)

1. `analyze_impact(term="Net Margin", workspace=workspace_id)` $\rightarrow$ inspect all TMDL measures and PBIR visuals referencing the measure.
2. `rename_measure(old_name="Net Margin", new_name="Operating Margin", table="Sales", workspace=workspace_id, dry_run=True)` $\rightarrow$ review `changed_files` and `diffs` across `.tmdl` and `.json` files.
3. `rename_measure(..., dry_run=False)` $\rightarrow$ atomically update declarations, DAX formulas, and visual projections with multi-file rollback protection.
