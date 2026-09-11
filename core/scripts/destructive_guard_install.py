#!/usr/bin/env python3
"""SessionStart hook: put Layer B of the destructive-command guard in
place — the `rm` shim that sees the EXPANDED arguments.

`scripts/destructive_guard.py` reads the command TEXT before the shell
touches it; that is blind to `rm -rf -- "$1"/*` once a script, `xargs`
or `find -exec` builds the argv at run time. The shim in `guard/rm`
closes that half, but only if it is actually on PATH, first, in a
session's Bash tool. This hook does both jobs, once per session start:

    1. copy `${CLAUDE_PLUGIN_ROOT}/guard/rm` to ~/.claude/guard/bin/rm
       when it is missing or its content differs (atomic write via a
       temp file in the same directory + os.replace, then chmod 0755 —
       a chmod failure on Windows is not fatal, Git Bash runs the
       script through `sh` either way)
    2. append `export PATH="$HOME/.claude/guard/bin:$PATH"` to the file
       named by $CLAUDE_ENV_FILE, once (the line is matched literally
       before appending, so resume/clear/compact re-fires cannot stack
       copies of it)

The shim lives under ~/.claude rather than inside the plugin because
Claude Code APPENDS a plugin's bin/ to PATH — appended loses to
/bin/rm, and only a prepend can win. The same target path is used by
the Codex harness's `codex-sync`, so both harnesses share one shim.

Emits nothing on stdout: SessionStart stdout becomes session context,
and this hook has nothing to say to the model. Always exits 0; every
failure (no plugin root, unreadable source, read-only HOME) is
swallowed — a session must start even when the shim cannot be placed.
Layer A still denies the dangerous text shapes on its own.

Configuration:
    DESTRUCTIVE_GUARD=0       disables the guard (no install, no PATH)
    FABLE_ORCH_METRICS=0      disables the local metrics log
"""
import json
import os
import sys
import tempfile
import time

ENV_LINE = 'export PATH="$HOME/.claude/guard/bin:$PATH"'


def _metric(event, session_id=None, **extra):
    """Append one event line to ~/.claude/fable-orch/metrics.jsonl (best
    effort). Stamped `"harness": <FABLE_ORCH_HARNESS>` when that env var
    is set (unset -> key omitted, output unchanged for Claude Code) so an
    external adapter (e.g. a Codex CLI harness) can tell its own events
    apart without rewriting this file's bytes after the fact."""
    if (os.environ.get("FABLE_ORCH_METRICS") or "").strip() == "0":
        return
    try:
        d = os.path.join(os.path.expanduser("~"), ".claude", "fable-orch")
        os.makedirs(d, exist_ok=True)
        rec = {"ts": round(time.time(), 3), "event": event}
        if session_id:
            rec["session"] = str(session_id)[:8]
        rec.update(extra)
        harness = (os.environ.get("FABLE_ORCH_HARNESS") or "").strip()
        if harness:
            rec["harness"] = harness
        with open(os.path.join(d, "metrics.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _source_path():
    """`${CLAUDE_PLUGIN_ROOT}/guard/rm`, falling back to this script's own
    repo (manual install, or a run straight out of a clone)."""
    root = (os.environ.get("CLAUDE_PLUGIN_ROOT") or "").strip()
    if not root:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "guard", "rm")


def _install_shim():
    """True when the shim was written this run (False = already current
    or not installable)."""
    src = _source_path()
    with open(src, "rb") as f:
        payload = f.read()
    if not payload:
        return False
    dst_dir = os.path.join(os.path.expanduser("~"), ".claude", "guard", "bin")
    dst = os.path.join(dst_dir, "rm")
    try:
        with open(dst, "rb") as f:
            if f.read() == payload:
                return False  # already current
    except Exception:
        pass
    os.makedirs(dst_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dst_dir, prefix=".rm-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        try:
            os.chmod(tmp, 0o755)
        except Exception:
            pass  # Windows: the mode bit is meaningless, sh runs it anyway
        os.replace(tmp, dst)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise
    return True


def _append_env_line():
    """True when the PATH export was appended this run."""
    env_file = (os.environ.get("CLAUDE_ENV_FILE") or "").strip()
    if not env_file:
        return False
    existing = ""
    try:
        with open(env_file, encoding="utf-8") as f:
            existing = f.read()
    except Exception:
        existing = ""
    if ENV_LINE in existing:
        return False  # idempotent across resume/clear/compact re-fires
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    with open(env_file, "a", encoding="utf-8") as f:
        f.write(sep + ENV_LINE + "\n")
    return True


def main():
    if (os.environ.get("DESTRUCTIVE_GUARD") or "").strip() == "0":
        return
    session_id = None
    try:
        data = json.load(sys.stdin)
        if isinstance(data, dict):
            session_id = data.get("session_id")
    except Exception:
        pass  # a missing/garbled payload is no reason to skip the install
    installed = False
    try:
        installed = _install_shim()
    except Exception:
        pass
    pathed = False
    try:
        pathed = _append_env_line()
    except Exception:
        pass
    if installed or pathed:
        _metric("destructive_shim_install", session_id,
                shim=bool(installed), path=bool(pathed))


if __name__ == "__main__":
    main()
