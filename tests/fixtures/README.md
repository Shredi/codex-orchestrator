# Codex hook payload fixtures

## Captured live — Codex CLI 0.153.4, 2026-09-05

Every file here is a **real Codex hook payload**, taken verbatim from
`<claude-repo>/.workflow/scratch/codex-probe/probe.log` (key
`stdin_payload`), written by a user-level observe-only hook set that logs
and always exits 0. Only two things were normalised for the tests: the
`session_id` is swapped for `codex-fixture-session` by the `fixture()`
helper, and tool bodies carry a `PLACEHOLDER_*` the tests replace.
Session ids and transcript paths are otherwise left as captured.

What the captures settled — the previous, docs-derived generation of
these fixtures guessed at least one of these wrong in every row:

| question | live answer |
|---|---|
| payload shape | identical to Claude Code's, plus `turn_id` and `tool_use_id`; `model` and `permission_mode` on every event |
| shell tool name | **`Bash`** (not `shell`/`exec`/`local_shell`) |
| patch tool name | **`apply_patch`**, exposed on a ChatGPT account with `gpt-5.6-luna` |
| where the patch body lives | **`tool_input["command"]`** — the whole `*** Begin Patch` envelope; no `patch`/`input`/`diff` key exists |
| spawn tool name | **`collaborationspawn_agent`** (feature `multi_agent_v2`, `--enable multi_agent_v2`) |
| spawn tool input | `{task_name, agent_type, fork_turns, message}` — `message` is a **Fernet ciphertext**, never the prompt |
| matcher syntax | Claude-style regex; the anchored `^(…)$` forms in `hooks/hooks.json` match |
| plugin root variable | **`CLAUDE_PLUGIN_ROOT`** (Codex exports the Claude-compatible name; `CODEX_PLUGIN_ROOT` never expands) |
| Stop payload | carries `stop_hook_active` and `last_assistant_message`; the retry sets `stop_hook_active: true` |
| SessionEnd | `reason` present, `model` absent; Codex clamps the hook timeout to 3 s |

## Files

| fixture | event | notes |
|---|---|---|
| `session_start.json` | SessionStart | live `gpt-5.6-luna`; tests override `model` for the tier assertions |
| `user_prompt_submit.json` | UserPromptSubmit | prompt normalised to `/sync` (slash-command passthrough) |
| `pre_tool_use_spawn.json` | PreToolUse | `collaborationspawn_agent`; `message` = `PLACEHOLDER_PROMPT` |
| `pre_tool_use_apply_patch.json` | PreToolUse | `apply_patch`; `command` = an envelope with `PLACEHOLDER_PATH` |
| `post_tool_use_apply_patch.json` | PostToolUse | same + a real `tool_response` shape |
| `pre_tool_use_shell.json` | PreToolUse | `Bash`; the write-guard tests put a shim/redirect command here |
| `post_tool_use_shell.json` | PostToolUse | `Bash` + `tool_response` |
| `stop.json` | Stop | `stop_hook_active: false`; tests flip it |
| `session_end.json` | SessionEnd | `reason: other` |

## Re-capturing

The observe-only probe at `~/.codex/hooks.json` is still installed and
still appends to `probe.log`. To watch what the *adapter* makes of a
payload instead:

```sh
CODEX_ADAPTER_DEBUG=/tmp/codex-adapter.jsonl \
  codex exec --dangerously-bypass-hook-trust --sandbox workspace-write \
             -m gpt-5.6-luna '…'
```

Each fire appends `{"guard", "raw", "normalised"}`. (Setting the variable
to `1` writes to `$TMPDIR/codex-adapter-debug.jsonl` rather than a file
called `1` in the cwd.)
