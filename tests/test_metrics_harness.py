"""FABLE_ORCH_HARNESS: the metrics stamp an external adapter reads.

`_metric()` is duplicated verbatim across every hook script — no
shared module, they run as standalone plugin hooks with no package to
import from. That means a fix here can silently miss one of them, so
this test is parametrized over every script rather than picking one as
a representative. Each is loaded in-process (same pattern as
`test_cold_cache_guard._module`) with HOME/USERPROFILE pinned to a
sandbox so no test ever touches ~/.claude/fable-orch/metrics.jsonl on
the real machine.
"""
import importlib.util
import json

import pytest

from conftest import SCRIPTS

GUARD_SCRIPTS = [
    "cleanup_session_cache.py",
    "cold_cache_guard.py",
    "ledger_guard_spawn.py",
    "ledger_guard_stop.py",
    "inject_instructions.py",
    "ledger_guard_write.py",
    "destructive_guard.py",
    "destructive_guard_install.py",
]


def _load(script):
    spec = importlib.util.spec_from_file_location(script.replace(".py", ""),
                                                   SCRIPTS / script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sandbox_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("FABLE_ORCH_METRICS", "1")
    return home


def _metrics_lines(home):
    path = home / ".claude" / "fable-orch" / "metrics.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in
            path.read_text(encoding="utf-8").splitlines() if l.strip()]


@pytest.mark.parametrize("script", GUARD_SCRIPTS)
def test_harness_key_added_when_env_set(tmp_path, monkeypatch, script):
    home = _sandbox_home(tmp_path, monkeypatch)
    monkeypatch.setenv("FABLE_ORCH_HARNESS", "codex")
    mod = _load(script)
    mod._metric("probe", "sess-12345678", extra_field=1)
    lines = _metrics_lines(home)
    assert len(lines) == 1
    assert lines[0]["harness"] == "codex"
    assert lines[0]["event"] == "probe"
    assert lines[0]["session"] == "sess-123"  # first 8 chars, unaffected
    assert lines[0]["extra_field"] == 1


@pytest.mark.parametrize("script", GUARD_SCRIPTS)
def test_harness_key_omitted_by_default(tmp_path, monkeypatch, script):
    home = _sandbox_home(tmp_path, monkeypatch)
    monkeypatch.delenv("FABLE_ORCH_HARNESS", raising=False)
    mod = _load(script)
    mod._metric("probe", "sess-12345678")
    lines = _metrics_lines(home)
    assert len(lines) == 1
    assert "harness" not in lines[0]  # unchanged output for Claude Code


@pytest.mark.parametrize("script", GUARD_SCRIPTS)
def test_harness_key_omitted_when_env_blank(tmp_path, monkeypatch, script):
    # Whitespace-only is treated the same as unset, matching every other
    # FABLE_ORCH_* knob's "(\s*).strip()" convention in these scripts.
    home = _sandbox_home(tmp_path, monkeypatch)
    monkeypatch.setenv("FABLE_ORCH_HARNESS", "   ")
    mod = _load(script)
    mod._metric("probe", "sess-12345678")
    lines = _metrics_lines(home)
    assert len(lines) == 1
    assert "harness" not in lines[0]
