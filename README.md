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
- `scripts/codex_adapter.py` + `hooks/hooks.json` — **not present yet**.
  These are a separate ledger item (Codex payload → Claude payload → core
  guard → Codex output, tool-name mapping), built against the P1 protocol
  probe dump once that lands. Until then this plugin has no hooks — `core/`
  and the tier/profile/sync tooling below are usable standalone.
- `agents/{sonnet,opus,fable}.toml` — tier subagents, generated from
  `profiles/openai.toml` (committed as the openai-profile output; regenerate
  with `codex-sync` if the profile changes).
- `profiles/{openai,openrouter-free,anthropic-api}.toml` — tier → model map.
- `bin/codex-sync` — generates `~/.codex/agents/*.toml`, `<repo>/.codex/agents/*.toml`
  from `<repo>/.claude/agents/*.md`, an `AGENTS.md` stub, the `~/.agents/skills`
  symlink farm, and an `[mcp_servers]` snippet from the Claude MCP config.
- `tests/` — `codex-sync` unit tests (frontmatter translation, idempotency,
  dry-run, AGENTS.md never-overwritten, secret redaction) and profile schema
  tests. `core/tests/` (vendored, unmodified) proves core/ itself.

## Testing

```sh
make test
# equivalent to:
cd core && python3 -m pytest tests/ -q   # core's own guard tests, unmodified
python3 -m pytest tests/ -q              # profiles, agents, codex-sync
```

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
needed (and none is set here, since that file doesn't exist yet). Skills are
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
