#!/usr/bin/env python3
"""ledger — tick, defer, add and annotate Requirements Ledger items in place.

Replaces the ad-hoc `python3 -` heredocs chairs used to edit
.workflow/LEDGER-<topic>.md. Stdlib only, Python 3.9+.

    ledger status                 open items + count
    ledger mark N ["note"]        - [ ] -> - [x]   (note appended as " — note")
    ledger mark V --verifier      only the fresh verifier closes V.
    ledger defer N "reason"       -> - [~] deferred: reason — <item text>
    ledger add "text"             new - [ ] M. text before the V. line
    ledger note N "text"          append " — text", state unchanged

Ledger: `-f PATH`, else the single live ledger in ./.workflow/ (same
name rule as the hooks; *-archive.md excluded). Zero or several -> error.
Parsing shares the stop guard's fence handling and open-item regex, so
items inside ``` fences are never touched. Edits are atomic (temp file
in the same directory + os.replace), keep the file's line endings and
trailing newline, and never create a ledger. Exit 0 on success, 1 on
any refusal (file unchanged), 2 on usage errors.
"""
import argparse
import os
import re
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ledger_guard_stop import OPEN_ITEM_RE, _fence_mask  # noqa: E402
from ledger_guard_write import _is_live_ledger_name  # noqa: E402

# Any item line: indent, bullet, box, optional "deferred: <reason> — ",
# then the id ("7." or "V.").
ITEM_RE = re.compile(
    r"^(?P<pre>\s*[-*] )\[(?P<box>.)\](?P<sp>\s+)"
    r"(?:deferred:.*? — )?(?P<id>\d+|V)\.(?:\s|$)"
)
ID_RE = re.compile(r"^(?P<pre>\s*[-*] \[.\]\s+)(?P<rest>(?P<id>\d+|V)\..*)$")


class LedgerError(Exception):
    pass


def resolve(path_arg):
    if path_arg:
        if not os.path.isfile(path_arg):
            raise LedgerError(f"no such ledger file: {path_arg} (ledger never creates one)")
        return path_arg
    workflow = os.path.join(os.getcwd(), ".workflow")
    try:
        names = sorted(os.listdir(workflow))
    except OSError:
        names = []
    found = [os.path.join(".workflow", n) for n in names
             if _is_live_ledger_name(n) and os.path.isfile(os.path.join(workflow, n))]
    if len(found) == 1:
        return found[0]
    if not found:
        raise LedgerError("no live ledger in ./.workflow/ — pass -f PATH")
    raise LedgerError("several live ledgers in ./.workflow/ — pass -f PATH:\n  "
                      + "\n  ".join(found))


def split_eol(line):
    body = line.rstrip("\r\n")
    return body, line[len(body):]


class Ledger:
    def __init__(self, path):
        self.path = path
        with open(path, encoding="utf-8", newline="") as f:
            self.text = f.read()
        self.lines = self.text.splitlines(keepends=True)
        self.mask = _fence_mask(self.lines)
        self.eol = "\r\n" if "\r\n" in self.text else "\n"

    def items(self):
        """(index, match) for every item line outside fences."""
        for i, line in enumerate(self.lines):
            if self.mask[i]:
                m = ITEM_RE.match(split_eol(line)[0])
                if m:
                    yield i, m

    def find(self, ident):
        ident = str(ident).upper() if str(ident).lower() == "v" else str(int(ident))
        hits = [i for i, m in self.items() if m.group("id") == ident]
        if not hits:
            raise LedgerError(f"item {ident} not found in {self.path}")
        if len(hits) > 1:
            raise LedgerError(f"item {ident} is ambiguous in {self.path} (lines "
                              + ", ".join(str(i + 1) for i in hits) + ")")
        return hits[0], ident

    def is_open(self, i):
        return bool(OPEN_ITEM_RE.match(split_eol(self.lines[i])[0]))

    def set_line(self, i, body):
        self.lines[i] = body + split_eol(self.lines[i])[1]

    def save(self):
        d = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(prefix=".ledger-", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write("".join(self.lines))
            try:
                os.chmod(tmp, stat.S_IMODE(os.stat(self.path).st_mode))
            except OSError:
                pass
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _require_open(led, i, ident, verb):
    if not led.is_open(i):
        box = ITEM_RE.match(split_eol(led.lines[i])[0]).group("box")
        state = {"x": "already done", "X": "already done", "~": "deferred"}.get(box, f"[{box}]")
        raise LedgerError(f"item {ident} is not open ({state}); cannot {verb} it")


def cmd_status(led, args):
    # Same open-item test as the stop guard, id or not.
    rows = [split_eol(l)[0].strip() for i, l in enumerate(led.lines)
            if led.mask[i] and OPEN_ITEM_RE.match(split_eol(l)[0])]
    print(f"{led.path}: {len(rows)} open item(s)")
    for r in rows:
        print(f"  {r}")
    return None


def cmd_mark(led, args):
    i, ident = led.find(args.n)
    if ident == "V" and not args.verifier:
        raise LedgerError("V. is closed only by the fresh verifier — "
                          "it runs `ledger mark V --verifier`")
    _require_open(led, i, ident, "mark")
    body = split_eol(led.lines[i])[0].replace("[ ]", "[x]", 1)
    if args.note:
        body += f" — {args.note}"
    led.set_line(i, body)
    return f"marked {ident}: {body.strip()}"


def cmd_defer(led, args):
    i, ident = led.find(args.n)
    _require_open(led, i, ident, "defer")
    m = ID_RE.match(split_eol(led.lines[i])[0])
    pre = m.group("pre").replace("[ ]", "[~]", 1)
    body = f"{pre}deferred: {args.reason} — {m.group('rest')}"
    led.set_line(i, body)
    return f"deferred {ident}: {body.strip()}"


def cmd_add(led, args):
    items = list(led.items())
    nums = [int(m.group("id")) for _, m in items if m.group("id") != "V"]
    new_n = max(nums, default=0) + 1
    v = [(i, m) for i, m in items if m.group("id") == "V"]
    if v:
        at, indent = v[0][0], v[0][1].group("pre")[:-2]
    elif items:
        at, indent = items[-1][0] + 1, items[-1][1].group("pre")[:-2]
    else:
        at, indent = len(led.lines), ""
    new = f"{indent}- [ ] {new_n}. {args.text}"
    if at == len(led.lines):
        # Appending at EOF: keep the file's trailing-newline state.
        if led.lines and not split_eol(led.lines[-1])[1]:
            led.lines[-1] += led.eol
            led.lines.append(new)
        else:
            led.lines.append(new + led.eol)
    else:
        led.lines.insert(at, new + led.eol)
    return f"added {new_n}: {new.strip()}"


def cmd_note(led, args):
    i, ident = led.find(args.n)
    body = split_eol(led.lines[i])[0] + f" — {args.text}"
    led.set_line(i, body)
    return f"noted {ident}: {body.strip()}"


def build_parser():
    p = argparse.ArgumentParser(prog="ledger", description=__doc__.split("\n")[0])
    p.add_argument("-f", "--file", dest="file", default=None, help="ledger path")
    sub = p.add_subparsers(dest="cmd")
    sub.required = True

    def sp(name, fn, help_):
        s = sub.add_parser(name, help=help_)
        s.add_argument("-f", "--file", dest="file", default=argparse.SUPPRESS,
                       help="ledger path")
        s.set_defaults(fn=fn)
        return s

    sp("status", cmd_status, "list open items")
    s = sp("mark", cmd_mark, "tick an open item [ ] -> [x]")
    s.add_argument("n")
    s.add_argument("note", nargs="?")
    s.add_argument("--verifier", action="store_true", help="required to tick V.")
    s = sp("defer", cmd_defer, "defer an open item with a reason")
    s.add_argument("n")
    s.add_argument("reason")
    s = sp("add", cmd_add, "append a new open item before V.")
    s.add_argument("text")
    s = sp("note", cmd_note, "append a note, state unchanged")
    s.add_argument("n")
    s.add_argument("text")
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # cp1252 consoles on Windows
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if getattr(args, "n", None) is not None and not re.fullmatch(r"\d+|[vV]", args.n):
        print(f"ledger: item must be a number or V, got {args.n!r}", file=sys.stderr)
        return 2
    try:
        led = Ledger(resolve(args.file))
        msg = args.fn(led, args)
        if msg:
            led.save()
            print(msg)
    except LedgerError as e:
        print(f"ledger: {e}", file=sys.stderr)
        return 1
    except (OSError, UnicodeDecodeError) as e:
        print(f"ledger: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
