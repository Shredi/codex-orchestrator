# Fable Orchestrator

[![CI](https://github.com/Rylaa/fable5-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/Rylaa/fable5-orchestrator/actions/workflows/ci.yml)

**Run Claude Fable 5 all day — without watching the usage meter.**

Fable 5 is the best chair a Claude Code session can have — and the most expensive seat in the house. Let it type every token itself and the session ends rate-limited, waiting out the reset window.

This plugin makes the split mechanical. **Fable 5 keeps the chair** and spends tokens only on planning, arbitration, and final decisions. The volume — implementation, research, briefs, review, bulk reading — goes to **Sonnet 5**; the predictably hard slices — architecture, irreversible migrations, security review — go **directly to Opus 5**, which doubles as the escalation lane. **Every close gets fresh-eyes verification** from **Opus 5 or Fable 5**, one bounded call per workflow, before the chair moves on.

## The division of labor

```
                        ┌─────────────────────────────────┐
                        │         FABLE 5 — chair         │
                        │    plan · arbitrate · decide    │
                        │       routes tier per task      │
                        └────────────────┬────────────────┘
                                         │
                specs & ledger down      │      briefs & verdicts up
                                         │
           ┌────────────────────────┬────┴───────────────────┬────────────────────────┐
           ▼                        ▼                        ▼                        ▼
┌─────────────────────┐  ┌─────────────────────┐  ┌─────────────────────┐  ┌─────────────────────┐
│      SONNET 5       │  │      SONNET 5       │  │      SONNET 5       │  │       OPUS 5        │
│   mechanical bulk   │  │   implementation    │  │   routine judgment  │  │  hard work · direct │
│   grep·fetch·scan   │  │   code · tests      │  │   briefs · review   │  │  architecture       │
│   format · read     │  │   debug · refactor  │  │   filtering         │  │  migrations·security│
└─────────────────────┘  └─────────────────────┘  └──────────┬──────────┘  └──────────┬──────────┘
                                                             │ uncertain /            │ beyond
                                                             │ high stakes            │ opus
                                                          ┌──▼────────────────────────▼──┐
                                                          │   the verification valve     │
                                                          │  verify: OPUS 5 / FABLE 5    │
                                                          │  escalate: OPUS 5 → FABLE 5  │
                                                          └──────────────────────────────┘
```

Fable thinks. Sonnet carries the volume, Opus takes the hard slices. Opus or Fable checks the close. Your limit pays for the thinking plus at most one verification per close:

```
┌─────────────────────────────────────────┬─────────────────┬─────────────────────┐
│ Work                                    │ Runs on         │ Fable limit pays    │
├─────────────────────────────────────────┼─────────────────┼─────────────────────┤
│ Phase planning, arbitration, decisions  │ Fable 5 (chair) │ yes                 │
│ Implementation, tests, refactors        │ Sonnet 5        │ nothing             │
│ Source briefs, filtering, code review   │ Sonnet 5        │ nothing             │
│ Bulk gathering (fetch, grep, scan)      │ Sonnet 5        │ nothing             │
│ Hard slices: architecture, migrations   │ Opus 5 (direct) │ nothing             │
│ Security / adversarial review           │ Opus 5          │ nothing             │
│ Escalations (sonnet "uncertain")        │ Opus → Fable    │ mostly nothing      │
│ Fresh-eyes verification — EVERY close   │ Opus/Fable 5    │ at most 1 per close │
└─────────────────────────────────────────┴─────────────────┴─────────────────────┘
```

## Why Fable 5 × Sonnet 5 × Opus 5 is the right trio

- **Fable tokens are the heaviest draw on your limit.** Every token of bulk work kept off the chair extends how long Fable stays in it.
- **Sonnet 5 carries the volume.** Near-Opus quality on coding and agentic work — the chair routes it every mechanical sweep, implementation, and routine-judgment task.
- **Opus 5 takes the hard slices directly.** Architecture tradeoffs, irreversible migrations, complex multi-system implementation, and all security/adversarial review are assigned straight to Opus — no failed Sonnet pass required — and Opus doubles as the escalation lane.
- **The valve is two-tier, and it never opens by itself.** Fresh-eyes verification is mandatory on **every** close. It runs on Opus 5 or Fable 5 — Opus spares the Fable limit; the largest, highest-stakes closes still get Fable, the strongest model at the single moment it matters most. Anthropic measured this worker+verifier split: Sonnet 5 with a Fable 5 advisor checking its work lands within 10% of Fable 5's score on the whole task. Escalations climb sonnet → opus → fable, with security reviews kept off Fable, whose classifiers decline benign security work most readily. Any tier can still decline it — the profile's rule is to first remove the documented false-positive causes (base64 in the worker's tool output, "does this compile" phrasing, a lesser-known language with no docs) and retry the SAME tier, which fixes the input rather than the wording; failing that, rerun the refused task unchanged on another tier and, if that tier declines too, stop and tell you, never to reword the request past a classifier. A worker that returns "uncertain" never bounces back to the chair.
- **Effort is not a per-spawn lever.** The Agent tool has no effort parameter — a spec that says "work at medium effort" is ignored, and every worker and verifier runs at the chair's own effort level. The only real per-spawn choice is the MODEL (sonnet/opus/fable via the `model` parameter), which is what this whole routing story is actually about.

## What the plugin does

Three layers, all mechanical — no CLAUDE.md editing, no manual routing.

### 1 · The Fable profile — a slim core plus a playbook

A SessionStart hook injects the Fable-in-chair profile ([`instructions/dynamic-workflow-fable.md`](instructions/dynamic-workflow-fable.md)) into every **chair** session — auto-detected per session start, nothing to configure:

```
┌──────────────────────────┬──────────────────────────────┐
│ Scarce resource          │ your usage limit             │
│ Bounded / medium work    │ delegated                    │
│ Requirements Ledger      │ file, before any delegation  │
│ Worker tier              │ routed per task by the chair │
│ Verification             │ fresh-eyes on every close    │
│ Disk hand-off            │ the default                  │
│ Subagent report cap      │ 40 lines; bulk to disk       │
│ Detail (full playbook)   │ skill, loaded on demand      │
│ Spawn-guard threshold    │ 1500 chars                   │
│ Task-list gate           │ 3rd task needs the ledger    │
└──────────────────────────┴──────────────────────────────┘
```

**The injected text is deliberately small.** It is prepended to every chair session, so every character is paid on every start — and most of it is detail the chair needs *once*, at its first delegation, not on the way in. So the core carries only what must be true from the first token (threshold, ledger, disk hand-off, spawn discipline, routing, the verification rule) and requires the chair to load [`skills/playbook/SKILL.md`](skills/playbook/SKILL.md) — the `orchestrator:playbook` skill, auto-discovered from the plugin — **before its first delegation**. The playbook holds the full contract: research pipeline, output contract, worker spec boilerplate, spawn economics, forks, teammate lifecycle, verification procedure, decline handling. Sessions that never delegate never pay for it, and the two paste-ready worker blocks it hands out ([`skills/playbook/spec-blocks.md`](skills/playbook/spec-blocks.md)) are read only when a spawn needs them.

The core routes subagents by tier name (`sonnet`, `opus`, `fable`) and keeps bulk material on disk (`./.workflow/scratch/` — the chair receives briefs and verdicts, never dumps). Effort is not a per-spawn setting — the Agent tool has none, so every worker and verifier runs at the chair's own effort level; a fable spawn asked for a long deliverable still carries the playbook's LONG OUTPUT note, since that is about the model's drafting habit, not an effort level. Before defaulting to a generic worker, the chair checks the project's own agent roster (its CLAUDE.md `## Orchestrator agents` section plus `.claude/agents/`) for a matching specialized agent. Context-heavy follow-ups go to a **fork** (`subagent_type: "fork"`), which inherits the full conversation with no spec-writing tax, capped at two per session.

Three rules in the contract exist purely to keep worker output from undoing the saving:

- **Reports are capped at 40 lines.** Any verbatim block over ten lines goes to `./.workflow/scratch/` and the report carries the path. A report that violates the contract is rejected and re-run, not silently accepted.
- **Research is one worker per source, not two.** A single Sonnet agent fetches the source **verbatim to disk first** — the disk copy is the audit trail, with no relevance filtering during the fetch — and only then returns a brief built from that copy: claims, evidence, exact quotes, confidence, contradictions, and the path. One synthesizer reads across the briefs. The chair checks the synthesis against the ledger and decides; intermediates never enter its context.
- **Similar mechanical work is batched into one worker.** Every spawn pays a fixed overhead — system prompt, project rules, tool schemas — before doing anything useful. Five greps are one agent with a checklist, not five agents. Separate spawns are for genuine parallelism or worktree isolation, which earn that overhead back.

**When the Fable limit runs dry**, move the chair to Opus (`/model`): the injector serves the matching OPUS profile ([`instructions/dynamic-workflow-opus.md`](instructions/dynamic-workflow-opus.md)) — same discipline, the fable tier rests, fresh-eyes verification and the escalation ceiling fall to a fresh Opus agent. Opus is a drop-in chair: it orchestrates exactly as Fable did, only Opus now sits in the seat.

**Switching chairs mid-session costs a few lines, not a whole profile.** If a session that already received a core profile is **resumed** with the other one selected, the full core is not sent again — it is still in that session's context, and re-sending it spends the very limit the switch is trying to preserve. A short switch note ([`instructions/profile-switch-to-opus.md`](instructions/profile-switch-to-opus.md), [`instructions/profile-switch-to-fable.md`](instructions/profile-switch-to-fable.md)) carries only what changed.

The delta is **resume-only, on purpose.** A `compact` re-fire happens precisely *because* the context was rewritten, and a `clear` because it was discarded; on either — and on any future session-start kind the injector does not recognise — the profile change is delivered as the **full core**, because the switch note's "every other rule from the already-injected core profile stays in force" would otherwise be a promise the chair has no way to check. A plain re-fire on an unchanged chair is untouched and always gets the full core, as does any session whose marker records no previous injection. The trade is deliberately lopsided: an unnecessary full core costs a few thousand characters, while a delta landing on a wiped context costs the ledger rule, the threshold, and the routing — silently.

Detection runs at each session start, in priority order: an explicit `FABLE_ORCH_PROFILE=fable|opus` pin, then the SessionStart payload's model, then **the default model `/model` wrote to `settings.json`** (so an Opus default is honored even when the harness omits the payload model on a resume/compact — the case that used to fall back to Fable), then the last model this session saw. A mid-session `/model` switch still only takes effect at the next session start (SessionStart is the only injection point) — but the settings fallback makes that next start reliable. To pin the chair regardless of detection, set `FABLE_ORCH_PROFILE=opus` while you ride out the Fable limit, `fable` (or unset) when it resets.

**Teammates never get the profile.** Named agent-teams workers are full sessions and fire SessionStart too — but the profile is written for the chair alone: delivered to a worker it says "you are the orchestrator" and invites it to spawn subagents of its own, inverting the discipline (measured in the wild: 172 of 270 injected sessions were teammates). The injector runs the same teammate detection as the close guard — `--agent-id` on the nearest `claude` ancestor — and skips the injection while still writing the session marker, so every other guard keeps working. `FABLE_ORCH_TEAMMATE_INJECT=1` restores the old inject-everyone behaviour.

### Project agent roster

The plugin itself stays generic and ships no domain agents of its own — a project that has built specialized ones documents them so the chair reaches for a matching agent instead of a generic worker. Convention: a `## Orchestrator agents` section in the project's own CLAUDE.md, one line per agent — name, when to use it, what it returns — mirroring whatever `.claude/agents/*.md` files Claude Code auto-discovers for that session. Both chair profiles now check that roster before spawning a generic worker (Rule 3), and the playbook's spawn-economics section repeats the rule: a specialized agent keeps bulky domain output off the chair and can carry its own `model:`/`effort:` frontmatter, which is the only place a per-worker effort setting is actually real.

### 2 · A Requirements Ledger

Before serious delegation the chair writes every requirement, constraint, and edge case as one checkbox line in a **new, topic-named** `./.workflow/LEDGER-<topic>.md` — never the bare `LEDGER.md`, and never an existing ledger file (a write guard denies overwriting one; see below). The hooks watch every `LEDGER*.md` in that directory, newest first; a name *ending* in `-archive.md` is retired and silences the close guard for good, so a live `LEDGER-archive-migration.md` still counts. Files survive context compaction; conversation context does not — which is why a `compact` or `resume` session start hands a chair already bound to a ledger one extra line naming that file's path, and why the chair's closing recap walks the whole ledger item by item instead of only the last phase.

**Ambiguity goes in the ledger, not into a question.** The chair asks only when different readings would lead to materially different work; otherwise it records `- [ ] N. ASSUMPTION: <reading>` and proceeds, and you rule on it at the plan checkpoint — the harness now tells the model you are not watching in real time, so a question mid-task stalls the work instead of resolving it.

**Per-session binding.** Two parallel sessions in the same repo used to share one newest-mtime "active ledger" — session A's close could be held on session B's newer ledger, or silenced by it. Now the first `Write`/`Edit`/`MultiEdit` a session makes to a live ledger binds that session to it (recorded in its own marker); a spawn or tracker task that is satisfied by a *discovered* ledger adopts and binds it the same way. A bound session's guards resolve only its own ledger, however new or old another session's is. A session with no binding yet — or none at all (manual install) — falls back to the original newest-wins discovery, so nothing before this changes.

```markdown
- [ ] 1. Every explicit requirement, one line each
- [ ] 2. Implicit expectations and constraints too
- [x] 3. Marked done only after verification confirms it
- [~] 4. deferred: user approved postponing this
```

### 3 · Guard hooks

Instructions are *advice*; hooks are *mechanism*. The failure points that get skipped under pressure are fenced:

**Spawn guard** (`PreToolUse` on `Agent|Task|Workflow`) — gates the spawn `prompt`, or the Workflow `script`:

```
spawn (Agent / Task / Workflow)
  │
  ├─ text ≤ threshold (default 1500) .............. PASS  (short spawns are never taxed)
  │
  └─ text > threshold
       │
       ├─ subagent_type == "fork" ................. PASS  (forks already see the ledger)
       │
       ├─ ACTIVE .workflow/LEDGER*.md found ....... PASS  (cite its items per agent)
       │  (cwd → repo root / $HOME)
       │
       └─ no ledger — or only a stale one ......... DENY  → "write the ledger first;
                                                     small single-phase → do directly"
```

A ledger is *stale* when every item is closed AND it was last touched before this session started — last week's finished ledger doesn't disarm the gates for a new task. Open items, or any touch during this session, keep it active.

**Task guard** (`PreToolUse` on `TaskCreate`) — the solo path the spawn guard can't see. A session that never spawns agents never meets the spawn guard; it just quietly implements a six-phase plan solo on the most expensive model (measured in the wild). The tracker tasks it creates for itself are the tell:

```
TaskCreate (tracker task)
  │
  ├─ ACTIVE .workflow/LEDGER*.md found ............. PASS
  ├─ fewer than 3 ledgerless tasks this session .... PASS  (small task lists are fine)
  │
  └─ 3rd ledgerless task ........................... DENY once → "multi-phase work:
                                                     write the ledger, delegate the
                                                     phases to workers" — then quiet
```

**Close guard** (`Stop`) — chair only; a named teammate's close is never held on the chair's ledger (`FABLE_ORCH_TEAMMATE_STOP=1` restores the old behaviour):

```
turn ends
  │
  ├─ no ledger on the search path (cwd → repo root / $HOME) ... pass
  ├─ every item "- [x]" or "- [~] deferred" ................... pass
  ├─ ledger untouched by this session ......................... pass
  ├─ this session already got its reminder .................... pass
  │
  └─ open items, touched here, first time ..................... BLOCK once,
       listing the items: finish them, defer with user approval, or
       acknowledge in one line and move on — one reminder per session.
       Archive paused ledgers (LEDGER-<topic>-archive.md) to silence for
       good; LEDGER_GUARD_STOP_MODE=every-turn restores per-turn blocking.
```

**Write guard** (`PreToolUse` on `Write`) — stops a fresh `Write` from clobbering an EXISTING live ledger that isn't this session's: `.workflow/` is shared, session-agnostic, and not git-tracked, so nothing else stops a chair from overwriting another session's (or an earlier task's) ledger outright:

```
Write <path>
  │
  ├─ target doesn't exist yet ...................... PASS  (this is how a new ledger is made)
  ├─ not a live LEDGER*.md name, or not in .workflow/ PASS  (not this guard's business)
  ├─ no session marker (manual install) ............. PASS  (fail open)
  ├─ this session is bound to exactly this path ..... PASS  (continuing your own ledger)
  │
  └─ existing live ledger, session bound elsewhere
     or not bound at all ........................... DENY  → "create a NEW
                                                     topic-named LEDGER-<topic>.md,
                                                     or use Edit to continue THIS one"
```

`LEDGER_WRITE_GUARD=0` disables it. It only reads — no lock needed — so it runs identically on macOS/Linux/Windows.

### 4 · Cold-cache guard

The guards above protect the *workflow*. This one protects the *limit*, and it exists because an audit of one chair's traffic found the money somewhere nobody looks — not in long sessions or big outputs, but in short messages typed into old ones.

Across 4,238 chair messages from 39 sessions (Aug 2026 onward), **99 messages — 2.3% of them — carried 66.1% of every cache-write token the chair spent**. Sorted differently: the first message after an idle gap longer than 60 minutes accounted for **51.4% of all chair cache-write tokens** (11,142k of 21,666k), which works out to roughly **27% of the chair's entire cost proxy**. The ten largest single events were morning resumes of 252k–808k-token contexts after gaps of 1.9 to 118 hours. A subscription session writes its cache at the 1-hour TTL, where a re-cached context token costs about **0.4 output tokens of limit** — so one "quick question" typed into yesterday's 400k-token session burns more limit than an hour of actual work.

Nothing restarts in that scenario: the tab stays open across sleep, the session never ends, `SessionStart` never fires again. `UserPromptSubmit` is the only event on that path. Its sibling — `claude --resume` on yesterday's session — *is* covered as well, because the activity stamps survive the marker rewrite that every SessionStart performs.

```
prompt submitted
  │
  ├─ starts with "/" · empty · teammate · FABLE_ORCH_COLD_GUARD=0 ... PASS  (unconditional)
  ├─ idle gap < FABLE_ORCH_COLD_MIN (55 min) ........................ PASS  (cache still warm)
  ├─ no session marker, or no usage in the transcript ............... PASS  (fail open)
  ├─ blocked less than FABLE_ORCH_COLD_ACK_MIN (3 min) ago .......... PASS  (you said yes)
  │
  ├─ cold, ctx ≥ FABLE_ORCH_COLD_WARN_TOKENS (50k) .................. WARN  one line to you, one
  │                                                                        to the chair: re-read
  │                                                                        the ledger, finish or
  │                                                                        hand over
  │
  └─ cold, ctx ≥ FABLE_ORCH_COLD_BLOCK_TOKENS (150k) ............... BLOCK  "this session holds
                                                                            ~412k tokens and was
                                                                            idle 9 h 12 min ..."
                                                                            → /clear, or re-send
                                                                              the same message
```

*Idle gap* is measured from the last turn-end (the `Stop` hook stamps it into the session marker) or the last prompt that actually reached the model, whichever is later — a **blocked** prompt never reaches it, so the gap keeps growing instead of resetting. *Context size* is the last assistant message's `input + cache_creation + cache_read` tokens, read from the **tail** of the transcript, never the whole file (they reach tens of MB).

**`/clear`, not `/compact`.** Compaction re-reads and re-writes the whole context to produce its summary — it pays the very bill this guard is about, and loses detail on top. The ledger on disk is what carries the task into a fresh session, which is why the block message names it.

**It is a time heuristic, and says so.** No API reports prompt-cache state, so the guard infers coldness from the documented 1-hour TTL. It can be wrong in both directions: a warm cache blocked costs one re-send (three minutes of patience), a cold one missed costs nothing worse than today's behaviour.

A seventh hook (`SessionEnd`) cleans up after the session: its temp files and **its tmux teammates**. The agent-teams backend parks teammates in tmux panes and never reaps them (measured in the wild: 63 orphaned agents holding ~5 GB; later, 9 panes parked for 11-30 hours) — on current Claude Code those panes sit inside **your own default tmux server**, on older versions in dedicated `claude-swarm-*` servers. The hook kills the session's own teammates wherever they live: the legacy `claude-swarm-<pid>` server whole (matched via the hook's nearest-claude ancestor or the `@session-<id>` pane tag), and on shared servers only the PANES carrying this session's `--parent-session-id` — a non-swarm server itself is never killed. Swarm servers idle 48h+ are swept too. Finished teammates don't wait for a SessionEnd that may be days away: a rate-limited sweep piggybacked on the Stop hook samples every teammate pane's CPU and kills panes idling below ~1% CPU for `FABLE_ORCH_TEAMMATE_IDLE_H` hours (default 1). A parked teammate still burns a mailbox-polling heartbeat, so idleness is a sustained low RATE, not a frozen clock — working siblings re-baseline and survive. The injected profile adds the front line: the chair dismisses a teammate (`shutdown_request`) the moment its final report is accepted.

## Fable 5.1 adjustments (2026-09)

Anthropic's [What's new in Claude Fable 5.1](https://platform.claude.com/docs/en/models/fable-5-1/whats-new-fable-5-1) and [Prompting Claude Fable 5.1](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5-1) changed what a chair profile has to say. What landed here:

- **Ambiguity becomes a ledger line** — `- [ ] N. ASSUMPTION: <reading>` and the work continues; the chair asks only when the readings mean materially different work.
- **The closing recap walks the whole ledger**, item by item: 5.1 writes fewer updates while working and a final message that otherwise covers only the last step.
- **The fable-tier effort floor was removed (2026-09-04).** Verification showed the Agent tool has no effort parameter, so a per-spawn ladder was never real; every worker and verifier now runs at the chair's own effort level, and a fable spawn asked for a long deliverable still carries the "don't draft it twice" note on its own terms.
- **A decline is treated as an input problem first**: the three documented false-positive triggers are removed and the same tier retried, before any unchanged rerun on another tier.
- **Every implementation spawn carries a SCOPE + EDITS block** — no unrequested fixes, no extra permanent test files, surgical edits rather than whole-file rewrites.
- **The chair keeps working while a wave runs**, and a `compact`/`resume` session start points a bound chair back at its ledger file.

Deliberately **not** duplicated, because Claude Code already injects them into the session: the batching nudge, the progress-update line, the autonomy block, the "Delivering work" block, and the "only you see that output" note. Security review still runs on Opus, and the chair profile still changes only at a session start.

## Watching the team live

Teammates are real `claude` processes in tmux panes — you can watch every agent think, call tools, and type in real time. Current Claude Code opens the panes inside **your own default tmux server**: if you launched `claude` from inside tmux, the team appears as extra panes right in your window (`prefix q` jumps between panes, `prefix z` zooms one to full screen, `prefix w` shows a session/window tree).

```
# who is on the field, by name
ps -axo pid=,command= | grep -- --agent-id

# every pane, mapped: session, pane id, pid, what it runs
tmux list-panes -a -F '#{session_name} #{pane_id} #{pane_pid} #{pane_current_command}'

# older Claude Code parked teams in dedicated servers — attach read-only
ls /tmp/tmux-$UID | grep claude-swarm
tmux -S /tmp/tmux-$UID/claude-swarm-<pid> attach -r
```

Watch, don't type: a teammate's pane is its working terminal, and stray input interferes with it — talk to agents through the lead session instead. The reaper keeps this view honest: dismissed and idle teammates disappear from the list instead of stacking up.

## Install

```
/plugin marketplace add Rylaa/fable5-orchestrator
/plugin install orchestrator@fable-orchestrator
```

Requires `python3` on PATH. **Windows** works too: the ledger guards, the SessionStart injector, and per-session ledger binding all run unmodified — only the tmux teammate-pane features (idle reaping, watching panes live) degrade to no-ops, since there is no `tmux`/POSIX `ps` there. On Windows, Claude Code executes hook commands through **Git Bash**, so `python3` must resolve as a real executable in bash's PATH lookup — a `python3.bat` shim passes a `cmd` check but is invisible to bash. The reliable fix: copy `python.exe` to `python3.exe` inside the Python install directory (re-copy after an in-place Python upgrade). If the Microsoft Store `python3.exe` app-execution alias is present, disable it first (Settings → Apps → Advanced app settings → App execution aliases), or it shadows the real one. No other configuration needed on any OS.

### Manual install (without the plugin system)

1. Copy `scripts/ledger_guard_spawn.py`, `scripts/ledger_guard_stop.py`, and `scripts/cleanup_session_cache.py` to `~/.claude/hooks/`.
2. Merge this into `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "^(Agent|Task|Workflow|TaskCreate)$",
        "hooks": [
          { "type": "command", "command": "python3 ~/.claude/hooks/ledger_guard_spawn.py", "timeout": 10 }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "python3 ~/.claude/hooks/ledger_guard_stop.py", "timeout": 10 }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          { "type": "command", "command": "python3 ~/.claude/hooks/cleanup_session_cache.py", "timeout": 20 }
        ]
      }
    ]
  }
}
```

3. Append `instructions/dynamic-workflow-fable.md` to `~/.claude/CLAUDE.md`, and copy `skills/playbook/SKILL.md` to `~/.claude/skills/playbook/SKILL.md`. Note the name mismatch: the core text asks for `orchestrator:playbook`, which is the *plugin*-namespaced name and only resolves when the plugin is installed. Copied by hand it is a personal skill listed as plain `playbook` — so either read that instruction as `playbook`, or install as a plugin and skip this step.
4. Without the SessionStart injector there is no per-session `started` marker, so the stop guard can't tell another session's ledger from yours (every open ledger costs one reminder per session instead of zero) and the spawn/task gates can't ignore stale fully-closed ledgers.

> Don't run the plugin AND the manual install side by side — you'd get every guard twice.

## Configuration

Set these in `~/.claude/settings.json` under `"env"`.

```
┌───────────────────────────────┬────────────────────┬────────────────────────────────────────────┐
│ Env var                       │ Default            │ Meaning                                    │
├───────────────────────────────┼────────────────────┼────────────────────────────────────────────┤
│ LEDGER_GUARD_THRESHOLD        │ 1500               │ spawn-guard gate (chars)                   │
│ FABLE_ORCH_PROFILE            │ auto               │ pin the chair profile: auto | fable | opus │
│ FABLE_ORCH_TEAMMATE_STOP      │ (off)              │ 1 lets the close guard hold teammates too  │
│ FABLE_ORCH_TEAMMATE_INJECT    │ (off)              │ 1 injects the profile into teammates too   │
│ LEDGER_GUARD_TASKS            │ 3                  │ 3rd ledgerless tracker task denied; 0 off  │
│ LEDGER_GUARD_STOP_MODE        │ once-per-session   │ every-turn restores per-turn blocking      │
│ LEDGER_WRITE_GUARD            │ (on)               │ 0 disables the ledger overwrite guard      │
│ FABLE_ORCH_METRICS            │ (on)               │ 0 disables local metrics logging           │
│ FABLE_ORCH_SWARM_CLEANUP      │ (on)               │ 0 disables all teammate reaping            │
│ FABLE_ORCH_SWARM_MAX_IDLE_H   │ 48                 │ sweep swarms idle ≥ N hours; 0 disables    │
│ FABLE_ORCH_TEAMMATE_IDLE_H    │ 1                  │ kill teammate panes idle ≥ N hours; 0 off  │
│ FABLE_ORCH_TEAMMATE_IDLE_RATE │ 0.01               │ cpu-sec/sec under which a pane is idle     │
│ FABLE_ORCH_COLD_GUARD         │ (on)               │ 0 disables the cold-cache guard entirely   │
│ FABLE_ORCH_COLD_MIN           │ 55                 │ idle minutes after which the cache is cold │
│ FABLE_ORCH_COLD_BLOCK_TOKENS  │ 150000             │ cold context at/above this blocks; 0 off   │
│ FABLE_ORCH_COLD_WARN_TOKENS   │ 50000              │ cold context at/above this warns; 0 off    │
│ FABLE_ORCH_COLD_ACK_MIN       │ 3                  │ minutes a blocked prompt can be re-sent    │
└───────────────────────────────┴────────────────────┴────────────────────────────────────────────┘
```

**The session marker.** The SessionStart injector writes a per-session temp file whose immutable `started` timestamp survives resume/clear/compact re-injections, and the SessionEnd reaper anchors its cleanup to it. It also carries the cold-cache guard's activity stamps (`last_stop`, `last_prompt`) and, while a block is outstanding, its acknowledgement. The same file carries the D1 `ledger` binding: bound → the close guard holds only that ledger; a marker that exists but was never bound → the close guard never holds it; no marker at all (manual install) → the original mtime-ownership rule (ledger touched after the session started). The SessionEnd hook removes the session's temp files and sweeps any older than 96 hours.

**Metrics.** Every hook appends one event line to `~/.claude/fable-orch/metrics.jsonl` (events only — never prompt content): injections per model, mid-session profile switches, spawn/task denies and passes, stop blocks and suppressions, reaps, and cold-cache blocks/warns/acks with the context size and idle gap behind each one. `python3 scripts/stats.py` prints the summary, so the next "how is this performing?" question is answered with data. Disable with `FABLE_ORCH_METRICS=0`.

## Tests

```
python3 -m pytest tests/ -q
```

The hooks are plain stdin/stdout JSON filters; the tests run them end-to-end as subprocesses — the spawn threshold and its env override, the fork exemption, Workflow script gating, the task-list gate (counting, one deny per session, session isolation), the upward ledger search and its repo-root/worktree/$HOME boundaries, stop-guard session scoping and ownership, the cold-cache bands (slash commands and teammates never blocked, the ack window and its expiry, tail-only transcript reads, fail-open on every corrupt input), metrics emission and opt-out, injection, the mid-session profile-switch delta, cache cleanup, and teammate reaping (against a fake tmux/ps on PATH). A second layer pins the *content*: the cores stay under their size budget, both keep requiring the playbook skill, and the decisions that survived the diet (fresh-eyes on every close, the fork cap, the report cap, the batching rule) plus the Fable 5.1 additions (ledger assumptions, the whole-ledger recap, the decline false-positive check, the worker spec blocks), the effort-not-selectable-per-spawn correction, and the project-agent-roster rule, are asserted line by line.

The hooks decide "chair or teammate?" by walking the real process tree, so the suite pins that ambient too — otherwise running the tests from inside a named teammate makes every chair-behaviour test fail for a reason unrelated to the code.

## Honest limitations

- Hooks check ledger **existence and checkbox state**, not fidelity — a shallow ledger passes; writing a faithful one stays a judgment task.
- Freshness is only half-checked: a fully-closed ledger from a previous session re-arms the gates, but a stale ledger with OPEN items still satisfies them (it looks like active work).
- Marking `- [x]` without actually verifying is possible; mechanizing further would invite ritual compliance.
- Chair detection knows two chairs: Fable (primary) and Opus (limit fallback). Any other session model gets the Fable profile, and the 1500-char spawn gate applies everywhere. A mid-session `/model` switch only re-profiles at the next session start — `FABLE_ORCH_PROFILE=opus` pins it immediately for the next fires.
- Enforcement is only as strong as the host's hook pipeline — on at least one experimental spawn backend we observed an async `Agent` launch proceed despite the guard's deny; verify once on your setup.
- Pane idleness is a CPU-rate heuristic: a teammate parked below ~1% CPU for the idle window is reaped, so one blocked for hours inside a single quiet external wait (a long remote build) can be killed mid-wait — raise `FABLE_ORCH_TEAMMATE_IDLE_H`, lower `FABLE_ORCH_TEAMMATE_IDLE_RATE`, or disable with `FABLE_ORCH_TEAMMATE_IDLE_H=0` for such workloads. A reaped teammate can no longer be resumed with SendMessage.
- The cold-cache guard cannot observe the prompt cache; it infers coldness from elapsed time against the 1-hour TTL. A session resumed on a shorter TTL, or one whose cache was evicted early, is judged wrong — and a *warm* session idle past 55 minutes gets a block it didn't need (re-send to proceed). It also can't tell a local slash command from an expensive one: any `/`-prefixed prompt passes, and passing one resets the idle clock, so a `/help` right before a big message can hide a genuine cold resume.
- Unverified: whether `!`-prefixed bash prompts and `#`-prefixed memory prompts fire `UserPromptSubmit` at all. If they do, the guard treats them like any other prompt and can block one, even though neither pays for a re-cache. `/`-prefixed commands are exempt by construction; these two forms are not.
- Its context number is the last assistant message's usage in the transcript, not the request that is about to be sent: work added since that message (a large paste, a re-read file) is invisible, so the estimate is a floor.
- The task guard counts tracker tasks, not work size: a solo multi-phase session that never creates tasks still slips through, and the deny is a single nudge per session, not a wall.
- The orchestration discipline itself is prompt-level; the hooks fence exactly three failure points — the ones that get skipped the most.
- Effort per worker is not controllable from the chair: the Agent tool has no effort parameter, so a spec asking for `low`/`high`/`max` is ignored and the worker inherits the chair's own effort. The plugin ships no agent definitions of its own (only frontmatter on a project's own `.claude/agents/*.md` can set a worker's effort), so every generic worker runs at the chair's level — the model tier (`sonnet`/`opus`/`fable`) is the only per-spawn choice this plugin actually makes.

## Why these guards

Details die *entering* the workflow (task→plan translation, fenced by the spawn guard) and *leaving* it (closing with silently unaddressed items, fenced by the close guard). The third measured failure is the workflow never starting at all: the chair quietly implementing a multi-phase plan solo on the most expensive model (fenced by the task guard). Everything between is judgment — and judgment belongs to the model, not to a regex.

## License

MIT
