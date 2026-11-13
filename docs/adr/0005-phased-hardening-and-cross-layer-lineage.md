# ADR 0005: Phased Hardening, Cross-Layer Dependency Graph, and Mutation Pipeline

## Status
Accepted

## Context
Following the consolidation of the repository into `src/pbi_mcp_server/` (ADR-0001), the 20-tool surface (ADR-0002), post-write validation (ADR-0003), and the initial analyst toolkit (ADR-0004), real-world usage by autonomous coding and analytics agents highlighted seven areas requiring production hardening without expanding the 20-tool MCP surface:

1. **Workspace Discovery & Path Normalization**: PBIP projects frequently reside on Windows paths, UNC shares, or OneDrive directories with spaces, nested sub-projects, orphan `.SemanticModel` folders, or `.pbip` files that link to the semantic model only indirectly via `<Report>/definition.pbir`.
2. **Canonical Object Identity**: Every workspace, table, column, measure, relationship, partition, page, and visual needs a deterministic, fully qualified `object_id` (`<workspace_id>::<kind>::<qualified_name>`) and explicit ambiguity detection.
3. **Cross-Layer Lineage**: Line-based text search is insufficient for tracing upstream SQL columns to TMDL columns (`sourceColumn`), multi-hop DAX measure dependencies, and downstream PBIR visual bindings.
4. **Mutation Preconditions & Unified Diffs**: Agents need unified diffs (`MutationDiff`) during `dry_run=True` and precondition guards (`expected_current_expression`, `expected_replacements`) to prevent stale overwrites.
5. **Expanded Model & Report BPA**: Best Practice Analyzer rules should inspect both Semantic Model anti-patterns (broken relationship endpoints, many-to-many relationships, complex calculated columns) and Report Layout defects (broken visual bindings to deleted fields, empty pages, duplicate visuals).
6. **Workflow Orchestration**: Multi-step agent tasks benefit from explicit workflow guidance metadata (`workflow_id`, `current_stage`, `next_step`) embedded directly in tool responses and a programmatic `WorkflowOrchestrator`.
7. **Structured Error Taxonomy & Redacted Logging**: Errors must carry machine-readable `ErrorCode` values (`WORKSPACE_NOT_FOUND`, `AMBIGUOUS_WORKSPACE`, `OBJECT_NOT_FOUND`, `AMBIGUOUS_OBJECT`, `PRECONDITION_FAILED`, `ROLLBACK_COMPLETED`, etc.) while keeping `stdio` clean and redacting sensitive user paths in logs.

## Decision
We implemented the 7-phase hardening architecture across `src/pbi_mcp_server/` while strictly preserving the 20-tool MCP surface:

- **`pbip_references.py`, `workspace.py`, `workspace_registry.py`**: Added `normalize_path_str` and `canonical_path_key` for Windows/UNC/OneDrive paths, `<Report>/definition.pbir` dataset resolution, recursive `os.walk` scanning with permission-error resilience, orphan/partial/nested project diagnostics, and `limit`/`offset` pagination.
- **`models.py`, `engine_a/parser.py`, `engine_b/parser.py`, `tmdl_sql.py`**: Added `make_object_id`, `FieldBinding`, `MutationDiff`, multi-line fenced DAX parsing/writing, partition SQL lineage extraction (`extract_sql_metadata`, `discover_partition_metadata`), and `AmbiguousObjectError` guards.
- **`lineage.py`**: Implemented `DependencyGraph` connecting Partition SQL $\rightarrow$ TMDL Tables/Columns $\rightarrow$ DAX Measures/Calculated Columns/Relationships $\rightarrow$ PBIR Visuals & Pages, powering `analyze_object_impact` and dead-branch detection (`indirectly_referenced_only`) in `find_unused_model_objects`.
- **`mutation.py`**: Standardized the mutation pipeline with `verify_expression_precondition`, `verify_replacements_precondition`, `build_mutation_diff`, and multi-file atomic backup/rollback.
- **`analyst.py`**: Added `BPA_BROKEN_RELATIONSHIP_ENDPOINT`, `BPA_MANY_TO_MANY_RELATIONSHIP`, `BPA_CALCULATED_COLUMN_COMPLEXITY`, `BPA_BROKEN_VISUAL_BINDING`, `BPA_EMPTY_REPORT_PAGE`, `BPA_DUPLICATE_VISUAL`, and `health_summary`.
- **`workflows.py`**: Added `WORKFLOW_RECIPES`, `build_workflow_guidance`, and `WorkflowOrchestrator`.
- **`errors.py` & `logging_utils.py`**: Created the `PbiMcpError` hierarchy (inheriting from both `PbiMcpError` and standard Python exceptions for backward compatibility) and `StructuredLogFormatter` with `redact_sensitive_paths`.

## Consequences
- **Positive**: Agents receive deterministic object IDs, cross-layer upstream/downstream lineage, unified diffs on dry-run, precondition protection against race conditions, report-level BPA validation, and structured error codes without adding a single extra tool to the 20-tool MCP surface.
- **Negative**: Slightly higher in-memory graph construction work during `analyze_impact` and `find_unused_objects`, mitigated by keeping full-workspace graph construction well under 1 second on projects with 25+ tables and 125+ measures.
