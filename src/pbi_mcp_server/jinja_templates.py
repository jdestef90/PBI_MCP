from __future__ import annotations

import re
from pathlib import Path

from .errors import InvalidArgumentError

PRESET_NAMES: tuple[str, ...] = (
    "qwen",
    "qwen3_coder",
    "gemma",
    "gemma4",
    "lfm2_5",
    "mellum2_1",
    "gpt_oss",
)

_MARKER_START = "{#- PBI_MCP_CATALOG_START -#}"
_MARKER_END = "{#- PBI_MCP_CATALOG_END -#}"


def _catalog_set_block(catalog_text: str) -> str:
    """Wrap catalog_text in a Jinja2 set+raw block safe against template syntax."""
    return (
        f"{_MARKER_START}\n"
        "{%- set _pbi_tools_catalog -%}\n"
        "{% raw %}\n"
        f"{catalog_text.rstrip()}\n"
        "{% endraw %}\n"
        "{%- endset -%}\n"
        f"{_MARKER_END}\n"
    )


def _qwen_template(catalog_text: str) -> str:
    """Qwen2.5 / Qwen3 ChatML template with baked-in PBI_MCP catalog and <tool_call> support."""
    return _catalog_set_block(catalog_text) + (
        "{%- if tools %}\n"
        "    {{- '<|im_start|>system\\n' }}\n"
        "    {%- if messages[0].role == 'system' %}\n"
        "        {{- messages[0].content + '\\n\\n' }}\n"
        "    {%- endif %}\n"
        "    {{- _pbi_tools_catalog + '\\n\\n' }}\n"
        '    {{- "# Tools\\n\\nYou may call one or more functions to assist with the user query.\\n\\n'
        'You are provided with function signatures within <tools></tools> XML tags:\\n<tools>" }}\n'
        "    {%- for tool in tools %}\n"
        "        {{- '\\n' }}\n"
        "        {{- tool | tojson }}\n"
        "    {%- endfor %}\n"
        '    {{- "\\n</tools>\\n\\nFor each function call, return a json object with function name and '
        'arguments within <tool_call></tool_call> XML tags:\\n<tool_call>\\n'
        '{\\"name\\": <function-name>, \\"arguments\\": <args-json-object>}\\n</tool_call><|im_end|>\\n" }}\n'
        "{%- else %}\n"
        "    {{- '<|im_start|>system\\n' }}\n"
        "    {%- if messages[0].role == 'system' %}\n"
        "        {{- messages[0].content + '\\n\\n' }}\n"
        "    {%- endif %}\n"
        "    {{- _pbi_tools_catalog + '<|im_end|>\\n' }}\n"
        "{%- endif %}\n"
        "{%- for message in messages %}\n"
        "    {%- if (message.role == 'user') or (message.role == 'system' and not loop.first) %}\n"
        "        {{- '<|im_start|>' + message.role + '\\n' + message.content + '<|im_end|>\\n' }}\n"
        "    {%- elif message.role == 'assistant' %}\n"
        "        {{- '<|im_start|>' + message.role }}\n"
        "        {%- if message.content %}\n"
        "            {{- '\\n' + message.content }}\n"
        "        {%- endif %}\n"
        "        {%- for tool_call in message.tool_calls | default([]) %}\n"
        "            {%- if tool_call.function is defined %}\n"
        "                {%- set tool_call = tool_call.function %}\n"
        "            {%- endif %}\n"
        "            {{- '\\n<tool_call>\\n{\"name\": \"' }}\n"
        "            {{- tool_call.name }}\n"
        "            {{- '\", \"arguments\": ' }}\n"
        "            {{- tool_call.arguments | tojson }}\n"
        "            {{- '}\\n</tool_call>' }}\n"
        "        {%- endfor %}\n"
        "        {{- '<|im_end|>\\n' }}\n"
        "    {%- elif message.role == 'tool' %}\n"
        "        {%- if (loop.index0 == 0) or (messages[loop.index0 - 1].role != 'tool') %}\n"
        "            {{- '<|im_start|>user' }}\n"
        "        {%- endif %}\n"
        "        {{- '\\n<tool_response>\\n' }}\n"
        "        {{- message.content }}\n"
        "        {{- '\\n</tool_response>' }}\n"
        "        {%- if loop.last or (messages[loop.index0 + 1].role != 'tool') %}\n"
        "            {{- '<|im_end|>\\n' }}\n"
        "        {%- endif %}\n"
        "    {%- endif %}\n"
        "{%- endfor %}\n"
        "{%- if add_generation_prompt %}\n"
        "    {{- '<|im_start|>assistant\\n' }}\n"
        "{%- endif %}\n"
    )


def _gemma_template(catalog_text: str) -> str:
    """Gemma 2/3/4 turn-based template with baked-in PBI_MCP catalog and JSON tool calls."""
    return _catalog_set_block(catalog_text) + (
        "{{- bos_token }}\n"
        "{%- set ns = namespace(sys_prefix='') %}\n"
        "{%- if messages[0].role == 'system' %}\n"
        "    {%- set ns.sys_prefix = messages[0].content + '\\n\\n' %}\n"
        "{%- endif %}\n"
        "{%- set ns.sys_prefix = ns.sys_prefix + _pbi_tools_catalog %}\n"
        "{%- if tools %}\n"
        "    {%- set ns.sys_prefix = ns.sys_prefix + '\\n\\n## Available MCP Functions\\n' %}\n"
        "    {%- for tool in tools %}\n"
        "        {%- set ns.sys_prefix = ns.sys_prefix + '\\n' + (tool | tojson) %}\n"
        "    {%- endfor %}\n"
        '    {%- set ns.sys_prefix = ns.sys_prefix + \'\\n\\nTo call a function, respond with JSON: '
        '```json\\n{"name": "<fn>", "arguments": {...}}\\n```\' %}\n'
        "{%- endif %}\n"
        "{%- set ns.injected = false %}\n"
        "{%- for message in messages %}\n"
        "    {%- if message.role == 'system' %}\n"
        "        {# merged into first user turn #}\n"
        "    {%- elif message.role == 'user' %}\n"
        "        {{- '<start_of_turn>user\\n' }}\n"
        "        {%- if not ns.injected %}\n"
        "            {{- ns.sys_prefix + '\\n\\n' }}\n"
        "            {%- set ns.injected = true %}\n"
        "        {%- endif %}\n"
        "        {{- message.content + '<end_of_turn>\\n' }}\n"
        "    {%- elif message.role == 'assistant' %}\n"
        "        {{- '<start_of_turn>model\\n' }}\n"
        "        {%- if message.content %}\n"
        "            {{- message.content }}\n"
        "        {%- endif %}\n"
        "        {%- for tool_call in message.tool_calls | default([]) %}\n"
        "            {%- if tool_call.function is defined %}\n"
        "                {%- set tool_call = tool_call.function %}\n"
        "            {%- endif %}\n"
        "            {{- '```json\\n{\"name\": \"' + tool_call.name + '\", \"arguments\": ' + (tool_call.arguments | tojson) + '}\\n```' }}\n"
        "        {%- endfor %}\n"
        "        {{- '<end_of_turn>\\n' }}\n"
        "    {%- elif message.role == 'tool' %}\n"
        "        {{- '<start_of_turn>user\\n<tool_response>\\n' + message.content + '\\n</tool_response><end_of_turn>\\n' }}\n"
        "    {%- endif %}\n"
        "{%- endfor %}\n"
        "{%- if add_generation_prompt %}\n"
        "    {{- '<start_of_turn>model\\n' }}\n"
        "{%- endif %}\n"
    )


def _lfm2_5_template(catalog_text: str) -> str:
    """Liquid AI LFM2.5 template with baked-in PBI_MCP catalog and LFM tool tokens."""
    return _catalog_set_block(catalog_text) + (
        "{{- '<|startoftext|>' }}\n"
        "{{- '<|im_start|>system\\n' }}\n"
        "{%- if messages[0].role == 'system' %}\n"
        "    {{- messages[0].content + '\\n\\n' }}\n"
        "{%- endif %}\n"
        "{{- _pbi_tools_catalog }}\n"
        "{%- if tools %}\n"
        "    {{- '\\n\\nList of tools: <|tool_list_start|>[' }}\n"
        "    {%- for tool in tools %}\n"
        "        {{- tool | tojson }}\n"
        "        {%- if not loop.last %}{{- ', ' }}{%- endif %}\n"
        "    {%- endfor %}\n"
        "    {{- ']<|tool_list_end|>' }}\n"
        "{%- endif %}\n"
        "{{- '<|im_end|>\\n' }}\n"
        "{%- for message in messages %}\n"
        "    {%- if (message.role == 'user') or (message.role == 'system' and not loop.first) %}\n"
        "        {{- '<|im_start|>' + message.role + '\\n' + message.content + '<|im_end|>\\n' }}\n"
        "    {%- elif message.role == 'assistant' %}\n"
        "        {{- '<|im_start|>assistant\\n' }}\n"
        "        {%- if message.tool_calls %}\n"
        "            {{- '<|tool_call_start|>[' }}\n"
        "            {%- for tool_call in message.tool_calls %}\n"
        "                {%- if tool_call.function is defined %}\n"
        "                    {%- set tool_call = tool_call.function %}\n"
        "                {%- endif %}\n"
        "                {{- '{\"name\": \"' + tool_call.name + '\", \"arguments\": ' + (tool_call.arguments | tojson) + '}' }}\n"
        "                {%- if not loop.last %}{{- ', ' }}{%- endif %}\n"
        "            {%- endfor %}\n"
        "            {{- ']<|tool_call_end|>' }}\n"
        "        {%- endif %}\n"
        "        {%- if message.content %}\n"
        "            {{- message.content }}\n"
        "        {%- endif %}\n"
        "        {{- '<|im_end|>\\n' }}\n"
        "    {%- elif message.role == 'tool' %}\n"
        "        {{- '<|im_start|>tool\\n<|tool_response_start|>' + message.content + '<|tool_response_end|><|im_end|>\\n' }}\n"
        "    {%- endif %}\n"
        "{%- endfor %}\n"
        "{%- if add_generation_prompt %}\n"
        "    {{- '<|im_start|>assistant\\n' }}\n"
        "{%- endif %}\n"
    )


def _mellum2_1_template(catalog_text: str) -> str:
    """JetBrains Mellum 2.1 ChatML template with baked-in PBI_MCP catalog."""
    return _catalog_set_block(catalog_text) + (
        "{{- '<|im_start|>system\\n' }}\n"
        "{%- if messages[0].role == 'system' %}\n"
        "    {{- messages[0].content + '\\n\\n' }}\n"
        "{%- endif %}\n"
        "{{- _pbi_tools_catalog }}\n"
        "{%- if tools %}\n"
        "    {{- '\\n\\n## Callable MCP Tools\\n' }}\n"
        "    {%- for tool in tools %}\n"
        "        {{- '\\n' + (tool | tojson) }}\n"
        "    {%- endfor %}\n"
        '    {{- \'\\n\\nReturn tool calls as <tool_call>{"name": "<name>", "arguments": {...}}</tool_call>.\' }}\n'
        "{%- endif %}\n"
        "{{- '<|im_end|>\\n' }}\n"
        "{%- for message in messages %}\n"
        "    {%- if (message.role == 'user') or (message.role == 'system' and not loop.first) %}\n"
        "        {{- '<|im_start|>' + message.role + '\\n' + message.content + '<|im_end|>\\n' }}\n"
        "    {%- elif message.role == 'assistant' %}\n"
        "        {{- '<|im_start|>assistant\\n' }}\n"
        "        {%- if message.content %}\n"
        "            {{- message.content }}\n"
        "        {%- endif %}\n"
        "        {%- for tool_call in message.tool_calls | default([]) %}\n"
        "            {%- if tool_call.function is defined %}\n"
        "                {%- set tool_call = tool_call.function %}\n"
        "            {%- endif %}\n"
        "            {{- '\\n<tool_call>{\"name\": \"' + tool_call.name + '\", \"arguments\": ' + (tool_call.arguments | tojson) + '}</tool_call>' }}\n"
        "        {%- endfor %}\n"
        "        {{- '<|im_end|>\\n' }}\n"
        "    {%- elif message.role == 'tool' %}\n"
        "        {{- '<|im_start|>tool\\n<tool_response>\\n' + message.content + '\\n</tool_response><|im_end|>\\n' }}\n"
        "    {%- endif %}\n"
        "{%- endfor %}\n"
        "{%- if add_generation_prompt %}\n"
        "    {{- '<|im_start|>assistant\\n' }}\n"
        "{%- endif %}\n"
    )


def _qwen3_coder_template(catalog_text: str) -> str:
    """Qwen3-Coder ChatML template with baked-in PBI_MCP catalog and XML <function=...> tool calls."""
    return _catalog_set_block(catalog_text) + (
        "{%- if tools %}\n"
        "    {{- '<|im_start|>system\\n' }}\n"
        "    {%- if messages[0].role == 'system' %}\n"
        "        {{- messages[0].content + '\\n\\n' }}\n"
        "    {%- endif %}\n"
        "    {{- _pbi_tools_catalog + '\\n\\n' }}\n"
        '    {{- "# Tools\\n\\nYou have access to the following functions:\\n\\n<tools>" }}\n'
        "    {%- for tool in tools %}\n"
        "        {{- '\\n<function>' + (tool | tojson) + '</function>' }}\n"
        "    {%- endfor %}\n"
        '    {{- "\\n</tools>\\n\\nIf you choose to call a function ONLY reply in the following format:\\n'
        '<tool_call>\\n<function=example_function_name>\\n<parameter=example_parameter_1>value_1</parameter>\\n'
        '</function>\\n</tool_call><|im_end|>\\n" }}\n'
        "{%- else %}\n"
        "    {{- '<|im_start|>system\\n' }}\n"
        "    {%- if messages[0].role == 'system' %}\n"
        "        {{- messages[0].content + '\\n\\n' }}\n"
        "    {%- endif %}\n"
        "    {{- _pbi_tools_catalog + '<|im_end|>\\n' }}\n"
        "{%- endif %}\n"
        "{%- for message in messages %}\n"
        "    {%- if (message.role == 'user') or (message.role == 'system' and not loop.first) %}\n"
        "        {{- '<|im_start|>' + message.role + '\\n' + message.content + '<|im_end|>\\n' }}\n"
        "    {%- elif message.role == 'assistant' %}\n"
        "        {{- '<|im_start|>assistant' }}\n"
        "        {%- if message.content %}\n"
        "            {{- '\\n' + message.content }}\n"
        "        {%- endif %}\n"
        "        {%- for tool_call in message.tool_calls | default([]) %}\n"
        "            {%- if tool_call.function is defined %}\n"
        "                {%- set tool_call = tool_call.function %}\n"
        "            {%- endif %}\n"
        "            {{- '\\n<tool_call>\\n<function=' + tool_call.name + '>\\n' }}\n"
        "            {%- for k, v in (tool_call.arguments | default({})).items() %}\n"
        "                {{- '<parameter=' + k + '>' + (v if v is string else (v | tojson)) + '</parameter>\\n' }}\n"
        "            {%- endfor %}\n"
        "            {{- '</function>\\n</tool_call>' }}\n"
        "        {%- endfor %}\n"
        "        {{- '<|im_end|>\\n' }}\n"
        "    {%- elif message.role == 'tool' %}\n"
        "        {{- '<|im_start|>user\\n<tool_response>\\n' + message.content + '\\n</tool_response><|im_end|>\\n' }}\n"
        "    {%- endif %}\n"
        "{%- endfor %}\n"
        "{%- if add_generation_prompt %}\n"
        "    {{- '<|im_start|>assistant\\n' }}\n"
        "{%- endif %}\n"
    )


def _gemma4_template(catalog_text: str) -> str:
    """Gemma 4 <|turn> template with baked-in PBI_MCP catalog and <|call:...> tool support."""
    return _catalog_set_block(catalog_text) + (
        "{{- bos_token }}\n"
        "{{- '<|turn>system\\n' }}\n"
        "{%- if messages[0].role == 'system' %}\n"
        "    {{- messages[0].content + '\\n\\n' }}\n"
        "{%- endif %}\n"
        "{{- _pbi_tools_catalog }}\n"
        "{%- if tools %}\n"
        "    {{- '\\n\\n## Callable MCP Tools\\n' }}\n"
        "    {%- for tool in tools %}\n"
        "        {{- '\\n' + (tool | tojson) }}\n"
        "    {%- endfor %}\n"
        "{%- endif %}\n"
        "{{- '<turn|>\\n' }}\n"
        "{%- for message in messages %}\n"
        "    {%- if message.role == 'system' and loop.first %}\n"
        "        {# emitted above #}\n"
        "    {%- elif message.role == 'user' or message.role == 'system' %}\n"
        "        {{- '<|turn>' + message.role + '\\n' + message.content + '<turn|>\\n' }}\n"
        "    {%- elif message.role == 'assistant' %}\n"
        "        {{- '<|turn>model\\n' }}\n"
        "        {%- if message.content %}\n"
        "            {{- message.content }}\n"
        "        {%- endif %}\n"
        "        {%- for tool_call in message.tool_calls | default([]) %}\n"
        "            {%- if tool_call.function is defined %}\n"
        "                {%- set tool_call = tool_call.function %}\n"
        "            {%- endif %}\n"
        "            {{- '<|call:' + tool_call.name + (tool_call.arguments | tojson) + '<call|>' }}\n"
        "        {%- endfor %}\n"
        "        {{- '<turn|>\\n' }}\n"
        "    {%- elif message.role == 'tool' %}\n"
        "        {{- '<|turn>user\\n<|tool_response>' + message.content + '<tool_response|><turn|>\\n' }}\n"
        "    {%- endif %}\n"
        "{%- endfor %}\n"
        "{%- if add_generation_prompt %}\n"
        "    {{- '<|turn>model\\n' }}\n"
        "{%- endif %}\n"
    )


def _gpt_oss_template(catalog_text: str) -> str:
    """OpenAI GPT-OSS Harmony template with baked-in PBI_MCP catalog."""
    return _catalog_set_block(catalog_text) + (
        "{{- '<|start|>system<|message|>' }}\n"
        "{%- if messages[0].role == 'system' %}\n"
        "    {{- messages[0].content + '\\n\\n' }}\n"
        "{%- endif %}\n"
        "{{- _pbi_tools_catalog }}\n"
        "{%- if tools %}\n"
        "    {{- '\\n\\n# Tools\\n' }}\n"
        "    {%- for tool in tools %}\n"
        "        {{- '\\n' + (tool | tojson) }}\n"
        "    {%- endfor %}\n"
        "{%- endif %}\n"
        "{{- '<|end|>' }}\n"
        "{%- for message in messages %}\n"
        "    {%- if message.role == 'system' and loop.first %}\n"
        "        {# emitted above #}\n"
        "    {%- elif message.role == 'user' or message.role == 'system' %}\n"
        "        {{- '<|start|>' + message.role + '<|message|>' + message.content + '<|end|>' }}\n"
        "    {%- elif message.role == 'assistant' %}\n"
        "        {%- if message.content %}\n"
        "            {{- '<|start|>assistant<|channel|>final<|message|>' + message.content + '<|end|>' }}\n"
        "        {%- endif %}\n"
        "        {%- for tool_call in message.tool_calls | default([]) %}\n"
        "            {%- if tool_call.function is defined %}\n"
        "                {%- set tool_call = tool_call.function %}\n"
        "            {%- endif %}\n"
        "            {{- '<|start|>assistant<|channel|>commentary to=functions.' + tool_call.name + '<|constrain|>json<|message|>' + (tool_call.arguments | tojson) + '<|call|>' }}\n"
        "        {%- endfor %}\n"
        "    {%- elif message.role == 'tool' %}\n"
        "        {{- '<|start|>functions' + '<|message|>' + message.content + '<|end|>' }}\n"
        "    {%- endif %}\n"
        "{%- endfor %}\n"
        "{%- if add_generation_prompt %}\n"
        "    {{- '<|start|>assistant' }}\n"
        "{%- endif %}\n"
    )


_PRESET_BUILDERS = {
    "qwen": _qwen_template,
    "qwen3_coder": _qwen3_coder_template,
    "gemma": _gemma_template,
    "gemma4": _gemma4_template,
    "lfm2_5": _lfm2_5_template,
    "mellum2_1": _mellum2_1_template,
    "gpt_oss": _gpt_oss_template,
}


def render_preset_template(preset: str, catalog_text: str) -> str:
    """Render a ready-to-use llama-server Jinja template for a model preset."""
    key = preset.strip().lower()
    builder = _PRESET_BUILDERS.get(key)
    if builder is None:
        raise InvalidArgumentError(
            f"Unknown Jinja preset {preset!r}. Expected one of: {', '.join(PRESET_NAMES)}."
        )
    return builder(catalog_text)


def inject_into_base_template(base_template: str, catalog_text: str) -> str:
    """Inject the PBI_MCP tool catalog into an existing GGUF Jinja chat template."""
    # Strip any previous PBI_MCP catalog block if re-running on an already-injected template.
    cleaned = re.sub(
        re.escape(_MARKER_START) + r".*?" + re.escape(_MARKER_END) + r"\n?",
        "",
        base_template,
        flags=re.DOTALL,
    )
    header = _catalog_set_block(catalog_text)

    if "_pbi_tools_catalog" in cleaned:
        return header + cleaned

    loop_match = re.search(r"(\{%-?\s*for\s+\w+\s+in\s+messages\s*-?%\})", cleaned)

    # ChatML-style templates (Qwen, Mellum, LFM2.5/3): inject system block if none exists.
    if "<|im_start|>system" in cleaned and loop_match:
        injection = (
            "{%- if not messages or messages[0].role != 'system' %}\n"
            "    {{- '<|im_start|>system\\n' + _pbi_tools_catalog + '<|im_end|>\\n' }}\n"
            "{%- endif %}\n"
        )
        idx = loop_match.start()
        return header + cleaned[:idx] + injection + cleaned[idx:]

    # OpenAI GPT-OSS Harmony templates (<|start|>system<|message|>): inject before message loop.
    if "<|start|>system<|message|>" in cleaned and loop_match:
        injection = "{{- '<|start|>system<|message|>' + _pbi_tools_catalog + '<|end|>' }}\n"
        idx = loop_match.start()
        return header + cleaned[:idx] + injection + cleaned[idx:]

    # Gemma 4 templates (<|turn>...<turn|>): inject system turn before message loop.
    if "<|turn>" in cleaned and loop_match:
        injection = "{{- '<|turn>system\\n' + _pbi_tools_catalog + '<turn|>\\n' }}\n"
        idx = loop_match.start()
        return header + cleaned[:idx] + injection + cleaned[idx:]

    # Gemma 2/3 templates (<start_of_turn>): prepend catalog before first user content.
    if "<start_of_turn>" in cleaned:
        return (
            header
            + "{%- set _pbi_ns = namespace(done=false) %}\n"
            + re.sub(
                r"(<start_of_turn>user\\n)",
                r"\1{%- if not _pbi_ns.done %}{{- _pbi_tools_catalog + '\\n\\n' }}{%- set _pbi_ns.done = true %}{%- endif %}",
                cleaned,
                count=1,
            )
        )

    # Generic fallback: emit catalog right before the message loop.
    if loop_match:
        idx = loop_match.start()
        return header + cleaned[:idx] + "{{- _pbi_tools_catalog + '\\n\\n' }}\n" + cleaned[idx:]

    return header + "{{- _pbi_tools_catalog + '\\n\\n' }}\n" + cleaned


def write_preset_templates(output_dir: Path, catalog_text: str) -> list[Path]:
    """Write all preset .jinja files into output_dir and return their paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for preset in PRESET_NAMES:
        target = output_dir / f"{preset}.jinja"
        target.write_text(render_preset_template(preset, catalog_text), encoding="utf-8")
        written.append(target)
    return written
