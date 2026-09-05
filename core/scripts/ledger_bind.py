#!/usr/bin/env python3
"""PostToolUse hook (Write|Edit|MultiEdit): bind this session to the ledger it writes.

D1 (per-session ledger binding): the spawn/task/close guards used to
discover the "active" ledger by newest-mtime alone, with no session
dimension — two parallel sessions in the same repo would step on each
other's ledgers (session A's close held on session B's newer file, or
worse, silenced by it). The fix threads a per-session binding through
the injector's marker (`$TMPDIR/fable-orch-model-<sid>.json`, key
`"ledger"`), and this hook is one of its three write points (the
others are the spawn/task gate's adoption-on-discovery, and the
close guard never writes it):

    a successful Write/Edit/MultiEdit whose target is the LIVE ledger
    in a `.workflow/` directory binds (or rebinds) this session to
    that exact path.

The live-name filter is the same one `active_ledger_in()` in both
guard scripts uses: `ledger[-._]*.md`, case-insensitive, "ledger" a
whole leading segment (not `ledgers.md`), *-archive.md/_archive.md
excluded. The parent directory must be literally named `.workflow` —
this hook does not walk upward like `find_ledger()`; a Write outside
that directory is not a ledger write.

No session marker yet (no SessionStart injection this session, or a
manual install without the injector) -> nothing to bind onto, so this
hook does nothing. Every other file write, and every write whose
target is not a live ledger name, is a no-op too.

Always exits 0. Every failure mode (malformed stdin, unreadable
marker, permission error on the atomic replace, ...) is swallowed —
this hook must never block or fail a Write/Edit/MultiEdit.
"""
import json
import os
import sys
import tempfile


def session_marker_path(session_id):
    """Path of the per-session injector marker, or None without an id."""
    if not session_id:
        return None
    safe = "".join(c for c in str(session_id) if c.isalnum() or c in "-_")
    return os.path.join(tempfile.gettempdir(), f"fable-orch-model-{safe}.json")


def _is_live_ledger_name(name):
    """Same live-name filter as active_ledger_in() in both guards:
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


def _bind(data):
    session_id = data.get("session_id")
    cache = session_marker_path(session_id)
    if not cache or not os.path.isfile(cache):
        return  # no marker to bind onto (no injection this session)

    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return

    real = os.path.realpath(file_path)
    if not os.path.isfile(real):
        return  # Write payload for a path that was never actually created
    if not _is_live_ledger_name(os.path.basename(real)):
        return
    # Accept EITHER the raw path's parent or the realpath's parent being
    # named ".workflow" (case-insensitively): a symlinked .workflow/ dir
    # resolves its realpath parent to the symlink's TARGET directory name,
    # which find_ledger() (walking the raw tree) still treats as live.
    raw_parent = os.path.basename(os.path.dirname(file_path)).lower()
    real_parent = os.path.basename(os.path.dirname(real)).lower()
    if raw_parent != ".workflow" and real_parent != ".workflow":
        return

    try:
        with open(cache, encoding="utf-8") as f:
            marker = json.load(f)
    except Exception:
        marker = {}
    if not isinstance(marker, dict):
        marker = {}
    marker["ledger"] = real

    # Atomic replace: same pattern as inject_instructions.py's marker
    # rewrite — a crash mid-write must never leave a truncated marker.
    tmp = f"{cache}.{os.getpid()}.tmp.json"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(marker, f)
    os.replace(tmp, cache)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return  # malformed input -> never block
    if not isinstance(data, dict):
        return
    try:
        _bind(data)
    except Exception:
        return  # fail open; this hook never crashes the pipeline


if __name__ == "__main__":
    main()
