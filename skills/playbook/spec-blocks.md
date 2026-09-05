# Paste-ready worker blocks

Two blocks the chair appends to a worker's prompt verbatim. Both are
the Fable 5.1 prompting guide's own wording ("Keep changes and tests
to what the task asks for", "Prefer targeted edits over whole-file
rewrites", "Leave room for long outputs at xhigh and max effort"),
adapted only where noted.

## SCOPE + EDITS — end every implementation spec with this

```text
If, while working or testing, you find a pre-existing bug, a
performance concern, or behavior the task doesn't mention, don't fix,
optimize or extend it in this change unless the requested behavior
cannot work without it; report it as a follow-up in your summary.
Where the task is ambiguous, implement the reading its wording and
the surrounding code most directly support, state that assumption in
your summary, and don't build for the other readings as well. Verify
your work however you like; scratch scripts and quick checks need not
be kept. Commit tests only where the task asks for them or this
repository already keeps tests for this kind of change, sized like
the neighboring test files — roughly one focused test per stated
behavior — and don't turn scratch checks into additional permanent
test files. This is about extras only: implement every behavior the
task asks for, completely.

The number of tokens used to edit files is best minimized, all else
being equal. Therefore, when it will not affect the end result, try
to surgically edit a file rather than rewrite the entire thing.
```

The follow-ups it asks for land in section 5 of the output contract,
"out of scope but noticed" — the report shape the chair already
requires, so nothing extra needs saying in the spec.

## LONG OUTPUT — add for a fable spawn writing a long deliverable

A report, a spec, a large file. The guide's `[max_tokens]` placeholder
is the turn's own output limit in an agent session, so the block below
names it that way; nothing else is changed.

```text
Everything produced in one reply, including any reasoning or drafting
done before the reply, counts toward a single limit — the turn's
output limit. If that limit is reached before the reply is finished,
the person receives a cut-off response and has to start over.
Composing an entire output or deliverable in full as reasoning and
then again as a reply would double the length of the turn without
improving the result, so don't do that.

Instead, when the person has asked for a long or effort-intensive
deliverable such as a multi-section document, a large table or
dataset, or a complete code file, spend extra effort on understanding
the request, checking the inputs the answer depends on, settling the
structure and other difficult decisions, and otherwise using the
reasoning space to reason and the output space to write an output.
Usually it is not needed to draft an output multiple times.
```
