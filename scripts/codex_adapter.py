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
                                   JSONL file (how to verify live); the
                                   value `1` means $TMPDIR/codex-adapter-debug.jsonl

VERIFIED LIVE 2026-09-05 against Codex CLI 0.153.4 (payloads captured in
`.workflow/scratch/codex-probe/probe.log`, replayed as
`tests/fixtures/*.json`). Two live facts drive the shape of this file:

  * The Codex payload IS the Claude payload — same key names, same
    `hook_event_name` values, plus `turn_id` / `tool_use_id`. The shell
    tool is called `Bash` (not `shell`/`exec`), the patch tool is
    `apply_patch`, and its body arrives in `tool_input["command"]` —
    NOT in a `patch`/`input`/`diff` key.
  * Codex ALSO writes files through the shell: when the first-party
    `apply_patch` tool errors, the model pipes the same envelope into
    the `apply_patch` arg0 shim from a `Bash` call (observed, see
    `shell_write_targets`). A write guard that only watches the
    `apply_patch` tool is trivially routed around, so the PreToolUse
    write guard and the PostToolUse binder both inspect `Bash`
    commands too.
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
    # subagent spawn  ->  Agent. LIVE (feature `multi_agent_v2`,
    # 2026-09-05): the tool name reaches the hook as the single token
    # `collaborationspawn_agent`, with `{task_name, agent_type,
    # fork_turns, message}` as its input — and `message` is a FERNET
    # CIPHERTEXT, not the prompt. The core's spawn gate only measures
    # its LENGTH (>1500 chars = "a real delegation"), and base64'd
    # ciphertext is ~1.4x the plaintext, so the gate still fires — one
    # notch early, never late. There is no way to read the prompt, so
    # the gate is length-only under Codex by construction.
    "collaborationspawn_agent": "Agent",
    "collaboration_spawn_agent": "Agent",
    "collaborationwait_agent": None,   # waiting is not spawning
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

# apply_patch is BOTH of Claude's write tools depending on the op in the
# envelope — see PATCH_OP_KIND: `Update File` is an Edit, `Add`/`Delete`/
# `Move to` replace the file wholesale and are a Write. The family name
# here is only the coarse first step; `map_tool_input` does the per-path
# refinement. At PostToolUse the distinction is moot (any successful
# write binds), and the honest name is Edit.
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
    # "Bash" is in both write-side tool sets on purpose: Codex's
    # apply_patch is ALSO reachable as a shell binary, and the model
    # falls back to it the moment the first-party tool errors (live
    # 2026-09-05). See `shell_write_targets`.
    "ledger_guard_write": {
        "script": "ledger_guard_write.py",
        "event": "PreToolUse", "tools": ("Write", "Bash"), "timeout": 10,
    },
    "ledger_bind": {
        "script": "ledger_bind.py",
        "event": "PostToolUse",
        "tools": ("Write", "Edit", "MultiEdit", "Bash"), "timeout": 10,
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
# `command` is the LIVE key for both tools: `Bash` carries the shell
# line there and `apply_patch` carries the whole patch envelope there
# (verified 2026-09-05). The rest of each list is defensive tolerance
# for other spellings; first non-empty match wins.
COMMAND_KEYS = ("command", "cmd", "argv", "script")
PATCH_TEXT_KEYS = ("patch", "patch_text", "diff", "input", "content", "text",
                   "command")
PATH_KEYS = ("file_path", "path", "filename", "file", "target_path")
PROMPT_KEYS = ("prompt", "message", "task", "instructions", "input",
               "developer_instructions", "description")
AGENT_KEYS = ("subagent_type", "agent_type", "agent", "agent_name", "name")

# The apply_patch envelope is parsed section by section (`parse_patch`),
# because the hunk BODY decides whether an Update is surgical. This is
# only the fallback for a tool that hands over a plain unified diff.
UNIFIED_DIFF_RE = re.compile(r"^\+\+\+\s+(?:b/)?(.+?)\s*$", re.M)

# How an operation compares to Claude Code's two write tools, which is
# what decides whether the core write guard may see it at all:
#
#   REPLACE  whole-file replace  ->  Claude `Write`  ->  guarded
#   EDIT     surgical change     ->  Claude `Edit`   ->  NOT guarded
#
# `*** Update File:` is apply_patch's surgical form and is therefore an
# Edit. Mapping it to Write (as this adapter first did) deadlocks the
# harness: the chair may not touch a ledger it did not create, so it
# never binds, so the Stop guard is inert forever — exactly the
# `stop_suppressed reason=unbound` seen live on 2026-09-05.
#
# The op label alone is NOT enough, though: live on 2026-09-05 a model
# that had read the ledger first wiped it with a single
# `*** Update File:` hunk whose every line was a deletion. So an Update
# counts as a REPLACE when nothing of the original survives it — see
# `_patch_kind`. That is one notch stricter than Claude Code (whose
# Edit tool could do the same unguarded) and is the deliberate
# divergence documented in the README.
REPLACE, EDIT = "replace", "edit"
PATCH_OP_KIND = {"add": REPLACE, "delete": REPLACE, "move": REPLACE,
                 "write": REPLACE, "update": EDIT}
# Section headers of an apply_patch envelope, in the order they appear.
PATCH_SECTIONS = (("*** Add File:", "add"), ("*** Update File:", "update"),
                  ("*** Delete File:", "delete"), ("*** Move to:", "move"))

# --- shell-route write detection --------------------------------------
#
# Codex exposes apply_patch TWICE: as a first-party tool and as an arg0
# shim binary the model can pipe a patch into from a `Bash` call. Live
# on 2026-09-05 the model did exactly that after the tool call errored,
# and overwrote a foreign ledger the PreToolUse write guard never saw.
# These three patterns re-attach that route to the guard.
#
# A path token ending in a live-ledger basename, anywhere in the line —
# NOT only in the patch envelope, because the observed command put the
# path in a shell variable (`target=...` / `*** Update File: $target`).
SHELL_LEDGER_PATH_RE = re.compile(
    r"""[^\s'"`|;&<>()]*ledger[^\s'"`|;&<>()/]*\.md""", re.I)
# The command is a patch application: an envelope, or the shim by name.
PATCH_ENVELOPE_RE = re.compile(
    r"\*\*\*\s+Begin\s+Patch|(?<![\w.-])apply_patch(?![\w-])")
# What sits immediately in front of a path token, and what it does to it.
# `> p` / `tee p` truncate (REPLACE); `>> p` / `tee -a p` append (EDIT).
TRUNCATING_BEFORE_RE = re.compile(
    r"(?:(?<!>)>|\btee\b(?:\s+-{1,2}(?!a)\w+)*)\s*['\"]?$")
APPENDING_BEFORE_RE = re.compile(
    r"(?:>>|\btee\b(?:\s+-{1,2}\w+)*\s+-a\b|\btee\b\s+-a)\s*['\"]?$")
# In-place edit: sed -i / sed -i.bak is surgical, i.e. an Edit.
SED_INPLACE_RE = re.compile(r"\bsed\b[^|;&]*\s-i(?:\.\w+)?(?:\s|$)")

# Appended to the write guard's deny reason under Codex: the core text
# tells the chair to "use Edit rather than Write", and Codex has no Edit
# tool to switch to. Additive on purpose — a substring rewrite of core
# prose would silently rot on the next `make pull-core`.
CODEX_WRITE_NOTE = (
    " [codex-orchestrator] In Codex this was an apply_patch (tool or "
    "shell shim): there is no separate Edit tool, so \"use Edit\" means "
    "keep the patch to the hunks this session owns, or write a fresh "
    "./.workflow/LEDGER-<topic>.md. Routing the same write through the "
    "shell is guarded too — do not try it. The guard only ever fires on "
    "live .workflow/LEDGER*.md files."
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


def _absolutise(pairs, cwd):
    """[(path, kind)] -> same, absolutised, order kept, first kind wins."""
    out, seen = [], {}
    for p, kind in pairs:
        p = str(p).strip().strip('"')
        if not p:
            continue
        if not os.path.isabs(p) and isinstance(cwd, str) and cwd:
            p = os.path.join(cwd, p)
        if p in seen:
            continue
        seen[p] = kind
        out.append((p, kind))
    return out


def parse_patch(text):
    """An apply_patch envelope -> [(op, path, body_lines)], in order."""
    out, cur = [], None
    for line in str(text or "").splitlines():
        stripped = line.strip()
        for prefix, op in PATCH_SECTIONS:
            if stripped.startswith(prefix):
                cur = (op, stripped[len(prefix):].strip(), [])
                out.append(cur)
                break
        else:
            if stripped.startswith("*** Begin Patch") or \
                    stripped.startswith("*** End Patch"):
                cur = None
            elif cur is not None:
                cur[2].append(line)
    return out


def _patch_kind(op, path, body):
    """REPLACE or EDIT for one parsed patch section.

    An `Update File` is surgical — UNLESS its hunks delete every line
    the target has on disk, which is a whole-file replace wearing a
    patch's clothes (observed live). Anything unreadable answers EDIT:
    the guard's own existence check will sort it out, and a false
    REPLACE would deny a legitimate patch."""
    kind = PATCH_OP_KIND.get(op, REPLACE)
    if kind is not EDIT:
        return kind
    deleted = sum(1 for line in body if line.startswith("-"))
    if not deleted:
        return EDIT
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            total = sum(1 for _ in f)
    except Exception:
        return EDIT
    return REPLACE if total and deleted >= total else EDIT


def patch_targets(tool_input, cwd):
    """Every file an apply_patch payload touches, as (path, kind).

    `kind` is REPLACE or EDIT — see PATCH_OP_KIND and `_patch_kind`. An
    explicit path key on the tool_input (a `write_file`-shaped tool,
    i.e. a real whole-file write) wins over parsing and is a REPLACE."""
    direct = _first_str(tool_input, PATH_KEYS)
    text = _first_str(tool_input, PATCH_TEXT_KEYS) or ""
    sections = []
    if direct:
        sections.append(("write", direct, []))
    sections.extend(parse_patch(text))
    if not sections:
        sections = [("update", p, []) for p in UNIFIED_DIFF_RE.findall(text)
                    if p != "/dev/null"]

    found = []
    for op, path, body in sections:
        path = str(path).strip().strip('"')
        if not path:
            continue
        if not os.path.isabs(path) and isinstance(cwd, str) and cwd:
            path = os.path.join(cwd, path)
        found.append((path, _patch_kind(op, path, body)))
    return _absolutise(found, cwd)


def patch_paths(tool_input, cwd):
    """Just the paths of `patch_targets`, order preserved."""
    return [p for p, _ in patch_targets(tool_input, cwd)]


def shell_write_targets(command, cwd):
    """Live ledgers a shell command writes, as (path, kind).

    Deliberately narrow: a false positive DENIES a shell call, so only
    shapes that unambiguously write are recognised —

        REPLACE  `> p`, `tee p`, or ANY piped apply_patch (envelope or
                 the arg0 shim by name; every live-ledger path in the
                 command counts, because the observed real command
                 passed the path through a shell variable and its
                 declared `Update File:` op still replaced the file)
        EDIT     `>> p`, `tee -a p`, `sed -i ... p`

    Everything else — `grep`/`sed -n`/`cat` on a ledger, `cp`/`mv` with
    a ledger as SOURCE, a python one-liner that opens it for writing —
    is not recognised as a write at all. The guard is a discipline
    reminder, not a sandbox; the remaining routes are documented in the
    README rather than guessed at.
    """
    text = str(command or "")
    if not text:
        return []
    hits = [(m.start(), m.group(0)) for m in SHELL_LEDGER_PATH_RE.finditer(text)
            if _is_live_ledger_name(os.path.basename(m.group(0)))]
    if not hits:
        return []
    patchy = bool(PATCH_ENVELOPE_RE.search(text))
    sedded = bool(SED_INPLACE_RE.search(text))
    found = []
    for start, p in hits:
        before = text[max(0, start - 32):start]
        if APPENDING_BEFORE_RE.search(before):
            found.append((p, EDIT))
        elif TRUNCATING_BEFORE_RE.search(before):
            found.append((p, REPLACE))
        elif patchy:
            found.append((p, REPLACE))
        elif sedded:
            found.append((p, EDIT))
    return _absolutise(found, cwd)


def map_tool_input(claude_tool, tool_input, cwd, guard=None):
    """Codex tool_input -> the Claude tool_input shape the guards read.

    Returns a LIST: one apply_patch call can touch several files, and
    the path-based guards (`ledger_guard_write`, `ledger_bind`) read a
    single `tool_input.file_path`. Every LEDGER-matching path gets its
    own guard run; when none match, one run with the first path keeps
    the guard's own filtering authoritative."""
    if claude_tool == "Bash":
        cmd = _first_str(tool_input, COMMAND_KEYS) or ""
        if guard in ("ledger_guard_write", "ledger_bind"):
            # The shell route into apply_patch: hand the path-based
            # guards one payload per ledger the command writes, and
            # nothing at all when it writes none.
            return [{"file_path": p}
                    for p, kind in shell_write_targets(cmd, cwd)
                    if guard == "ledger_bind" or kind == REPLACE]
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
        targets = patch_targets(tool_input, cwd)
        if guard == "ledger_guard_write":
            # Only whole-file replaces reach the write guard — the same
            # scope as core's `^Write$` matcher. A surgical
            # `*** Update File:` hunk is Codex's Edit and must pass, or
            # no session can ever continue (let alone bind to) a ledger
            # it did not create in this very session.
            targets = [t for t in targets if t[1] == REPLACE]
        ledgers = [p for p, _ in targets
                   if _is_live_ledger_name(os.path.basename(p))]
        chosen = ledgers or [p for p, _ in targets[:1]]
        return [{"file_path": p} for p in chosen]
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

    # A Bash call reaching a path-based guard is the shell route into
    # apply_patch: it is emitted as the write tool it really is, while
    # map_tool_input still needs to see "Bash" to parse the command.
    if claude_tool == "Bash" and guard in ("ledger_guard_write", "ledger_bind"):
        emit_tool = "Write" if spec["event"] == "PreToolUse" else "Edit"
    else:
        emit_tool = EVENT_TOOL_ALIAS.get((spec["event"], claude_tool), claude_tool)

    payloads = []
    for mapped in map_tool_input(claude_tool, tool_input, base.get("cwd"), guard):
        item = dict(base)
        item["tool_name"] = emit_tool
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
        # Every write-guard deny under Codex gets the note: the core's
        # "use Edit rather than Write" has no referent in a harness whose
        # only write tools are apply_patch and the shell.
        if (guard == "ledger_guard_write"
                and hso.get("permissionDecision") == "deny"):
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
    if path.lower() in ("1", "true", "yes", "on"):
        # Someone will inevitably set this like a boolean; do not drop a
        # file called `1` into their repo root (observed 2026-09-05).
        import tempfile
        path = os.path.join(tempfile.gettempdir(), "codex-adapter-debug.jsonl")
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
