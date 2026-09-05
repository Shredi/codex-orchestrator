#!/usr/bin/env python3
"""SessionStart hook: inject the Dynamic Workflow instructions.

This plugin is built for a Claude Fable 5 chair, with an Opus
fallback: when the Fable limit is spent and the user moves the chair to
Opus, the OPUS profile keeps the same discipline (the fable tier rests,
verification and the escalation ceiling fall to opus). The chair is
detected per session start and the matching profile injected:

    opus chair    -> dynamic-workflow-opus.md
    anything else -> dynamic-workflow-fable.md   (fable / unknown)

Detection, in priority order (first hit wins):

    1. FABLE_ORCH_PROFILE = fable | opus   — explicit pin, overrides all
       (auto / unset falls through to detection)
    2. the SessionStart payload's `model`  — authoritative for THIS
       session start, but the harness omits it on some resume/compact
       fires
    3. the user's configured default model in Claude Code settings.json
       — what `/model` persists, so it still tracks the chair when (2)
       is absent (the common "I switched to Opus but the payload was
       empty" case)
    4. the last model this session's marker saw — sticky fallback so a
       null-payload resume never regresses an opus session to fable
    5. fable — the safe default

A mid-session /model switch still only takes visible effect at the next
session start (startup/resume/clear), because SessionStart is the sole
injection point — but (3) makes that next start reliable instead of
racy.

PROFILE-SWITCH DELTA. When a session that already received a core
profile re-fires with the OTHER profile selected (the Fable limit ran
dry mid-session and the chair moved to Opus, or back), the full core is
NOT re-sent — it is already in context, and re-sending it spends the
very limit it exists to protect. A short switch note carries only the
deltas instead:

    fable -> opus -> profile-switch-to-opus.md
    opus  -> fable -> profile-switch-to-fable.md

The marker records the profile this session was last TOLD, so a plain
re-fire (same profile) is indistinguishable from before — it still gets
the full core. A marker with no recorded profile (a pre-0.15.0 marker,
or a session whose only fires were teammate skips) also gets the full
core: a delta is only ever safe on top of a core this session saw.

The delta is further gated to SessionStart `source == "resume"`, the
only fire that provably leaves the earlier injection in context.
`compact` fires precisely BECAUSE the context was rewritten, `clear`
because it was discarded, and a future source is simply unproven — all
three get the full core even when the profile changed. The switch note
says "every other rule from the already-injected core profile stays in
force", which is a lie the chair cannot detect if the core is gone.

LEDGER REMINDER. On the two fires that can cost the chair its own
earlier reasoning — `compact` (the context was rewritten) and `resume`
(a transcript this model never thought through) — a session that
already carries a ledger binding gets one extra line after the profile,
naming that ledger's path. Fable 5.1 executes long horizons well but
only from what is in front of it; the file on disk is what survived.
Unbound sessions, startup/clear fires, and bindings whose file is gone
or archived get nothing.

TEAMMATE sessions are skipped entirely. Named agent-teams workers are
full claude sessions and fire SessionStart like the chair does — but the
profile is written for the chair alone: injected into a worker it says
"you are the ORCHESTRATOR" and invites it to spawn subagents, inverting
the very discipline the plugin enforces (measured in the wild: 172 of
270 injected sessions were teammates). Detection is the same ancestor
walk the stop guard uses (`--agent-id` on the nearest claude ancestor);
the session marker is still written so the other guards keep working.
FABLE_ORCH_TEAMMATE_INJECT=1 restores the old inject-everyone
behaviour.

The hook also maintains the per-session marker the Stop and SessionEnd
hooks rely on: its immutable `started` timestamp survives the re-runs
SessionStart gets on resume/clear/compact, and the stop guard compares
ledger mtimes against it to decide ownership.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time


def session_model_cache_path(session_id):
    """Per-session marker file the stop/cleanup hooks read. None if no id."""
    if not session_id:
        return None
    safe = "".join(c for c in str(session_id) if c.isalnum() or c in "-_")
    return os.path.join(tempfile.gettempdir(), f"fable-orch-model-{safe}.json")


def _metric(event, session_id=None, **extra):
    """Append one event line to ~/.claude/fable-orch/metrics.jsonl (best effort)."""
    if (os.environ.get("FABLE_ORCH_METRICS") or "").strip() == "0":
        return
    try:
        d = os.path.join(os.path.expanduser("~"), ".claude", "fable-orch")
        os.makedirs(d, exist_ok=True)
        rec = {"ts": round(time.time(), 3), "event": event}
        if session_id:
            rec["session"] = str(session_id)[:8]
        rec.update(extra)
        with open(os.path.join(d, "metrics.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _is_opus(value):
    """True when the model string names the opus tier.

    Bounded, not a bare substring: `claude-octopus-1` and `opusculum`
    contain "opus" but are not Opus chairs. The bound stays permissive
    on the right so a version can follow with or without a separator —
    `claude-opus-5`, `opus5`, `opus[1m]`, `Opus 5 (1M context)` all
    match; only a letter immediately after "opus" disqualifies it.
    """
    return re.search(r"\bopus(?![a-z])", str(value or ""),
                     re.IGNORECASE) is not None


def _configured_model():
    """The user's configured default model from Claude Code settings, or
    None. `/model` persists the default here, so it tracks the current
    chair even when the SessionStart payload omits `model`. settings.local
    overrides settings; either may carry the key."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude")
    for name in ("settings.local.json", "settings.json"):
        try:
            with open(os.path.join(base, name), encoding="utf-8") as f:
                m = json.load(f).get("model")
        except Exception:
            continue
        if isinstance(m, str) and m.strip():
            return m
    return None


def _read_marker(cache):
    """(started, model, profile, ledger) from the marker; all None if unreadable.

    `profile` is the profile this session was last INJECTED with — the
    switch detector's only input. It is absent on markers written by
    pre-0.15.0 versions and on sessions whose fires were all teammate
    skips; in both cases the caller must fall back to the full core.

    `ledger` is the D1 per-session ledger binding (scripts/ledger_bind.py,
    and the spawn/task guards' adoption-on-discovery). It must be carried
    forward through every marker rewrite below — SessionStart re-fires on
    resume/clear/compact and rewrites this file each time, and a rewrite
    that dropped the key would silently unbind the session mid-workflow."""
    if not cache:
        return None, None, None, None
    try:
        with open(cache, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d.get("started"), d.get("model"), d.get("profile"), d.get("ledger")
    except Exception:
        pass
    return None, None, None, None


# Keys in the marker that belong to OTHER hooks and that this rewrite
# must carry forward untouched. The cold-cache guard's activity stamps
# live here: `last_stop` (written by the Stop hook), `last_prompt`
# (written by the guard itself), and the `cold_ack`/`cold_ctx` pair of an
# outstanding block. SessionStart re-fires on resume/clear/compact and
# rebuilds this file from a whitelist, so a key not named here is
# silently dropped — which for the stamps meant that a `claude --resume`
# of yesterday's 400k-token session lost its idle baseline and sailed
# through the guard on exactly the message the guard exists for.
CARRIED_KEYS = ("last_stop", "last_prompt", "cold_ack", "cold_ctx")


def _carried(cache):
    """The CARRIED_KEYS present in the marker, or {} if unreadable.

    A second tiny read rather than a wider `_read_marker` signature: this
    hook has no business interpreting those values, only preserving
    them."""
    if not cache:
        return {}
    try:
        with open(cache, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    return {k: d[k] for k in CARRIED_KEYS if k in d}


# SessionStart fires after which the chair's own earlier reasoning may
# be gone: `compact` rewrote the context, `resume` reloads a transcript
# this model never actually thought through. The ledger on disk is what
# survives both — so a session already BOUND to one is pointed back at
# it, right after the profile. `startup` and `clear` open a new task,
# where an old binding is not something to re-open.
LEDGER_REMINDER_FIRES = ("compact", "resume")


def ledger_reminder(fire, ledger):
    """The 'your ledger is still on disk' line, or '' when it doesn't apply.

    Only for a session that already carries a `ledger` binding (written
    by the write guard / spawn-guard adoption): with no binding there is
    no path to name, and pointing an unbound session at a stale ledger
    would hand it another task's requirements.

    A binding can also outlive what it points at — the file was deleted,
    or retired by the `-archive.md` rename the close guard honours. Both
    are silent: naming a path that no longer holds the task's
    requirements is worse than saying nothing."""
    path = str(ledger or "").strip()
    if fire not in LEDGER_REMINDER_FIRES or not path:
        return ""
    if path.endswith("-archive.md") or not os.path.isfile(path):
        return ""
    return (f"Live ledger for this session: {path} — re-read it before "
            "your next decision; reasoning from before this point may be "
            "gone.")


TEAMMATE_DETECT_BUDGET = 1.5  # seconds; the walk measures ~5ms in practice


def _budget(deadline, cap=5.0):
    """Seconds a subprocess may run without overshooting the deadline.

    Monotonic, exactly as in the stop guard: a wall clock can step
    backwards (NTP, a manual change) and would then hand back a budget
    that never expires, defeating the bound entirely."""
    if deadline is None:
        return cap
    return max(0.2, min(cap, deadline - time.monotonic()))


def _is_teammate_session(max_hops=12):
    """True when this hook is running inside a named teammate.

    Teammates are launched with `--agent-id`. The profile belongs to the
    CHAIR: a worker that receives it is told it is the orchestrator and
    may spawn subagents liberally — the inverse of its actual job. Walks
    up to the first claude ancestor and answers from its argv; same
    logic as the stop guard's copy, kept verbatim so a future common
    module can unify them.

    HARD-BUDGETED because SessionStart must never hang a session open:
    on budget exhaustion the answer is False — "assume chair", so the
    profile is still delivered. That failure costs one teammate carrying
    the profile (the pre-fix behaviour for every teammate); the opposite
    default would strip the chair of its orchestration instructions.
    """
    deadline = time.monotonic() + TEAMMATE_DETECT_BUDGET
    pid = os.getpid()
    for _ in range(max_hops):
        if time.monotonic() > deadline:
            return False
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=,command=", "-p", str(pid)],
                capture_output=True, text=True, timeout=_budget(deadline),
            ).stdout.strip()
            bits = (out.splitlines()[0] if out else "").split(None, 1)
            ppid = int(bits[0])
        except Exception:
            return False
        command = bits[1] if len(bits) > 1 else ""
        for tok in command.split():
            base = os.path.basename(tok.strip("\"'"))
            if base == "claude" or "claude-code" in tok or base.startswith("2."):
                return "--agent-id" in command
        if ppid <= 1:
            return False
        pid = ppid
    return False


def resolve_profile(payload_model, configured_model, marker_model):
    """Return (profile, source) — 'opus'|'fable' and which signal decided.
    Priority: env override > payload model > settings default > marker."""
    override = (os.environ.get("FABLE_ORCH_PROFILE") or "").strip().lower()
    if override in ("fable", "opus"):
        return override, "override"
    if str(payload_model or "").strip():
        return ("opus" if _is_opus(payload_model) else "fable"), "payload"
    if str(configured_model or "").strip():
        return ("opus" if _is_opus(configured_model) else "fable"), "settings"
    if str(marker_model or "").strip():
        return ("opus" if _is_opus(marker_model) else "fable"), "marker"
    return "fable", "default"


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    model = data.get("model")  # optional; the harness omits it on some fires
    session_id = data.get("session_id")
    fire = data.get("source")  # startup | resume | clear | compact (advisory)
    cache = session_model_cache_path(session_id)
    prev_started, prev_model, prev_profile, prev_ledger = _read_marker(cache)

    profile, source = resolve_profile(model, _configured_model(), prev_model)

    # Profile-switch delta: this session already carries a core profile
    # and the chair has since moved to the other tier. Re-sending ~3.7k
    # chars of unchanged rules costs the limit the profile exists to
    # protect, so only the deltas go out. Requires a RECORDED previous
    # profile — never inferred, because a delta on top of no core would
    # silently strip the chair of every orchestration rule.
    # GATED TO `resume`, the only fire that provably keeps the core in
    # context. `compact` re-fires BECAUSE the context was rewritten and
    # `clear` because it was discarded — a delta on either can leave the
    # chair with no threshold, no ledger rule and no routing, silently.
    # Any unrecognised future source takes the same safe side: an
    # unproven source gets the full core. Wrong-delta costs a ruleless
    # chair; wrong-full-core costs ~3.7k chars.
    switched = (bool(prev_profile) and prev_profile != profile
                and fire == "resume")
    filename = (f"profile-switch-to-{profile}.md" if switched
                else f"dynamic-workflow-{profile}.md")

    # The profile is chair-only; a teammate session skips the injection
    # but still gets its marker below — stop, spawn, and cleanup key off
    # it. Resolution ran first so the skip metric records which profile
    # the worker WOULD have received.
    teammate = False
    if (os.environ.get("FABLE_ORCH_TEAMMATE_INJECT") or "").strip() != "1":
        teammate = _is_teammate_session()

    text = None
    if not teammate:
        root = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
        path = os.path.join(root, "instructions", filename)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except Exception:
            return  # never break session start
        # Appended AFTER the profile (full core or switch delta), so the
        # last thing a compacted/resumed chair reads is where its own
        # requirements live. No-op for an unbound session and for
        # startup/clear.
        reminder = ledger_reminder(fire, prev_ledger)
        if reminder:
            text = text.rstrip("\n") + "\n\n" + reminder + "\n"

    # Session marker for the guards (best effort; never fatal).
    # `started` marks the session's FIRST start and must survive the
    # re-runs SessionStart gets on resume/clear/compact — the stop guard
    # compares ledger mtimes against it to decide ownership, so it can
    # never move forward. `model` keeps the last NON-EMPTY model seen, so
    # a later null-payload fire stays sticky instead of forgetting the
    # chair.
    try:
        if cache:
            started = prev_started
            try:
                started = float(started)
            except (TypeError, ValueError):
                # Marker from an older version (no `started`) or corrupt:
                # fall back to the file's mtime — NEVER to "now", which
                # would disown every ledger touched before this re-run.
                try:
                    started = os.path.getmtime(cache)
                except OSError:
                    started = time.time()
            stored_model = model if str(model or "").strip() else prev_model
            # `profile` records what this session was actually TOLD, so
            # the next fire can tell a switch from a plain re-fire. A
            # teammate received nothing, so its marker carries the
            # previous value forward rather than claiming an injection
            # that never happened.
            stored_profile = prev_profile if teammate else profile
            # D1 per-session ledger binding: this rewrite must carry the
            # existing `ledger` key forward, or every resume/clear/compact
            # re-injection would silently unbind the session mid-workflow.
            # Omitted entirely when there was never a binding to carry.
            marker = {"model": stored_model, "session_id": session_id,
                      "started": round(started, 3), "profile": stored_profile}
            if prev_ledger:
                marker["ledger"] = prev_ledger
            # Same rule as `ledger`, for the same reason: this rewrite is
            # a re-injection, not a new session, and every key another
            # hook owns has to survive it. See CARRIED_KEYS.
            marker.update(_carried(cache))
            # Atomic replace: a crash mid-write must never leave a
            # truncated marker. The tmp name keeps the fable-orch-*.json
            # shape so an orphan from a crash still matches the 96h sweep.
            tmp = f"{cache}.{os.getpid()}.tmp.json"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(marker, f)
            os.replace(tmp, cache)
    except Exception:
        pass

    if teammate:
        _metric("inject_skipped", session_id, model=model, profile=profile,
                source=source, reason="teammate")
        return

    if switched:
        # Distinct event, not a field on `inject`: an inject counts a
        # session that received the discipline, a switch counts a chair
        # that moved tiers mid-session. `fire` records which SessionStart
        # kind delivered the delta (resume/compact/clear).
        _metric("inject_switch", session_id, model=model, profile=profile,
                source=source, from_profile=prev_profile, fire=fire)
    else:
        _metric("inject", session_id, model=model, profile=profile,
                source=source)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }))


if __name__ == "__main__":
    main()
