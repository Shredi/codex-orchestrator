"""Cold-cache guard (UserPromptSubmit) + the Stop hook's last_stop stamp.

The guard's whole job is a decision made from two numbers — how long the
session sat idle, and how much context the next request would re-cache —
so every test here drives it as a subprocess with those two numbers set
explicitly.
"""
import json
import os
import time

from conftest import POSIX, REPO, run_hook, write_marker

SCRIPT = "cold_cache_guard.py"
SESSION = "test-session"
HOUR = 3600.0


def write_transcript(tmp_path, tokens, name="transcript.jsonl", sidechain=False):
    """A minimal transcript whose last assistant message reports `tokens`
    of context, split across the three usage fields the guard sums."""
    read = max(0, tokens - 1100)
    rec = {
        "type": "assistant",
        "message": {"usage": {"input_tokens": 100,
                              "cache_creation_input_tokens": 1000,
                              "cache_read_input_tokens": read}},
    }
    if sidechain:
        rec["isSidechain"] = True
    path = tmp_path / name
    path.write_text(
        json.dumps({"type": "user", "message": {"role": "user"}}) + "\n"
        + json.dumps(rec) + "\n",
        encoding="utf-8",
    )
    return path


def prompt_payload(tmp_path, prompt="do the thing", transcript=None, **extra):
    payload = {"session_id": SESSION, "cwd": str(tmp_path), "prompt": prompt,
               "hook_event_name": "UserPromptSubmit"}
    if transcript is not None:
        payload["transcript_path"] = str(transcript)
    payload.update(extra)
    return payload


def cold_marker(tmp_path, idle_hours=9.0, **extra):
    """A marker for a session last active `idle_hours` ago, carrying the
    keys a real session's marker carries."""
    return write_marker(tmp_path, started=time.time() - 30 * HOUR,
                        session=SESSION, model="claude-fable-5-1",
                        profile="fable", last_stop=time.time() - idle_hours * HOUR,
                        **extra)


def marker_body(marker):
    return json.loads(marker.read_text(encoding="utf-8"))


def blocks(result):
    return result is not None and result.get("decision") == "block"


# --- item 1: the Stop hook stamps last_stop --------------------------

def test_stop_hook_stamps_last_stop(repo_dir, tmp_path):
    marker = write_marker(tmp_path, started=time.time() - HOUR, session=SESSION,
                          model="claude-fable-5-1", profile="fable")
    before = time.time()
    run_hook("ledger_guard_stop.py",
             {"cwd": str(repo_dir), "session_id": SESSION}, tmpdir=tmp_path)
    body = marker_body(marker)
    assert before <= body["last_stop"] <= time.time() + 1
    # every pre-existing key survives the rewrite
    assert body["profile"] == "fable" and body["model"] == "claude-fable-5-1"
    assert body["started"] < before


def test_stop_hook_never_creates_a_marker(repo_dir, tmp_path):
    # Marker presence flips the write guard from fail-open to enforcing;
    # a Stop hook must not conjure one for a manual install.
    run_hook("ledger_guard_stop.py",
             {"cwd": str(repo_dir), "session_id": SESSION}, tmpdir=tmp_path)
    assert not (tmp_path / f"fable-orch-model-{SESSION}.json").exists()


# --- items 3, 5: the always-pass paths -------------------------------

def test_small_gap_passes_and_stamps_last_prompt(tmp_path):
    marker = cold_marker(tmp_path, idle_hours=0.1)
    transcript = write_transcript(tmp_path, 400000)
    assert run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                    tmpdir=tmp_path) is None
    assert marker_body(marker)["last_prompt"] > time.time() - 30


def test_slash_command_passes_even_when_cold_and_huge(tmp_path):
    cold_marker(tmp_path)
    transcript = write_transcript(tmp_path, 800000)
    for command in ("/clear", "/compact", "/exit", "/model opus"):
        assert run_hook(SCRIPT,
                        prompt_payload(tmp_path, command, transcript),
                        tmpdir=tmp_path) is None, command


def test_clear_leaves_the_session_warm(tmp_path):
    # /clear is what the block message asks for: the prompt right after it
    # must not be nagged again.
    cold_marker(tmp_path)
    transcript = write_transcript(tmp_path, 800000)
    run_hook(SCRIPT, prompt_payload(tmp_path, "/clear", transcript), tmpdir=tmp_path)
    assert run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                    tmpdir=tmp_path) is None


def test_empty_prompt_passes(tmp_path):
    cold_marker(tmp_path)
    transcript = write_transcript(tmp_path, 800000)
    assert run_hook(SCRIPT, prompt_payload(tmp_path, "   ", transcript),
                    tmpdir=tmp_path) is None


def test_kill_switch_passes(tmp_path):
    cold_marker(tmp_path)
    transcript = write_transcript(tmp_path, 800000)
    assert run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                    env_extra={"FABLE_ORCH_COLD_GUARD": "0"},
                    tmpdir=tmp_path) is None


@POSIX  # POSIX shebang script on PATH stands in for `ps`; chmod +x
def test_teammate_prompt_is_never_blocked(tmp_path):
    cold_marker(tmp_path)
    transcript = write_transcript(tmp_path, 800000)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ps = bin_dir / "ps"
    ps.write_text(
        "#!/usr/bin/env python3\n"
        "print('1 claude --agent-id worker@session-t --agent-name worker')\n",
        encoding="utf-8",
    )
    os.chmod(ps, 0o755)
    env = {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}
    assert run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                    env_extra=env, tmpdir=tmp_path) is None


def test_missing_marker_passes_and_creates_nothing(tmp_path):
    transcript = write_transcript(tmp_path, 800000)
    assert run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                    tmpdir=tmp_path) is None
    # Deliberate: inventing a marker here would flip the write guard from
    # fail-open to enforcing and the stop guard to binding-only.
    assert not (tmp_path / f"fable-orch-model-{SESSION}.json").exists()


def test_marker_without_a_baseline_passes_and_seeds_one(tmp_path):
    marker = write_marker(tmp_path, started=time.time() - 30 * HOUR,
                          session=SESSION, profile="fable")
    transcript = write_transcript(tmp_path, 800000)
    assert run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                    tmpdir=tmp_path) is None
    assert marker_body(marker)["last_prompt"] > time.time() - 30


def test_missing_transcript_passes(tmp_path):
    cold_marker(tmp_path)
    assert run_hook(SCRIPT,
                    prompt_payload(tmp_path, transcript=tmp_path / "gone.jsonl"),
                    tmpdir=tmp_path) is None
    assert run_hook(SCRIPT, prompt_payload(tmp_path), tmpdir=tmp_path) is None


def test_sidechain_usage_is_not_this_sessions_context(tmp_path):
    # A subagent's usage interleaved into the chair transcript must not
    # stand in for the chair's own context.
    cold_marker(tmp_path)
    transcript = write_transcript(tmp_path, 800000, sidechain=True)
    assert run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                    tmpdir=tmp_path) is None


# --- items 4, 6: the block band --------------------------------------

def test_block_band_blocks_with_computed_numbers(tmp_path):
    marker = cold_marker(tmp_path, idle_hours=9.2)
    transcript = write_transcript(tmp_path, 412000)
    result = run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                      tmpdir=tmp_path)
    assert blocks(result)
    reason = result["reason"]
    assert "~412k tokens" in reason          # input+creation+read, summed
    assert "9 h 12 min" in reason            # gap, not hard-coded
    assert "~$8.24 list" in reason           # 412k at the 1h write rate
    assert "~165k output-token equivalents" in reason
    assert "/clear" in reason and "none bound" in reason
    # The ack path accepts ANY prompt inside the window, and says so.
    assert "send any prompt again within 3 min" in reason
    assert len(reason.splitlines()) <= 6
    body = marker_body(marker)
    assert body["cold_ack"] > time.time() - 30
    assert body["cold_ctx"] == 412000
    # A blocked prompt never reached the model, so the cache stayed cold:
    # the gap must keep growing rather than reset.
    assert "last_prompt" not in body
    assert body["profile"] == "fable"  # keys carried forward


def test_block_names_the_bound_ledger(tmp_path):
    ledger = tmp_path / "LEDGER-topic.md"
    ledger.write_text("- [ ] 1. open\n", encoding="utf-8")
    cold_marker(tmp_path, ledger=str(ledger))
    transcript = write_transcript(tmp_path, 400000)
    result = run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                      tmpdir=tmp_path)
    assert blocks(result) and str(ledger) in result["reason"]


def test_block_threshold_is_configurable(tmp_path):
    cold_marker(tmp_path)
    transcript = write_transcript(tmp_path, 60000)
    env = {"FABLE_ORCH_COLD_BLOCK_TOKENS": "50000"}
    assert blocks(run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                           env_extra=env, tmpdir=tmp_path))


def test_cold_minutes_is_configurable(tmp_path):
    cold_marker(tmp_path, idle_hours=0.5)  # 30 min: warm by default
    transcript = write_transcript(tmp_path, 400000)
    assert run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                    tmpdir=tmp_path) is None
    cold_marker(tmp_path, idle_hours=0.5)
    assert blocks(run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                           env_extra={"FABLE_ORCH_COLD_MIN": "20"},
                           tmpdir=tmp_path))


def test_last_prompt_also_counts_as_activity(tmp_path):
    # No Stop stamp at all (the hook never fired): a recent last_prompt
    # still says the session is warm.
    write_marker(tmp_path, started=time.time() - 30 * HOUR, session=SESSION,
                 last_prompt=time.time() - 60)
    transcript = write_transcript(tmp_path, 800000)
    assert run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                    tmpdir=tmp_path) is None


# --- item 7: the opt-through -----------------------------------------

def test_ack_within_window_passes_and_clears(tmp_path):
    marker = cold_marker(tmp_path)
    transcript = write_transcript(tmp_path, 400000)
    assert blocks(run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                           tmpdir=tmp_path))
    assert run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                    tmpdir=tmp_path) is None
    body = marker_body(marker)
    assert "cold_ack" not in body and "cold_ctx" not in body
    assert body["last_prompt"] > time.time() - 30


def test_ack_after_window_is_evaluated_afresh(tmp_path):
    marker = cold_marker(tmp_path)
    transcript = write_transcript(tmp_path, 400000)
    assert blocks(run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                           tmpdir=tmp_path))
    body = marker_body(marker)
    body["cold_ack"] = time.time() - 600  # 10 min ago: window long gone
    (tmp_path / f"fable-orch-model-{SESSION}.json").write_text(
        json.dumps(body), encoding="utf-8")
    assert blocks(run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                           tmpdir=tmp_path))


def test_ack_window_is_configurable(tmp_path):
    marker = cold_marker(tmp_path)
    transcript = write_transcript(tmp_path, 400000)
    assert blocks(run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                           tmpdir=tmp_path))
    body = marker_body(marker)
    body["cold_ack"] = time.time() - 120  # 2 min ago
    (tmp_path / f"fable-orch-model-{SESSION}.json").write_text(
        json.dumps(body), encoding="utf-8")
    assert blocks(run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                           env_extra={"FABLE_ORCH_COLD_ACK_MIN": "1"},
                           tmpdir=tmp_path))


# --- item 8: the warn band -------------------------------------------

def test_warn_band_warns_without_blocking(tmp_path):
    ledger = tmp_path / "LEDGER-topic.md"
    ledger.write_text("- [ ] 1. open\n", encoding="utf-8")
    marker = cold_marker(tmp_path, idle_hours=2.5, ledger=str(ledger))
    transcript = write_transcript(tmp_path, 82000)
    result = run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                      tmpdir=tmp_path)
    assert result is not None and "decision" not in result
    assert "~82k tokens" in result["systemMessage"]
    assert "2 h 30 min" in result["systemMessage"]
    assert "~$1.64 list" in result["systemMessage"]
    extra = result["hookSpecificOutput"]
    assert extra["hookEventName"] == "UserPromptSubmit"
    assert extra["additionalContext"].startswith("Cold-cache resume after 2 h 30 min")
    assert str(ledger) in extra["additionalContext"]
    # A warned prompt DOES reach the model, so the clock resets.
    assert marker_body(marker)["last_prompt"] > time.time() - 30


def test_below_warn_band_is_silent(tmp_path):
    cold_marker(tmp_path)
    transcript = write_transcript(tmp_path, 30000)
    assert run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                    tmpdir=tmp_path) is None


def test_warn_band_without_a_ledger_still_gives_context(tmp_path):
    cold_marker(tmp_path, idle_hours=2.0)
    transcript = write_transcript(tmp_path, 82000)
    result = run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                      tmpdir=tmp_path)
    assert "fresh session" in result["hookSpecificOutput"]["additionalContext"]


# --- item 9: metrics --------------------------------------------------

def test_metrics_record_the_bands(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {"FABLE_ORCH_METRICS": "1", "HOME": str(home), "USERPROFILE": str(home)}
    cold_marker(tmp_path)
    transcript = write_transcript(tmp_path, 412000)
    run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
             env_extra=env, tmpdir=tmp_path)   # block
    run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
             env_extra=env, tmpdir=tmp_path)   # ack
    lines = [json.loads(l) for l in
             (home / ".claude" / "fable-orch" / "metrics.jsonl")
             .read_text(encoding="utf-8").splitlines() if l.strip()]
    events = {rec["event"]: rec for rec in lines}
    assert set(events) == {"cold_block", "cold_ack"}
    for rec in events.values():
        assert rec["ctx_tokens"] == 412000
        assert rec["est_usd"] == 8.24
        assert rec["est_out_equiv"] == 164800
        assert rec["gap_min"] > 500


def test_metrics_opt_out_is_honoured(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cold_marker(tmp_path)
    transcript = write_transcript(tmp_path, 412000)
    run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
             env_extra={"FABLE_ORCH_METRICS": "0", "HOME": str(home)},
             tmpdir=tmp_path)
    assert not (home / ".claude" / "fable-orch" / "metrics.jsonl").exists()


# --- robustness: the guard must never wedge a prompt ------------------

def test_garbage_payload_passes(tmp_path):
    assert run_hook(SCRIPT, raw="not json at all", tmpdir=tmp_path) is None
    assert run_hook(SCRIPT, raw="[1, 2, 3]", tmpdir=tmp_path) is None


def test_corrupt_marker_and_transcript_pass(tmp_path):
    (tmp_path / f"fable-orch-model-{SESSION}.json").write_text(
        "{not json", encoding="utf-8")
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{oops\nnot json either\n", encoding="utf-8")
    assert run_hook(SCRIPT, prompt_payload(tmp_path, transcript=bad),
                    tmpdir=tmp_path) is None


def test_only_the_transcript_tail_is_read(tmp_path):
    # A 5 MB transcript whose HEAD claims a huge context and whose tail
    # reports a small one: the guard must answer from the tail (and stay
    # fast doing it).
    cold_marker(tmp_path)
    path = tmp_path / "big.jsonl"
    head = json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": 900000, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0}}})
    filler = json.dumps({"type": "user", "message": {"role": "user"},
                         "pad": "x" * 2000})
    tail = json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": 100, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 9900}}})
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "\n")
        for _ in range(2600):  # ~5 MB, well past the 2 MB tail window
            f.write(filler + "\n")
        f.write(tail + "\n")
    assert path.stat().st_size > 4 * 1024 * 1024
    started = time.time()
    assert run_hook(SCRIPT, prompt_payload(tmp_path, transcript=path),
                    tmpdir=tmp_path) is None
    assert time.time() - started < 5  # includes interpreter startup


# --- the resume path: SessionStart must not wipe the baseline --------

def test_resume_of_a_cold_session_is_still_guarded(tmp_path):
    # End to end: a stamped marker, the SessionStart fire a `claude
    # --resume` produces, then the first prompt. Before the injector
    # carried the stamps forward this passed silently — the resume being
    # the one moment the whole 400k context is provably re-cached.
    cold_marker(tmp_path)
    transcript = write_transcript(tmp_path, 412000)
    run_hook("inject_instructions.py",
             {"model": "claude-fable-5", "session_id": SESSION,
              "source": "resume"},
             env_extra={"CLAUDE_PLUGIN_ROOT": str(REPO)}, tmpdir=tmp_path)
    assert blocks(run_hook(SCRIPT, prompt_payload(tmp_path, transcript=transcript),
                           tmpdir=tmp_path))


# --- a block the marker cannot record must not be a block ------------

def _module(tmp_path, monkeypatch):
    """Import the guard in-process, with its marker pinned to tmp_path.

    The marker-write failure below cannot be staged from outside: an
    unwritable temp dir makes tempfile.gettempdir() relocate, so the
    marker is simply not found rather than not written. Only the guard's
    OWN path helper is redirected — patching tempfile.gettempdir here
    would reach the whole pytest process (it did: it moved conftest's
    cached `ps` shim into a tmp_path that pytest then deleted, and the
    teammate tests started failing three files later)."""
    import importlib.util
    from conftest import SCRIPTS
    spec = importlib.util.spec_from_file_location("cold_cache_guard",
                                                  SCRIPTS / SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(
        mod, "session_marker_path",
        lambda sid: str(tmp_path / f"fable-orch-model-{sid}.json"))
    return mod


def test_stamp_reports_failure(tmp_path, monkeypatch):
    mod = _module(tmp_path, monkeypatch)
    cold_marker(tmp_path)
    assert mod.stamp(SESSION, last_prompt=1.0) is True
    monkeypatch.setattr(mod.os, "replace",
                        lambda *a: (_ for _ in ()).throw(OSError(28, "ENOSPC")))
    assert mod.stamp(SESSION, last_prompt=2.0) is False
    # and no half-written sibling is left in the temp dir
    assert not list(tmp_path.glob("fable-orch-model-*.tmp.json"))


def test_block_that_cannot_be_recorded_fails_open(tmp_path, monkeypatch, capsys):
    # The block advertises an escape hatch that lives in the marker. If
    # the marker cannot be written, that hatch never opens and the block
    # would repeat on every prompt — so the guard passes instead and says
    # so in the metrics.
    mod = _module(tmp_path, monkeypatch)
    monkeypatch.setattr(mod.os, "replace",
                        lambda *a: (_ for _ in ()).throw(OSError(28, "ENOSPC")))
    logged = []
    monkeypatch.setattr(mod, "_metric",
                        lambda event, sid=None, **kw: logged.append(event))
    monkeypatch.setattr(mod, "_is_teammate_session", lambda: False)
    cold_marker(tmp_path)
    transcript = write_transcript(tmp_path, 412000)
    mod.run_guard(prompt_payload(tmp_path, transcript=transcript))
    assert capsys.readouterr().out == ""      # nothing printed: no block
    assert logged == ["cold_stamp_failed"]
