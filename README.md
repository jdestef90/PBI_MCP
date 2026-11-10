# PBI MCP Server

Local-first MCP server for inspecting, auditing, and safely editing Power BI PBIP projects (`.SemanticModel` TMDL and `.Report` PBIR layouts).

---

## Highlights (v0.3.0)

- **Consolidated `src/pbi_mcp_server/` Architecture**: Clean package hierarchy separating Semantic Model (`engine_a`), Report Layout (`engine_b`), Cross-Layer Integrity (`integrity`), Partition SQL (`tmdl_sql`), and Analyst Superpowers (`analyst`).
- **Streamlined 20-Tool Surface**: Focused, workflow-oriented tools designed specifically for LLM coding and analytics agents without context bloat.
- **Natural-Language Agent Workflows**:
  - *"Find the sales revenue metric DAX and update it"*: Fuzzy measure search (`search_measures`) returning table, measure name, and DAX expression, paired with `update_measure_dax`.
  - *"Change this line in the SQL query"*: Partition SQL extraction with line numbers (`get_table_sql`) and surgical replacements inside `Value.NativeQuery` (`update_table_sql`).
  - *"Add or remove fields from visual X"*: Visual lookup by human-readable title or ID (`get_visual`) and role-aware field binding updates (`update_visual_fields`) that preserve 100% of existing visual styling and formatting.
- **Power BI Analyst Toolkit**:
  - `find_unused_objects`: Detects zombie columns and unreferenced measures across TMDL and report visuals.
  - `format_dax`: Formats DAX to standard SQLBI conventions (uppercase functions, clean operator spacing).
  - `generate_measure_docs`: Generates and writes `///` TMDL documentation comments for measures.
  - `check_model_bpa`: Runs Best Practice Analyzer rules (unhidden foreign keys, bidirectional relationships, floating-point columns, missing format strings).
- **Dry-Run First + Post-Write Auto-Rollback**: All mutations default to `dry_run=True` and automatically validate and roll back from backup if post-write structural checks fail.

---

## MCP Tools (20 Total)

### 1. Workspace Discovery & Lifecycle (3 tools)
| Tool | Description |
| :--- | :--- |
| `list_workspaces(query?)` | List discovered PBIP workspaces with stable IDs, optionally filtered by query. |
| `get_workspace(workspace?)` | Get workspace paths, validity status, and object counts. |
| `refresh_workspaces()` | Re-scan configured workspace roots and refresh registry state. |

### 2. Semantic Model Inspection & Search (5 tools)
| Tool | Description |
| :--- | :--- |
| `list_tables(workspace?)` | List tables with column/measure counts and `Value.NativeQuery` SQL status. |
| `get_table(table_name, workspace?)` | Retrieve complete table schema (columns, measures, relationships, SQL source). |
| `search_measures(query?, table?, workspace?)` | Fuzzy search across measure names, DAX formulas, and descriptions. |
| `get_table_sql(table_name?, partition?, workspace?)` | Extract partition SQL query with line numbers and M context. |
| `analyze_impact(term, workspace?)` | Cross-layer reference and visual impact analysis for any measure or column. |

### 3. Report Layout & Visuals (3 tools)
| Tool | Description |
| :--- | :--- |
| `list_report_pages(workspace?)` | List report pages with display names and visual counts. |
| `list_visuals(page?, query?, workspace?)` | List visuals filtered by page, title, visual type, or bound field. |
| `get_visual(visual_id_or_title, page?, workspace?)` | Resolve a visual by ID or title and inspect its role-grouped field bindings. |

### 4. Safe Mutations (4 tools)
| Tool | Description |
| :--- | :--- |
| `update_measure_dax(measure_name, expression, table?, workspace?, dry_run=True)` | Update a measure's DAX formula in TMDL cleanly. |
| `update_table_sql(old_text, new_text, table_name?, partition?, expected_replacements?, workspace?, dry_run=True)` | Surgically replace lines or blocks inside a table's `Value.NativeQuery` SQL. |
| `update_visual_fields(visual_id_or_title, add_fields?, remove_fields?, page?, workspace?, dry_run=True)` | Add or remove measures/columns on visual roles without altering formatting. |
| `rename_measure(old_name, new_name, workspace?, dry_run=True)` | Atomically rename a measure across TMDL declarations, DAX, and report visuals. |

### 5. Power BI Analyst Toolkit (4 tools)
| Tool | Description |
| :--- | :--- |
| `find_unused_objects(workspace?)` | Identify unused measures and unreferenced columns across model and report. |
| `format_dax(measure_name?, dax_expression?, table?, workspace?, dry_run=True)` | Standardize DAX formatting standalone or in-place in TMDL. |
| `generate_measure_docs(measure_name, description?, table?, workspace?, dry_run=True)` | Generate and write `///` TMDL documentation comments for a measure. |
| `check_model_bpa(workspace?)` | Audit the semantic model against Best Practice Analyzer rules. |

### 6. Validation (1 tool)
| Tool | Description |
| :--- | :--- |
| `validate_workspace(workspace?)` | Validate TMDL syntax, balanced fences, and PBIR JSON integrity. |

---

## Running Locally

```bash
# Run tests
uv run pytest

# Start server for a single PBIP project
uv run pbi-mcp-server --workspace ./sample_project.pbip

# Start server across one or more workspace root directories
uv run pbi-mcp-server --workspace-root "/path/to/pbip-projects"
```

---

## Domain & Architecture Documentation

- [`GLOSSARY.md`](GLOSSARY.md): Ubiquitous domain language for PBIP workspaces, TMDL, PBIR, and analyst concepts.
- [`docs/adr/`](docs/adr/): Architecture Decision Records.
- [`docs/agents/`](docs/agents/): Engineering skill configurations for issue tracking and domain docs.
