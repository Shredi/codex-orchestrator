"""Chair read guard (0.21.0): a Fable top chair explores through sonnet
scouts; its own non-brief reads are counted per task, warned at W and
denied from D. Subagents, teammates and non-fable chairs are untouched."""
import json
import os

from conftest import POSIX, REPO, run_hook, write_marker

GUARD = "chair_read_guard.py"
SID = "s-read"


def _chair(tmp_path, profile="fable", session=SID):
    write_marker(tmp_path, started=123.0, session=session, profile=profile)


def _read(path, **extra):
    return {"tool_name": "Read", "tool_input": {"file_path": str(path), **extra}}


def _bash(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _run(tmp_path, call, session=SID, env=None, cwd=None):
    payload = {"session_id": session, "hook_event_name": "PreToolUse",
               "cwd": str(cwd or tmp_path), **call}
    return run_hook(GUARD, payload, env_extra=env, tmpdir=tmp_path)


def _count(tmp_path, session=SID):
    path = tmp_path / f"fable-orch-reads-{session}.json"
    return json.loads(path.read_text())["count"] if path.exists() else 0


def _big(tmp_path, name="big.py"):
    path = tmp_path / name
    path.write_text("x = 1\n" * 2000, encoding="utf-8")   # ~12 KB > 6 KB
    return path


def _decision(out):
    return ((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision")


def test_no_marker_is_silent(tmp_path):
    assert _run(tmp_path, _read(_big(tmp_path))) is None
    assert _count(tmp_path) == 0


def test_non_fable_profiles_are_silent(tmp_path):
    big = _big(tmp_path)
    for i, profile in enumerate(("opus-primary", "opus", None)):
        _chair(tmp_path, profile=profile, session=f"s-p{i}")
        for _ in range(15):
            assert _run(tmp_path, _read(big), session=f"s-p{i}") is None
        assert _count(tmp_path, f"s-p{i}") == 0


def test_plain_mode_and_kill_switch_are_silent(tmp_path):
    _chair(tmp_path)
    big = _big(tmp_path)
    for env in ({"FABLE_ORCH_MODE": "plain"}, {"FABLE_ORCH_READ_GUARD": "off"},
                {"FABLE_ORCH_READ_GUARD": "0"}):
        for _ in range(15):
            assert _run(tmp_path, _read(big), env=env) is None
    assert _count(tmp_path) == 0


def test_subagent_reads_are_silent_and_uncounted(tmp_path):
    # Live payload (Claude Code 2.1.284): an Explore subagent's Read
    # carries agent_id + agent_type; the chair's own reads carry neither.
    _chair(tmp_path)
    big = _big(tmp_path)
    for _ in range(15):
        call = {**_read(big), "agent_id": "a1ea4810bae012795",
                "agent_type": "Explore"}
        assert _run(tmp_path, call) is None
    assert _count(tmp_path) == 0


@POSIX  # fake `ps` fixture needs POSIX shebang+chmod exec
def test_teammate_is_silent(tmp_path):
    _chair(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ps = bin_dir / "ps"
    ps.write_text("#!/usr/bin/env python3\nprint(%r)\n"
                  % "1 claude --agent-id w@s --agent-name w", encoding="utf-8")
    os.chmod(ps, 0o755)
    env = {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}
    for _ in range(15):
        assert _run(tmp_path, _read(_big(tmp_path)), env=env) is None
    assert _count(tmp_path) == 0


@POSIX  # HOME= is meant to redirect expanduser("~"); nt's expanduser ignores it
def test_exempt_reads_are_never_counted(tmp_path):
    _chair(tmp_path)
    home = tmp_path / "home"
    plans = home / ".claude" / "plans"
    plans.mkdir(parents=True)
    scratch = tmp_path / ".workflow" / "scratch"
    scratch.mkdir(parents=True)
    exempt = [
        _read(_big(scratch, "x.md")),
        _read(_big(plans, "p.md")),
        _read(_big(tmp_path, "CLAUDE.md")),
        _read(_big(tmp_path), limit=40),
        _read(tmp_path / "small.txt"),
        {"tool_name": "Grep", "tool_input": {"pattern": "x",
                                             "path": str(tmp_path / ".workflow")}},
        _bash(f"cat {scratch / 'b.md'}"),
    ]
    (tmp_path / "small.txt").write_text("tiny\n", encoding="utf-8")
    for _ in range(3):
        for call in exempt:
            assert _run(tmp_path, call, env={"HOME": str(home)}) is None, call
    assert _count(tmp_path) == 0


def test_counted_reads(tmp_path):
    _chair(tmp_path)
    counted = [
        _read(_big(tmp_path)),
        _read(_big(tmp_path), limit=400),
        {"tool_name": "Grep", "tool_input": {"pattern": "x"}},
        {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}},
        {"tool_name": "WebSearch", "tool_input": {"query": "x"}},
    ]
    for call in counted:
        _run(tmp_path, call)
    assert _count(tmp_path) == len(counted)


def test_bash_reads_are_classified(tmp_path):
    _chair(tmp_path)
    for cmd in ("git status", "npm test", "cd src && make", "sed -i s/a/b/ f"):
        assert _run(tmp_path, _bash(cmd)) is None
    assert _count(tmp_path) == 0
    for cmd in ("rg foo src/", "cat big.py", "sed -n 1,400p f",
                "curl https://example.com", "FOO=1 cd src && grep -r x ."):
        _run(tmp_path, _bash(cmd))
    assert _count(tmp_path) == 5


def test_bash_writes_are_ignored_not_counted(tmp_path):
    # cat with a stdout redirect or heredoc is a WRITE; curl with a
    # non-GET method and find with -delete/-exec are not reads either.
    _chair(tmp_path)
    for cmd in ("cat > f <<EOF\nx\nEOF", "cat <<EOF > f\nx\nEOF",
                "cat > .workflow/scratch/x.md <<EOF\nx\nEOF", "cat a >> b",
                "curl -X POST https://example.com", "curl -XDELETE https://e.x",
                "curl --request PUT https://e.x", "find . -name '*.pyc' -delete",
                "find . -exec rm {} +"):
        assert _run(tmp_path, _bash(cmd)) is None, cmd
    assert _count(tmp_path) == 0
    for cmd in ("grep x big.py 2>/dev/null", "curl -X GET https://e.x",
                "cat big.py | head"):
        _run(tmp_path, _bash(cmd))
    assert _count(tmp_path) == 3


def test_dotdot_out_of_an_allowlisted_segment_is_not_exempt(tmp_path):
    _chair(tmp_path)
    big = _big(tmp_path)
    sneaky = tmp_path / "skills" / "playbook" / ".." / ".." / big.name
    _run(tmp_path, _read(sneaky))
    assert _count(tmp_path) == 1


def test_warn_exactly_at_warn_then_deny_from_deny(tmp_path):
    _chair(tmp_path)
    big = _big(tmp_path)
    outs = [_run(tmp_path, _read(big)) for _ in range(13)]
    for i, out in enumerate(outs, start=1):
        if i == 6:
            ctx = out["hookSpecificOutput"]["additionalContext"]
            assert ctx.startswith("CHAIR READ BUDGET: 6/12 non-brief reads")
            assert "sonnet scout" in ctx
            assert _decision(out) is None
        elif i >= 12:
            reason = out["hookSpecificOutput"]["permissionDecisionReason"]
            assert _decision(out) == "deny"
            assert reason.startswith(f"CHAIR READ GUARD: {i} non-brief reads")
            assert "sonnet" in reason and ".workflow/scratch" in reason
        else:
            assert out is None, i
    # Brief-sized and .workflow reads stay open after the deny.
    assert _run(tmp_path, _read(big, limit=60)) is None


def test_budget_env_and_malformed_fallback(tmp_path):
    big = _big(tmp_path)
    _chair(tmp_path, session="s-b1")
    env = {"FABLE_ORCH_READ_BUDGET": "2,3"}
    outs = [_run(tmp_path, _read(big), session="s-b1", env=env) for _ in range(3)]
    assert outs[0] is None
    assert "2/3" in outs[1]["hookSpecificOutput"]["additionalContext"]
    assert _decision(outs[2]) == "deny"
    for i, bad in enumerate(("x", "5", "4,2", "0,3", "a,b")):
        sid = f"s-bad{i}"
        _chair(tmp_path, session=sid)
        outs = [_run(tmp_path, _read(big), session=sid,
                     env={"FABLE_ORCH_READ_BUDGET": bad}) for _ in range(6)]
        assert outs[:5] == [None] * 5, bad
        assert "6/12" in outs[5]["hookSpecificOutput"]["additionalContext"], bad


def test_sessions_are_isolated_and_corrupt_sidecar_is_zero(tmp_path):
    big = _big(tmp_path)
    _chair(tmp_path, session="s-a")
    _chair(tmp_path, session="s-b")
    for _ in range(4):
        _run(tmp_path, _read(big), session="s-a")
    _run(tmp_path, _read(big), session="s-b")
    assert (_count(tmp_path, "s-a"), _count(tmp_path, "s-b")) == (4, 1)
    (tmp_path / "fable-orch-reads-s-b.json").write_text("{not json", encoding="utf-8")
    _run(tmp_path, _read(big), session="s-b")
    assert _count(tmp_path, "s-b") == 1


def test_malformed_stdin_and_never_allow(tmp_path):
    assert run_hook(GUARD, raw="not json", tmpdir=tmp_path) is None
    assert run_hook(GUARD, raw="[1, 2]", tmpdir=tmp_path) is None
    _chair(tmp_path)
    big = _big(tmp_path)
    for _ in range(14):
        for call in (_read(big), _read(big, limit=10), _bash("cat big.py")):
            out = _run(tmp_path, call)
            assert _decision(out) in (None, "deny")


def test_clear_and_compact_reset_the_counter(tmp_path):
    for fire in ("clear", "compact"):
        sidecar = tmp_path / f"fable-orch-reads-s-{fire}.json"
        sidecar.write_text(json.dumps({"count": 9, "warned": True}), encoding="utf-8")
        run_hook("inject_instructions.py",
                 {"model": "claude-fable-5", "session_id": f"s-{fire}", "source": fire},
                 env_extra={"CLAUDE_PLUGIN_ROOT": str(REPO)}, tmpdir=tmp_path)
        assert not sidecar.exists(), fire
    sidecar = tmp_path / "fable-orch-reads-s-resume.json"
    sidecar.write_text(json.dumps({"count": 9}), encoding="utf-8")
    run_hook("inject_instructions.py",
             {"model": "claude-fable-5", "session_id": "s-resume", "source": "resume"},
             env_extra={"CLAUDE_PLUGIN_ROOT": str(REPO)}, tmpdir=tmp_path)
    assert sidecar.exists()


@POSIX  # HOME= is meant to redirect expanduser("~"); nt's expanduser ignores it
def test_metrics_record_decisions_without_paths(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _chair(tmp_path)
    env = {"HOME": str(home), "FABLE_ORCH_METRICS": "1",
           "FABLE_ORCH_READ_BUDGET": "1,2"}
    big = _big(tmp_path)
    for call in (_read(big, limit=5), _read(big), _read(big)):
        _run(tmp_path, call, env=env)
    lines = (home / ".claude" / "fable-orch" / "metrics.jsonl").read_text().splitlines()
    recs = [json.loads(l) for l in lines]
    assert [r["decision"] for r in recs] == ["exempt", "warn", "deny"]
    assert all(r["event"] == "chair_read" and r["tool"] == "Read" for r in recs)
    assert str(big) not in "\n".join(lines)


def test_stats_summarizes_chair_reads(tmp_path):
    import subprocess
    import sys

    log = tmp_path / "metrics.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in [
        {"ts": 1.0, "event": "chair_read", "session": "a", "decision": "count"},
        {"ts": 2.0, "event": "chair_read", "session": "a", "decision": "warn"},
        {"ts": 3.0, "event": "chair_read", "session": "a", "decision": "deny"},
        {"ts": 4.0, "event": "chair_read", "session": "b", "decision": "count"},
        {"ts": 5.0, "event": "chair_read", "session": "b", "decision": "exempt"},
    ]) + "\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "stats.py"), str(log)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert ("chair reads (fable top): 4 counted in 2 sessions "
            "(max 3/session), 1 denied in 1") in proc.stdout
