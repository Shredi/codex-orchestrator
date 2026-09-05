"""Adapter tests: Codex hook payload -> core guard -> Codex hook output.

Every payload starts from a fixture in `tests/fixtures/` — CAPTURED
LIVE from Codex CLI 0.153.4 on 2026-09-05 (see that directory's README
and `<claude-repo>/.workflow/scratch/codex-probe/probe.log`), with only
the session id and the tool bodies normalised for the tests.

The core guards run as real subprocesses, exactly as the adapter runs
them in production. `TMPDIR`/`TEMP`/`TMP` point at the test sandbox so
the session markers the guards read and write stay inside it, and
`FABLE_ORCH_TEAMMATE_{STOP,INJECT}=1` short-circuit the guards' "am I a
teammate?" process-tree walk — without it the whole suite's result
depends on whether it was started from inside an agent session.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ADAPTER = REPO / "scripts" / "codex_adapter.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
HOOKS_JSON = REPO / "hooks" / "hooks.json"

# Anything of the core's (or this adapter's) that the developer's shell
# might have exported and that would change a verdict here.
STRIP_ENV = [
    "LEDGER_GUARD_THRESHOLD", "LEDGER_GUARD_TASKS", "LEDGER_GUARD_STOP_MODE",
    "LEDGER_WRITE_GUARD", "FABLE_ORCH_METRICS", "FABLE_ORCH_PROFILE",
    "FABLE_ORCH_COLD_GUARD", "FABLE_ORCH_COLD_MIN",
    "FABLE_ORCH_COLD_BLOCK_TOKENS", "FABLE_ORCH_COLD_WARN_TOKENS",
    "FABLE_ORCH_COLD_ACK_MIN", "FABLE_ORCH_SWARM_CLEANUP",
    "FABLE_ORCH_SWARM_MAX_IDLE_H", "FABLE_ORCH_TEAMMATE_IDLE_H",
    "FABLE_ORCH_TEAMMATE_IDLE_RATE", "FABLE_ORCH_TEAMMATE_STOP",
    "FABLE_ORCH_TEAMMATE_INJECT", "CLAUDE_CONFIG_DIR", "CLAUDE_PLUGIN_ROOT",
    "TMUX_TMPDIR", "CODEX_ADAPTER_HARNESS", "CODEX_ADAPTER_TOOL_GATE",
    "CODEX_ADAPTER_EXIT2", "CODEX_ADAPTER_DEBUG",
]

SESSION = "codex-fixture-session"


# --- harness ----------------------------------------------------------

def fixture(name, **overrides):
    """A captured payload, with the live session id swapped for a
    deterministic one (the markers the guards read are keyed on it)."""
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    data["session_id"] = SESSION
    data.update(overrides)
    return data


def run_adapter(guard, payload, tmpdir, env_extra=None):
    """Run the adapter as Codex would. Returns (rc, parsed_stdout, stderr)."""
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    env["FABLE_ORCH_METRICS"] = "0"        # no writes to the real ~/.claude
    env["FABLE_ORCH_SWARM_CLEANUP"] = "0"  # no reaping of real tmux servers
    env["FABLE_ORCH_TEAMMATE_STOP"] = "1"  # skip the teammate process walk
    env["FABLE_ORCH_TEAMMATE_INJECT"] = "1"
    env["CLAUDE_CONFIG_DIR"] = str(Path(tmpdir) / "cfg")
    for var in ("TMPDIR", "TEMP", "TMP"):
        env[var] = str(tmpdir)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(ADAPTER), guard],
        input=json.dumps(payload), capture_output=True, text=True,
        env=env, timeout=60,
    )
    out = (proc.stdout or "").strip()
    return proc.returncode, (json.loads(out) if out else None), proc.stderr


@pytest.fixture
def sandbox(tmp_path):
    """A temp dir that is both $TMPDIR (session markers) and a repo root."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    return tmp_path, repo, tmp


def write_marker(tmp, session=SESSION, **body):
    path = Path(tmp) / f"fable-orch-model-{session}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def write_ledger(repo, name="LEDGER-topic.md", body="- [ ] 1. open item\n"):
    d = Path(repo) / ".workflow"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(body, encoding="utf-8")
    return path


def patch_text(*paths, op="Update File"):
    """A SURGICAL patch: one context line kept, one line added."""
    hunks = "".join(f"*** {op}: {p}\n@@\n context\n+new\n" for p in paths)
    return f"*** Begin Patch\n{hunks}*** End Patch\n"


def wipe_text(path):
    """An `Update File` hunk that deletes every line the file has — the
    live 2026-09-05 bypass: a patch that is really a whole-file replace."""
    body = "".join(f"-{line}\n" for line in
                   Path(path).read_text(encoding="utf-8").splitlines())
    return (f"*** Begin Patch\n*** Update File: {path}\n@@\n{body}+x\n"
            "*** End Patch\n")


@pytest.fixture(scope="session")
def adapter_mod():
    """The adapter imported in-process, for the pure-function tests."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("codex_adapter", ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- PreToolUse: spawn ------------------------------------------------

def test_spawn_without_ledger_is_denied(sandbox):
    _, repo, tmp = sandbox
    payload = fixture("pre_tool_use_spawn", cwd=str(repo))
    payload["tool_input"]["prompt"] = "x" * 2000  # over the 1500-char gate
    rc, out, _ = run_adapter("ledger_guard_spawn", payload, tmp)

    assert rc == 0
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "LEDGER GUARD" in hso["permissionDecisionReason"]
    assert ".workflow/LEDGER-<topic>.md" in hso["permissionDecisionReason"]


def test_spawn_with_open_ledger_passes(sandbox):
    _, repo, tmp = sandbox
    write_ledger(repo)
    payload = fixture("pre_tool_use_spawn", cwd=str(repo))
    payload["tool_input"]["prompt"] = "x" * 2000
    assert run_adapter("ledger_guard_spawn", payload, tmp)[1] is None


def test_short_spawn_prompt_passes(sandbox):
    _, repo, tmp = sandbox
    payload = fixture("pre_tool_use_spawn", cwd=str(repo))
    payload["tool_input"]["prompt"] = "go fix the typo"
    assert run_adapter("ledger_guard_spawn", payload, tmp)[1] is None


def test_shell_tool_never_reaches_the_spawn_guard(sandbox):
    """The tool gate, not the matcher: a shell call maps to Bash, which
    is not in ledger_guard_spawn's tool set, so nothing runs even if
    Codex's matcher over-matches."""
    _, repo, tmp = sandbox
    payload = fixture("pre_tool_use_shell", cwd=str(repo))
    payload["tool_input"]["command"] = "echo " + "x" * 2000
    assert run_adapter("ledger_guard_spawn", payload, tmp)[1] is None


def test_live_spawn_payload_is_gated_on_the_ciphertext_length(sandbox):
    """The captured `collaborationspawn_agent` payload, unmodified in
    shape: `message` is a Fernet ciphertext, so the core's 1500-char
    delegation gate measures the ciphertext. Long enough -> denied when
    no ledger exists; the same payload passes once one does."""
    _, repo, tmp = sandbox
    payload = fixture("pre_tool_use_spawn", cwd=str(repo))
    assert payload["tool_name"] == "collaborationspawn_agent"
    payload["tool_input"]["message"] = "gAAAAAB" + "A" * 2000
    out = run_adapter("ledger_guard_spawn", payload, tmp)[1]
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    write_ledger(repo)
    assert run_adapter("ledger_guard_spawn", payload, tmp)[1] is None


def test_spawn_prompt_read_from_alternate_key(sandbox):
    """PROMPT_KEYS tolerance: the real key name is not verified live."""
    _, repo, tmp = sandbox
    payload = fixture("pre_tool_use_spawn", cwd=str(repo))
    payload["tool_input"] = {"agent": "sonnet", "instructions": "x" * 2000}
    rc, out, _ = run_adapter("ledger_guard_spawn", payload, tmp)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- PreToolUse: apply_patch / write ----------------------------------

def test_patch_replacing_an_existing_foreign_ledger_is_denied(sandbox):
    """`Delete File` + `Add File` (or `Add File` alone) is apply_patch's
    whole-file replace — Claude Code's `Write`, and the exact shape that
    destroyed a live ledger on 2026-09-05."""
    _, repo, tmp = sandbox
    ledger = write_ledger(repo, "LEDGER-other.md")
    write_marker(tmp, started=time.time())  # a session, bound to nothing
    payload = fixture("pre_tool_use_apply_patch", cwd=str(repo))
    payload["tool_input"]["command"] = patch_text(ledger, op="Delete File")

    rc, out, _ = run_adapter("ledger_guard_write", payload, tmp)
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "EXISTING live ledger" in hso["permissionDecisionReason"]
    # Codex has no Edit tool; the adapter appends the harness note.
    assert "[codex-orchestrator]" in hso["permissionDecisionReason"]


def test_surgical_patch_into_a_foreign_ledger_passes(sandbox):
    """`Update File` is Codex's Edit. Core guards `^Write$` only, so this
    must pass — and it MUST, or a chair can never touch (and so never
    bind to) a ledger it did not create this session, which is why every
    live Codex session logged `stop_suppressed reason=unbound`."""
    _, repo, tmp = sandbox
    ledger = write_ledger(repo, "LEDGER-other.md")
    write_marker(tmp, started=time.time())
    payload = fixture("pre_tool_use_apply_patch", cwd=str(repo))
    payload["tool_input"]["command"] = patch_text(ledger, op="Update File")
    assert run_adapter("ledger_guard_write", payload, tmp)[1] is None


def test_update_hunk_that_deletes_the_whole_ledger_is_denied(sandbox):
    """LIVE BYPASS 2026-09-05: the model read the ledger, then wiped it
    with a single `*** Update File:` hunk in which every line was a
    deletion. The op label says "surgical"; the effect is a Write."""
    _, repo, tmp = sandbox
    ledger = write_ledger(repo, "LEDGER-other.md",
                          "# title\n\n- [ ] 1. item\n- [ ] 2. item\n")
    write_marker(tmp, started=time.time())
    payload = fixture("pre_tool_use_apply_patch", cwd=str(repo))
    payload["tool_input"]["command"] = wipe_text(ledger)
    out = run_adapter("ledger_guard_write", payload, tmp)[1]
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_patch_into_own_bound_ledger_passes(sandbox):
    _, repo, tmp = sandbox
    ledger = write_ledger(repo, "LEDGER-mine.md")
    write_marker(tmp, started=time.time(), ledger=str(ledger.resolve()))
    payload = fixture("pre_tool_use_apply_patch", cwd=str(repo))
    payload["tool_input"]["command"] = patch_text(ledger, op="Delete File")
    assert run_adapter("ledger_guard_write", payload, tmp)[1] is None


def test_patch_creating_a_new_ledger_passes(sandbox):
    _, repo, tmp = sandbox
    (repo / ".workflow").mkdir()
    write_marker(tmp, started=time.time())
    payload = fixture("pre_tool_use_apply_patch", cwd=str(repo))
    payload["tool_input"]["command"] = patch_text(
        repo / ".workflow" / "LEDGER-new.md", op="Add File")
    assert run_adapter("ledger_guard_write", payload, tmp)[1] is None


def test_apply_patch_multi_file_paths_are_extracted(adapter_mod, tmp_path):
    """Unit-level: relative paths absolutised against cwd, order kept,
    Add/Update/Delete/Move all seen."""
    text = ("*** Begin Patch\n"
            "*** Update File: src/a.py\n@@\n-x\n+y\n"
            "*** Add File: .workflow/LEDGER-topic.md\n+- [ ] 1. item\n"
            "*** Delete File: /abs/c.txt\n"
            "*** Move to: src/b.py\n"
            "*** End Patch\n")
    paths = adapter_mod.patch_paths({"command": text}, str(tmp_path))
    assert paths == [
        str(tmp_path / "src/a.py"),
        str(tmp_path / ".workflow/LEDGER-topic.md"),
        "/abs/c.txt" if os.name != "nt" else str(tmp_path / "/abs/c.txt"),
        str(tmp_path / "src/b.py"),
    ]


def test_apply_patch_multi_file_runs_the_guard_on_the_ledger_path(sandbox):
    """A patch touching three files, one of them a foreign live ledger:
    the guard must be run for THAT path, not just the first one."""
    _, repo, tmp = sandbox
    ledger = write_ledger(repo, "LEDGER-other.md")
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x\n", encoding="utf-8")
    write_marker(tmp, started=time.time())
    payload = fixture("pre_tool_use_apply_patch", cwd=str(repo))
    payload["tool_input"]["command"] = patch_text(
        "src/a.py", ledger, "src/b.py", op="Add File")

    rc, out, _ = run_adapter("ledger_guard_write", payload, tmp)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "LEDGER-other.md" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_patch_touching_no_ledger_passes(sandbox):
    _, repo, tmp = sandbox
    write_marker(tmp, started=time.time())
    payload = fixture("pre_tool_use_apply_patch", cwd=str(repo))
    payload["tool_input"]["command"] = patch_text("src/a.py", "src/b.py")
    assert run_adapter("ledger_guard_write", payload, tmp)[1] is None


# --- PreToolUse: the shell route into apply_patch ---------------------
#
# LIVE FAILURE 2026-09-05: the first-party `apply_patch` tool errored,
# the model piped the same envelope into the arg0 shim from a `Bash`
# call, and a foreign ledger was overwritten with every PreToolUse hook
# reporting "Completed". The command carried the path in a shell
# variable, so the envelope itself only said `*** Update File: $target`.

SHIM = "/Users/x/.codex/tmp/arg0/codex-arg0AbCdEf/apply_patch"


def shim_command(target):
    """The exact shape Codex used live (path in a shell variable)."""
    return (f"target='{target}'\n{{\n"
            "  printf '%s\\n' '*** Begin Patch' \"*** Update File: $target\" '@@'\n"
            "  sed 's/^/-/' \"$target\"\n"
            "  printf '%s\\n' '+x' '*** End Patch'\n"
            f"}} | {SHIM} >/tmp/result\n")


def test_shell_shim_patch_into_a_foreign_ledger_is_denied(sandbox):
    _, repo, tmp = sandbox
    ledger = write_ledger(repo, "LEDGER-other.md")
    write_marker(tmp, started=time.time())
    payload = fixture("pre_tool_use_shell", cwd=str(repo))
    payload["tool_input"]["command"] = shim_command(ledger)

    rc, out, _ = run_adapter("ledger_guard_write", payload, tmp)
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "LEDGER-other.md" in hso["permissionDecisionReason"]
    assert "[codex-orchestrator]" in hso["permissionDecisionReason"]


def test_shell_redirect_over_a_foreign_ledger_is_denied(sandbox):
    _, repo, tmp = sandbox
    ledger = write_ledger(repo, "LEDGER-other.md")
    write_marker(tmp, started=time.time())
    payload = fixture("pre_tool_use_shell", cwd=str(repo))
    payload["tool_input"]["command"] = f"printf 'x\\n' > {ledger}"
    assert (run_adapter("ledger_guard_write", payload, tmp)[1]
            ["hookSpecificOutput"]["permissionDecision"] == "deny")


def test_shell_append_to_a_foreign_ledger_passes(sandbox):
    """`>>` is additive — Claude's Edit, not Write."""
    _, repo, tmp = sandbox
    ledger = write_ledger(repo, "LEDGER-other.md")
    write_marker(tmp, started=time.time())
    payload = fixture("pre_tool_use_shell", cwd=str(repo))
    payload["tool_input"]["command"] = f"printf 'x\\n' >> {ledger}"
    assert run_adapter("ledger_guard_write", payload, tmp)[1] is None


def test_shell_write_into_the_sessions_own_ledger_passes(sandbox):
    _, repo, tmp = sandbox
    ledger = write_ledger(repo, "LEDGER-mine.md")
    write_marker(tmp, started=time.time(), ledger=str(ledger.resolve()))
    payload = fixture("pre_tool_use_shell", cwd=str(repo))
    payload["tool_input"]["command"] = shim_command(ledger)
    assert run_adapter("ledger_guard_write", payload, tmp)[1] is None


@pytest.mark.parametrize("command", [
    "sed -n '1,240p' .workflow/LEDGER-other.md",
    "grep -c '\\[ \\]' .workflow/LEDGER-other.md",
    "git diff -- .workflow/LEDGER-other.md | head",
    "cp .workflow/LEDGER-other.md /tmp/backup.md",
    "echo probe && ls .workflow",
])
def test_reading_a_ledger_from_the_shell_is_never_denied(sandbox, command):
    """A false positive here DENIES an ordinary shell call — the
    detector must stay narrow."""
    _, repo, tmp = sandbox
    write_ledger(repo, "LEDGER-other.md")
    write_marker(tmp, started=time.time())
    payload = fixture("pre_tool_use_shell", cwd=str(repo))
    payload["tool_input"]["command"] = command
    assert run_adapter("ledger_guard_write", payload, tmp)[1] is None


def test_shell_write_targets_unit(adapter_mod, tmp_path):
    f = adapter_mod.shell_write_targets
    a = str(tmp_path / ".workflow/LEDGER-a.md")
    b = str(tmp_path / ".workflow/LEDGER-b.md")
    R, E = adapter_mod.REPLACE, adapter_mod.EDIT
    assert f("printf x > .workflow/LEDGER-a.md", str(tmp_path)) == [(a, R)]
    assert f("printf x >> .workflow/LEDGER-a.md", str(tmp_path)) == [(a, E)]
    assert f("cat x | tee .workflow/LEDGER-b.md", str(tmp_path)) == [(b, R)]
    assert f("cat x | tee -a .workflow/LEDGER-b.md", str(tmp_path)) == [(b, E)]
    assert f("sed -i '' s/a/b/ .workflow/LEDGER-a.md", str(tmp_path)) == [(a, E)]
    # archived and non-ledger names are not this guard's business
    assert f("printf x > .workflow/LEDGER-a-archive.md", str(tmp_path)) == []
    assert f("printf x > notes.md", str(tmp_path)) == []
    assert f("cat .workflow/LEDGER-a.md", str(tmp_path)) == []


# --- PostToolUse: ledger_bind -----------------------------------------

def test_post_tool_use_binds_the_session_to_the_ledger(sandbox):
    _, repo, tmp = sandbox
    ledger = write_ledger(repo, "LEDGER-bound.md")
    marker = write_marker(tmp, started=time.time())
    payload = fixture("post_tool_use_apply_patch", cwd=str(repo))
    payload["tool_input"]["command"] = patch_text("src/a.py", ledger)

    rc, out, _ = run_adapter("ledger_bind", payload, tmp)
    assert (rc, out) == (0, None)  # binding is silent
    bound = json.loads(marker.read_text(encoding="utf-8"))["ledger"]
    assert os.path.realpath(bound) == os.path.realpath(str(ledger))


def test_post_tool_use_binds_through_the_shell_route(sandbox):
    """LIVE FAILURE 2026-09-05: every Codex session logged
    `stop_suppressed reason=unbound` because nothing ever bound it — the
    binder read `tool_input.file_path`, which an apply_patch (tool OR
    shell shim) payload never has. Both routes must bind."""
    _, repo, tmp = sandbox
    ledger = write_ledger(repo, "LEDGER-bound.md")
    marker = write_marker(tmp, started=time.time())
    payload = fixture("post_tool_use_shell", cwd=str(repo))
    payload["tool_input"]["command"] = shim_command(ledger)
    payload["tool_response"] = "apply_patch completed\n"

    assert run_adapter("ledger_bind", payload, tmp)[1] is None
    bound = json.loads(marker.read_text(encoding="utf-8"))["ledger"]
    assert os.path.realpath(bound) == os.path.realpath(str(ledger))


def test_an_ordinary_shell_call_binds_nothing(sandbox):
    _, repo, tmp = sandbox
    write_ledger(repo, "LEDGER-other.md")
    marker = write_marker(tmp, started=time.time())
    payload = fixture("post_tool_use_shell", cwd=str(repo))
    payload["tool_input"]["command"] = "grep -c x .workflow/LEDGER-other.md"
    run_adapter("ledger_bind", payload, tmp)
    assert "ledger" not in json.loads(marker.read_text(encoding="utf-8"))


def test_bind_then_stop_blocks_the_open_ledger(sandbox):
    """End-to-end for the `unbound` failure: patch the ledger, then try
    to stop. One block, then the retry passes — the same sequence the
    live `codex exec` run must produce."""
    _, repo, tmp = sandbox
    ledger = write_ledger(repo, "LEDGER-open.md",
                          "- [x] 1. done\n- [ ] 2. still open\n")
    write_marker(tmp, started=time.time())

    # unbound: the Stop guard must NOT block (parity with Claude Code)
    assert run_adapter("ledger_guard_stop",
                       fixture("stop", cwd=str(repo)), tmp)[1] is None

    post = fixture("post_tool_use_apply_patch", cwd=str(repo))
    post["tool_input"]["command"] = patch_text(ledger, op="Update File")
    run_adapter("ledger_bind", post, tmp)

    out = run_adapter("ledger_guard_stop", fixture("stop", cwd=str(repo)), tmp)[1]
    assert out["decision"] == "block"
    assert "LEDGER GUARD" in out["reason"]


# --- Stop -------------------------------------------------------------

def test_stop_blocks_once_then_lets_the_retry_through(sandbox):
    _, repo, tmp = sandbox
    ledger = write_ledger(repo, "LEDGER-open.md",
                          "- [x] 1. done\n- [ ] 2. still open\n")
    write_marker(tmp, started=time.time(), ledger=str(ledger.resolve()))

    rc, out, _ = run_adapter("ledger_guard_stop",
                             fixture("stop", cwd=str(repo)), tmp)
    assert rc == 0
    assert out["decision"] == "block"
    assert "1 open item(s)" in out["reason"]
    assert "still open" in out["reason"]

    # Codex's own loop guard: the retry carries stop_hook_active.
    retry = fixture("stop", cwd=str(repo), stop_hook_active=True)
    assert run_adapter("ledger_guard_stop", retry, tmp)[1] is None


def test_stop_with_all_items_closed_passes(sandbox):
    _, repo, tmp = sandbox
    ledger = write_ledger(repo, "LEDGER-done.md", "- [x] 1. done\n")
    write_marker(tmp, started=time.time(), ledger=str(ledger.resolve()))
    assert run_adapter("ledger_guard_stop",
                       fixture("stop", cwd=str(repo)), tmp)[1] is None


# --- SessionStart -----------------------------------------------------

def test_session_start_injects_the_profile(sandbox):
    _, repo, tmp = sandbox
    payload = fixture("session_start", cwd=str(repo), model="gpt-6-astra")
    rc, out, _ = run_adapter("inject_instructions", payload, tmp)
    assert rc == 0
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "SessionStart"
    ctx = hso["additionalContext"]
    assert "(FABLE profile)" in ctx          # gpt-6-astra -> chair tier
    assert "Requirements Ledger" in ctx
    assert len(ctx) > 1000


def test_session_start_maps_the_opus_tier_model(sandbox):
    """Codex model ids never match the core's `_is_opus`; the adapter
    pins FABLE_ORCH_PROFILE from MODEL_PROFILE_MAP instead."""
    _, repo, tmp = sandbox
    payload = fixture("session_start", cwd=str(repo), model="gpt-5.6-sol")
    _, out, _ = run_adapter("inject_instructions", payload, tmp)
    assert "(OPUS profile)" in out["hookSpecificOutput"]["additionalContext"]


def test_user_pin_beats_the_model_map(sandbox):
    _, repo, tmp = sandbox
    payload = fixture("session_start", cwd=str(repo), model="gpt-5.6-sol")
    _, out, _ = run_adapter("inject_instructions", payload, tmp,
                            env_extra={"FABLE_ORCH_PROFILE": "fable"})
    assert "(FABLE profile)" in out["hookSpecificOutput"]["additionalContext"]


# --- UserPromptSubmit -------------------------------------------------

def test_cold_cache_guard_passes_slash_commands(sandbox):
    """A cold session (last prompt 2h ago) submitting `/sync`: never
    blocked — and the marker still gets stamped, proving the guard ran
    rather than being gated out."""
    _, repo, tmp = sandbox
    marker = write_marker(tmp, started=time.time() - 7200,
                          last_prompt=time.time() - 7200)
    rc, out, _ = run_adapter("cold_cache_guard",
                             fixture("user_prompt_submit", cwd=str(repo)), tmp,
                             env_extra={"FABLE_ORCH_COLD_BLOCK_TOKENS": "1"})
    assert (rc, out) == (0, None)
    stamped = json.loads(marker.read_text(encoding="utf-8"))["last_prompt"]
    assert stamped > time.time() - 120


def test_cold_cache_guard_blocks_a_cold_expensive_prompt(sandbox):
    _, repo, tmp = sandbox
    transcript = Path(tmp) / "transcript.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"usage": {"input_tokens": 400000,
                              "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0}},
    }) + "\n", encoding="utf-8")
    write_marker(tmp, started=time.time() - 7200,
                 last_prompt=time.time() - 7200)
    payload = fixture("user_prompt_submit", cwd=str(repo),
                      prompt="what did we decide about the shading?",
                      transcript_path=str(transcript))
    rc, out, _ = run_adapter("cold_cache_guard", payload, tmp)
    assert out["decision"] == "block"
    assert "cold" in out["reason"].lower()
    # Claude Code's `systemMessage` extra has no documented Codex
    # counterpart and must not leak into the output.
    assert "systemMessage" not in out


# --- SessionEnd -------------------------------------------------------

def test_session_end_is_silent(sandbox):
    _, repo, tmp = sandbox
    write_marker(tmp, started=time.time())
    rc, out, _ = run_adapter("cleanup_session_cache",
                             fixture("session_end", cwd=str(repo)), tmp)
    assert (rc, out) == (0, None)


# --- metrics stamp ----------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="HOME-based expanduser is POSIX")
def test_metrics_lines_are_stamped_with_the_harness(sandbox):
    home, repo, tmp = sandbox
    payload = fixture("pre_tool_use_spawn", cwd=str(repo))
    payload["tool_input"]["prompt"] = "x" * 2000
    rc, out, _ = run_adapter("ledger_guard_spawn", payload, tmp,
                             env_extra={"HOME": str(home),
                                        "FABLE_ORCH_METRICS": "1"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    log = home / ".claude" / "fable-orch" / "metrics.jsonl"
    records = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert records and records[-1]["event"] == "spawn_deny"
    assert all(r["harness"] == "codex" for r in records)


@pytest.mark.skipif(os.name == "nt", reason="HOME-based expanduser is POSIX")
def test_metrics_stamp_leaves_earlier_lines_alone(sandbox):
    """Only the byte range the child appended is rewritten — a line from
    a Claude Code session in the same log keeps its (absent) harness."""
    home, repo, tmp = sandbox
    log = home / ".claude" / "fable-orch" / "metrics.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({"ts": 1, "event": "inject"}) + "\n",
                   encoding="utf-8")
    payload = fixture("pre_tool_use_spawn", cwd=str(repo))
    payload["tool_input"]["prompt"] = "x" * 2000
    run_adapter("ledger_guard_spawn", payload, tmp,
                env_extra={"HOME": str(home), "FABLE_ORCH_METRICS": "1"})

    records = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(records) == 2
    assert "harness" not in records[0]
    assert records[1]["harness"] == "codex"


# --- tool-name map ----------------------------------------------------

@pytest.mark.parametrize("codex_name,claude_name", [
    ("Bash", "Bash"),       # the live shell tool name
    ("shell", "Bash"),
    ("exec", "Bash"),
    ("local_shell", "Bash"),
    ("container.exec", "Bash"),
    ("apply_patch", "Write"),
    ("SpawnAgent", "Agent"),
    ("collab: SpawnAgent", "Agent"),
    ("collab__spawn_agent", "Agent"),
    ("mcp__home-assistant__ha_get_state", None),
])
def test_tool_name_map(adapter_mod, codex_name, claude_name):
    assert adapter_mod.canonical_tool(codex_name) == claude_name


def test_apply_patch_is_a_write_or_an_edit_per_op(adapter_mod):
    """The write guard sees only whole-file replaces (Claude `Write`);
    a surgical `Update File` hunk never reaches it. PostToolUse takes
    both, as Edit — any successful write binds the session."""
    def norm(event, guard, op):
        return adapter_mod.normalise(
            {"hook_event_name": event, "tool_name": "apply_patch",
             "cwd": "/repo",
             "tool_input": {"command": patch_text("a.md", op=op)}}, guard)[1]

    replace = norm("PreToolUse", "ledger_guard_write", "Add File")
    assert [p["tool_name"] for p in replace] == ["Write"]
    assert norm("PreToolUse", "ledger_guard_write", "Update File") == []
    for op in ("Add File", "Update File"):
        post = norm("PostToolUse", "ledger_bind", op)
        assert [p["tool_name"] for p in post] == ["Edit"]


# --- robustness -------------------------------------------------------

def test_unknown_guard_never_breaks_the_pipeline(sandbox):
    _, repo, tmp = sandbox
    rc, out, err = run_adapter("no_such_guard", fixture("stop", cwd=str(repo)), tmp)
    assert (rc, out) == (0, None)
    assert "unknown guard" in err


def test_malformed_stdin_fails_open(tmp_path):
    proc = subprocess.run([sys.executable, str(ADAPTER), "ledger_guard_stop"],
                          input="not json at all", capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_exit2_mode_signals_the_deny_on_stderr(sandbox):
    _, repo, tmp = sandbox
    payload = fixture("pre_tool_use_spawn", cwd=str(repo))
    payload["tool_input"]["prompt"] = "x" * 2000
    rc, out, err = run_adapter("ledger_guard_spawn", payload, tmp,
                               env_extra={"CODEX_ADAPTER_EXIT2": "1"})
    assert rc == 2
    assert out is None
    assert "LEDGER GUARD" in err


# --- hooks manifest ---------------------------------------------------

def test_hooks_manifest_covers_every_guard(adapter_mod):
    manifest = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
    seen = {}
    for event, entries in manifest.items():
        for entry in entries:
            for hook in entry["hooks"]:
                guard = hook["command"].rsplit(" ", 1)[-1]
                seen[guard] = (event, entry.get("matcher"), hook)
    assert set(seen) == set(adapter_mod.GUARDS)
    for guard, (event, _, _) in seen.items():
        assert adapter_mod.GUARDS[guard]["event"] == event


def test_context_carrying_hooks_raise_the_context_limit():
    """Codex truncates `additionalContext` at 2500 chars by default; the
    injected chair profile is several times that (verified live — the
    full profile only reached the transcript once the limit was set)."""
    manifest = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
    for event in ("SessionStart", "UserPromptSubmit"):
        for entry in manifest[event]:
            for hook in entry["hooks"]:
                assert hook["additionalContextLimit"] >= 10000
    # SessionEnd is clamped to 3s by Codex; asking for more is a lie.
    for entry in manifest["SessionEnd"]:
        for hook in entry["hooks"]:
            assert hook["timeout"] <= 3


def test_hooks_manifest_has_windows_commands():
    manifest = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
    for entries in manifest.values():
        for entry in entries:
            for hook in entry["hooks"]:
                assert hook["type"] == "command"
                assert "scripts/codex_adapter.py" in hook["command"]
                win = hook["commandWindows"]
                assert win.startswith("python ")            # not python3
                assert "\\scripts\\codex_adapter.py" in win  # Windows path form
                assert win.rsplit(" ", 1)[-1] == hook["command"].rsplit(" ", 1)[-1]


def test_hooks_manifest_matchers_accept_the_mapped_tool_names(adapter_mod):
    """Every Codex tool name that maps into a guard's tool set must also
    get past that entry's matcher (read as a Claude-style regex)."""
    import re
    manifest = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
    for event in ("PreToolUse", "PostToolUse"):
        for entry in manifest[event]:
            matcher = entry["matcher"]
            guard = entry["hooks"][0]["command"].rsplit(" ", 1)[-1]
            tools = adapter_mod.GUARDS[guard]["tools"]
            wanted = {codex for codex, claude
                      in adapter_mod.TOOL_NAME_MAP.items() if claude in tools}
            matched = {t for t in wanted if re.match(matcher, t)}
            # Not every defensive spelling needs to be in the matcher, but
            # the ones Codex actually uses do.
            assert matched, f"{guard}: matcher {matcher} matches nothing"
    pre = {e["hooks"][0]["command"].rsplit(" ", 1)[-1]: e["matcher"]
           for e in manifest["PreToolUse"]}
    assert re.match(pre["ledger_guard_write"], "apply_patch")
    assert re.match(pre["ledger_guard_spawn"], "SpawnAgent")
    assert re.match(pre["ledger_guard_write"], "Bash")  # the shell write route
    assert not re.match(pre["ledger_guard_write"], "SpawnAgent")
