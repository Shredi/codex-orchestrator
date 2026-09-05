---
name: playbook
description: Orchestrator playbook — the delegation contract: research, output contract, worker specs, spawn economics, forks, teammates, verification, declines, hygiene. The chair MUST load it before its first delegation each session; the core only summarizes it.
---

# Orchestrator Playbook

Both chair profiles (FABLE and OPUS). The injected core wins on
routing and limits; this is the detail behind it. Paste-ready worker
blocks: `spec-blocks.md`, next to this SKILL.md (repo path
`skills/playbook/spec-blocks.md`).

## Research pipeline — fan out, no mid-flight dumps

YOU pick the questions and sources — never a fetch worker. ONE sonnet
per source: it fetches the source VERBATIM to ./.workflow/scratch/
FIRST (that copy is the audit trail, no filtering during fetch), THEN
returns a brief from it: claims, evidence, exact quotes, confidence,
contradictions, path. A final sonnet synthesizes; YOU check it and
its evidence against the ledger and decide. Intermediates never enter
your context.

## Subagent output contract (enforced)

Every subagent returns:

1. ledger items addressed, by number
2. summary
3. VERBATIM code/config/errors/quotes the conclusion depends on —
   at most 10 lines inline, longer to ./.workflow/scratch/ with the
   path in the report
4. confidence: "confident" / "uncertain because X"
5. "out of scope but noticed"

Reports are at most 40 lines TOTAL. A violating return is rejected
and re-run — never silently accepted.

## Worker spec boilerplate — every implementation spec

Every implementation spec ends with the SCOPE + EDITS block from
`spec-blocks.md` (same directory), verbatim: it fences the change to
what the task asks for — scope, ambiguity, test volume — and keeps
edits surgical, not rewrites. Read it before your first spawn.

## Spawn economics

Every spawn pays a fixed overhead (system prompt, project rules, tool
schemas) first. Batch similar mechanical steps into ONE worker with a
checklist; spawn separately only when parallelism or isolation pays
that back. Read-only agents share the repo; parallel EDITORS each
run with `isolation: "worktree"`.

Before defaulting to a generic worker, check the project's agent
roster — CLAUDE.md's `## Orchestrator agents` section plus
auto-discovered `.claude/agents/` — for a matching specialized agent;
it keeps bulky domain output off the chair and carries its own
`model:`/`effort:` frontmatter, so spawn it via `subagent_type`
instead of a generic spec.

## Keep working while workers run

A spawn is not a pause: while a wave runs, write the next phase's spec
or the verifier brief; results arrive as notifications.

## Forks

`subagent_type: "fork"` clones your FULL conversation at your model
and spends the usage limit: at most 2 per session, only while the
conversation is short, and only for bounded follow-ups leaning on
context a spec cannot carry. Forking a plan's phases is disguised
solo work — phases go to workers with specs.

## Named teammates

NAME every substantive worker (implementation, review, research,
verification): named teammates run in tmux panes the user watches
live; an unnamed one is a silent spinner until it returns. Only
sub-minute lookups stay unnamed. Steer a running teammate with
SendMessage; on an ACCEPTED report with no follow-up planned,
dismiss it: `{"type": "shutdown_request"}`. Dismissal is final, so
dismiss only after processing the output; never leave finished
teammates stacked (the plugin reaps them).

## Long outputs (fable)

Any fable spawn asked for a long deliverable — a report, spec or
large file — carries the LONG OUTPUT block from `spec-blocks.md`
(same directory): left to itself, a fable spawn drafts the
deliverable twice, once in reasoning and again as the reply.

## Verification procedure

The verifier is FRESH — it has not worked on the task. Give it the
original request, the ledger path and the work-product paths (diffs,
reports, not the raw scratch dump). It reads from disk to find what
is missing, wrong or unaddressed, item by item; only it closes `V.`.
Findings become new phases; re-verify. CAP 3 cycles, then STOP and
report the open items.

## Declines — fix the input

Three documented false-positive triggers: base64 in tool output a
worker read (remove it), "does this compile" phrasing (ask "are there
bugs"), a lesser-known language with no docs (supply them). Removing
such a cause and rerunning the SAME tier fixes the input, not the
wording. Otherwise rerun UNCHANGED on another tier; a second decline
STOPS the work: tell the user. Security review stays on opus.

## Chair context hygiene

Consume briefs + verbatim snippets; bulk stays on disk. When a
decision hinges on short exact content, read it yourself — never on a
summary of a source that fits in a few hundred lines. Prefer per-task
sessions: ledger and scratch survive /clear — finish a task, close it,
start the next clean. Drop closed-phase raw material. Your closing
recap walks the WHOLE ledger, item by item.
