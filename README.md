# codex-orchestrator

Codex CLI counterpart to [`fable5-opus5-orchestrator`](https://github.com/Shredi/fable5-opus5-orchestrator)
(the `orchestrator@fable-orchestrator` Claude Code plugin). Same Requirements
Ledger, same guard discipline, same tier-routed chair/worker model — Codex
native hooks, subagent TOML files and skills instead of Claude Code's.

## Purpose

Marc runs Claude Code when the Anthropic subscription is active and Codex CLI
when the OpenAI subscription is active (plus OpenRouter free/stealth models
on the side). Both harnesses should behave identically: a Requirements Ledger
in `.workflow/LEDGER-*.md`, guard hooks that block ungoverned spawns/writes/
stops, and chair/worker tiers routed by profile rather than a per-spawn effort
dial. This repo is the Codex side of that pair.

## Two-repo model

The Python guards, instruction text and the playbook skill are the **core**
and live in `fable5-opus5-orchestrator` (the Claude repo, which owns them). A
fix lands there, gets tagged (`core-vN`), and is pulled into this repo via
`git subtree` under `core/`. This repo never edits `core/` directly — changes
go upstream, get tagged, then get pulled here. See `make pull-core` below.

```
fable5-opus5-orchestrator/   owns the core, tags core-vN releases
codex-orchestrator/          this repo — vendors core/ via git subtree,
                              adds the Codex adapter + tier profiles + sync tool
```

## Layout

- `.codex-plugin/plugin.json` — plugin manifest (see "Plugin manifest" below).
- `core/` — `git subtree` vendor of `fable5-opus5-orchestrator` at tag
  `core-v1` (see `core/VERSION`). `git subtree add` pulls the **whole** source
  repo tree into `core/`, not just `scripts/instructions/skills/playbook/tests`
  — subtree only vendors at repo granularity. The extra files (this repo's
  own `.github/`, `.claude-plugin/`, top-level `README.md`/`LICENSE` duplicated
  inside `core/`) are harmless dead weight, left in place rather than pruned
  by hand (pruning would fight every future `pull-core`).
- `scripts/codex_adapter.py` + `hooks/hooks.json` — the hook layer: Codex
  payload → Claude payload → core guard → Codex output, with the tool-name
  map. See **"The adapter"** below.
- `agents/{sonnet,opus,fable}.toml` — tier subagents, generated from
  `profiles/openai.toml` (committed as the openai-profile output; regenerate
  with `codex-sync` if the profile changes).
- `profiles/{openai,openrouter-free,anthropic-api}.toml` — tier → model map.
- `bin/codex-sync` — generates `~/.codex/agents/*.toml`, `<repo>/.codex/agents/*.toml`
  from `<repo>/.claude/agents/*.md`, an `AGENTS.md` stub, the `~/.agents/skills`
  symlink farm, and an `[mcp_servers]` snippet from the Claude MCP config.
- `tests/` — `codex-sync` unit tests (frontmatter translation, idempotency,
  dry-run, AGENTS.md never-overwritten, secret redaction), profile schema
  tests, and the adapter tests (`test_adapter.py` + `fixtures/`).
  `core/tests/` (vendored, unmodified) proves core/ itself.

## The adapter

`scripts/codex_adapter.py <guard-name>` is the whole Codex↔core bridge —
one process per hook fire, stdlib only, and the only place a harness
difference is ever absorbed. `core/` is a subtree and is **never** edited
here; a core fix goes upstream, gets tagged, and comes back via
`make pull-core`.

```
Codex hook stdin JSON
  └─ normalise → the Claude Code payload the guard was written against
        (session_id, cwd, transcript_path, prompt, source, model,
         stop_hook_active, tool_name, tool_input, hook_event_name)
  └─ subprocess: core/scripts/<guard>.py   (unmodified, CLAUDE_PLUGIN_ROOT=core/)
  └─ translate stdout → Codex output schema, exit 0
```

**Guards and events** (`hooks/hooks.json`, one entry each; `command` =
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/codex_adapter.py" <guard>`,
`commandWindows` = the same with `python` and backslashes, for MGMT01):

| Codex event | guard | output |
|---|---|---|
| SessionStart | `inject_instructions` | `additionalContext` |
| UserPromptSubmit | `cold_cache_guard` | `decision: block` (+ 3-min re-send ack) |
| PreToolUse (spawn tools) | `ledger_guard_spawn` | `permissionDecision: deny` |
| PreToolUse (apply_patch) | `ledger_guard_write` | `permissionDecision: deny` |
| PostToolUse (apply_patch) | `ledger_bind` | none (session↔ledger binding) |
| Stop | `ledger_guard_stop` | `decision: block`, once per session |
| SessionEnd | `cleanup_session_cache` | none |

**Tool-name map** (data at the top of the adapter, `TOOL_NAME_MAP`):
shell/`exec`/`local_shell`/`container.exec` → `Bash`; `apply_patch` (and
`write_file`/`edit_file` spellings) → `Write` at PreToolUse, `Edit` at
PostToolUse; `SpawnAgent`/`collab__spawn_agent`/`create_agent` → `Agent`
with the spawn prompt mapped onto `tool_input.prompt`, so the spawn
guard's 1500-char gate and its `subagent_type: fork` bypass both work.
For `apply_patch` the adapter parses the patch envelope
(`*** Add/Update/Delete File:`, `*** Move to:`, unified-diff `+++` as a
fallback), absolutises each path against `cwd`, and runs the guard **once
per LEDGER-matching path** — a patch touching five files still gets its
ledger path checked, not just the first hunk.

The hooks.json matchers are Claude-style anchored regexes over the Codex
tool names, **and** the adapter re-checks the mapped tool name in-process
(`CODEX_ADAPTER_TOOL_GATE=0` disables), because Codex's matcher syntax is
not verified live: an over-matching matcher still behaves, an
under-matching one is the risk to watch.

**Divergences from the Claude harness, on purpose:**

- `apply_patch` is treated as a `Write` at PreToolUse even though it is
  surgical like `Edit`. It *can* replace a file wholesale and Codex has
  no second write tool to fall back to, so the ledger keeps the stricter
  protection; the deny reason gets a `[codex-orchestrator]` note appended
  explaining what "use Edit" means in this harness (appended, never a
  rewrite of core prose — a substring patch would rot on the next
  `pull-core`).
- `systemMessage` (a Claude Code extra with no documented Codex
  counterpart) is dropped; its content is already in `additionalContext`
  on the only path that emits it.
- Chair profile: Codex model ids never match the core's `_is_opus`, so
  `MODEL_PROFILE_MAP` (Astra→fable, Sol/Terra→opus) sets the core's own
  documented `FABLE_ORCH_PROFILE` override for the child — never faking a
  Claude model name into the payload, and never overriding an explicit
  user pin.
- `cold_cache_guard`'s context estimate reads a **Claude Code** JSONL
  transcript. A Codex transcript will not parse, `context_tokens()`
  returns None and the guard passes silently — the cold-cache band is
  effectively off under Codex until a Codex transcript reader exists
  (upstream work for `core/`, not for this repo).

**Shared state, identical paths** — deliberately: session markers stay at
`$TMPDIR/fable-orch-*-<session>.json` and metrics at
`~/.claude/fable-orch/metrics.jsonl`, so `core/scripts/stats.py`, the
cold-cache stamps and the retro tooling read both harnesses out of one
place. To tell them apart, every metrics line a Codex fire produces is
stamped `"harness": "codex"`. The core writes those lines itself and
honours no harness env var, so the adapter records the log's size before
running the guard and re-writes **only** the bytes appended after it —
earlier lines (a Claude session's) are untouched.

Knobs (the core's own still apply): `CODEX_ADAPTER_HARNESS` (stamp
value), `CODEX_ADAPTER_TOOL_GATE=0`, `CODEX_ADAPTER_EXIT2=1` (signal
deny/block via exit code 2 + stderr instead of stdout JSON — the other
documented Codex mechanism), `CODEX_ADAPTER_DEBUG=<path>` (append raw +
normalised payloads to a JSONL file).

### Verified live: **no** — every event

As of 2026-09-05 **no Codex hook has ever fired on this machine.** Codex
gates hooks behind a persisted hook-trust decision that needs one
interactive accept (or `--dangerously-bypass-hook-trust` from a normal
terminal); the P1 probe's `probe.log` stayed empty, so the payloads,
tool names and output schema below come from the docs
(`developers.openai.com/codex/hooks`) via
`<claude-repo>/.workflow/scratch/codex-probe/protocol.md`.

| event | payload fields | block/deny semantics | tool names | live? |
|---|---|---|---|---|
| SessionStart | docs | n/a (additionalContext) | n/a | **no** |
| UserPromptSubmit | docs | docs | n/a | **no** |
| PreToolUse (spawn) | docs | docs | UI banner `collab: SpawnAgent` only | **no** |
| PreToolUse (apply_patch) | docs | docs | not exposed at all under API-key auth + `gpt-5` | **no** |
| PostToolUse | docs | n/a | as above | **no** |
| Stop | docs | docs | n/a | **no** |
| SessionEnd | docs | n/a | n/a | **no** |

To close this out (P1 items 8/9): accept the hook-trust prompt once, run
a session with `CODEX_ADAPTER_DEBUG=/tmp/codex-adapter.jsonl`, replace
`tests/fixtures/*.json` with the captured `raw` objects, and flip this
table. `tests/fixtures/README.md` lists exactly which guesses each
capture would confirm or refute.

## Testing

```sh
make test
# equivalent to:
cd core && python3 -m pytest tests/ -q   # core's own guard tests, unmodified
python3 -m pytest tests/ -q              # profiles, agents, codex-sync, adapter
```

The adapter tests run the real core guards as subprocesses against the
fixture payloads in `tests/fixtures/` (docs-derived, **not** live captures —
see that directory's README), with `$TMPDIR` pointed at the test sandbox so
the session markers stay inside it and
`FABLE_ORCH_TEAMMATE_{STOP,INJECT}=1` set so the guards' "am I a teammate?"
process-tree walk can't make the verdict depend on who started pytest.

Run as **two separate pytest invocations**, not one combined
`pytest core/tests tests`. `core/tests/conftest.py` (vendored, untouched by
design — see the pull-core workflow) and `tests/conftest.py` are both named
`conftest` with no package `__init__.py`; pytest's default import mode holds
only one module of that name in `sys.modules` per session, so a single
combined run makes one of the two conftests shadow the other and every
`core/tests/test_*.py` fails to collect ("cannot import name X from
conftest"). Confirmed by running it both ways — this is that "conftest path
adjust" from the ledger, and the fix is process separation, not a code
change to either conftest. CI (`.github/workflows/ci.yml`) runs the same two
steps.

## Plugin manifest

Per `developers.openai.com/plugins/build/plugins` (fetched 2026-09-05): the
entry point is `.codex-plugin/plugin.json` — required fields `name`, `version`,
`description`; optional `author`, `homepage`, `repository`, `license`,
`keywords`, `skills`, `mcpServers`, `apps`, `hooks`, `interface`. Hooks are
auto-discovered at `./hooks/hooks.json` if present — no manifest `hooks` entry
needed, and none is set here; `hooks/hooks.json` relies on that
auto-discovery. **Could not confirm**: that the plugin root expands as
`${CLAUDE_PLUGIN_ROOT}` (the name the hook commands use) and that
`timeout` is honoured per hook — both are Claude-Code-shaped guesses,
and only the adapter's own path depends on the variable (`core/` is
resolved relative to the adapter file). If hooks never fire after the
trust prompt is accepted, hard-code the absolute plugin path in
`hooks/hooks.json` first. Skills are
referenced via `skills: "./skills/"` (this repo symlinks `skills/playbook` to
`core/skills/playbook`, matching the Claude repo's skill).

Marketplace catalogs (`marketplace.json`) can live at `$REPO_ROOT/.agents/plugins/marketplace.json`,
`~/.agents/plugins/marketplace.json`, or a **legacy-compatible**
`$REPO_ROOT/.claude-plugin/marketplace.json`. This repo uses the legacy path
for parity with the Claude side's `.claude-plugin/marketplace.json` layout.
**Could not confirm**: whether Codex's marketplace installer scans the legacy
path by default or only as a fallback when it can't find `.agents/plugins/`
— worth checking against a real `/plugins` install once Codex CLI is on the
Mac (P1 item 1/9 in the ledger). If it doesn't pick it up, duplicate the same
JSON at `.agents/plugins/marketplace.json`.

The manifest omits an `mcpServers` field: this plugin doesn't ship a bundled
`.mcp.json` with server definitions (the source repo's MCP servers — HA,
UniFi — carry live credentials per-machine in the *project's* `.mcp.json`,
which is git-ignored, not something to bundle into a distributed plugin).
`bin/codex-sync` generates the `[mcp_servers]` snippet into the user's
`~/.codex/config.toml` instead, at sync time, on the machine that has the
credentials.

## Pull-core workflow

```sh
# in fable5-opus5-orchestrator, after a fix lands on main:
git tag core-vN
# (git tag only — do not push; see also `make tag-core VERSION=vN` on the
#  core-tagging branch, which does the same thing as a documented target)

# in codex-orchestrator:
make pull-core TAG=core-vN
```

`make pull-core` runs `git subtree pull --prefix=core <path-to-source-repo> <TAG> --squash`
and rewrites `core/VERSION` to `<TAG>`.

## Install

Not yet published — do not push this repo or create the GitHub remote until
Marc says so (per ledger P2 scope). Once published, install like the Claude
side:

```sh
codex plugin marketplace add Shredi/codex-orchestrator
codex plugin install codex-orchestrator@codex-orchestrator
```

**Could not confirm**: the exact `codex plugin marketplace add` / `codex
plugin install` subcommand names and syntax — inferred from the Claude Code
equivalent (`claude plugin marketplace add` / `claude plugin install`) and
the docs' mention of a `/plugins` browser inside the CLI, but the fetched
pages didn't show the install CLI syntax verbatim. Verify against the real
CLI in P1/P3 and correct this section.

## Tiers and profiles

| tier | openai profile | anthropic-api profile |
|------|-----------------|------------------------|
| chair | `gpt-6-astra` / high (fallback `gpt-5.6-sol` while Astra isn't rolled out) | `claude-fable-5-1` |
| fable | `gpt-6-astra` / high | `claude-fable-5-1` |
| opus | `gpt-5.6-sol` / high | `claude-opus-5` |
| sonnet | `gpt-5.6-luna` / xhigh | `claude-sonnet-5` |
| cheap | `gpt-5.6-luna` / low | `claude-haiku-4-5` |

Model ids verified 2026-09-05 against `developers.openai.com/api/docs/models/*`
(`gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` are the exact
API `model=` strings; GPT-5.6 Terra is deliberately unused — see the plan's
tier-value rationale). `openrouter-free` is a single configurable model for
all tiers (default placeholder `openrouter/openrouter/omen-alpha`) — **never
for customer/credential work**.

## Codex subagent TOML

Per `learn.chatgpt.com/docs/agent-configuration/subagents` (fetched
2026-09-05): files live at `~/.codex/agents/*.toml` (user) or
`.codex/agents/*.toml` (project), required fields `name`, `description`,
`developer_instructions`; optional `model`, `model_reasoning_effort`,
`sandbox_mode` (`read-only` | `workspace-write`), `mcp_servers`,
`skills.config`. `model_reasoning_effort` valid values: `low`, `medium`,
`high`, `xhigh`/`max`, `ultra` (model-dependent).
