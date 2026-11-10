# PBI MCP

Local-first MCP server for inspecting and safely editing Power BI PBIP projects.

## Language

**Workspace**:
A resolved Power BI Project directory containing a `.pbip` file or paired `.Report` and `.SemanticModel` folders.
_Avoid_: Project folder, report root

**Semantic Model**:
The Tabular Model Definition Language (TMDL) metadata layer defining tables, columns, relationships, partitions, and measures.
_Avoid_: Dataset, data model

**Report Layout**:
The Power BI Enhanced Report Format (PBIR / PBIR-legacy) JSON structures defining pages, visuals, visual properties, and field bindings.
_Avoid_: Report template, canvas config

**Visual Field Binding**:
The association between a visual's visual roles (e.g. Y-axis, Tooltips, Values) and a Semantic Model measure or column.
_Avoid_: Visual property, data projection

**Visual Role**:
A designated data shelf on a visual (such as Category, Values, Legend, or Tooltips) that receives field bindings.
_Avoid_: Axis property, field bucket

**Unused Object**:
A column or measure in the Semantic Model with zero references across all DAX formulas and Report Layout visual field bindings.
_Avoid_: Orphan field, dead code

**Best Practice Analyzer (BPA)**:
Rule-based static analysis auditing the Semantic Model for performance, relationship integrity, and modeling standards.
_Avoid_: Model linter, rule checker

**DAX Measure**:
A dynamic calculation defined in TMDL associated with a table.
_Avoid_: Metric formula, calculated field

**Partition SQL**:
The source SQL query embedded within a table's TMDL partition M-expression (e.g., `Value.NativeQuery`).
_Avoid_: DirectQuery script, source query
