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
  `core-v3` (see `core/VERSION`). `git subtree add` pulls the **whole** source
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
  symlink farm, an `[mcp_servers]` snippet from the Claude MCP config, and the
  destructive guard's Layer B: `~/.claude/guard/bin/rm` plus the
  `[shell_environment_policy]` PATH entry that puts it in front of `/bin/rm`.
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
| SessionStart | `inject_instructions` | `additionalContext` (`additionalContextLimit: 20000`) |
| UserPromptSubmit | `cold_cache_guard` | `decision: block` (+ 3-min re-send ack) |
| PreToolUse (`collaborationspawn_agent`) | `ledger_guard_spawn` | `permissionDecision: deny` |
| PreToolUse (`Bash`) | `destructive_guard` | `permissionDecision: deny` (ask band included, see below) |
| PreToolUse (`apply_patch`, `Bash`) | `ledger_guard_write` | `permissionDecision: deny` |
| PostToolUse (`apply_patch`, `Bash`) | `ledger_bind` | none (session↔ledger binding) |
| Stop | `ledger_guard_stop` | `decision: block`, once per session |
| SessionEnd | `cleanup_session_cache` | none (timeout ≤ 3 s, Codex clamps) |

Codex's default `additionalContextLimit` is documented as 2500 characters
and the injected chair profile is 3763 — 0.153.4 delivered it in full
even without the field, but it is set explicitly on both
context-carrying hooks so a longer profile can never be silently
truncated.

**Tool-name map** (data at the top of the adapter, `TOOL_NAME_MAP`) —
the live names on Codex CLI 0.153.4 are `Bash`, `apply_patch` and
`collaborationspawn_agent`; the rest of the map is defensive tolerance.
`Bash` → `Bash`; `apply_patch` → `Write`/`Edit` per patch op (below);
`collaborationspawn_agent` → `Agent`.

Two live details that shape the whole write path:

- **`tool_input.command` carries everything.** Both `Bash` and
  `apply_patch` deliver their payload there — `apply_patch` puts the
  entire `*** Begin Patch` envelope in it. (A first cut of this adapter
  looked for `patch`/`input`/`diff` keys, found nothing, extracted no
  path, and let a live ledger be overwritten.)
- **The shell is a second write tool.** `apply_patch` also exists as an
  arg0 shim binary, and the model pipes patches into it from `Bash` the
  moment the first-party tool errors — observed destroying a ledger with
  every PreToolUse hook reporting "Completed". So `Bash` is in the write
  guard's and the binder's tool sets, and `shell_write_targets()` reads
  the command for `>` / `tee` / `sed -i` / a piped patch envelope
  (`grep`, `cat`, `sed -n`, `cp` with a ledger as *source* are not
  writes and never denied).

**Write vs Edit, per operation.** Core guards `^Write$` only — a
whole-file replace. apply_patch is both tools at once, so the adapter
classifies each hunk:

| shape | kind | write guard |
|---|---|---|
| `*** Add File:` / `*** Delete File:` / `*** Move to:` | REPLACE | runs |
| `*** Update File:` whose hunks delete **every** line of the target | REPLACE | runs |
| any other `*** Update File:` | EDIT | skipped |
| shell `> p`, `tee p`, piped apply_patch | REPLACE | runs |
| shell `>> p`, `tee -a p`, `sed -i … p` | EDIT | skipped |

Both halves of that table are load-bearing and were learned the hard
way: mapping *every* `apply_patch` to `Write` deadlocks the harness (the
chair may not touch a ledger it did not create, so it never binds, so
the Stop guard stays inert — `stop_suppressed reason=unbound` on every
session), while trusting the `Update File` label lets a model that read
the file first wipe it with one all-deletions hunk.

The spawn payload is `{task_name, agent_type, fork_turns, message}` and
`message` is a **Fernet ciphertext**, not the prompt: the core's
1500-char delegation gate therefore measures ciphertext length (~1.4× the
plaintext), so it fires one notch early and never late. There is no way
to read a Codex spawn prompt from a hook — the gate is length-only here
by construction.

The hooks.json matchers are Claude-style anchored regexes over the Codex
tool names, **and** the adapter re-checks the mapped tool name in-process
(`CODEX_ADAPTER_TOOL_GATE=0` disables), because Codex's matcher syntax is
verified only for the names Codex uses today: an over-matching matcher
still behaves, an under-matching one is the risk to watch.

**Divergences from the Claude harness, on purpose:**

- **The shell write route is guarded, Claude's is not.** A `cat > file`
  from Bash is invisible to the Claude harness. Under Codex the shell is
  where apply_patch itself lives, and it is demonstrably the path a
  blocked model takes next, so the same rule applies there. Narrow by
  design (see the table above) because a false positive denies an
  ordinary shell call.
- **An all-deletions `Update File` counts as a replace.** Claude's Edit
  could wipe a ledger unguarded; here it cannot. One notch stricter,
  deliberately: it is the only shape that both looks surgical and
  destroys the file.
- Every write-guard deny gets a `[codex-orchestrator]` note appended to
  the core's reason, explaining what "use Edit" means in a harness with
  no Edit tool (appended, never a rewrite of core prose — a substring
  patch would rot on the next `pull-core`).
- **The destructive guard's ASK band is a DENY here.** Codex parses
  `permissionDecision: "ask"` but does not honour it: "Codex marks the
  hook run as failed, reports the error, and continues the tool call"
  (hooks docs, read 2026-09-11). Continuing is the one outcome an
  unapproved recursive `rm` must not get, so the adapter converts the
  ask to a deny and appends `CODEX_ASK_NOTE` — what the approval prompt
  would have offered — to the core's own reason.
- **The guard's `updatedInput` PATH rewrite is dropped.** Codex accepts
  `updatedInput` only next to `permissionDecision: "allow"`, and emitting
  that allow would hand every rm-bearing command a blanket approval it
  never had. Layer B reaches Codex a different way: `bin/codex-sync`
  installs `core/guard/rm` to `~/.claude/guard/bin/rm` (0755, atomic) and
  merges `[shell_environment_policy] set = { PATH = "<guard dir>:…" }`
  into `~/.codex/config.toml`. `set` is the only documented knob there and
  has no prepend form, so the value is a literal joined at sync time and
  re-prepended (never duplicated) on every later run; other keys in that
  section, and everything else in the file, are left untouched.
- `systemMessage` (a Claude Code extra with no documented Codex
  counterpart) is dropped; its content is already in `additionalContext`
  on the only path that emits it.
- Chair profile: Codex model ids never match the core's `_is_opus`, so
  `MODEL_PROFILE_MAP` (Astra→fable, Sol/Terra→opus) sets the core's own
  documented `FABLE_ORCH_PROFILE` override for the child — never faking a
  Claude model name into the payload, and never overriding an explicit
  user pin.
- `cold_cache_guard`'s context estimate reads the session transcript.
  Since `core-v2` it parses the **Codex** session JSONL as well as the
  Claude Code one, so the cold-cache bands are live under Codex too.
  (Before `core-v2` a Codex transcript did not parse, `context_tokens()`
  returned None and the guard passed silently.)

**Shared state, identical paths** — deliberately: session markers stay at
`$TMPDIR/fable-orch-*-<session>.json` and metrics at
`~/.claude/fable-orch/metrics.jsonl`, so `core/scripts/stats.py`, the
cold-cache stamps and the retro tooling read both harnesses out of one
place. To tell them apart, every metrics line a Codex fire produces is
stamped `"harness": "codex"`. Since `core-v2` the core does that itself:
the adapter exports `FABLE_ORCH_HARNESS=codex` into the guard
subprocess's environment and the guard writes the key as it writes the
line — exactly once, never a second pass. (Up to `core-v1` the adapter
rewrote the log's tail after the fact; that code is gone.) Lines a
Claude Code session wrote carry no `harness` key and are never touched.

Knobs (the core's own still apply): `CODEX_ADAPTER_HARNESS` (the value
exported as `FABLE_ORCH_HARNESS`, default `codex`),
`CODEX_ADAPTER_TOOL_GATE=0`, `CODEX_ADAPTER_EXIT2=1` (signal deny/block
via exit code 2 + stderr instead of stdout JSON — the other documented
Codex mechanism), and `CODEX_ADAPTER_DEBUG` — a **path**: the adapter
appends the raw and the normalised payload of every fire to that JSONL
file (this is how a payload question gets settled live). It is not a
boolean; the boolean-looking values `1`/`true`/`yes`/`on` are accepted
and redirected to `$TMPDIR/codex-adapter-debug.jsonl` rather than
dropping a file literally named `1` into the session's cwd.

### Verified live: **yes** — every event, Codex CLI 0.153.4, 2026-09-05

Payloads captured with a user-level observe-only hook set
(`<claude-repo>/.workflow/scratch/codex-probe/probe.log`) and replayed as
`tests/fixtures/*.json`; guard behaviour re-checked by running the
plugin for real with
`codex exec --dangerously-bypass-hook-trust --sandbox workspace-write -m gpt-5.6-luna`.
Hook verdicts show up on stderr as `hook: <Event> Completed|Blocked`.

| event | payload | block/deny semantics | live tool name | live? |
|---|---|---|---|---|
| SessionStart | `session_id, transcript_path, cwd, hook_event_name, model, permission_mode, source` | `additionalContext`, full 3763-char profile in the transcript | n/a | **yes** |
| UserPromptSubmit | + `turn_id, prompt` | `decision: block` (schema exercised in tests; no cold Codex session yet) | n/a | **partial** |
| PreToolUse (spawn) | + `tool_name, tool_input, tool_use_id` | `hook: PreToolUse Blocked`, `spawn_deny chars=1956` | `collaborationspawn_agent` (feature `multi_agent_v2`) | **yes** |
| PreToolUse (write) | as above | `hook: PreToolUse Blocked`, `write_deny`, ledger intact after 4 bypass attempts | `apply_patch`, `Bash` | **yes** |
| PostToolUse | + `tool_response` | binds the session (`marker["ledger"]`) | `apply_patch`, `Bash` | **yes** |
| Stop | + `stop_hook_active, last_assistant_message` | exactly one `hook: Stop Blocked` + `stop_block open=2`, retry passes | n/a | **yes** |
| SessionEnd | `session_id, transcript_path, cwd, hook_event_name, reason` | none | n/a | **yes** |

Evidence for the two failures this closed:

```
# A — write guard, before: all hooks "Completed", ledger overwritten
PreToolUse apply_patch {"command": "*** Begin Patch\n*** Delete File: …/LEDGER-kissenbox.md\n*** Add File: …"}
PreToolUse Bash        {"command": "target='…/LEDGER-kissenbox.md' … | …/arg0/…/apply_patch"}
PostToolUse Bash       tool_response: "apply_patch completed\n"
# A — after
error=Command blocked by PreToolUse hook: LEDGER GUARD: …/LEDGER-kissenbox.md is an EXISTING live ledger
hook: PreToolUse Blocked          (× 4: apply_patch tool, shim pipe, `printf >`, `LEDGER_WRITE_GUARD=0 printf >`)
{"event": "write_deny", "path": "LEDGER-kissenbox.md", "bound": null, "harness": "codex"}

# B — stop guard, before / after
{"event": "stop_suppressed", "session": "01a0730a", "reason": "unbound", "harness": "codex"}
{"event": "stop_block", "session": "01a0731e", "open": 2, "ledger": "…/LEDGER-stoptest.md", "harness": "codex"}

# C — spawn gate, isolated repo with no ledger
error=Tool call blocked by PreToolUse hook: LEDGER GUARD: this looks like a detailed delegation…
{"event": "spawn_deny", "chars": 1956, "threshold": 1500, "tool": "Agent", "harness": "codex"}
```

Still not exercised live: the `cold_cache_guard` block band itself
(needs a Codex session left idle past the cold threshold), and the
`CODEX_ADAPTER_EXIT2=1` signalling path (tests only; the JSON schema
works, so there is no reason to switch). Its *input* is confirmed since
`core-v2`, though — `context_tokens()` on a live Codex rollout JSONL
returned `16108` on 2026-09-05, where `core-v1` returned `None`, so the
band is armed rather than inert:

```
$ python3 -c "…; print(m.context_tokens('~/.codex/sessions/2026/09/05/rollout-…-01a0733c-….jsonl'))"
16108
```

**Re-installing after a change** — the plugin cache is a *copy* of this
repo, so edits do not take effect until:

```sh
codex plugin remove codex-orchestrator@codex-orchestrator
codex plugin add    codex-orchestrator@codex-orchestrator
```

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
make tag-core VERSION=v3          # or: git tag core-v3
# tag only — pushing rolls the change out to the live Claude Code plugin
# via autoUpdate, so that is Marc's call, not the release step's

# in codex-orchestrator:
make pull-core TAG=core-v3
make test                         # TWO pytest runs, see "Testing"
```

`make pull-core` runs `git subtree pull --prefix=core <path-to-source-repo> <TAG> --squash`
and rewrites `core/VERSION` to `<TAG>`. `SOURCE` defaults to the sibling
checkout `../fable5-opus5-orchestrator`; override it once this repo has a
git remote for the core.

Current: **`core-v2`** (2026-09-05) — brought two things this repo had been
working around: `FABLE_ORCH_HARNESS`, honoured by every core `_metric()`
writer, which retired the adapter's metrics tail-rewrite entirely; and a
`cold_cache_guard.context_tokens()` that parses Codex session JSONL, which
turned the cold-cache bands on under Codex. Core tests at `core-v2`: 268.

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
