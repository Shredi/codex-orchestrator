"""scripts/ledger_guard_write.py: the PreToolUse guard that stops a
fresh `Write` from clobbering an EXISTING live ledger this session is
not bound to. `.workflow/` is shared and not git-tracked, so nothing
else stands between a Write and another session's (or an earlier
task's) ledger state.
"""
import json
import os
import time

from conftest import REPO, run_hook, write_marker

SCRIPT = "ledger_guard_write.py"


def write_payload(file_path, session_id="test-session", **extra):
    payload = {
        "session_id": session_id,
        "tool_name": "Write",
        "tool_input": {"file_path": str(file_path), "content": "new content\n"},
    }
    payload.update(extra)
    return payload


def is_deny(result):
    return (
        result is not None
        and result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        and result["hookSpecificOutput"]["permissionDecision"] == "deny"
    )


def _named_ledger(root, name, body="- [ ] 1. existing item\n"):
    d = root / ".workflow"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(body, encoding="utf-8")
    return p


# --- DENY: existing foreign live ledger -----------------------------------

def test_deny_existing_ledger_unbound_session(repo_dir, tmp_path):
    # Marker exists (SessionStart fired) but carries no `ledger` key at
    # all — this session never touched any ledger.
    write_marker(tmp_path, time.time())
    ledger = _named_ledger(repo_dir, "LEDGER-other-task.md")
    result = run_hook(SCRIPT, write_payload(ledger), tmpdir=tmp_path)
    assert is_deny(result)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "EXISTING live ledger" in reason
    assert "LEDGER-<topic>.md" in reason
    assert "LEDGER_WRITE_GUARD=0" in reason


def test_deny_bare_ledger_md_the_original_incident(repo_dir, tmp_path):
    # The 2026-08-24 incident verbatim: an unbound session Writes the
    # bare .workflow/LEDGER.md that a parallel session's task lives in.
    write_marker(tmp_path, time.time())
    ledger = _named_ledger(repo_dir, "LEDGER.md")
    assert is_deny(run_hook(SCRIPT, write_payload(ledger), tmpdir=tmp_path))


def test_deny_existing_ledger_bound_to_a_different_file(repo_dir, tmp_path):
    other = _named_ledger(repo_dir, "LEDGER-mine.md")
    write_marker(tmp_path, time.time(), ledger=other)
    target = _named_ledger(repo_dir, "LEDGER-someone-elses.md")
    assert is_deny(run_hook(SCRIPT, write_payload(target), tmpdir=tmp_path))


# --- ALLOW: brand-new file --------------------------------------------------

def test_allow_nonexistent_target(repo_dir, tmp_path):
    write_marker(tmp_path, time.time())
    brand_new = repo_dir / ".workflow" / "LEDGER-fresh-topic.md"
    (repo_dir / ".workflow").mkdir(parents=True, exist_ok=True)
    assert not brand_new.exists()
    assert run_hook(SCRIPT, write_payload(brand_new), tmpdir=tmp_path) is None


# --- ALLOW: session bound to exactly this file ------------------------------

def test_allow_bound_to_same_file(repo_dir, tmp_path):
    ledger = _named_ledger(repo_dir, "LEDGER-mine.md")
    write_marker(tmp_path, time.time(), ledger=ledger)
    assert run_hook(SCRIPT, write_payload(ledger), tmpdir=tmp_path) is None


def test_allow_bound_same_file_case_insensitive_on_windows_semantics():
    # os.path.normcase is a no-op on POSIX, so this exercises the
    # comparison function directly with a folding normcase (nt's
    # actual behavior) monkeypatched in — proving the guard's
    # equality check is normcase-based, not a raw string compare,
    # without requiring a live Windows box to run the suite.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_probe_write_guard", REPO / "scripts" / "ledger_guard_write.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    real_normcase = mod.os.path.normcase
    mod.os.path.normcase = str.lower
    try:
        assert mod._paths_equal(
            "/tmp/repo/.workflow/LEDGER-Topic.md",
            "/tmp/repo/.workflow/ledger-topic.md",
        )
    finally:
        mod.os.path.normcase = real_normcase

    # Sanity: the REAL (unpatched, platform-native) normcase agrees
    # that a path equals itself.
    assert mod._paths_equal(
        "/tmp/repo/.workflow/LEDGER-Topic.md",
        "/tmp/repo/.workflow/LEDGER-Topic.md",
    )


# --- ALLOW: no marker at all (manual install / fail open) ------------------

def test_allow_no_marker_at_all(repo_dir, tmp_path):
    ledger = _named_ledger(repo_dir, "LEDGER-orphan.md")
    # No write_marker() call: no fable-orch-model-*.json exists.
    assert run_hook(SCRIPT, write_payload(ledger), tmpdir=tmp_path) is None


# --- ALLOW: LEDGER_WRITE_GUARD=0 --------------------------------------------

def test_allow_when_disabled_via_env(repo_dir, tmp_path):
    write_marker(tmp_path, time.time())
    ledger = _named_ledger(repo_dir, "LEDGER-other-task.md")
    result = run_hook(SCRIPT, write_payload(ledger),
                      env_extra={"LEDGER_WRITE_GUARD": "0"}, tmpdir=tmp_path)
    assert result is None


# --- ALLOW: not a live ledger name, or outside .workflow/ ------------------

def test_allow_non_ledger_name_inside_workflow(repo_dir, tmp_path):
    write_marker(tmp_path, time.time())
    d = repo_dir / ".workflow"
    d.mkdir(parents=True, exist_ok=True)
    other = d / "notes.md"
    other.write_text("whatever\n", encoding="utf-8")
    assert run_hook(SCRIPT, write_payload(other), tmpdir=tmp_path) is None


def test_allow_live_name_outside_workflow_directory(repo_dir, tmp_path):
    write_marker(tmp_path, time.time())
    outside = repo_dir / "LEDGER.md"  # not inside .workflow/
    outside.write_text("- [ ] 1. open\n", encoding="utf-8")
    assert run_hook(SCRIPT, write_payload(outside), tmpdir=tmp_path) is None


def test_allow_archived_name(repo_dir, tmp_path):
    write_marker(tmp_path, time.time())
    archived = _named_ledger(repo_dir, "LEDGER-done-work-archive.md")
    assert run_hook(SCRIPT, write_payload(archived), tmpdir=tmp_path) is None


def test_allow_non_segment_name(repo_dir, tmp_path):
    write_marker(tmp_path, time.time())
    fake = _named_ledger(repo_dir, "ledgers.md")  # "ledger" not a whole segment
    assert run_hook(SCRIPT, write_payload(fake), tmpdir=tmp_path) is None


# --- fail-open hardening ----------------------------------------------------

def test_malformed_stdin_never_blocks():
    assert run_hook(SCRIPT, raw="not json at all") is None
    assert run_hook(SCRIPT, raw="[1, 2, 3]") is None
    assert run_hook(SCRIPT, raw="") is None


def test_missing_file_path_is_a_noop(tmp_path):
    write_marker(tmp_path, time.time())
    payload = {"session_id": "test-session", "tool_name": "Write", "tool_input": {}}
    assert run_hook(SCRIPT, payload, tmpdir=tmp_path) is None


def test_corrupt_marker_json_still_denies(repo_dir, tmp_path):
    ledger = _named_ledger(repo_dir, "LEDGER-x.md")
    marker_path = tmp_path / "fable-orch-model-test-session.json"
    marker_path.write_text("not json at all", encoding="utf-8")
    # A marker present-but-corrupt still counts as "no known binding" ->
    # _bound_path returns None -> deny (an unreadable marker must not
    # silently grant "you're bound to it").
    assert is_deny(run_hook(SCRIPT, write_payload(ledger), tmpdir=tmp_path))


def test_non_dict_marker_json_still_denies(repo_dir, tmp_path):
    ledger = _named_ledger(repo_dir, "LEDGER-x.md")
    marker_path = tmp_path / "fable-orch-model-test-session.json"
    marker_path.write_text("[1, 2, 3]", encoding="utf-8")
    assert is_deny(run_hook(SCRIPT, write_payload(ledger), tmpdir=tmp_path))


# --- metrics -----------------------------------------------------------------

def test_deny_emits_write_deny_metric(repo_dir, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    write_marker(tmp_path, time.time())
    ledger = _named_ledger(repo_dir, "LEDGER-other-task.md")
    env = {"HOME": str(home), "FABLE_ORCH_METRICS": "1"}
    result = run_hook(SCRIPT, write_payload(ledger), env_extra=env, tmpdir=tmp_path)
    assert is_deny(result)
    log = home / ".claude" / "fable-orch" / "metrics.jsonl"
    assert log.exists()
    lines = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
    events = [l for l in lines if l["event"] == "write_deny"]
    assert len(events) == 1
    assert events[0]["path"] == "LEDGER-other-task.md"
    assert events[0]["bound"] is None
