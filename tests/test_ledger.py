"""scripts/ledger.py: the `ledger` helper that ticks, defers, adds and
annotates ledger items in place instead of ad-hoc heredoc edits."""
import json
import os
import subprocess
import sys

from conftest import SCRIPTS

BODY = (
    "# LEDGER\n"
    "\n"
    "- [ ] 1. first\n"
    "- [x] 2. done already\n"
    "```\n"
    "- [ ] 3. example inside a fence\n"
    "```\n"
    "- [ ] 4. fourth\n"
    "- [ ] V. fresh-eyes verification passed\n"
)


def ledger(cwd, *args, env=None):
    # Never inherit the live session's id or LEDGER from the test runner.
    base = {k: v for k, v in os.environ.items()
            if k not in ("CLAUDE_CODE_SESSION_ID", "LEDGER")}
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "ledger.py"), *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=30,
        env={**base, **(env or {})},
    )


def make(tmp_path, body=BODY, name="LEDGER-topic.md", newline="\n"):
    d = tmp_path / ".workflow"
    d.mkdir(exist_ok=True)
    p = d / name
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(body.replace("\n", newline))
    return p


def read(p):
    with open(p, encoding="utf-8", newline="") as f:
        return f.read()


def test_status_counts_open_items_outside_fences(tmp_path):
    make(tmp_path)
    r = ledger(tmp_path, "status")
    assert r.returncode == 0, r.stderr
    assert "3 open item(s)" in r.stdout
    assert "example inside a fence" not in r.stdout


def test_mark_with_note(tmp_path):
    p = make(tmp_path)
    r = ledger(tmp_path, "mark", "1", "verified by tests")
    assert r.returncode == 0, r.stderr
    assert "- [x] 1. first — verified by tests\n" in read(p)


def test_mark_already_done_refuses_and_leaves_file(tmp_path):
    p = make(tmp_path)
    r = ledger(tmp_path, "mark", "2")
    assert r.returncode == 1 and "already done" in r.stderr
    assert read(p) == BODY


def test_fenced_item_is_never_touched(tmp_path):
    p = make(tmp_path)
    r = ledger(tmp_path, "mark", "3")
    assert r.returncode == 1 and "not found" in r.stderr
    assert read(p) == BODY


def test_defer_keeps_item_text(tmp_path):
    p = make(tmp_path)
    r = ledger(tmp_path, "defer", "4", "user approved")
    assert r.returncode == 0, r.stderr
    assert "- [~] deferred: user approved — 4. fourth\n" in read(p)
    # the deferred item is still addressable by its number
    assert ledger(tmp_path, "note", "4", "later").returncode == 0
    assert "4. fourth — later\n" in read(p)


def test_note_keeps_state(tmp_path):
    p = make(tmp_path)
    assert ledger(tmp_path, "note", "1", "wip").returncode == 0
    assert "- [ ] 1. first — wip\n" in read(p)


def test_add_goes_before_v_and_continues_numbering(tmp_path):
    p = make(tmp_path)
    r = ledger(tmp_path, "add", "new thing")
    assert r.returncode == 0, r.stderr
    lines = read(p).splitlines()
    assert lines[-2] == "- [ ] 5. new thing"
    assert lines[-1].startswith("- [ ] V.")


def test_add_without_v_appends_after_last_item(tmp_path):
    p = make(tmp_path, "- [ ] 1. a\n- [x] 2. b\n\nnotes\n")
    assert ledger(tmp_path, "add", "c").returncode == 0
    assert read(p) == "- [ ] 1. a\n- [x] 2. b\n- [ ] 3. c\n\nnotes\n"


def test_v_needs_verifier_flag(tmp_path):
    p = make(tmp_path)
    r = ledger(tmp_path, "mark", "V")
    assert r.returncode != 0 and "verifier" in r.stderr
    assert read(p) == BODY
    assert ledger(tmp_path, "mark", "V", "--verifier").returncode == 0
    assert "- [x] V. fresh-eyes" in read(p)


def test_auto_pick_ambiguous_and_zero(tmp_path):
    r = ledger(tmp_path, "status")
    assert r.returncode == 1 and "no live ledger" in r.stderr
    make(tmp_path, name="LEDGER-a.md")
    make(tmp_path, name="LEDGER-old-archive.md")
    assert ledger(tmp_path, "status").returncode == 0  # archive excluded
    make(tmp_path, name="LEDGER-b.md")
    r = ledger(tmp_path, "status")
    assert r.returncode == 1
    assert "LEDGER-a.md" in r.stderr and "LEDGER-b.md" in r.stderr
    p = tmp_path / ".workflow" / "LEDGER-b.md"
    assert ledger(tmp_path, "-f", str(p), "mark", "1").returncode == 0


def test_missing_file_is_never_created(tmp_path):
    p = tmp_path / ".workflow" / "LEDGER-x.md"
    r = ledger(tmp_path, "-f", str(p), "add", "x")
    assert r.returncode == 1 and not p.exists()


def test_crlf_preserved(tmp_path):
    p = make(tmp_path, newline="\r\n")
    assert ledger(tmp_path, "mark", "1").returncode == 0
    assert ledger(tmp_path, "add", "crlf item").returncode == 0
    out = read(p)
    assert "\n" not in out.replace("\r\n", "")
    assert "- [x] 1. first\r\n" in out and "- [ ] 5. crlf item\r\n" in out
    assert out.endswith("passed\r\n")


def test_session_bound_ledger_wins_over_ambiguity(tmp_path):
    a = make(tmp_path, name="LEDGER-a.md")
    make(tmp_path, name="LEDGER-b.md")
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    (tmp / "fable-orch-model-sid-1.json").write_text(
        json.dumps({"ledger": str(a.resolve())}), encoding="utf-8")
    env = {"CLAUDE_CODE_SESSION_ID": "sid-1", "TMPDIR": str(tmp),
           "TEMP": str(tmp), "TMP": str(tmp)}
    r = ledger(tmp_path, "status", env=env)
    assert r.returncode == 0, r.stderr
    assert "LEDGER-a.md" in r.stdout
    # a bound ledger that was archived away is not live -> falls through
    a.rename(a.with_name("LEDGER-a-archive.md"))
    r = ledger(tmp_path, "status", env=env)
    assert r.returncode == 0 and "LEDGER-b.md" in r.stdout, r.stderr


def test_env_ledger_is_used(tmp_path):
    make(tmp_path, name="LEDGER-a.md")
    b = make(tmp_path, name="LEDGER-b.md")
    assert ledger(tmp_path, "mark", "1", env={"LEDGER": str(b)}).returncode == 0
    assert "- [x] 1. first\n" in read(b)


def test_ambiguous_list_is_capped(tmp_path):
    for i in range(8):
        p = make(tmp_path, name=f"LEDGER-{i}.md")
        os.utime(p, (1000 + i, 1000 + i))
    r = ledger(tmp_path, "status")
    assert r.returncode == 1
    assert "8 live ledgers" in r.stderr and "(3 more)" in r.stderr
    assert "LEDGER-7.md" in r.stderr and "LEDGER-0.md" not in r.stderr
    assert "LEDGER=" in r.stderr
