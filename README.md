# Power BI MCP Server (PBI_MCP)

A standalone Model Context Protocol (MCP) server for Power BI authoring and editing that works exclusively with local `.pbip` files (no Microsoft Cloud or Azure authentication required).

---

## 1. Core Architecture

The server implements a **Dual-Engine Architecture** to cleanly manage the two distinct data layers within a Power BI project (`.pbip`):

```
                     ┌──────────────────────────┐
                     │   Model Context Protocol │
                     │   (Stdio MCP Server)     │
                     └─────────────┬────────────┘
                                   │
         ┌─────────────────────────┴─────────────────────────┐
         ▼                                                   ▼
┌──────────────────────────────┐            ┌──────────────────────────────┐
│ Engine A: Semantic Model     │            │ Engine B: Report Layout      │
│ (The "Brain")                │            │ (The "Body")                 │
│ - TMDL Parser & AST          │            │ - JSON Shadow Model          │
│ - Round-Trip Fidelity        │            │ - Path-based visual queries  │
│ - Zero-diff formatting       │            │ - Embedded JSON decoding     │
└──────────────┬───────────────┘            └──────────────┬───────────────┘
               │                                           │
               └───────────────────┬───────────────────────┘
                                   │
                                   ▼
                   ┌───────────────────────────────┐
                   │ Cross-Layer Integrity Engine  │
                   │ - Reference-Aware Renames     │
                   │ - Bound Identifier Rewriting  │
                   │ - Post-Write Safety Validator │
                   └───────────────────────────────┘
```

- **Engine A: Semantic Model (TMDL)**: Parses and serializes tabular model definitions (`.tmdl`). Provides typed Pydantic models for tables, columns, and measures, with 100% round-trip fidelity preserving indentation, comments, and whitespace to prevent noisy git diffs.
- **Engine B: Report Layout (JSON Shadow Model)**: Ingests deeply nested `report.json` files, decodes embedded JSON strings (`config`, `query`, `dataTransforms`), and exposes a flat path-based addressing scheme and JMESPath querying.
- **Cross-Layer Integrity & Safety Net**: Coordinates atomic renames between TMDL and report layout JSON. Prevents destructive naive substring replacement, and runs an automated syntax safety net after writes.
- **In-Memory State Cache**: Uses `os.path.getmtime` timestamps to avoid redundant parsing across agent turns.

---

## 2. MCP Tools Overview

The server registers 11 tools over standard input/output (stdio):

### Semantic Model (Engine A)
| Tool | Description |
| :--- | :--- |
| `get_model_summary(project_path?)` | Returns a high-level summary of all tables, columns, and measures without flooding context. |
| `list_tables(project_path?)` | Lists tables with column and measure counts. |
| `get_table_schema(table_name, project_path?)` | Retrieves full schema for a table including columns, measures, and partitions. |
| `get_measure(table_name, measure_name, project_path?)` | Gets DAX formula, description, formatting, and lineage tag for a measure. |
| `update_measure_dax(table_name, measure_name, new_dax, project_path?)` | Updates a measure's DAX formula in TMDL cleanly while preserving comments and whitespace. |

### Report Layout (Engine B)
| Tool | Description |
| :--- | :--- |
| `list_pages(project_path?)` | Lists all report pages with names, ordinals, and visual counts. |
| `get_page_visuals(page_name_or_id, project_path?)` | Returns flat shadow models of all visuals on a page. |
| `get_visual_property(page_name_or_id, visual_id, property_path, project_path?)` | Query visual properties via path syntax (e.g. `position/width`, `visual_type`, `bound_fields`). |
| `query_report_layout(jmespath_query, project_path?)` | Executes JMESPath queries across the report layout shadow model. |

### Cross-Layer Integrity & Validation
| Tool | Description |
| :--- | :--- |
| `rename_measure(table_name, old_measure_name, new_measure_name, project_path?)` | Renames a measure across TMDL and updates all bound references in `report.json` (`queryRef`, `Name`, `Measure.Property`, `Restatement`, `NativeReferenceName`, and DAX formulas). |
| `validate_pbip_structure(project_path?)` | Runs post-write structural and syntax validation across descriptor, TMDLs, and report layout JSON. |

---

## 3. Running with Docker / Docker Compose

### Prerequisites
- Docker Engine & Docker Compose

### Run with Docker Compose
```bash
docker compose up -d
```

### Build and Run with Docker
```bash
# Build the image
docker build -t pbi-mcp-server:latest .

# Run container mounting local PBIP workspace
docker run --rm -i \
  -v $(pwd):/app/workspace:rw \
  -e PBI_WORKSPACE_PATH=/app/workspace \
  -e LOG_LEVEL=INFO \
  pbi-mcp-server:latest
```

---

## 4. Local Development

### Installation using uv
```bash
# Create virtual environment and install dependencies
uv sync

# Or install with pip
pip install -e .
```

### Running the Server Locally
```bash
export PBI_WORKSPACE_PATH=$(pwd)/sample_project.pbip
python -m mcp_server.main
```

### Running Tests
```bash
pytest -v
```

---

## 5. Integrating with MCP Clients

Add the server to your client configuration (e.g., Claude Desktop `claude_desktop_config.json` or Antigravity):

```json
{
  "mcpServers": {
    "powerbi": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "-v",
        "/absolute/path/to/pbip_project:/app/workspace:rw",
        "-e",
        "PBI_WORKSPACE_PATH=/app/workspace",
        "pbi-mcp-server:latest"
      ]
    }
  }
}
```

Or running directly with Python:

```json
{
  "mcpServers": {
    "powerbi": {
      "command": "python",
      "args": ["-m", "mcp_server.main"],
      "env": {
        "PBI_WORKSPACE_PATH": "/absolute/path/to/pbip_project"
      }
    }
  }
}
```
