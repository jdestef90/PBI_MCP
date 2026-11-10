# Power BI Analyst Workflow Enhancements

To empower Power BI analysts in daily model optimization and documentation, the MCP server incorporates four high-leverage analyst capabilities:
1. `find_unused_objects`: Audits the Semantic Model against all DAX formulas and PBIR visual bindings to detect unreferenced columns and measures.
2. `format_dax`: Standardizes DAX formatting according to tabular best practices before committing to TMDL.
3. `generate_measure_docs`: Formulates and inserts structured TMDL documentation descriptions into measure declarations.
4. `check_model_bpa`: Evaluates the model against foundational Best Practice Analyzer rules (e.g., unhidden foreign keys, bidirectional relationships, and missing descriptions).
