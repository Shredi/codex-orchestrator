"""FABLE_ORCH_MODE=plain: the orchestration layer switches off, the
destructive-command guard does not.

Plain mode exists for token-cheap dispatched turns (e.g. a Telegram
front handing a small task to `claude -p`): no chair profile, no ledger
gates, no cold-cache guard. The rm guard is the one piece that must
survive — it is the reason the plugin cannot simply be disabled for
those turns — so every test pair below shows the orchestration hook
going quiet next to the guard still firing under the same env.
"""
import json
import time

import pytest

from conftest import REPO, SCRIPTS, run_hook, write_ledger, write_marker

PLAIN = {"FABLE_ORCH_MODE": "plain"}
INCIDENT = 'bash -c \'rm -rf -- "$1"/*\' x ""'
PREFIX = 'export PATH="$HOME/.claude/guard/bin:$PATH"; '
HOUR = 3600.0


def _ctx(result):
    assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    return result["hookSpecificOutput"]["additionalContext"]


# --- SessionStart: profile injection ----------------------------------

@pytest.mark.parametrize("value", ["plain", "PLAIN", " Plain "])
def test_inject_sends_one_line_instead_of_a_profile(tmp_path, value):
    result = run_hook("inject_instructions.py",
                      {"session_id": "s-plain", "source": "startup",
                       "model": "claude-opus-5-5"},
                      env_extra={"FABLE_ORCH_MODE": value}, tmpdir=tmp_path)
    ctx = _ctx(result)
    assert ctx.startswith("Orchestrator plain mode (FABLE_ORCH_MODE=plain)")
    assert "destructive-command guard stays active" in ctx
    assert len(ctx) < 300


def test_inject_plain_writes_no_marker(tmp_path):
    # A marker `profile` would claim an injection that never happened; a
    # later orchestrated --resume would then get a switch delta on top of
    # no core. No marker -> that resume gets the full core.
    run_hook("inject_instructions.py",
             {"session_id": "s-plain", "source": "startup"},
             env_extra=PLAIN, tmpdir=tmp_path)
    assert not (tmp_path / "fable-orch-model-s-plain.json").exists()


def test_orchestrated_resume_after_plain_gets_the_full_core(tmp_path):
    payload = {"session_id": "s-up", "model": "claude-opus-5-5"}
    run_hook("inject_instructions.py", dict(payload, source="startup"),
             env_extra=PLAIN, tmpdir=tmp_path)
    ctx = _ctx(run_hook("inject_instructions.py", dict(payload, source="resume"),
                        tmpdir=tmp_path))
    core = (REPO / "instructions" / "dynamic-workflow-opus-primary.md"
            ).read_text(encoding="utf-8")
    assert ctx == core


@pytest.mark.parametrize("value", ["", "orch", "orchestrated", "plainish", "0"])
def test_any_other_value_keeps_the_profile(tmp_path, value):
    ctx = _ctx(run_hook("inject_instructions.py",
                        {"session_id": "s-orch", "source": "startup",
                         "model": "claude-opus-5-5"},
                        env_extra={"FABLE_ORCH_MODE": value}, tmpdir=tmp_path))
    core = (REPO / "instructions" / "dynamic-workflow-opus-primary.md"
            ).read_text(encoding="utf-8")
    assert ctx == core
    assert (tmp_path / "fable-orch-model-s-orch.json").exists()


# --- ledger gates -----------------------------------------------------

def _is_deny(result):
    return (result is not None
            and result["hookSpecificOutput"]["permissionDecision"] == "deny")


def test_spawn_guard_is_off(repo_dir, tmp_path):
    payload = {"tool_name": "Agent", "tool_input": {"prompt": "x" * 2000},
               "cwd": str(repo_dir), "session_id": "test-session"}
    assert _is_deny(run_hook("ledger_guard_spawn.py", payload, tmpdir=tmp_path))
    assert run_hook("ledger_guard_spawn.py", payload,
                    env_extra=PLAIN, tmpdir=tmp_path) is None


def test_write_guard_is_off(repo_dir, tmp_path):
    write_marker(tmp_path, time.time())
    ledger = repo_dir / ".workflow" / "LEDGER-other.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("- [ ] 1. existing\n", encoding="utf-8")
    payload = {"session_id": "test-session", "tool_name": "Write",
               "tool_input": {"file_path": str(ledger), "content": "new\n"}}
    assert _is_deny(run_hook("ledger_guard_write.py", payload, tmpdir=tmp_path))
    assert run_hook("ledger_guard_write.py", payload,
                    env_extra=PLAIN, tmpdir=tmp_path) is None


def test_bind_is_off(repo_dir, tmp_path):
    marker = write_marker(tmp_path, time.time())
    ledger = repo_dir / ".workflow" / "LEDGER-topic.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("- [ ] 1. item\n", encoding="utf-8")
    run_hook("ledger_bind.py",
             {"session_id": "test-session", "tool_name": "Write",
              "tool_input": {"file_path": str(ledger)}},
             env_extra=PLAIN, tmpdir=tmp_path)
    assert "ledger" not in json.loads(marker.read_text(encoding="utf-8"))


def test_stop_guard_is_off_and_stamps_nothing(repo_dir, tmp_path):
    write_ledger(repo_dir, "- [ ] 1. still open\n")
    payload = {"cwd": str(repo_dir), "session_id": "test-session"}
    result = run_hook("ledger_guard_stop.py", payload, tmpdir=tmp_path)
    assert result is not None and result["decision"] == "block"
    marker = write_marker(tmp_path, started=time.time() - HOUR)
    before = marker.read_text(encoding="utf-8")
    assert run_hook("ledger_guard_stop.py", payload,
                    env_extra=PLAIN, tmpdir=tmp_path) is None
    assert marker.read_text(encoding="utf-8") == before  # no last_stop stamp


# --- cold-cache guard -------------------------------------------------

def test_cold_cache_guard_is_off(tmp_path):
    def setup():
        write_marker(tmp_path, started=time.time() - 30 * HOUR,
                     session="test-session", model="claude-fable-5-1",
                     profile="fable", last_stop=time.time() - 9 * HOUR)
        t = tmp_path / "t.jsonl"
        t.write_text(json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 100, "cache_creation_input_tokens": 1000,
            "cache_read_input_tokens": 400000}}}) + "\n", encoding="utf-8")
        return {"session_id": "test-session", "cwd": str(tmp_path),
                "prompt": "go on", "hook_event_name": "UserPromptSubmit",
                "transcript_path": str(t)}
    payload = setup()
    assert run_hook("cold_cache_guard.py", payload,
                    env_extra=PLAIN, tmpdir=tmp_path) is None
    payload = setup()
    result = run_hook("cold_cache_guard.py", payload, tmpdir=tmp_path)
    assert result is not None and result.get("decision") == "block"


# --- the destructive-command guard stays fully active ------------------

def _decide(command, env=None):
    result = run_hook("destructive_guard.py",
                      {"session_id": "test-session", "tool_name": "Bash",
                       "cwd": "/Users/tester/Documents/git/project",
                       "tool_input": {"command": command}},
                      env_extra=env)
    return result["hookSpecificOutput"] if result else None


def test_destructive_guard_still_denies_in_plain_mode():
    out = _decide(INCIDENT, PLAIN)
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"].startswith("DESTRUCTIVE GUARD: ")


def test_destructive_guard_still_prefixes_path_in_plain_mode():
    out = _decide("rm -rf ./build", PLAIN)
    assert out["updatedInput"]["command"] == PREFIX + "rm -rf ./build"


def test_shim_install_and_path_export_still_run_in_plain_mode(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env_file = tmp_path / "session-env.sh"
    env_file.write_text("", encoding="utf-8")
    run_hook("destructive_guard_install.py",
             {"session_id": "s-install", "source": "startup"},
             env_extra=dict(PLAIN, HOME=str(home), USERPROFILE=str(home),
                            CLAUDE_PLUGIN_ROOT=str(REPO),
                            CLAUDE_ENV_FILE=str(env_file)),
             tmpdir=tmp_path)
    shim = home / ".claude" / "guard" / "bin" / "rm"
    assert shim.read_text(encoding="utf-8") == \
        (REPO / "guard" / "rm").read_text(encoding="utf-8")
    assert "guard/bin:$PATH" in env_file.read_text(encoding="utf-8")


def test_destructive_guard_scripts_never_read_the_switch():
    # The guarantee is structural, not a branch that could regress: the
    # two rm-guard hooks do not know plain mode exists.
    for name in ("destructive_guard.py", "destructive_guard_install.py"):
        assert "FABLE_ORCH_MODE" not in (SCRIPTS / name).read_text(encoding="utf-8")
