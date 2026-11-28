# PBI MCP Server

Local-first MCP server for inspecting, auditing, and safely editing Power BI PBIP projects (`.SemanticModel` TMDL and `.Report` PBIR layouts).

---

## Highlights

- **Consolidated `src/pbi_mcp_server/` Architecture**: Clean package hierarchy separating Workspace Discovery (`workspace_registry`, `pbip_references`), Semantic Model (`engine_a`), Report Layout (`engine_b`), Cross-Layer Lineage (`lineage`), Standardized Mutations (`mutation`), Partition SQL Codec, Sections & Editing (`m_codec`, `sql_sections`, `sql_edit`, `tmdl_sql`), Analyst Superpowers (`analyst`), Meta-Tools & Jinja Presets (`meta_tools`, `jinja_templates`), and Agent Workflows (`workflows`).
- **Compact 20-Tool Surface (+ `--meta-tools` Mode)**: Focused, high-leverage tools designed for LLM coding and analytics agents with optional pagination (`limit`, `offset`) and precondition guards (`expected_current_expression`, `expected_replacements`, `expected_sql_hash`). Pass `--meta-tools` to collapse the surface to `pbi_call` and `pbi_help` alongside baked-in `llama-server` Jinja templates (`templates/`).
- **Partition SQL Clean SQL View & Progressive CTE Editing**:
  - Bidirectional M–SQL codec decodes fenced and unfenced TMDL partitions (`Value.NativeQuery`, `Databricks.Query`, and `[Query=...]` connectors like `Sql.Database`) into a 1-based line-numbered **Clean SQL View** with explicit `{{m:<expr>}}` **Dynamic M Parameter** placeholders, then re-encodes edits back into each partition's original M serialization style.
  - Plain-text default MCP output (`format="text"`, opt-in `format="json"`) eliminates JSON newline/quote escaping and 4× payload duplication, while `get_table` returns summary-only Partition SQL metadata.
  - Outline-first progressive disclosure (`> 120` lines) lists **SQL Sections** (top-level CTEs + `main`, or non-CTE clause landmarks) with canonical line ranges so agents can read specific `ctes`, `start_line`/`end_line` windows, or scoped `search` hits before applying forgiving text, line-range, CTE-scoped, whole-CTE, positional CTE insert/delete, or atomic batch `edits`.
- **Cross-Layer Dependency Graph**: Traces lineage from Partition SQL (`source_tables`, `main` `sql_fields`) $\rightarrow$ TMDL Tables & Columns (`sourceColumn`) $\rightarrow$ DAX Calculated Columns & Measures $\rightarrow$ PBIR Visual Field Bindings & Report Pages.
- **Hardened Mutation Pipeline**:
  - Ambiguity detection (`AMBIGUOUS_WORKSPACE`, `AMBIGUOUS_OBJECT`) before any analysis or edit.
  - Precondition verification (`PRECONDITION_FAILED`) via `expected_current_expression`, `expected_replacements`, and `expected_sql_hash` to prevent overwriting stale expressions.
  - Unified diff previews (`MutationDiff` / clean-SQL unified diff) during `dry_run=True`.
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
| `list_tables(workspace?, limit?, offset=0)` | List tables with `object_id`, column/measure/partition counts, and Partition SQL status. |
| `get_table(table_name, workspace?)` | Retrieve complete table schema (columns, measures, relationships, partition metadata, and summary-only Partition SQL metadata without the SQL body). |
| `search_measures(query?, table?, workspace?, limit?, offset=0)` | Search across measure names, qualified names, DAX formulas, and descriptions. |
| `get_table_sql(table_name?, partition?, outline=False, ctes?, start_line?, end_line?, search?, context_lines=2, format="text", workspace?)` | Return a table's **Clean SQL View** (unescaped plain text by default, or `format="json"`), outline-first structural summary (`> 120` lines or `outline=True`), targeted **SQL Sections** (`ctes`, including `"main"`), 1-based line ranges, or workspace/table/CTE-scoped `search` hits. |
| `analyze_impact(term, workspace?)` | Cross-layer upstream (Partition SQL `main` projections & external source tables, partitions, columns, measures) and downstream (measures, columns, relationships, visuals, pages) impact analysis. |

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
| `update_table_sql(table_name?, old_text?, new_text?, cte?, start_line?, end_line?, after_cte?, before_cte?, delete_cte=False, edits?, partition?, expected_replacements?, expected_sql_hash?, format="text", workspace?, dry_run=True)` | Edit **Partition SQL** against the **Clean SQL View** via forgiving text replacement, 1-based line ranges, CTE-scoped or whole-CTE rewrites, positional CTE insertion (`after_cte`/`before_cte`), CTE deletion, or atomic batch `edits`, returning compact plain-text clean-SQL diffs by default (`format="json"` opt-in) while preserving **Dynamic M Parameters** and original M formatting. |
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

## Installation & Quick Start

### Option 1: `npx` (Node.js 18+ + `uv` or Python 3.11–3.12)

```bash
# Inspect / edit a single PBIP project
npx -y pbi-mcp-server --workspace ./sample_project.pbip

# Serve all PBIP projects under a root directory
npx -y pbi-mcp-server --workspace-root "/path/to/pbip-projects"
```

Example MCP client config (Claude Desktop, Cursor, VS Code):

```json
{
  "mcpServers": {
    "pbi-local": {
      "command": "npx",
      "args": ["-y", "pbi-mcp-server", "--workspace-root", "C:\\Users\\you\\PowerBI"]
    }
  }
}
```

> The `npx` package delegates directly to `uvx` if [`uv`](https://docs.astral.sh/uv/) is installed (automatically managing Python 3.12), or falls back to a cached local Python 3.11–3.12 virtual environment. All diagnostics write to `stderr` so MCP `stdio` JSON-RPC framing stays clean.

### Option 2: `uvx` / `pip` (Python)

```bash
# Zero-install via uvx (once published to PyPI)
uvx pbi-mcp-server --workspace ./sample_project.pbip

# Or directly from GitHub
uvx --from git+https://github.com/jdestef90/PBI_MCP pbi-mcp-server --workspace ./sample_project.pbip

# Or install with pip
pip install pbi-mcp-server
pbi-mcp-server --workspace ./sample_project.pbip
```

---

## Running Locally (Development)

```bash
# Run full Python test suite
.venv/bin/pytest

# Run npm launcher tests
npm --prefix npm test

# Start server for a single PBIP project
.venv/bin/pbi-mcp-server --workspace ./sample_project.pbip

# Start server across one or more workspace root directories
.venv/bin/pbi-mcp-server --workspace-root "/path/to/pbip-projects"

# Start server in 2-tool meta-tools mode (for local llama-server + Jinja templates)
.venv/bin/pbi-mcp-server --workspace ./sample_project.pbip --meta-tools

# Regenerate all checked-in llama-server Jinja templates
.venv/bin/pbi-mcp-server --export-jinja all --output-dir templates
```

---

## Domain & Architecture Documentation

- [`GLOSSARY.md`](GLOSSARY.md): Ubiquitous domain language for PBIP workspaces, TMDL, PBIR, Partition SQL, lineage, and mutations.
- [`docs/agent-workflows.md`](docs/agent-workflows.md): Canonical multi-step agent workflow recipes and precondition guidelines.
- [`templates/README.md`](templates/README.md): Local `llama-server` Jinja chat templates and `--meta-tools` usage.
- [`docs/adr/`](docs/adr/): Architecture Decision Records (`0001` through `0006`).
- [`docs/agents/`](docs/agents/): Engineering skill configurations for issue tracking and domain docs.
