"""scripts/destructive_guard_install.py: the SessionStart hook that puts
the `rm` shim at ~/.claude/guard/bin/rm and prepends it to the session's
PATH via $CLAUDE_ENV_FILE."""
from conftest import REPO, run_hook

SCRIPT = "destructive_guard_install.py"
SHIM = REPO / "guard" / "rm"
ENV_LINE = 'export PATH="$HOME/.claude/guard/bin:$PATH"'


def _run(tmp_path, home, env_file=None, **extra):
    env = {"HOME": str(home), "USERPROFILE": str(home),
           "CLAUDE_PLUGIN_ROOT": str(REPO)}
    if env_file is not None:
        env["CLAUDE_ENV_FILE"] = str(env_file)
    env.update(extra)
    return run_hook(SCRIPT, {"session_id": "s-install", "source": "startup"},
                    env_extra=env, tmpdir=tmp_path)


def test_installs_the_shim_and_says_nothing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    assert _run(tmp_path, home) is None  # SessionStart stdout becomes context
    installed = home / ".claude" / "guard" / "bin" / "rm"
    assert installed.read_text(encoding="utf-8") == SHIM.read_text(encoding="utf-8")


def test_refreshes_a_stale_copy(tmp_path):
    home = tmp_path / "home"
    target = home / ".claude" / "guard" / "bin"
    target.mkdir(parents=True)
    (target / "rm").write_text("#!/bin/sh\n# an older shim\n", encoding="utf-8")
    _run(tmp_path, home)
    assert (target / "rm").read_text(encoding="utf-8") == \
        SHIM.read_text(encoding="utf-8")


def test_appends_the_path_export_once(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env_file = tmp_path / "session-env.sh"
    env_file.write_text("export FOO=1\n", encoding="utf-8")
    _run(tmp_path, home, env_file=env_file)
    _run(tmp_path, home, env_file=env_file)  # resume/clear/compact re-fire
    body = env_file.read_text(encoding="utf-8")
    assert body.count(ENV_LINE) == 1
    assert body.startswith("export FOO=1\n")


def test_no_env_file_still_installs_the_shim(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _run(tmp_path, home)
    assert (home / ".claude" / "guard" / "bin" / "rm").is_file()


def test_destructive_guard_0_installs_nothing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env_file = tmp_path / "session-env.sh"
    env_file.write_text("", encoding="utf-8")
    _run(tmp_path, home, env_file=env_file, DESTRUCTIVE_GUARD="0")
    assert not (home / ".claude" / "guard").exists()
    assert env_file.read_text(encoding="utf-8") == ""
