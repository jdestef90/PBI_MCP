# PBI MCP Server

Local-first MCP server for inspecting, auditing, and safely editing Power BI PBIP projects (`.SemanticModel` TMDL and `.Report` PBIR layouts).

---

## Highlights

- **Consolidated `src/pbi_mcp_server/` Architecture**: Clean package hierarchy separating Workspace Discovery (`workspace_registry`, `pbip_references`), Semantic Model (`engine_a`), Report Layout (`engine_b`), Cross-Layer Lineage (`lineage`), Standardized Mutations (`mutation`), Partition SQL (`tmdl_sql`), Analyst Superpowers (`analyst`), and Agent Workflows (`workflows`).
- **Compact 20-Tool Surface**: Focused, high-leverage tools designed for LLM coding and analytics agents with optional pagination (`limit`, `offset`) and precondition guards (`expected_current_expression`, `expected_replacements`).
- **Cross-Layer Dependency Graph**: Traces lineage from Partition SQL (`source_tables`, `sql_fields`) $\rightarrow$ TMDL Tables & Columns (`sourceColumn`) $\rightarrow$ DAX Calculated Columns & Measures $\rightarrow$ PBIR Visual Field Bindings & Report Pages.
- **Hardened Mutation Pipeline**:
  - Ambiguity detection (`AMBIGUOUS_WORKSPACE`, `AMBIGUOUS_OBJECT`) before any analysis or edit.
  - Precondition verification (`PRECONDITION_FAILED`) to prevent overwriting stale expressions.
  - Unified diff previews (`MutationDiff`) during `dry_run=True`.
  - Multi-file atomic backups and automatic rollback (`ROLLBACK_COMPLETED`) if post-write TMDL or PBIR validation fails.
- **Expanded Power BI Analyst Toolkit**:
  - `find_unused_objects`: Detects unreferenced measures, unused columns, and dead-branch (`indirectly_referenced_only`) objects.
  - `format_dax`: Formats single-line and multi-line `VAR`/`RETURN` DAX expressions to SQLBI conventions.
  - `generate_measure_docs`: Synthesizes lineage-aware `///` TMDL documentation comments for measures.
  - `check_model_bpa`: Audits both Semantic Model and Report Layout against 11 Best Practice Analyzer rules (bidirectional relationships, many-to-many relationships, broken relationship endpoints, unhidden foreign keys, missing format strings, missing descriptions, direct `/` division, floating-point columns, complex calculated columns, broken visual bindings, empty report pages, and duplicate visuals).

---

## MCP Tools (20 Total)

### 1. Workspace Discovery & Lifecycle (3 tools)
| Tool | Description |
| :--- | :--- |
| `list_workspaces(query?, limit?, offset=0)` | List discovered PBIP workspaces with deterministic `workspace_id`s, optionally filtered and paginated. |
| `get_workspace(workspace?)` | Get workspace paths, validity status, object counts, scan diagnostics, and BPA `health_summary`. |
| `refresh_workspaces()` | Re-scan configured workspace roots and refresh registry state. |

### 2. Semantic Model Inspection & Search (5 tools)
| Tool | Description |
| :--- | :--- |
| `list_tables(workspace?, limit?, offset=0)` | List tables with `object_id`, column/measure/partition counts, and `Value.NativeQuery` SQL status. |
| `get_table(table_name, workspace?)` | Retrieve complete table schema (columns, measures, relationships, partition metadata, and SQL source). |
| `search_measures(query?, table?, workspace?, limit?, offset=0)` | Search across measure names, qualified names, DAX formulas, and descriptions. |
| `get_table_sql(table_name?, partition?, workspace?)` | Extract partition SQL query with line numbers, M context, and parsed SQL metadata. |
| `analyze_impact(term, workspace?)` | Cross-layer upstream (SQL, partitions, columns, measures) and downstream (measures, columns, relationships, visuals, pages) impact analysis. |

### 3. Report Layout & Visuals (3 tools)
| Tool | Description |
| :--- | :--- |
| `list_report_pages(workspace?, limit?, offset=0)` | List report pages with `object_id`, display names, visual counts, and page-level filter fields. |
| `list_visuals(page?, query?, workspace?, limit?, offset=0)` | List visuals with `object_id`, titles, types, role-grouped `field_bindings`, and `filter_fields`. |
| `get_visual(visual_id_or_title, page?, workspace?)` | Resolve a visual unambiguously by ID or title and inspect its role-grouped field bindings. |

### 4. Safe Mutations (4 tools)
| Tool | Description |
| :--- | :--- |
| `update_measure_dax(measure_name, expression, table?, create_if_missing=False, format_string?, display_folder?, description?, expected_current_expression?, expected_replacements?, workspace?, dry_run=True)` | Update an existing measure's DAX or create a new measure (`create_if_missing=True`) with formatString, displayFolder, and unified diff output. |
| `update_table_sql(old_text, new_text, table_name?, partition?, expected_replacements?, workspace?, dry_run=True)` | Surgically replace lines or blocks inside a table's `Value.NativeQuery` SQL with unified diffs. |
| `update_visual_fields(visual_id_or_title, add_fields?, remove_fields?, page?, expected_replacements?, workspace?, dry_run=True)` | Add or remove measures/columns on visual roles without altering visual layout or styling. |
| `rename_measure(old_name, new_name, table?, expected_replacements?, workspace?, dry_run=True)` | Atomically rename a measure across TMDL declarations, DAX formulas, and report visuals. |

### 5. Power BI Analyst Toolkit (4 tools)
| Tool | Description |
| :--- | :--- |
| `find_unused_objects(workspace?)` | Identify unused measures, unused columns, and dead-branch (`indirectly_referenced_only`) objects. |
| `format_dax(measure_name?, dax_expression?, table?, workspace?, dry_run=True)` | Standardize DAX formatting standalone or in-place in TMDL. |
| `generate_measure_docs(measure_name, description?, table?, workspace?, dry_run=True)` | Generate and write lineage-aware `///` TMDL documentation comments for a measure. |
| `check_model_bpa(workspace?)` | Audit the Semantic Model and Report Layout against 11 Best Practice Analyzer rules. |

### 6. Validation (1 tool)
| Tool | Description |
| :--- | :--- |
| `validate_workspace(workspace?)` | Validate TMDL syntax, balanced fences, measure expressions, and PBIR JSON integrity. |

---

## MCP Resources & Prompts (Zero Tool-Surface Overhead)

In addition to the 20 tools above, the server exposes **MCP Resources** (for instant context attachment) and **MCP Prompts** (for slash-command workflows in Claude Desktop, Cursor, or Antigravity):

### MCP Resources
- `pbip://workspaces`: Live JSON list of all discovered PBIP workspaces and their `workspace_id`s.
- `pbip://workspace/{workspace}/summary`: Object counts (tables, columns, measures, relationships, partitions, calculation groups, RLS roles, pages, visuals) and BPA `health_summary`.
- `pbip://workspace/{workspace}/bpa`: Full Best Practice Analyzer report for the workspace.

### MCP Prompts
- `workflow_model_audit(workspace?)`: Guided 4-step Semantic Model & Report Layout audit prompt.
- `workflow_measure_update(measure_name, new_dax, workspace?)`: Guided 5-stage prompt for resolving, impact-analyzing, dry-running, applying, and validating a measure update or creation.

---

## Configuration & Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PBI_WORKSPACE_PATH` | *(None)* | Default single `.pbip` file or workspace directory when CLI flags are omitted. |
| `PBI_MCP_CREATE_BACKUPS` | `true` | Create `.bak` backup files before applying mutations. |
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`). All logs route to `stderr`. |
| `PBI_MCP_LOG_FORMAT` | `text` | Set to `json` for structured JSON log records on `stderr`. |
| `PBI_MCP_REDACT_PATHS` | `true` | Redact sensitive user/OneDrive home directory segments in log output. |

---

## Running Locally

```bash
# Run full test suite
.venv/bin/pytest

# Start server for a single PBIP project
.venv/bin/pbi-mcp-server --workspace ./sample_project.pbip

# Start server across one or more workspace root directories
.venv/bin/pbi-mcp-server --workspace-root "/path/to/pbip-projects"
```

---

## Domain & Architecture Documentation

- [`GLOSSARY.md`](GLOSSARY.md): Ubiquitous domain language for PBIP workspaces, TMDL, PBIR, lineage, and mutations.
- [`docs/agent-workflows.md`](docs/agent-workflows.md): Canonical multi-step agent workflow recipes and precondition guidelines.
- [`docs/adr/`](docs/adr/): Architecture Decision Records (`0001` through `0005`).
- [`docs/agents/`](docs/agents/): Engineering skill configurations for issue tracking and domain docs.
