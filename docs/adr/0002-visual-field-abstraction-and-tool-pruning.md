# Tool Surface Simplification and High-Level Visual Mutations

To prevent LLM agent context exhaustion and tool invocation failures, the MCP surface is pruned from ~26 granular endpoints to a curated, workflow-oriented suite. Ad-hoc scripts (SSN masking) and duplicate discovery queries are removed. Rather than exposing raw JMESPath or requiring agents to synthesize deeply nested PBIR JSON transforms, visual editing is encapsulated in a dedicated `update_visual_fields` domain tool that manages query projections and data transforms safely.
