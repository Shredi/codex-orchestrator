#!/usr/bin/env python3
"""PreToolUse guard (Bash): refuse the command shapes that delete a
machine, BEFORE the shell ever expands them.

The 2026-09-09 incident that motivates this file: a verifier agent
"tested" a rescue one-liner in a throwaway shell —

    bash -c 'rm -rf -- "$1"/*' x ""

— with an empty second argument. The shell expanded it to `rm -rf /*`
and emptied every admin-writable directory it could reach. Approval
(auto mode) only ever saw the UNEXPANDED text, and the text looks
harmless: a quoted variable, a `--` terminator, a `/*` suffix. Nothing
in the pipeline reads it as "this may be the root directory".

This guard is Layer A: static, text-only, in front of the shell. It
cannot know what `$1` will be, so it refuses to find out. Layer B (the
`rm` shim installed by destructive_guard_install.py, first on PATH)
sees the EXPANDED argv and is the only layer that can stop the same
line when it arrives through a script, `xargs`, or `find -exec`.

DENY (permissionDecision "deny", reason prefixed `DESTRUCTIVE GUARD: `)
when a RECURSIVE `rm` (`-r`, `-R`, `--recursive`, any letter cluster
containing r/R) has an operand that is:
    - a variable (`$x`, `${x}`, `"$1"/*`, a backtick substitution)
    - empty (`""` / `''`) — the incident's payload
    - `/`, `/*`, a protected top-level path (/Applications /Library
      /System /Users /Volumes /opt /usr /private /etc /var /bin /sbin
      /home /mnt /srv /dev /boot /root), a single-segment home under
      /Users, /home, /Volumes, /mnt, or `~` / `$HOME` itself
    - a glob whose parent directory is one of the above
...or when a recursive `rm` arrives through a bypass or a nested
context, whatever its operands:
    - `sudo`, `command -p`, `env -i …`, a literal `/bin/rm` path
      (all three sidestep the PATH shim of Layer B)
    - inside `bash -c` / `sh -c` / `zsh -c` / `dash -c` / `ksh -c`
    - inside `eval`, `xargs`
    - inside a function body (`f(){ rm -rf $1; }`) with an operand this
      guard cannot see through — the body is analysed like a `-c` string
    - behind an applet multiplexer (`busybox rm`, `toybox rm`) or a
      variable command word (`$RM -rf …`)
    - on `*`, `.` or `..` after a `cd` earlier in the same line, where
      the payload's `cwd` no longer says where the rm lands
    - inside a REMOTE command string (`ssh …`, `docker exec …`,
      `kubectl exec …`) — Layer B lives on THIS machine only, so a
      remote recursive rm has no runtime net underneath it at all
Also denied, same prefix: `git clean` with a variable/empty operand or
an `-e`/`--exclude` pattern, `find … -delete` / `find … -exec rm -r`
whose start path is a variable, a protected directory or outside the
allowed roots,
`dd of=/dev/…` (or a variable `of=`), any `mkfs*`, `diskutil erase*` /
`zeroDisk` / `secureErase` / `partitionDisk`, `chmod -R` / `chown -R`
on a protected path or a variable, `truncate -s 0` on a variable,
`shred`, a `>`/`:>` truncation of a variable target, and any command
mentioning `SAFE_RM_DRYRUN` or `SAFE_RM_BYPASS` (the shim's dry-run
switch is for the test suite; there is no in-band bypass).

Self-protection: any WRITE to `~/.claude/guard` (`rm`, `mv`/`cp` over
it, `chmod`, `ln -sf`, `: >`, `sed -i`, literal or through `~`/`$HOME`)
is denied — that directory is Layer B, and removing it removes the only
check that sees expanded arguments. The shim refuses the same operand
whatever its flags.

Compound statements: a segment's leading shell keywords and grouping
tokens are stripped before the command word is read, so the rm inside
`for d in …; do rm -rf "/$d"; done`, `if …; then rm -rf /; fi`,
`( rm -rf / )`, `{ rm -rf /; }` and `case $x in a) rm -rf $x;; esac` is
the command this guard judges — not a command called `do`, `then` or
`(`.

ASK (permissionDecision "ask") for the cases that are dangerous but
legitimate: a recursive `rm` on a LITERAL path that resolves outside
the allowed roots (the payload's `cwd`, $TMPDIR, /tmp, /private/tmp,
/var/folders, ~/.claude, ~/.workflow, ~/Documents/git), and a
`find … -delete` / `find … -exec rm -r` whose every start path is a
literal INSIDE those roots. Inside them — `rm -rf .workflow/scratch/x`
— a plain rm stays silent.

ALLOW (no output) everything else. Non-recursive `rm`, `trash`, `git
status`, a recursive rm under cwd: all pass untouched.

PATH injection: when a command actually CALLS `rm` — an rm command word
in some parsed segment, a `-c` body, an xargs/eval tail or a
`find -exec`, never merely the letters inside `git commit -m 'rm -rf
cleanup'` — and is NOT denied, the guard returns
`hookSpecificOutput.updatedInput.command` with
`export PATH="$HOME/.claude/guard/bin:$PATH"; ` prepended, so Layer B
is in front of `/bin/rm` for that call even if the session's env file
was not honoured. Only rm-bearing commands are rewritten, so
`Bash(git *)`-style permission rules keep matching everything else.

Always exits 0; any exception fails OPEN (a guard that crashes must
not be a guard that blocks work). Stdlib only, Python 3.9+, no
shell=True, no symlinks, no fcntl — identical on macOS/Linux/Windows.

Configuration:
    DESTRUCTIVE_GUARD=0       disables this guard entirely
    FABLE_ORCH_METRICS=0      disables the local metrics log
"""
import json
import os
import posixpath
import re
import shlex
import sys
import tempfile
import time

PREFIX = 'export PATH="$HOME/.claude/guard/bin:$PATH"; '

# Deleting any of these recursively is never a task step; it is an
# accident or a runaway expansion.
PROTECTED_TOP = frozenset([
    "/Applications", "/Library", "/System", "/Users", "/Volumes",
    "/opt", "/usr", "/private", "/etc", "/var", "/bin", "/sbin",
    "/home", "/mnt", "/srv", "/dev", "/boot", "/root",
])
# One level below these is a whole user/volume/mount, equally off limits.
PROTECTED_PARENTS = frozenset(["Users", "home", "Volumes", "mnt"])

SHELLS = frozenset(["bash", "sh", "zsh", "dash", "ksh", "ash", "busybox"])
# Wrappers that carry a real command behind them.
WRAPPERS = frozenset(["sudo", "env", "command", "exec", "nice", "nohup",
                      "time", "timeout", "doas"])
# ...of which these also defeat the PATH shim of Layer B.
BYPASS_WRAPPERS = frozenset(["sudo", "env", "command", "doas"])
# Wrapper options that consume the NEXT token as their value.
OPT_WITH_ARG = {
    "sudo": frozenset(["-u", "-g", "-p", "-C", "-U", "-h", "-r", "-t"]),
    "doas": frozenset(["-u", "-C"]),
    "env": frozenset(["-u", "-C", "-S"]),
    "nice": frozenset(["-n"]),
    "timeout": frozenset(["-s", "-k", "--signal"]),
}
GLOB_CHARS = "*?["
RECURSIVE_RE = re.compile(r"^-[A-Za-z]*[rR][A-Za-z]*$")
# `> $VAR`, `: > $VAR`, `>| $VAR` — truncation of a target that is a BARE
# variable (or a command substitution). `> "$TMP/out.txt"` is a normal
# write to a known-shaped path and stays allowed. The `(?<!>)` is what
# keeps `>> $LOG` out: an append truncates nothing, and without the
# lookbehind the SECOND `>` of `>>` matches this pattern.
REDIRECT_VAR_RE = re.compile(
    r"(?<!>)>\|?\s*([\"']?)(\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|`[^`]*`)\1\s*(?:$|[;&|\n])")
# `name() {` / `function name {` — a function DEFINITION segment. Its body
# is a command line in its own right (and `$1` inside it is the incident's
# own shape), so it is analysed instead of being read as a command called
# `f(){`.
FUNC_DEF_RE = re.compile(
    r"^\s*(?:function\s+[A-Za-z_][A-Za-z0-9_]*\s*(?:\(\s*\))?"
    r"|[A-Za-z_][A-Za-z0-9_]*\s*\(\s*\))\s*\{?\s*")
# Applet multiplexers: `busybox rm -rf /` IS an rm.
MULTIPLEXERS = frozenset(["busybox", "toybox"])
CD_COMMANDS = frozenset(["cd", "pushd"])
# Shell keywords and grouping tokens that can stand IN FRONT of the real
# command word of a segment: `do rm -rf "$d"`, `then rm -rf /`,
# `( rm -rf / )`, `{ rm -rf /`. Without stripping them the segment reads
# as a command named `do` / `(` and the rm behind it is invisible.
KEYWORDS = frozenset([
    "do", "done", "then", "else", "elif", "fi", "esac", "!",
    "{", "}", "(", ")", "((", "))",
])
# Compound headers: everything up to their `in` belongs to the header.
KEYWORDS_UNTIL_IN = frozenset(["for", "case", "select"])
# Headers whose remainder IS a command (`while read f`, `if true`).
KEYWORDS_BARE = frozenset(["if", "while", "until"])
OPENERS = {"(": ")", "{": "}", "((": "))"}
# The guard's own directory — Layer B lives here, so a command that
# rewrites, moves, empties or un-executes it is disarming the guard.
GUARD_MARK = ".claude/guard"
# Commands whose guard-dir operand is a WRITE to it.
TAMPER_COMMANDS = frozenset([
    "rm", "unlink", "truncate", "chmod", "chown", "shred", "chflags",
    "xattr", "mkfifo", "mv",
])
# ...`mv` included: moving the shim away removes it just as well. For
# these only the DESTINATION (last operand) counts, so reading the shim
# out (`cp …/guard/bin/rm /tmp/copy`) stays allowed.
TAMPER_DEST_COMMANDS = frozenset(["cp", "ln", "install", "tee", "dd"])
GUARD_REDIRECT_RE = re.compile(r">\|?\s*[\"']?[^\s;&|>]*" + re.escape(GUARD_MARK))
BYPASS_STRINGS = ("SAFE_RM_DRYRUN", "SAFE_RM_BYPASS")
MAX_DEPTH = 4


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


# --- text plumbing ---------------------------------------------------------

def _split_segments(text):
    """Split a command line into command segments on unquoted `;`, `|`,
    `||`, `&&`, `&` and newlines. Quote-aware by hand rather than by
    regex: the whole point is that `echo "a; rm -rf /"` is ONE segment
    (an echo) while `a; rm -rf /` is two."""
    segs, buf = [], []
    quote = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            buf.append(c)
            buf.append(text[i + 1])
            i += 2
            continue
        if c in ";\n":
            segs.append("".join(buf))
            buf = []
            i += 1
            continue
        if c in "&|":
            segs.append("".join(buf))
            buf = []
            i += 2 if text[i:i + 2] in ("&&", "||") else 1
            continue
        buf.append(c)
        i += 1
    segs.append("".join(buf))
    return [s.strip() for s in segs if s.strip()]


def _substitutions(text):
    """Bodies of `$(…)` and backtick command substitutions outside single
    quotes — each is its own command and gets analysed as one."""
    out = []
    i, n = 0, len(text)
    quote = None
    while i < n:
        c = text[i]
        if quote:
            if c == quote:
                quote = None
            i += 1
            continue
        if c == "'":
            quote = c
            i += 1
            continue
        if c == '"':
            quote = c
            i += 1
            continue
        if c == "$" and text[i:i + 2] == "$(":
            depth, j = 1, i + 2
            while j < n and depth:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            out.append(text[i + 2:j - 1])
            i = j
            continue
        if c == "`":
            j = text.find("`", i + 1)
            if j < 0:
                break
            out.append(text[i + 1:j])
            i = j + 1
            continue
        i += 1
    return [s for s in out if s.strip()]


def _tokens(seg):
    """shlex in posix mode (so `""` survives as an empty token and
    `'rm -rf x'` survives as one token), whitespace split as fallback
    for anything shlex refuses (unbalanced quotes)."""
    try:
        return shlex.split(seg, posix=True)
    except ValueError:
        return seg.split()


def _base(tok):
    """Command name without its path: `/bin/rm` -> `rm`, `C:\\x\\rm.exe`
    -> `rm.exe` -> `rm`."""
    name = tok.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name


def _is_recursive(tok):
    return tok == "--recursive" or bool(RECURSIVE_RE.match(tok))


# --- path reasoning --------------------------------------------------------

# Windows forms of an absolute path: `C:/...` (native, backslashes already
# folded) and `/c/...` (Git Bash / MSYS, what `~` expands to on MGMT01).
# posixpath.isabs() knows neither, so without this a drive path would be
# joined under cwd and a `rm -rf ~/*` under Git Bash lands "inside" the
# project root and is waved through.
DRIVE_RE = re.compile(r"^[A-Za-z]:(/|$)")
MSYS_DRIVE_RE = re.compile(r"^/([A-Za-z])(/|$)")
# Top-level Windows directories nobody deletes as a task step (compared
# case-folded, one level below the drive root).
PROTECTED_TOP_NT = frozenset([
    "users", "windows", "program files", "program files (x86)", "programdata",
])


def _home():
    return os.path.expanduser("~").replace("\\", "/").rstrip("/")


def _fold_drive(p):
    """`/c/Users/x` -> `C:/Users/x`; other paths unchanged."""
    m = MSYS_DRIVE_RE.match(p)
    if m:
        return m.group(1).upper() + ":" + p[2:]
    return p


def _is_abs(p):
    return posixpath.isabs(p) or bool(DRIVE_RE.match(p))


def _abspath(op, cwd):
    """Absolute, normalised, forward-slash form of an operand — string
    work only, never a filesystem lookup (the path may not exist, and a
    guard must not stat whatever it is handed)."""
    p = op.replace("\\", "/")
    if p.startswith("~"):
        p = _home() + p[1:]
    p = _fold_drive(p)
    if not _is_abs(p):
        p = posixpath.join(_fold_drive((cwd or "").replace("\\", "/")), p)
    return posixpath.normpath(p)


def _is_protected(path):
    p = _fold_drive((path or "").replace("\\", "/")).rstrip("/") or "/"
    if p == "/" or p in PROTECTED_TOP:
        return True
    home = _fold_drive(_home())
    if home and p == home:
        return True
    parts = p.split("/")
    if len(parts) == 3 and parts[0] == "" and parts[1] in PROTECTED_PARENTS:
        return True
    if DRIVE_RE.match(p):
        # `C:` (drive root), `C:/Users`, `C:/Windows`, ..., `C:/Users/<name>`
        if len(parts) == 1:
            return True
        if len(parts) == 2 and parts[1].lower() in PROTECTED_TOP_NT:
            return True
        if len(parts) == 3 and parts[1].lower() == "users":
            return True
    return False


def _allowed_roots(cwd):
    home = _home()
    roots = [cwd, tempfile.gettempdir(), "/tmp", "/private/tmp", "/var/folders",
             home + "/.claude", home + "/.workflow", home + "/Documents/git"]
    out = []
    for r in roots:
        if not r:
            continue
        r = posixpath.normpath(r.replace("\\", "/")).rstrip("/")
        if not r or _is_protected(r):
            continue
        out.append(r)
    return out


def _inside_allowed(path, cwd):
    return any(path.startswith(root + "/") for root in _allowed_roots(cwd))


def _glob_parent(op):
    """For an operand carrying a glob, the directory whose CONTENTS the
    glob names — `/ *` -> `/`, `~/x/*` -> `~/x`, `*` -> `` (cwd)."""
    idx = min([op.find(c) for c in GLOB_CHARS if op.find(c) >= 0])
    head = op[:idx]
    if head.endswith("/"):
        return head.rstrip("/") or "/"
    return posixpath.dirname(head)


# --- rm operand verdicts ---------------------------------------------------

def _operand_verdict(op, cwd, cwd_unknown=False):
    """(decision, kind) for ONE operand of a recursive rm, or None."""
    if op == "":
        return ("deny", "empty-operand")
    if "$" in op or "`" in op:
        return ("deny", "variable-operand")
    if op.rstrip("/") in ("~", ""):
        return ("deny", "home-or-root")
    # After a `cd` earlier in the same line the payload's cwd is a lie, so
    # a relative operand names a directory nobody here can point at.
    if cwd_unknown and not op.startswith("~") \
            and not posixpath.isabs(op.replace("\\", "/")):
        if (op.rstrip("/") or op) in ("*", ".", ".."):
            return ("deny", "recursive-rm-in-unknown-cwd")
        return ("ask", "relative-path-in-unknown-cwd")
    if any(c in op for c in GLOB_CHARS):
        parent = _glob_parent(op)
        target = _abspath(parent, cwd) if parent else _abspath(".", cwd)
        if _is_protected(target):
            return ("deny", "glob-in-protected-dir")
        if _inside_allowed(target, cwd) or target == posixpath.normpath(
                (cwd or "").replace("\\", "/")):
            return None
        return ("ask", "glob-outside-roots")
    target = _abspath(op, cwd)
    if _is_protected(target):
        return ("deny", "protected-path")
    if _inside_allowed(target, cwd):
        return None
    return ("ask", "outside-allowed-roots")


def _worse(a, b):
    if b is None:
        return a
    if a is None:
        return b
    return a if a[0] == "deny" else (b if b[0] == "deny" else a)


# --- command shapes --------------------------------------------------------

def _has_recursive_rm(text, depth=0):
    """Does this text contain a recursive `rm` ANYWHERE — as a nested
    command, behind a wrapper, mid-pipeline? Used for the contexts the
    guard refuses regardless of operands (a `-c` string, eval, xargs,
    find -exec, a remote shell), where reasoning about operands is
    hopeless anyway."""
    if depth > MAX_DEPTH:
        return False
    for seg in _split_segments(text):
        toks = _tokens(seg)
        for i, tok in enumerate(toks):
            if _base(tok) != "rm":
                continue
            if any(_is_recursive(t) for t in toks[i + 1:]):
                return True
        for tok in toks:
            if (" " in tok or ";" in tok) and _has_recursive_rm(tok, depth + 1):
                return True
        for sub in _substitutions(seg):
            if _has_recursive_rm(sub, depth + 1):
                return True
    return False


def _func_body(seg):
    """The body of a function-definition segment, or None. `f(){ rm -rf
    $1; }; f ""` splits on `;` into `f(){ rm -rf $1` — without this the
    segment reads as a command named `f(){` and the rm is never seen."""
    m = FUNC_DEF_RE.match(seg)
    if not m or m.end() == 0:
        return None
    body = seg[m.end():].strip()
    return body or None


def _strip_multiplexer(toks):
    """`busybox rm -rf /` / `toybox rm …` — drop the multiplexer so the
    applet behind it is judged as the command it is."""
    while len(toks) > 1 and _base(toks[0]) in MULTIPLEXERS \
            and not toks[1].startswith("-"):
        toks = toks[1:]
    return toks


def _strip_keywords(toks):
    """Drop the leading shell keywords and grouping tokens of a segment so
    what is left is the command the segment actually RUNS.

    `for d in $(ls /); do rm -rf "/$d"; done` splits on `;` into three
    segments, the middle one being `do rm -rf "/$d"` — read literally that
    is a command called `do`, and the recursive rm behind it is never
    examined. Same for `then`, `else`, `( rm -rf / )`, `{ rm -rf /`, a
    `case` pattern label, and a grouping char glued to the command word
    (`(rm`). A closing `)`/`}` is removed again at the end, but only as
    many as this segment opened — so `rm -rf x)` (whatever that is) keeps
    its operand intact.
    """
    toks = list(toks)
    opened, i = [], 0
    while i < len(toks):
        tok = toks[i]
        if tok in KEYWORDS:
            if tok in OPENERS:
                opened.append(OPENERS[tok])
            i += 1
            continue
        if tok in KEYWORDS_UNTIL_IN:
            j = i + 1
            while j < len(toks) and toks[j] != "in":
                j += 1
            i = j + 1 if j < len(toks) else i + 1
            continue
        if tok in KEYWORDS_BARE:
            i += 1
            continue
        if len(tok) > 1 and tok[0] in OPENERS and tok[:2] not in OPENERS:
            toks[i:i + 1] = [tok[0], tok[1:]]
            continue
        # a `case` pattern label: `a)`, `*)`, `x|y)` — only ever reachable
        # once a keyword before it was stripped.
        if i and len(tok) > 1 and tok.endswith(")") and "(" not in tok:
            i += 1
            continue
        break
    rest = toks[i:]
    while opened and rest:
        closer = opened[-1]
        if rest[-1] == closer:
            rest = rest[:-1]
        elif len(rest[-1]) > len(closer) and rest[-1].endswith(closer):
            rest = rest[:-1] + [rest[-1][:-len(closer)]]
        else:
            break
        opened.pop()
    return rest


def _command_tokens(seg):
    """The tokens of the command a segment runs: keywords stripped, then
    leading `VAR=…` assignments."""
    return _strip_assignments(_strip_keywords(_tokens(seg)))


def _invokes_rm(text, depth=0):
    """Does this line actually CALL `rm` — as a command word, in a `-c`
    body, behind xargs/eval, in `find -exec`? Unlike a text match this is
    false for `git commit -m 'rm -rf cleanup'`, `grep -r "rm -rf" docs/`
    and `docker rm c`, none of which should be rewritten."""
    if depth > MAX_DEPTH:
        return False
    for seg in _split_segments(text):
        body = _func_body(seg)
        if body is not None:
            if _invokes_rm(body, depth + 1):
                return True
            continue
        toks, _ = _strip_wrappers(_command_tokens(seg))
        toks = _strip_multiplexer(toks)
        if not toks:
            continue
        name = _base(toks[0])
        if name == "rm":
            return True
        if name in SHELLS:
            for i, tok in enumerate(toks[1:], 1):
                if tok == "-c" and i + 1 < len(toks):
                    if _invokes_rm(toks[i + 1], depth + 1):
                        return True
                    break
        elif name in ("eval", "xargs"):
            if _invokes_rm(" ".join(toks[1:]), depth + 1):
                return True
        elif name == "find":
            if _invokes_rm(" ".join(_find_exec_tail(toks)), depth + 1):
                return True
        for sub in _substitutions(seg):
            if _invokes_rm(sub, depth + 1):
                return True
    return False


def _strip_assignments(toks):
    i = 0
    while i < len(toks) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[i]):
        i += 1
    return toks[i:]


def _strip_wrappers(toks):
    """Peel sudo/env/command/exec/nice/nohup/time/timeout (and their own
    options) off the front. Returns (remaining tokens, bypass_seen)."""
    bypass = False
    while toks:
        name = _base(toks[0])
        if name not in WRAPPERS:
            break
        if name in BYPASS_WRAPPERS:
            bypass = True
        toks = toks[1:]
        # the wrapper's own options; `-u user` / `-n 5` / `VAR=1`. Only the
        # options that really take a value consume the next token —
        # `command -p rm …` must not eat the `rm`.
        with_arg = OPT_WITH_ARG.get(name, frozenset())
        while toks and (toks[0].startswith("-")
                        or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0])):
            opt = toks[0]
            toks = toks[1:]
            if opt in with_arg and toks:
                toks = toks[1:]
        if name == "timeout" and toks and not toks[0].startswith("-"):
            toks = toks[1:]  # the duration
        toks = _strip_assignments(toks)
    return toks, bypass


def _check_rm(toks, cwd, bypass, literal_path, cwd_unknown=False):
    if not any(_is_recursive(t) for t in toks[1:]):
        return None
    if bypass:
        return ("deny", "recursive-rm-behind-wrapper")
    if literal_path:
        return ("deny", "recursive-rm-by-absolute-path")
    worst = None
    seen_ddash = False
    for tok in toks[1:]:
        if not seen_ddash and tok == "--":
            seen_ddash = True
            continue
        if not seen_ddash and tok.startswith("-") and tok != "-":
            continue
        worst = _worse(worst, _operand_verdict(tok, cwd, cwd_unknown))
    return worst


def _check_git(toks, cwd):
    if len(toks) < 2 or toks[1] != "clean":
        return None
    for tok in toks[2:]:
        if "$" in tok or "`" in tok or tok == "":
            return ("deny", "git-clean-variable-operand")
        # `-e <pattern>` reads as "keep this", so the operator's mental
        # model of what survives is the pattern — and a pattern that
        # matches nothing means the whole tree goes.
        if tok == "--exclude" or tok.startswith("--exclude=") or (
                tok.startswith("-") and not tok.startswith("--") and "e" in tok):
            return ("deny", "git-clean-exclude")
    return None


def _check_others(name, toks, cwd):
    if name.startswith("mkfs"):
        return ("deny", "mkfs")
    if name == "shred":
        return ("deny", "shred")
    if name == "dd":
        for tok in toks[1:]:
            if tok.startswith("of="):
                target = tok[3:]
                if "$" in target or "`" in target or target.startswith("/dev/"):
                    return ("deny", "dd-to-device")
        return None
    if name == "diskutil":
        for tok in toks[1:2]:
            low = tok.lower()
            if low.startswith("erase") or low in ("zerodisk", "securerase",
                                                  "secureerase", "partitiondisk"):
                return ("deny", "diskutil-erase")
        return None
    if name in ("chmod", "chown"):
        if not any(_is_recursive(t) for t in toks[1:]):
            return None
        for tok in toks[2:]:
            if tok.startswith("-"):
                continue
            if "$" in tok or "`" in tok:
                return ("deny", "recursive-chmod-variable")
            if _is_protected(_abspath(tok, cwd)):
                return ("deny", "recursive-chmod-protected-path")
        return None
    if name == "truncate":
        zero = any(t in ("0", "--size=0", "-s0") for t in toks[1:])
        if not zero:
            return None
        for tok in toks[1:]:
            if tok.startswith("-"):
                continue
            if "$" in tok or "`" in tok:
                return ("deny", "truncate-variable")
        return None
    return None


FIND_GLOBAL_OPTS = frozenset(["-H", "-L", "-P", "-E", "-s", "-x", "-d", "-f"])


def _find_exec_tail(toks):
    """The command tokens of the first `-exec`/`-execdir`, up to its
    terminator."""
    for i, tok in enumerate(toks):
        if tok in ("-exec", "-execdir"):
            rest = []
            for t in toks[i + 1:]:
                if t in (";", "+", "\\;"):
                    break
                rest.append(t)
            return rest
    return []


def _find_scope(toks, cwd):
    """Where does this `find` START walking? `ask` when every start path
    is a literal inside the allowed roots, `deny` otherwise — a walk that
    begins at `/`, at a protected directory or at a variable deletes an
    unknown set, and that is not something to confirm, it is something to
    refuse."""
    paths, i = [], 1
    while i < len(toks) and toks[i] in FIND_GLOBAL_OPTS:
        i += 1
    while i < len(toks):
        tok = toks[i]
        if tok.startswith("-") or tok in ("(", ")", "!", ","):
            break
        paths.append(tok)
        i += 1
    if not paths:
        paths = ["."]  # GNU find defaults to the working directory
    cwd_norm = posixpath.normpath((cwd or "").replace("\\", "/"))
    for op in paths:
        if "$" in op or "`" in op or op == "":
            return "deny"
        if any(c in op for c in GLOB_CHARS):
            op = _glob_parent(op) or "."
        target = _abspath(op, cwd)
        if _is_protected(target):
            return "deny"
        if not (_inside_allowed(target, cwd) or target == cwd_norm):
            return "deny"
    return "ask"


def _check_find(toks, cwd, depth):
    deletes = "-delete" in toks
    kind = "find-delete" if deletes else None
    if not deletes and _has_recursive_rm(" ".join(_find_exec_tail(toks)),
                                         depth + 1):
        kind = "recursive-rm-in-find-exec"
    if kind is None:
        return None
    return (_find_scope(toks, cwd), kind)


def _check_remote(toks, cwd, depth):
    """ssh / docker exec / kubectl exec: the trailing argument(s) are a
    command on ANOTHER machine, where the rm shim does not exist. The
    same static rules apply, and an `ask` there becomes a deny — there
    is no second layer behind it."""
    tail = " ".join(toks)
    if _has_recursive_rm(tail, depth + 1):
        return ("deny", "recursive-rm-in-remote-command")
    inner = _check_command(tail, cwd, depth + 1)
    if inner:
        return ("deny", "remote-" + inner[1])
    return None


def _mentions_guard(text):
    return GUARD_MARK in (text or "").replace("\\", "/")


def _check_guard_tamper(toks, name):
    """The guard protecting itself. Layer B is a file — `rm`, `mv`, `cp`
    over it, `chmod -x`, `: >` it, `ln -sf` something else in its place or
    `sed -i` it, and the shim that would have caught the expanded argv is
    simply gone. Reading it (`cat`, `sed -n`, `cp …/guard/bin/rm /tmp/x`)
    stays allowed; writing to it never is."""
    operands = [t for t in toks[1:] if not t.startswith("-")]
    if name in TAMPER_COMMANDS:
        if any(_mentions_guard(t) for t in toks[1:]):
            return ("deny", "guard-self-tamper")
    elif name in TAMPER_DEST_COMMANDS:
        dest = [t for t in toks[1:] if t.startswith("of=")] or operands[-1:]
        if any(_mentions_guard(t) for t in dest):
            return ("deny", "guard-self-tamper")
    elif name == "sed":
        in_place = any(t == "-i" or t.startswith("-i") or t == "--in-place"
                       or t.startswith("--in-place") for t in toks[1:]
                       if t.startswith("-"))
        if in_place and any(_mentions_guard(t) for t in operands):
            return ("deny", "guard-self-tamper")
    return None


def _check_segment(seg, cwd, depth, cwd_unknown=False):
    if REDIRECT_VAR_RE.search(seg):
        return ("deny", "truncating-redirect-to-variable")
    if _mentions_guard(seg) and GUARD_REDIRECT_RE.search(seg):
        return ("deny", "guard-self-tamper")
    body = _func_body(seg)
    if body is not None:
        return _check_command(body, cwd, depth + 1, cwd_unknown)
    toks = _command_tokens(seg)
    if not toks:
        return None
    toks, bypass = _strip_wrappers(toks)
    if not toks:
        return None
    toks = _strip_multiplexer(toks)
    name = _base(toks[0])
    literal_path = "/" in toks[0] or "\\" in toks[0]

    if _mentions_guard(seg):
        tamper = _check_guard_tamper(toks, name)
        if tamper:
            return tamper

    # `RM=rm; $RM -rf /` — the command word itself is a variable, so what
    # runs is unknowable here; a recursive flag behind it is enough.
    if ("$" in toks[0] or "`" in toks[0]) and \
            any(_is_recursive(t) for t in toks[1:]):
        return ("deny", "variable-command")

    if name in SHELLS:
        for i, tok in enumerate(toks[1:], 1):
            if tok == "-c" and i + 1 < len(toks):
                body = toks[i + 1]
                if _has_recursive_rm(body, depth + 1):
                    return ("deny", "recursive-rm-in-shell-c-string")
                return _check_command(body, cwd, depth + 1)
        return None
    if name == "eval":
        body = " ".join(toks[1:])
        if _has_recursive_rm(body, depth + 1):
            return ("deny", "recursive-rm-in-eval")
        return _check_command(body, cwd, depth + 1)
    if name == "xargs":
        body = " ".join(toks[1:])
        if _has_recursive_rm(body, depth + 1):
            return ("deny", "recursive-rm-in-xargs")
        return _check_command(body, cwd, depth + 1)
    if name == "find":
        return _check_find(toks, cwd, depth)
    if name == "ssh":
        return _check_remote(toks[1:], cwd, depth)
    if name in ("docker", "podman", "kubectl") and len(toks) > 2 and \
            toks[1] in ("exec", "run"):
        return _check_remote(toks[2:], cwd, depth)
    if name == "rm":
        return _check_rm(toks, cwd, bypass, literal_path, cwd_unknown)
    if name == "git":
        return _check_git(toks, cwd)
    return _check_others(name, toks, cwd)


def _is_cd(seg):
    toks, _ = _strip_wrappers(_command_tokens(seg))
    return bool(toks) and _base(toks[0]) in CD_COMMANDS


def _check_command(text, cwd, depth=0, cwd_unknown=False):
    if depth > MAX_DEPTH:
        return None
    worst = None
    for seg in _split_segments(text):
        worst = _worse(worst, _check_segment(seg, cwd, depth, cwd_unknown))
        for sub in _substitutions(seg):
            worst = _worse(worst, _check_command(sub, cwd, depth + 1,
                                                 cwd_unknown))
        # Everything after a `cd` runs somewhere the payload's `cwd` does
        # not name — `cd / && rm -rf *` is the whole reason this exists.
        if not cwd_unknown and _is_cd(seg):
            cwd_unknown = True
    return worst


# --- reasons ---------------------------------------------------------------

REASONS = {
    "empty-operand":
        "a recursive `rm` with an EMPTY operand — this is the 2026-09-09 "
        "incident verbatim (`rm -rf -- \"$1\"/*` with $1 unset expands to "
        "`rm -rf /*`)",
    "variable-operand":
        "a recursive `rm` whose operand is a VARIABLE — its value at run "
        "time is unknown here, and an empty one means `/`",
    "home-or-root":
        "a recursive `rm` on `~` / the root directory",
    "protected-path":
        "a recursive `rm` on a protected system or home directory",
    "glob-in-protected-dir":
        "a recursive `rm` on a glob inside a protected directory "
        "(`/*`-shaped: every top-level entry)",
    "recursive-rm-behind-wrapper":
        "a recursive `rm` behind `sudo`/`env`/`command` — which also "
        "sidesteps the `rm` shim that would check the expanded arguments",
    "recursive-rm-by-absolute-path":
        "a recursive `rm` invoked by absolute path (`/bin/rm`), which "
        "sidesteps the `rm` shim that checks expanded arguments",
    "recursive-rm-in-shell-c-string":
        "a recursive `rm` inside a `-c` shell string — the operands are "
        "expanded by that inner shell, after every check up here",
    "recursive-rm-in-eval": "a recursive `rm` inside `eval`",
    "recursive-rm-in-xargs":
        "a recursive `rm` driven by `xargs` (operands come from stdin, "
        "unknown here)",
    "recursive-rm-in-find-exec": "a recursive `rm` inside `find -exec`",
    "find-delete": "`find … -delete`, which deletes whatever the walk matches",
    "recursive-rm-in-remote-command":
        "a recursive `rm` inside a REMOTE command string — the local `rm` "
        "shim cannot protect another machine",
    "git-clean-variable-operand":
        "`git clean` with a variable/empty operand",
    "git-clean-exclude":
        "`git clean` with `-e`/`--exclude` — what survives then depends "
        "on a pattern matching, and a pattern that matches nothing "
        "deletes the whole tree",
    "variable-command":
        "a recursive delete whose COMMAND WORD is a variable (`$RM -rf …`) "
        "— what actually runs is unknown here",
    "recursive-rm-in-unknown-cwd":
        "a recursive `rm` on `*`, `.` or `..` after a `cd` earlier in the "
        "same line — the directory it would empty is not the one this "
        "approval shows",
    "relative-path-in-unknown-cwd":
        "a recursive `rm` on a relative path after a `cd` earlier in the "
        "same line, so the directory it resolves against is not the one "
        "shown here",
    "mkfs": "a filesystem-creating command (`mkfs*`) — it destroys a volume",
    "shred": "`shred`, which overwrites file contents irrecoverably",
    "dd-to-device": "`dd` writing to a device or a variable target",
    "diskutil-erase": "a `diskutil` erase/partition operation",
    "recursive-chmod-variable":
        "a recursive `chmod`/`chown` on a variable path",
    "recursive-chmod-protected-path":
        "a recursive `chmod`/`chown` on a protected system path",
    "truncate-variable": "`truncate -s 0` on a variable path",
    "truncating-redirect-to-variable":
        "a `>` truncation whose target is a variable",
    "guard-self-tamper":
        "a write to `~/.claude/guard` — that directory IS Layer B of this "
        "guard, and a command that deletes, moves, truncates, un-executes "
        "or rewrites the `rm` shim disarms the only check that sees "
        "expanded arguments",
    "in-band-bypass":
        "a reference to SAFE_RM_DRYRUN/SAFE_RM_BYPASS — the shim's "
        "dry-run switch belongs to the test suite; there is no in-band "
        "way to turn the guard off",
    "outside-allowed-roots":
        "a recursive `rm` on a path outside the working directory, the "
        "temp dirs and ~/.claude, ~/.workflow, ~/Documents/git",
    "glob-outside-roots":
        "a recursive `rm` on a glob outside the working directory and "
        "the other allowed roots",
}


def _deny_reason(kind):
    what = REASONS.get(kind, "a destructive command shape (%s)" % kind)
    return ("DESTRUCTIVE GUARD: refusing " + what + ". Rewrite it with a "
            "literal path under the working directory, or probe it with "
            "`echo` first — never with a test, empty or variable operand. "
            "Set DESTRUCTIVE_GUARD=0 to disable this guard.")


def _ask_reason(kind):
    what = REASONS.get(kind, "a destructive command (%s)" % kind)
    return ("DESTRUCTIVE GUARD: " + what + ". Confirm this is the path you "
            "mean before it runs. Set DESTRUCTIVE_GUARD=0 to disable this "
            "guard.")


# --- hook body -------------------------------------------------------------

def _guard(data):
    if (os.environ.get("DESTRUCTIVE_GUARD") or "").strip() == "0":
        return

    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return

    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        try:
            cwd = os.getcwd()
        except Exception:
            cwd = ""

    if any(s in command for s in BYPASS_STRINGS):
        verdict = ("deny", "in-band-bypass")
    else:
        verdict = _check_command(command, cwd)

    session_id = data.get("session_id")
    head = command[:60]
    out = {"hookEventName": "PreToolUse"}

    if verdict and verdict[0] == "deny":
        _metric("destructive_deny", session_id, kind=verdict[1], cmd_head=head)
        out["permissionDecision"] = "deny"
        out["permissionDecisionReason"] = _deny_reason(verdict[1])
        print(json.dumps({"hookSpecificOutput": out}))
        return

    if verdict and verdict[0] == "ask":
        _metric("destructive_ask", session_id, kind=verdict[1], cmd_head=head)
        out["permissionDecision"] = "ask"
        out["permissionDecisionReason"] = _ask_reason(verdict[1])

    # Layer B in front of /bin/rm for this call — only for commands that
    # actually mention `rm`, so allow-rules on other tools keep matching.
    if not command.startswith(PREFIX) and _invokes_rm(command):
        updated = dict(tool_input)
        updated["command"] = PREFIX + command
        out["updatedInput"] = updated

    if len(out) > 1:
        print(json.dumps({"hookSpecificOutput": out}))


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
