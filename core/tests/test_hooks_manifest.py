"""The registration contract: every other test drives the scripts
directly, so a broken hooks.json (dropped matcher entry, mistyped
script path) would ship green. This file pins the manifest itself."""
import json
import re

from conftest import REPO


def _manifest():
    with open(REPO / "hooks" / "hooks.json", encoding="utf-8") as f:
        return json.load(f)["hooks"]


def test_all_six_events_registered():
    assert set(_manifest()) == {
        "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
        "Stop", "SessionEnd",
    }


def test_userpromptsubmit_runs_the_cold_cache_guard():
    # No matcher: the event fires on every prompt, and the guard decides.
    entries = _manifest()["UserPromptSubmit"]
    assert all("matcher" not in e for e in entries)
    commands = [h["command"] for e in entries for h in e["hooks"]]
    assert any("cold_cache_guard.py" in c for c in commands)
    # This one runs BEFORE every prompt, so its timeout has to be short.
    timeouts = [h["timeout"] for e in entries for h in e["hooks"]]
    assert all(t <= 10 for t in timeouts)


def test_every_hook_command_script_exists():
    for entries in _manifest().values():
        for entry in entries:
            for hook in entry["hooks"]:
                cmd = hook["command"]
                assert cmd.startswith('python3 "${CLAUDE_PLUGIN_ROOT}/')
                rel = cmd.split("${CLAUDE_PLUGIN_ROOT}/", 1)[1].rstrip('"')
                assert (REPO / rel).is_file(), f"missing script: {rel}"
                assert isinstance(hook.get("timeout"), int)


def _matcher_for(event, tool):
    """The matcher entry in `event` that actually matches `tool`, found by
    CONTENT rather than positional index — a manifest that grows more than
    one entry per event (as PostToolUse joins PreToolUse here) must not
    silently pin whichever entry happens to sit at [0]."""
    for entry in _manifest().get(event, []):
        matcher = entry.get("matcher")
        if matcher and re.compile(matcher).search(tool):
            return matcher
    return None


def test_pretooluse_matcher_covers_the_gated_tools():
    matcher = _matcher_for("PreToolUse", "Agent")
    assert matcher is not None, "no PreToolUse matcher covers Agent"
    pattern = re.compile(matcher)
    for tool in ("Agent", "Task", "Workflow", "TaskCreate"):
        assert pattern.search(tool), f"matcher misses {tool}"
    for tool in ("TaskUpdate", "TaskList", "AgentOutput", "WorkflowX"):
        assert not pattern.search(tool), f"matcher over-matches {tool}"


def test_posttooluse_matcher_covers_ledger_bind():
    matcher = _matcher_for("PostToolUse", "Write")
    assert matcher is not None, "no PostToolUse matcher covers Write"
    pattern = re.compile(matcher)
    for tool in ("Write", "Edit", "MultiEdit"):
        assert pattern.search(tool), f"matcher misses {tool}"
    for tool in ("Read", "Bash", "NotebookEdit", "MultiEditX"):
        assert not pattern.search(tool), f"matcher over-matches {tool}"


def test_pretooluse_runs_write_guard():
    entries = [e for e in _manifest()["PreToolUse"]
               if re.compile(e["matcher"]).search("Write")]
    assert entries, "no PreToolUse matcher covers Write"
    commands = [h["command"] for e in entries for h in e["hooks"]]
    assert any("ledger_guard_write.py" in c for c in commands)


def test_posttooluse_runs_ledger_bind():
    entry = next(e for e in _manifest()["PostToolUse"]
                 if re.compile(e["matcher"]).search("Write"))
    commands = [h["command"] for h in entry["hooks"]]
    assert any("ledger_bind.py" in c for c in commands)
