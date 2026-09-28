#!/usr/bin/env python3
"""PreToolUse guard (Read|Grep|Glob|WebFetch|WebSearch|Bash): the Fable
top chair explores through scouts, not through its own context.

A Fable chair is long-lived (0.21.0 top-orchestrator profile): it talks
with the user and judges, while exploration — greps, file reads beyond
a short brief, web — goes to sonnet scouts that write briefs to
.workflow/scratch/. Every non-brief read the chair does itself is paid
from the scarcest limit AND stays in a context meant to live for many
tasks. This hook counts those reads per task and nudges, then denies.

Active only when ALL hold (checked cheapest first):
    FABLE_ORCH_READ_GUARD is not off/0
    the payload has no `agent_id` — subagents (Explore, workers,
    verifiers) carry one in their PreToolUse payload (verified live
    on Claude Code 2.1.284: main-session payloads have no agent_id,
    an Explore subagent's Read carries agent_id + agent_type)
    the session marker exists and its profile is "fable" (a teammate's
    marker records profile None — the injector skips it)
    the ancestor walk does not find a named teammate (backstop)

Exempt (metered as decision=exempt, never counted):
    any path with a `.workflow` segment; ~/.claude/plans/,
    ~/.claude/handoffs/, ${CLAUDE_PLUGIN_ROOT}/skills/, any
    /skills/playbook/ path; MEMORY.md / CLAUDE.md;
    Read with limit <= 60, or no limit and a file <= 6 KB;
    Grep/Glob whose `path` is allowlisted (no path = counted).
    WebFetch/WebSearch are never exempt.
Bash: leading VAR=x assignments and `cd ... &&` are stripped; only a
first word of cat/rg/grep/egrep/sed -n/head/tail/find/curl/wget/less
is a read (anything else passes silently, uncounted); a read whose
path-like tokens are all allowlisted is exempt.

Budget per session sidecar $TMPDIR/fable-orch-reads-<sid>.json, reset
by the injector on clear/compact: a one-time warning AT the warn count,
a deny from the deny count on. Never emits permissionDecision "allow".

Configuration:
    FABLE_ORCH_READ_GUARD=off|0    disables the guard
    FABLE_ORCH_READ_BUDGET=W,D     warn at W, deny from D (default 6,12;
                                   malformed -> default)
    FABLE_ORCH_METRICS=0           disables the local metrics log
"""
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time

try:
    import fcntl
except ImportError:  # non-POSIX: run unlocked, best effort
    fcntl = None

DEFAULT_WARN = 6
DEFAULT_DENY = 12
READ_LIMIT_FREE = 60          # Read with limit <= this is a brief-sized read
SMALL_FILE_BYTES = 6 * 1024   # Read without limit of a file this small
GUARDED_TOOLS = ("Read", "Grep", "Glob", "WebFetch", "WebSearch", "Bash")
BASH_READ_RE = re.compile(
    r"^(cat|rg|grep|egrep|sed\s+-n|head|tail|find|curl|wget|less)\b")
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SHELL_OPS = ("|", "||", "&", "&&", ";", ";;", ">", ">>", "<", "<<", "(", ")")
PIPELINE_OPS = ("|", "||", "&", "&&", ";", ";;", "(", ")")


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


def plain_mode():
    """True when FABLE_ORCH_MODE=plain (case-insensitive) is set.

    Plain mode turns the orchestration layer off for a session — no chair
    profile, no ledger gates, no cold-cache guard — while the
    destructive-command guard stays fully active (its scripts never read
    this switch). Any other value, or unset, is the normal behaviour.
    Duplicated verbatim in every hook it affects: the hooks run as
    standalone scripts with no shared module to import from."""
    return (os.environ.get("FABLE_ORCH_MODE") or "").strip().lower() == "plain"


def _marker_dict(session_id):
    """This session's injector marker as a dict, or None if there is no
    marker file to read (manual install, or before the first SessionStart
    fire) — callers must fall back to legacy (session-agnostic) behavior
    in that case. An unreadable/corrupt-but-present marker still counts
    as present, so it decodes to {} rather than None."""
    if not session_id:
        return None
    safe = "".join(c for c in str(session_id) if c.isalnum() or c in "-_")
    cache = os.path.join(tempfile.gettempdir(), f"fable-orch-model-{safe}.json")
    if not os.path.isfile(cache):
        return None
    try:
        with open(cache, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# --- teammate detection (verbatim from the other guards) --------------

TEAMMATE_DETECT_BUDGET = 1.5  # seconds; the walk measures ~5ms in practice


def _budget(deadline, cap=5.0):
    """Seconds a subprocess may run without overshooting the deadline.
    Monotonic — a wall clock can step backwards and hand back a budget
    that never expires."""
    if deadline is None:
        return cap
    return max(0.2, min(cap, deadline - time.monotonic()))


def _is_teammate_session(max_hops=12):
    """True when this hook is running inside a named teammate.

    A teammate's context is its own and short-lived; the chair's cost
    story is not its business, and a blocked worker prompt would eat the
    report it was about to deliver. Same ancestor walk as the stop guard
    (`--agent-id` on the nearest claude ancestor), hard-budgeted, and on
    budget exhaustion answering False ("assume chair") — the guard then
    still runs and, at worst, warns a worker."""
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


# --- budget ------------------------------------------------------------

def read_budget():
    """(warn, deny) from FABLE_ORCH_READ_BUDGET=<warn>,<deny>; anything
    that is not two positive ints with warn <= deny falls back to the
    defaults."""
    raw = os.environ.get("FABLE_ORCH_READ_BUDGET")
    if raw is not None:
        try:
            warn, deny = (int(x) for x in raw.split(","))
            if 0 < warn <= deny:
                return warn, deny
        except ValueError:
            pass
    return DEFAULT_WARN, DEFAULT_DENY


def _reads_sidecar(session_id):
    if not session_id:
        return None
    safe = "".join(c for c in str(session_id) if c.isalnum() or c in "-_")
    return os.path.join(tempfile.gettempdir(), f"fable-orch-reads-{safe}.json")


def _bump_read_count(path, warn):
    """Read-increment-write the sidecar under an exclusive lock (same
    pattern as the spawn guard's task counter: parallel read hooks race
    on this file). Wrong-typed or corrupt content counts as 0. Returns
    (count, warn_now) or None when the file can't be used."""
    try:
        f = open(path, "a+", encoding="utf-8")
    except OSError:
        return None
    try:
        if fcntl is not None:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass
        f.seek(0)
        try:
            state = json.load(f)
        except Exception:
            state = {}
        if not isinstance(state, dict):
            state = {}
        try:
            count = int(state.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        count += 1
        warned_before = bool(state.get("warned"))
        warn_now = count == warn and not warned_before
        try:
            f.seek(0)
            f.truncate()
            json.dump({"count": count, "warned": warned_before or warn_now}, f)
            f.flush()
        except (OSError, ValueError):
            pass
        return count, warn_now
    finally:
        f.close()


# --- classification ----------------------------------------------------

def _norm(path):
    return path.replace(os.sep, "/") if os.sep != "/" else path


def allowlisted(path, cwd):
    """The exemption reason for `path`, or None."""
    if not isinstance(path, str) or not path.strip():
        return None
    p = os.path.expanduser(path.strip())
    if not os.path.isabs(p) and isinstance(cwd, str) and cwd:
        p = os.path.join(cwd, p)
    real = _norm(os.path.realpath(p))
    if ".workflow" in real.split("/"):
        return "workflow"
    home = _norm(os.path.realpath(os.path.expanduser("~")))
    for sub in ("plans", "handoffs"):
        if real.startswith(f"{home}/.claude/{sub}/"):
            return sub
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root and real.startswith(_norm(os.path.realpath(root)) + "/skills/"):
        return "skills"
    # normpath, never the raw string: `/x/skills/playbook/../../etc/f`
    # must not ride on the allowlisted segment it climbs out of.
    lexical = _norm(os.path.normpath(p))
    if "/skills/playbook/" in real or "/skills/playbook/" in lexical:
        return "skills"
    if os.path.basename(real).lower() in ("memory.md", "claude.md") or \
            os.path.basename(lexical).lower() in ("memory.md", "claude.md"):
        return "memory"
    return None


def _limit(tool_input):
    raw = tool_input.get("limit")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _bash_tokens(command):
    """The first simple command's tokens, after leading VAR=x
    assignments and `cd ... &&` prefixes are stripped; [] when there is
    none, or when that command writes (see _writes)."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        tokens = command.split()
    while tokens:
        if ASSIGN_RE.match(tokens[0]):
            tokens = tokens[1:]
        elif tokens[0] == "cd":
            if "&&" not in tokens:
                return []
            tokens = tokens[tokens.index("&&") + 1:]
        else:
            break
    simple = []
    prev = ""
    for tok in tokens:
        if tok in PIPELINE_OPS:
            break
        # A stdout redirect (`>`, `>>`, `&>`, `>|`; not `2>`) or a heredoc
        # / herestring makes `cat > f <<EOF` a WRITE, not a read.
        if tok and set(tok) <= set("<>&|"):
            if tok.startswith("<<") or (">" in tok and prev != "2"):
                return []
        elif tok not in SHELL_OPS:
            simple.append(tok)
        prev = tok
    if simple and _writes(simple):
        return []
    return simple


def _writes(tokens):
    """curl with a non-GET method, find with an action that changes or
    runs things: not reads."""
    if tokens[0] == "curl":
        for i, tok in enumerate(tokens):
            method = None
            if tok in ("-X", "--request") and i + 1 < len(tokens):
                method = tokens[i + 1]
            elif tok.startswith("-X") and len(tok) > 2:
                method = tok[2:]
            elif tok.startswith("--request="):
                method = tok.split("=", 1)[1]
            if method is not None and method.upper() not in ("GET", "HEAD"):
                return True
    if tokens[0] == "find":
        return any(t in ("-delete", "-exec", "-execdir", "-ok", "-okdir")
                   for t in tokens)
    return False


def _path_like(tok):
    if tok.startswith("-") or "://" in tok:
        return False
    return "/" in tok or "." in tok or tok.startswith("~")


def classify(data):
    """('ignore'|'exempt'|'count', reason). 'ignore' = not a read at all
    (non-read Bash, unguarded tool): no output, no metric, no count."""
    tool = data.get("tool_name") or ""
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    cwd = data.get("cwd")
    if tool in ("WebFetch", "WebSearch"):
        return "count", "web"
    if tool == "Read":
        path = tool_input.get("file_path")
        why = allowlisted(path, cwd)
        if why:
            return "exempt", why
        limit = _limit(tool_input)
        if limit is not None:
            return ("exempt", "limit") if 0 < limit <= READ_LIMIT_FREE \
                else ("count", "read")
        try:
            p = os.path.expanduser(str(path))
            if not os.path.isabs(p) and isinstance(cwd, str) and cwd:
                p = os.path.join(cwd, p)
            if os.stat(p).st_size <= SMALL_FILE_BYTES:
                return "exempt", "small"
        except (OSError, TypeError, ValueError):
            pass
        return "count", "read"
    if tool in ("Grep", "Glob"):
        why = allowlisted(tool_input.get("path"), cwd)
        return ("exempt", why) if why else ("count", tool.lower())
    if tool == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return "ignore", None
        tokens = _bash_tokens(command)
        if not tokens or not BASH_READ_RE.match(" ".join(tokens)):
            return "ignore", None
        paths = [t for t in tokens[1:] if _path_like(t)]
        if tokens[0] not in ("curl", "wget") and paths and \
                all(allowlisted(t, cwd) for t in paths):
            return "exempt", "bash-" + allowlisted(paths[0], cwd)
        return "count", "bash"
    return "ignore", None


# --- the guard -----------------------------------------------------------

def _guard(data):
    if (os.environ.get("FABLE_ORCH_READ_GUARD") or "").strip().lower() in ("off", "0"):
        return
    if data.get("agent_id"):
        return  # a subagent's own read: that IS the delegation working
    tool = data.get("tool_name") or ""
    if tool not in GUARDED_TOOLS:
        return
    session_id = data.get("session_id")
    marker = _marker_dict(session_id)
    if marker is None or marker.get("profile") != "fable":
        return
    decision, reason = classify(data)
    if decision == "ignore":
        return
    if _is_teammate_session():
        return
    if decision == "exempt":
        _metric("chair_read", session_id, decision="exempt", tool=tool,
                reason=reason)
        return

    path = _reads_sidecar(session_id)
    if path is None:
        return
    warn, deny = read_budget()
    bumped = _bump_read_count(path, warn)
    if bumped is None:
        return
    count, warn_now = bumped

    if count >= deny:
        _metric("chair_read", session_id, decision="deny", tool=tool,
                count=count, reason=reason)
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"CHAIR READ GUARD: {count} non-brief reads this task. "
                    "Spawn a sonnet scout (the project's `scout` agent, or "
                    "Explore/general-purpose with model: \"sonnet\") and have "
                    "it write the brief to .workflow/scratch/; reads with "
                    "limit ≤ 60 and .workflow/ files stay open."
                ),
            }
        }))
        return
    if warn_now:
        _metric("chair_read", session_id, decision="warn", tool=tool,
                count=count, reason=reason)
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": (
                    f"CHAIR READ BUDGET: {count}/{deny} non-brief reads this "
                    "task — spawn a sonnet scout for the rest."
                ),
            }
        }))
        return
    _metric("chair_read", session_id, decision="count", tool=tool,
            count=count, reason=reason)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return  # malformed input -> never block
    if not isinstance(data, dict):
        return
    if plain_mode():
        return  # plain mode: no chair profile, no read budget
    try:
        _guard(data)
    except Exception:
        return  # a guard fails open; it never crashes the hook pipeline


if __name__ == "__main__":
    main()
