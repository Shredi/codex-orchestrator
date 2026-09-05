#!/usr/bin/env python3
"""PreToolUse guard (Write): stop a fresh Write from clobbering an
EXISTING live ledger that belongs to another session or a previous
task.

.workflow/ is shared, session-agnostic, and not git-tracked: nothing
stops a chair from `Write`-ing straight over a ledger a concurrent
session (or an earlier task in this same repo) is still tracking
state in — the tool has no notion of "this file already has content
I did not write." A Write is a full-file replace, unlike Edit's
surgical patch, so this is exactly the operation that destroys that
other task's ledger outright. This guard closes that gap the same
way the spawn/stop guards close the discovery gap: by refusing the
call, once, when it looks unowned.

DENY only when EVERY one of these holds:
    - the Write target already exists on disk (an in-place overwrite,
      not a brand-new ledger)
    - it is a live-named ledger (`_is_live_ledger_name`, the same
      filter `ledger_bind.py` and both spawn/stop guards use:
      "ledger" a whole leading segment, case-insensitive,
      *-archive.md/_archive.md excluded)
    - its parent directory is literally named `.workflow` (raw OR
      realpath parent, exactly like `ledger_bind.py` — a symlinked
      `.workflow/` must still count)
    - this session has a marker (an injected session), AND
    - the marker's `ledger` binding does not resolve to this exact
      path (`os.path.normcase(os.path.realpath(...))` on both sides —
      Windows paths are case-insensitive and may carry 8.3 short
      forms or drive-letter case differences that raw string
      comparison would miss)

ALLOW (return with no output) in every other case: the target does
not exist yet (this IS how a new ledger gets created); the session is
bound to exactly this path (continuing its own ledger via Write, e.g.
a full rewrite after heavy edits); there is no session marker at all
(manual install, or before this session's first SessionStart fire —
fail open rather than block on a signal we don't have); the target
is not a live ledger name or not under `.workflow/` (none of this
guard's business); or `LEDGER_WRITE_GUARD=0`.

No file is written or locked here — only read — so no fcntl is
needed, and the guard runs identically on macOS/Linux/Windows
(Git Bash `python3`, per the shim documented in ledger_guard_spawn.py
and friends).

Always exits 0. Every failure mode (malformed stdin, unreadable
marker, a realpath that raises, ...) is swallowed — this hook must
never block or fail a Write it can't fully reason about.

Configuration:
    LEDGER_WRITE_GUARD=0      disables this guard entirely
    FABLE_ORCH_METRICS=0      disables the local metrics log
"""
import json
import os
import sys
import tempfile
import time


def session_marker_path(session_id):
    """Path of the per-session injector marker, or None without an id.
    Same helper as ledger_bind.py's copy."""
    if not session_id:
        return None
    safe = "".join(c for c in str(session_id) if c.isalnum() or c in "-_")
    return os.path.join(tempfile.gettempdir(), f"fable-orch-model-{safe}.json")


def _is_live_ledger_name(name):
    """Same live-name filter as active_ledger_in()/ledger_bind.py:
    `LEDGER*.md`, case-insensitive, "ledger" a whole segment, and not
    retired via a trailing -archive.md/_archive.md."""
    low = name.lower()
    if not (low.startswith("ledger") and low.endswith(".md")):
        return False
    if low[6:7] not in (".", "-", "_"):
        return False
    if low.endswith("-archive.md") or low.endswith("_archive.md"):
        return False
    return True


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


def _paths_equal(a, b):
    """True when `a` and `b` name the same file, compared the way the
    guard must on Windows: `os.path.normcase(os.path.realpath(...))`
    on BOTH sides. normcase is a no-op on POSIX (case-sensitive
    filesystems) but folds case (and forward/back slashes) on nt —
    this is what lets a session bound to `C:\\...\\Ledger.md` still
    match a Write to `c:\\...\\ledger.md`. Any resolution failure
    (bad path, permissions) answers False rather than raise."""
    try:
        return (os.path.normcase(os.path.realpath(a))
                == os.path.normcase(os.path.realpath(b)))
    except Exception:
        return False


def _bound_path(session_id):
    """The realpath this session's marker is bound to, or None — no
    marker, unreadable marker, or no `ledger` key all return None
    (the caller's fail-open cases)."""
    cache = session_marker_path(session_id)
    if not cache or not os.path.isfile(cache):
        return None
    try:
        with open(cache, encoding="utf-8") as f:
            marker = json.load(f)
    except Exception:
        return None
    if not isinstance(marker, dict):
        return None
    ledger = marker.get("ledger")
    if not isinstance(ledger, str) or not ledger:
        return None
    return ledger


def _guard(data):
    if (os.environ.get("LEDGER_WRITE_GUARD") or "").strip() == "0":
        return

    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return

    real = os.path.realpath(file_path)
    if not os.path.isfile(real):
        return  # brand-new file: this IS how a ledger gets created

    if not _is_live_ledger_name(os.path.basename(real)):
        return

    # Same raw-OR-realpath-parent acceptance as ledger_bind.py: a
    # symlinked .workflow/ resolves its realpath parent to the
    # symlink's TARGET directory name.
    raw_parent = os.path.basename(os.path.dirname(file_path)).lower()
    real_parent = os.path.basename(os.path.dirname(real)).lower()
    if raw_parent != ".workflow" and real_parent != ".workflow":
        return

    session_id = data.get("session_id")
    cache = session_marker_path(session_id)
    if not cache or not os.path.isfile(cache):
        return  # no marker at all -> fail open (manual install)

    bound = _bound_path(session_id)
    if bound and _paths_equal(bound, real):
        return  # this session's own ledger -> Write is fine

    bound_name = os.path.basename(bound) if bound else None
    _metric("write_deny", session_id,
            path=os.path.basename(real), bound=bound_name)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"LEDGER GUARD: {real} is an EXISTING live ledger — "
                "likely another session's or a previous task's. "
                "Overwriting it destroys that task's state (.workflow/ "
                "is shared and not git-tracked). Create a NEW "
                "topic-named ledger at ./.workflow/LEDGER-<topic>.md "
                "instead. If you genuinely mean to continue THIS "
                "ledger, use Edit (surgical, content-preserving) "
                "rather than Write. Set LEDGER_WRITE_GUARD=0 to "
                "disable this guard."
            ),
        }
    }))


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return  # malformed input -> never block
    if not isinstance(data, dict):
        return
    try:
        _guard(data)
    except Exception:
        return  # fail open; this hook never crashes the pipeline


if __name__ == "__main__":
    main()
