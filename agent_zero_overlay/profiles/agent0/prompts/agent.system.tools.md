## Available local tools

Use exactly one JSON tool call with only `tool_name` and `tool_args`.

- `response`: final answer only. Required shape:
  `{"tool_name":"response","tool_args":{"text":"your answer"}}`
- `code_execution_tool`: run terminal, Python, or Node work. Use `runtime`,
  `code`, optional `session`, and optional `reset` in `tool_args`.
- `text_editor`: read, write, or patch a text file. Use `action` and `path`;
  `write` needs `content`.
- `search_engine`: search the web. Use `query`.
- `wait`: wait for a real in-progress task. Use `seconds`, `minutes`, or
  `until`.

Do not invent tools. For anything outside this compact set, explain the limit
with `response` instead of emitting malformed JSON.
