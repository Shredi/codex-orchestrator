# Codex hook payload fixtures

## ⚠ UNVERIFIED LIVE (2026-09-05)

**No Codex hook has ever fired on this machine.** Every payload in this
directory is reconstructed from the documented field lists in
`<claude-repo>/.workflow/scratch/codex-probe/protocol.md` (P1), which in turn
come from `developers.openai.com/codex/hooks` — **not** from a captured
`probe.log`. The probe's own log stayed empty across every run because Codex
gates hooks behind a *persisted hook-trust* decision that needs either one
interactive session accepting the prompt or a `--dangerously-bypass-hook-trust`
invocation from a normal terminal (see protocol.md, "BLOCKER").

So these fixtures prove the **adapter's** behaviour, not Codex's. What is
genuinely unknown, and what the adapter therefore treats tolerantly:

| Unknown | How the adapter copes |
|---|---|
| `tool_name` for the shell tool (UI shows `exec`) | `TOOL_NAME_MAP` accepts `shell`/`exec`/`local_shell`/`container.exec`/`run_command`/`bash` |
| `tool_name` for the subagent spawn (UI shows `collab: SpawnAgent`) | accepts `SpawnAgent`/`spawn_agent`/`collab__spawn_agent`/`collab.spawn_agent`/`create_agent`, and strips a `collab:` prefix |
| which `tool_input` key holds an apply_patch body | candidate list `PATCH_TEXT_KEYS` = patch, input, content, text, diff, patch_text |
| which `tool_input` key holds a spawn prompt | candidate list `PROMPT_KEYS` = prompt, task, instructions, input, message, developer_instructions, description |
| whether `matcher` is a regex (Claude-style) or a glob | hooks.json ships Claude-style anchored regexes **and** the adapter re-checks the tool name in-process (`CODEX_ADAPTER_TOOL_GATE`), so a matcher that over-matches still behaves |
| whether the plugin root variable is `${CODEX_PLUGIN_ROOT}` | only the adapter's own path uses it; `core/` is resolved relative to the adapter file |
| whether `apply_patch` is exposed at all under the current auth | on plain `gpt-5` + API-key auth it was **not** (protocol.md); the shell-only fallback (`printf > file`) is invisible to the write guard either way |

## Verifying against reality

Once the hook-trust prompt has been accepted once:

```sh
CODEX_ADAPTER_DEBUG=/tmp/codex-adapter.jsonl codex "…"
```

Every fire appends `{"guard": …, "raw": <Codex payload>, "normalised": [<Claude payloads>]}`.
Replace the fixtures here with the `raw` objects, drop this warning, and flip
the README's "verified live" table to yes.

## Files

| fixture | event | notes |
|---|---|---|
| `session_start.json` | SessionStart | `source`, `model` (`gpt-6-astra` → FABLE profile) |
| `user_prompt_submit.json` | UserPromptSubmit | slash-command prompt (`/sync`) |
| `pre_tool_use_spawn.json` | PreToolUse | `SpawnAgent`; `PLACEHOLDER_PROMPT` is replaced per test |
| `pre_tool_use_apply_patch.json` | PreToolUse | `apply_patch`; `PLACEHOLDER_PATH` is replaced per test |
| `post_tool_use_apply_patch.json` | PostToolUse | same + `tool_response` |
| `pre_tool_use_shell.json` | PreToolUse | `shell` with an argv-list command; must reach no guard |
| `stop.json` | Stop | `stop_hook_active: false`; tests flip it |
| `session_end.json` | SessionEnd | `reason` |
