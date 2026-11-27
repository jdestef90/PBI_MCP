# PBI_MCP `llama-server` Jinja Templates

These templates bake the compact 20-tool Power BI (`PBI_MCP`) catalog into your model's system prompt so `llama-server` can KV-cache it and the MCP server only needs to send **2 meta-tools** (`pbi_call` and `pbi_help`) per turn.

## Which template should I use?

| Template File | Model Families | Tool-Call Format |
| --- | --- | --- |
| `qwen.jinja` | **Qwen2.5, Qwen3, Qwen3.8** (non-Coder) | ChatML (`<|im_start|>`) + `<tool_call>` JSON |
| `qwen3_coder.jinja` | **Qwen3-Coder** | ChatML (`<|im_start|>`) + XML `<function=><parameter=>` |
| `gemma.jinja` | **Gemma 2, Gemma 3, MedGemma** | `<start_of_turn>` + JSON block |
| `gemma4.jinja` | **Gemma 4** (12B, 26B-A4B, etc.) | `<\|turn>` + `<\|call:...>` |
| `lfm2_5.jinja` | **Liquid AI LFM2.5, LFM3-Omni** | `<\|tool_list_start\|>` / `<\|tool_call_start\|>` |
| `mellum2_1.jinja` | **JetBrains Mellum 2.1** | ChatML (`<|im_start|>`) + `<tool_call>` JSON |
| `gpt_oss.jinja` | **OpenAI GPT-OSS** (20B / 120B) | Harmony (`<\|start\|>system<\|message\|>`) |

> **Tip for thinking/custom GGUFs:** If a GGUF has custom `<think>` or reasoning tokens, extract its native `tokenizer.chat_template` and inject the catalog into it directly:
> ```bash
> pbi-mcp-server --base-template my_model_native.jinja --output templates/my_model_pbi.jinja
> ```

## Usage

1. **Start `pbi-mcp-server` in meta-tools mode** (exposes only `pbi_call` and `pbi_help`):
   ```bash
   pbi-mcp-server --workspace /path/to/project.pbip --meta-tools
   ```
   *(Or set `PBI_MCP_META_TOOLS=true` in your MCP client env.)*

2. **Point `llama-server` to the matching template** (per-model in your preset INI):
   ```ini
   [Gemma-4-12b]
   chat-template-file = /path/to/PBI_MCP/templates/gemma4.jinja

   [GPT-OSS-20B]
   chat-template-file = /path/to/PBI_MCP/templates/gpt_oss.jinja
   ```

> **Note on Partition SQL tools:** `get_table_sql` and `update_table_sql` return compact, unescaped plain text (**Clean SQL View**, outline-first **SQL Section** navigation, and clean-SQL unified diffs) by default (`format="text"`), keeping prompt/response token usage low for sub-30B models. Pass `format="json"` if a caller explicitly requires JSON.

## Regenerating Checked-In Presets

Whenever tool signatures or descriptions change, regenerate all seven checked-in presets so they stay in sync with the live catalog (enforced by `tests/test_meta_tools_and_jinja.py`):

```bash
pbi-mcp-server --export-jinja all --output-dir templates
```
