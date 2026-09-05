#!/usr/bin/env python3
"""Codex CLI hook adapter for the fable5-opus5-orchestrator core guards.

    codex_adapter.py <guard-name>      (stdin: Codex hook JSON)

One process per hook fire. It does three things and nothing else:

  1. NORMALISE  Codex's hook payload into the Claude Code payload the
     vendored `core/scripts/<guard>.py` was written against — the same
     field names (`session_id`, `cwd`, `transcript_path`, `prompt`,
     `source`, `model`, `stop_hook_active`, `tool_name`, `tool_input`,
     `hook_event_name`), with Codex's tool names and tool inputs mapped
     onto Claude's (`TOOL_NAME_MAP` below).
  2. RUN the core guard as a subprocess, unmodified. `core/` is a git
     subtree of the Claude-side repo and is NEVER edited here; every
     harness difference is absorbed in this file.
  3. TRANSLATE the guard's stdout/exit code back into Codex's hook
     output schema (PreToolUse `permissionDecision: deny`,
     UserPromptSubmit / Stop `decision: block`, SessionStart
     `additionalContext`).

Session marker paths (`$TMPDIR/fable-orch-*-<sid>.json`) and the metrics
log (`~/.claude/fable-orch/metrics.jsonl`) are deliberately IDENTICAL to
the Claude harness, so `core/scripts/stats.py`, the cold-cache stamps and
the retro tooling read both harnesses out of one place. Every metrics
line this adapter causes is stamped `"harness": "codex"` (see
`_stamp_metrics`) so the two can still be told apart.

FAIL OPEN, ALWAYS. Unparsable stdin, an unknown guard name, a crashing
or hanging core script, an unwritable metrics log — every one of them
exits 0 with no output. A hook that can wedge a session is worse than
the discipline it enforces.

Configuration (this file; the core's own knobs still apply):
    CODEX_ADAPTER_HARNESS=<name>   metrics stamp value (default "codex")
    CODEX_ADAPTER_TOOL_GATE=0      don't re-check tool names in-process
                                   (trust the hooks.json matcher alone)
    CODEX_ADAPTER_EXIT2=1          signal deny/block via exit code 2 +
                                   stderr instead of stdout JSON
    CODEX_ADAPTER_DEBUG=<path>     append raw + normalised payloads to a
                                   JSONL file (how to verify live)

UNVERIFIED LIVE: as of 2026-09-05 no Codex hook has ever fired on this
machine (the CLI gates hooks behind a persisted trust decision that
needs an interactive accept — see the P1 protocol probe). Everything
below is built against the documented payloads in
`.workflow/scratch/codex-probe/protocol.md`; the places where the docs
are silent are marked `# GUESS:` and are all tolerant (candidate key
lists, not single hard-coded names).
"""
import json
import os
import re
import subprocess
import sys

# --- data: Codex tool name -> Claude Code tool name -------------------
#
# Keys are matched lowercased, after stripping a `collab:`/`codex:` UI
# prefix and normalising `.`/`-`/` ` to `_`. Anything not listed here
# passes through unmapped, which means the tool gate below drops it —
# the guards only ever cared about three tool families.
TOOL_NAME_MAP = {
    # shell / command execution  ->  Bash
    "shell": "Bash",
    "exec": "Bash",
    "local_shell": "Bash",
    "container_exec": "Bash",
    "run_command": "Bash",
    "bash": "Bash",
    # file writes  ->  Write (PreToolUse) / Edit (PostToolUse, see
    # EVENT_TOOL_ALIAS). Codex's only first-party write tool is
    # apply_patch; the rest are defensive spellings.
    "apply_patch": "Write",
    "applypatch": "Write",
    "write_file": "Write",
    "edit_file": "Write",
    "create_file": "Write",
    "write": "Write",
    "edit": "Edit",
    "multiedit": "MultiEdit",
    # subagent spawn (Codex `Collab` feature)  ->  Agent
    "spawnagent": "Agent",
    "spawn_agent": "Agent",
    "collab_spawnagent": "Agent",
    "collab_spawn_agent": "Agent",
    "collab__spawnagent": "Agent",
    "collab__spawn_agent": "Agent",
    "create_agent": "Agent",
    "agent": "Agent",
    "task": "Agent",
    # Claude-only spellings, kept so a shared payload round-trips
    "taskcreate": "TaskCreate",
    "workflow": "Workflow",
}

# apply_patch is surgical like Claude's Edit, but it can also replace a
# file wholesale, so at PreToolUse it is treated as a Write (the write
# guard's protection is worth the stricter reading). At PostToolUse only
# the binding matters and the honest name is Edit.
EVENT_TOOL_ALIAS = {
    ("PostToolUse", "Write"): "Edit",
}

# --- data: guard registry ---------------------------------------------
#
# `tools` is the post-mapping tool-name gate — the same set as the
# matcher in the Claude core's hooks/hooks.json, re-checked in-process
# because Codex's matcher syntax (regex? glob?) is not verified live.
GUARDS = {
    "inject_instructions": {
        "script": "inject_instructions.py",
        "event": "SessionStart", "tools": None, "timeout": 10,
    },
    "cold_cache_guard": {
        "script": "cold_cache_guard.py",
        "event": "UserPromptSubmit", "tools": None, "timeout": 10,
    },
    "ledger_guard_spawn": {
        "script": "ledger_guard_spawn.py",
        "event": "PreToolUse",
        "tools": ("Agent", "Task", "Workflow", "TaskCreate"), "timeout": 10,
    },
    "ledger_guard_write": {
        "script": "ledger_guard_write.py",
        "event": "PreToolUse", "tools": ("Write",), "timeout": 10,
    },
    "ledger_bind": {
        "script": "ledger_bind.py",
        "event": "PostToolUse",
        "tools": ("Write", "Edit", "MultiEdit"), "timeout": 10,
    },
    "ledger_guard_stop": {
        "script": "ledger_guard_stop.py",
        "event": "Stop", "tools": None, "timeout": 10,
    },
    "cleanup_session_cache": {
        "script": "cleanup_session_cache.py",
        "event": "SessionEnd", "tools": None, "timeout": 20,
    },
}

# --- data: Codex model id -> core chair profile -----------------------
#
# `inject_instructions.py` decides FABLE vs OPUS profile from a Claude
# model string (`_is_opus`), which no Codex model id will ever match.
# Rather than fake a Claude name into the payload (it would land in the
# session marker and in every metrics line), the adapter sets the core's
# own documented override, FABLE_ORCH_PROFILE — an explicit pin the user
# can still beat by exporting it themselves.
#
# Tier mapping comes from profiles/openai.toml: Astra = chair/fable,
# Sol = opus, Luna = worker tiers (never a chair; unmapped so the core
# falls through to its own default).
MODEL_PROFILE_MAP = {
    "gpt-6-astra": "fable",
    "gpt-5.6-sol": "opus",
    "gpt-5.6-terra": "opus",
}

# --- data: where the pieces of a Codex tool_input live ----------------
# GUESS: the probe never fired, so each of these is a candidate list
# rather than one documented key. First non-empty match wins.
COMMAND_KEYS = ("command", "cmd", "argv", "script")
PATCH_TEXT_KEYS = ("patch", "input", "content", "text", "diff", "patch_text")
PATH_KEYS = ("file_path", "path", "filename", "file", "target_path")
PROMPT_KEYS = ("prompt", "task", "instructions", "input", "message",
               "developer_instructions", "description")
AGENT_KEYS = ("subagent_type", "agent", "agent_name", "agent_type", "name")

# `*** Add File: x` / `*** Update File: x` / `*** Delete File: x` /
# `*** Move to: x` — the apply_patch envelope, plus a unified-diff
# fallback for a tool that hands over a plain patch.
PATCH_FILE_RE = re.compile(
    r"^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s*(.+?)\s*$", re.M)
PATCH_MOVE_RE = re.compile(r"^\*\*\*\s+Move\s+to:\s*(.+?)\s*$", re.M)
UNIFIED_DIFF_RE = re.compile(r"^\+\+\+\s+(?:b/)?(.+?)\s*$", re.M)

# Appended to the write guard's deny reason under Codex: the core text
# tells the chair to "use Edit rather than Write", and Codex has no Edit
# tool to switch to. Additive on purpose — a substring rewrite of core
# prose would silently rot on the next `make pull-core`.
CODEX_WRITE_NOTE = (
    " [codex-orchestrator] In Codex this was an apply_patch: there is no "
    "separate Edit tool, so \"use Edit\" means keep the patch to the hunks "
    "this session owns, or write a fresh ./.workflow/LEDGER-<topic>.md. "
    "The guard only ever fires on live .workflow/LEDGER*.md files."
)

HARNESS = (os.environ.get("CODEX_ADAPTER_HARNESS") or "codex").strip() or "codex"

ADAPTER_DIR = os.path.dirname(os.path.abspath(__file__))
# CLAUDE_PLUGIN_ROOT-style resolution: core/ is found relative to THIS
# file, never from an env var or the cwd, so the adapter works the same
# whether the plugin lives in ~/.codex/plugins, a marketplace checkout,
# or a dev clone.
CORE_ROOT = os.path.join(os.path.dirname(ADAPTER_DIR), "core")
CORE_SCRIPTS = os.path.join(CORE_ROOT, "scripts")


# --- helpers ----------------------------------------------------------

def canonical_tool(name):
    """Codex tool name -> Claude tool name, or None when unmapped."""
    key = str(name or "").strip().lower()
    if not key:
        return None
    for prefix in ("collab:", "codex:", "mcp:"):
        if key.startswith(prefix):
            key = key[len(prefix):].strip()
    key = key.replace(".", "_").replace("-", "_").replace(" ", "_")
    return TOOL_NAME_MAP.get(key)


def _first_str(d, keys):
    """First key in `keys` holding a non-empty string (or a list of
    strings, joined — Codex may hand a shell argv rather than a line)."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, (list, tuple)) and v:
            joined = " ".join(str(x) for x in v if isinstance(x, (str, int)))
            if joined.strip():
                return joined
    return None


def _as_dict(value):
    """tool_input as a dict — Codex may deliver it as a JSON string."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _is_live_ledger_name(name):
    """Same live-name filter as the core guards (LEDGER*.md, "ledger" a
    whole leading segment, *-archive.md excluded). Duplicated rather
    than imported: core/ is a subtree and this adapter must keep working
    if its internals move."""
    low = str(name or "").lower()
    if not (low.startswith("ledger") and low.endswith(".md")):
        return False
    if low[6:7] not in (".", "-", "_"):
        return False
    return not (low.endswith("-archive.md") or low.endswith("_archive.md"))


def patch_paths(tool_input, cwd):
    """Every file path an apply_patch payload touches, absolutised.

    Order is preserved and duplicates dropped. An explicit path key on
    the tool_input (a `write_file`-shaped tool) wins over parsing."""
    direct = _first_str(tool_input, PATH_KEYS)
    text = _first_str(tool_input, PATCH_TEXT_KEYS) or ""
    found = []
    if direct:
        found.append(direct)
    found.extend(PATCH_FILE_RE.findall(text))
    found.extend(PATCH_MOVE_RE.findall(text))
    if not found:
        found.extend(p for p in UNIFIED_DIFF_RE.findall(text)
                     if p != "/dev/null")
    out = []
    for p in found:
        p = p.strip().strip('"')
        if not p:
            continue
        if not os.path.isabs(p) and isinstance(cwd, str) and cwd:
            p = os.path.join(cwd, p)
        if p not in out:
            out.append(p)
    return out


def map_tool_input(claude_tool, tool_input, cwd):
    """Codex tool_input -> the Claude tool_input shape the guards read.

    Returns a LIST: one apply_patch call can touch several files, and
    the path-based guards (`ledger_guard_write`, `ledger_bind`) read a
    single `tool_input.file_path`. Every LEDGER-matching path gets its
    own guard run; when none match, one run with the first path keeps
    the guard's own filtering authoritative."""
    if claude_tool == "Bash":
        cmd = _first_str(tool_input, COMMAND_KEYS) or ""
        return [{"command": cmd}]
    if claude_tool in ("Agent", "Task"):
        out = {"prompt": _first_str(tool_input, PROMPT_KEYS) or ""}
        agent = _first_str(tool_input, AGENT_KEYS)
        if agent:
            out["subagent_type"] = agent
        return [out]
    if claude_tool == "Workflow":
        return [{"script": _first_str(tool_input, ("script",) + PROMPT_KEYS) or ""}]
    if claude_tool == "TaskCreate":
        return [dict(tool_input)]
    if claude_tool in ("Write", "Edit", "MultiEdit"):
        paths = patch_paths(tool_input, cwd)
        ledgers = [p for p in paths if _is_live_ledger_name(os.path.basename(p))]
        targets = ledgers or paths[:1]
        return [{"file_path": p} for p in targets]
    return [dict(tool_input)]


def normalise(payload, guard):
    """Codex hook payload -> (codex_event, [claude payloads]).

    An empty payload list means "this fire is not this guard's business"
    (tool gate) and the adapter exits silently."""
    spec = GUARDS[guard]
    codex_event = str(payload.get("hook_event_name") or spec["event"])

    base = {"hook_event_name": spec["event"]}
    for key in ("session_id", "cwd", "transcript_path", "prompt", "source",
                "model", "stop_hook_active"):
        if key in payload:
            base[key] = payload[key]
    # The shell tool carries its own working directory; the guards' ledger
    # search starts from `cwd`, so prefer the payload's but accept the
    # tool's when the payload omits it.
    tool_input = _as_dict(payload.get("tool_input"))
    if not base.get("cwd"):
        wd = _first_str(tool_input, ("workdir", "cwd", "working_directory"))
        if wd:
            base["cwd"] = wd

    if spec["event"] not in ("PreToolUse", "PostToolUse"):
        return codex_event, [base]

    claude_tool = canonical_tool(payload.get("tool_name"))
    gate_on = (os.environ.get("CODEX_ADAPTER_TOOL_GATE") or "").strip() != "0"
    if spec["tools"] and gate_on and claude_tool not in spec["tools"]:
        return codex_event, []
    if claude_tool is None:
        return codex_event, []
    claude_tool = EVENT_TOOL_ALIAS.get((spec["event"], claude_tool), claude_tool)

    payloads = []
    for mapped in map_tool_input(claude_tool, tool_input, base.get("cwd")):
        item = dict(base)
        item["tool_name"] = claude_tool
        item["tool_input"] = mapped
        payloads.append(item)
    return codex_event, payloads


# --- metrics stamping -------------------------------------------------

def _metrics_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "fable-orch",
                        "metrics.jsonl")


def _metrics_offset():
    """Size of the metrics log before the guard runs (0 when absent)."""
    if (os.environ.get("FABLE_ORCH_METRICS") or "").strip() == "0":
        return None
    try:
        return os.path.getsize(_metrics_path())
    except OSError:
        return 0


def _stamp_metrics(offset):
    """Add `"harness": "<HARNESS>"` to the lines the guard just appended.

    The core writes its metrics itself and honours no harness env var
    (checked against every `os.environ.get` in core/scripts), and core/
    is a subtree that must not be edited here — so the adapter rewrites
    only the byte range the child appended, leaving everything before
    `offset` untouched. Best effort in every failure mode; a lost stamp
    is a cosmetic loss, a broken metrics log is not."""
    if offset is None:
        return
    path = _metrics_path()
    try:
        if os.path.getsize(path) <= offset:
            return
        with open(path, "r+", encoding="utf-8") as f:
            try:  # POSIX only; the race window is one hook fire wide
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except Exception:
                pass
            f.seek(offset)
            tail = f.read()
            lines = []
            for line in tail.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except ValueError:
                    lines.append(line)
                    continue
                if isinstance(rec, dict) and "harness" not in rec:
                    rec["harness"] = HARNESS
                    lines.append(json.dumps(rec))
                else:
                    lines.append(line)
            if not lines:
                return
            f.seek(offset)
            f.truncate()
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


# --- running the core guard -------------------------------------------

def child_env(payload):
    env = dict(os.environ)
    # The core reads its instructions/ from CLAUDE_PLUGIN_ROOT; point it
    # at the vendored core, never at whatever a surrounding Claude
    # session may have exported.
    env["CLAUDE_PLUGIN_ROOT"] = CORE_ROOT
    if not (os.environ.get("FABLE_ORCH_PROFILE") or "").strip():
        profile = MODEL_PROFILE_MAP.get(
            str(payload.get("model") or "").strip().lower())
        if profile:
            env["FABLE_ORCH_PROFILE"] = profile
    return env


def run_guard(guard, claude_payload):
    """Run core/scripts/<guard>.py; return its parsed stdout or None."""
    script = os.path.join(CORE_SCRIPTS, GUARDS[guard]["script"])
    if not os.path.isfile(script):
        return None
    offset = _metrics_offset()
    try:
        proc = subprocess.run(
            [sys.executable, script],
            input=json.dumps(claude_payload),
            capture_output=True, text=True,
            env=child_env(claude_payload),
            timeout=GUARDS[guard]["timeout"],
        )
    except Exception:
        return None  # timeout, missing interpreter, ... -> fail open
    finally:
        _stamp_metrics(offset)
    if proc.returncode != 0:
        return None  # a core guard always exits 0; anything else = fail open
    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        parsed = json.loads(out)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


# --- output translation -----------------------------------------------

def to_codex(result, codex_event, guard, codex_tool=None):
    """Claude hook output -> Codex hook output, or None for "no output"."""
    if not isinstance(result, dict):
        return None
    out = {}

    hso = result.get("hookSpecificOutput")
    if isinstance(hso, dict):
        hso = dict(hso)
        # Codex keys hookSpecificOutput off ITS event name; the core
        # stamps the Claude one. They coincide today, but the adapter
        # owns the value either way.
        hso["hookEventName"] = codex_event
        if (guard == "ledger_guard_write"
                and hso.get("permissionDecision") == "deny"
                and canonical_tool(codex_tool) == "Write"
                and str(codex_tool or "").lower().startswith("apply")):
            reason = hso.get("permissionDecisionReason")
            if isinstance(reason, str):
                hso["permissionDecisionReason"] = reason + CODEX_WRITE_NOTE
        out["hookSpecificOutput"] = hso

    if result.get("decision") == "block":
        out["decision"] = "block"
        reason = result.get("reason")
        if isinstance(reason, str):
            out["reason"] = reason

    # `systemMessage` is a Claude Code extra with no documented Codex
    # counterpart; its content is already carried by additionalContext
    # on every path that emits it (cold_cache_guard's warn band).
    return out or None


def decision_reason(out):
    """The user-facing reason of a deny/block, or None."""
    if not isinstance(out, dict):
        return None
    hso = out.get("hookSpecificOutput")
    if isinstance(hso, dict) and hso.get("permissionDecision") == "deny":
        return hso.get("permissionDecisionReason") or "denied"
    if out.get("decision") == "block":
        return out.get("reason") or "blocked"
    return None


def _debug(record):
    path = (os.environ.get("CODEX_ADAPTER_DEBUG") or "").strip()
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass


# --- entry point ------------------------------------------------------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write("codex_adapter.py: usage: codex_adapter.py <guard>\n")
        return 0
    guard = argv[0]
    if guard.endswith(".py"):
        guard = guard[:-3]
    if guard not in GUARDS:
        sys.stderr.write(f"codex_adapter.py: unknown guard {guard!r}\n")
        return 0

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    try:
        codex_event, claude_payloads = normalise(payload, guard)
    except Exception:
        return 0
    _debug({"guard": guard, "raw": payload, "normalised": claude_payloads})
    if not claude_payloads:
        return 0

    final = None
    for claude_payload in claude_payloads:
        try:
            result = run_guard(guard, claude_payload)
        except Exception:
            result = None
        out = to_codex(result, codex_event, guard, payload.get("tool_name"))
        if out is None:
            continue
        final = out
        if decision_reason(out):
            break  # first deny/block wins; no point running the rest
    if final is None:
        return 0

    reason = decision_reason(final)
    if reason and (os.environ.get("CODEX_ADAPTER_EXIT2") or "").strip() == "1":
        # Documented alternative signalling path: exit 2 + reason on
        # stderr. Off by default — the JSON schema carries strictly more
        # information (permissionDecision vs decision).
        sys.stderr.write(reason + "\n")
        return 2
    sys.stdout.write(json.dumps(final))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # fail open, always
