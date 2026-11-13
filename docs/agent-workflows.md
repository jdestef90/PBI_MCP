# Agent Workflows Guide

This guide describes the four canonical multi-step workflows supported by the PBI MCP Server and its `WorkflowOrchestrator` (`src/pbi_mcp_server/workflows.py`). Every mutation tool response includes a `workflow` metadata block indicating `workflow_id`, `current_stage`, `stages`, and `next_step`.

---

## Core Operating Principles

1. **Resolve Ambiguity First**: Always resolve the target Workspace (`list_workspaces`) and target object (`search_measures`, `get_table`, `get_visual`) before attempting mutations. If multiple objects match, the server raises `AMBIGUOUS_WORKSPACE` or `AMBIGUOUS_OBJECT` with candidate details rather than guessing.
2. **Analyze Cross-Layer Impact**: Run `analyze_impact(term)` to inspect upstream SQL/columns and downstream measures, visuals, and pages before changing DAX, SQL, or measure names.
3. **Dry-Run Preview with Unified Diffs**: All mutation tools default to `dry_run=True` and return a `ChangePlan` containing `diffs` (`unified_diff`, `lines_added`, `lines_removed`) and `impact_summary`.
4. **Enforce Preconditions on Apply**: Pass `expected_current_expression` (for `update_measure_dax`) or `expected_replacements` (for `update_table_sql`, `update_visual_fields`, `rename_measure`) when calling with `dry_run=False`.
5. **Post-Write Validation & Auto-Rollback**: Every applied mutation automatically runs `validate_workspace`. If any structural error is detected (unbalanced TMDL fences, invalid JSON, empty measure expression, duplicate measure), all modified files are restored from backup and `status="rolled_back"` (`error_code="ROLLBACK_COMPLETED"`) is returned.

---

## Canonical Workflows

### 1. Measure Lookup and Safe DAX Update (`measure_lookup_and_update`)

1. `list_workspaces()` $\rightarrow$ select `workspace_id`.
2. `search_measures(query="Total Revenue", workspace=workspace_id)` $\rightarrow$ inspect `name`, `table`, `object_id`, and current `expression`.
3. `analyze_impact(term="Sales[Total Revenue]", workspace=workspace_id)` $\rightarrow$ review upstream columns and downstream measures/visuals.
4. `update_measure_dax(measure_name="Total Revenue", expression="...", table="Sales", expected_current_expression="...", workspace=workspace_id, dry_run=True)` $\rightarrow$ inspect `diffs`.
5. `update_measure_dax(..., dry_run=False)` $\rightarrow$ apply and verify `status == "applied"`.

### 2. Partition SQL Inspection and Surgical Replacement (`sql_inspection_and_replacement`)

1. `get_table_sql(table_name="Sales", workspace=workspace_id)` $\rightarrow$ inspect `sql_with_line_numbers`, `partition`, and `sql_metadata` (`source_tables`, `selected_fields`, `dynamic_parameters`).
2. `analyze_impact(term="Sales", workspace=workspace_id)` $\rightarrow$ check downstream columns, measures, and visuals.
3. `update_table_sql(old_text="...", new_text="...", table_name="Sales", expected_replacements=1, workspace=workspace_id, dry_run=True)` $\rightarrow$ verify `replacement_count == 1` and inspect `diffs`.
4. `update_table_sql(..., dry_run=False)` $\rightarrow$ apply and validate.

### 3. Visual Field Binding Update (`visual_field_binding_update`)

1. `list_visuals(page="Executive Summary", workspace=workspace_id)` or `get_visual(visual_id_or_title="Revenue by Region", workspace=workspace_id)` $\rightarrow$ inspect `visual_id`, `role_bindings`, and `field_bindings`.
2. `update_visual_fields(visual_id_or_title="vis_bar_revenue_region", add_fields=[{"field": "Sales[Total Cost]", "role": "Y"}], remove_fields=None, workspace=workspace_id, dry_run=True)` $\rightarrow$ preview changes.
3. `update_visual_fields(..., dry_run=False)` $\rightarrow$ apply and validate report JSON.

### 4. Cross-Layer Atomic Measure Rename (`cross_layer_measure_rename`)

1. `analyze_impact(term="Net Margin", workspace=workspace_id)` $\rightarrow$ inspect all TMDL measures and PBIR visuals referencing the measure.
2. `rename_measure(old_name="Net Margin", new_name="Operating Margin", table="Sales", workspace=workspace_id, dry_run=True)` $\rightarrow$ review `changed_files` and `diffs` across `.tmdl` and `.json` files.
3. `rename_measure(..., dry_run=False)` $\rightarrow$ atomically update declarations, DAX formulas, and visual projections with multi-file rollback protection.
