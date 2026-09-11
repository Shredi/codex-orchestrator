"""scripts/destructive_guard.py: Layer A of the destructive-command
guard — the PreToolUse hook that reads a Bash command's TEXT before any
shell expands it.

Every case here is a JSON payload fed to the hook, exactly as Claude
Code would. Nothing in this file ever runs a shell command: the strings
below (the 2026-09-09 incident line first among them) are INPUT DATA.
"""
import json

import pytest

from conftest import run_hook

SCRIPT = "destructive_guard.py"
CWD = "/Users/tester/Documents/git/project"
INCIDENT = 'bash -c \'rm -rf -- "$1"/*\' x ""'


def payload(command, cwd=CWD, session_id="test-session", **extra):
    p = {
        "session_id": session_id,
        "tool_name": "Bash",
        "cwd": cwd,
        "tool_input": {"command": command, "description": "probe"},
    }
    p.update(extra)
    return p


def decide(command, cwd=CWD, **kwargs):
    result = run_hook(SCRIPT, payload(command, cwd=cwd), **kwargs)
    if result is None:
        return "allow", None
    out = result["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    return out.get("permissionDecision", "allow"), out


# --- DENY: the incident itself ---------------------------------------------

def test_denies_the_2026_09_09_incident_line():
    decision, out = decide(INCIDENT)
    assert decision == "deny"
    reason = out["permissionDecisionReason"]
    assert reason.startswith("DESTRUCTIVE GUARD: ")
    assert "DESTRUCTIVE_GUARD=0" in reason
    # A denied command is never rewritten — there is nothing to run.
    assert "updatedInput" not in out


# --- DENY: operands that are unknown at approval time -----------------------

@pytest.mark.parametrize("command", [
    'rm -rf "$TARGET"',            # variable
    'rm -rf "$TARGET"/*',          # variable + glob, the incident's shape
    'rm -rf ${DIR}',
    'rm -rf `cat target.txt`',
    'rm -rf ""',                   # empty operand
    "rm -rf ''",
    'rm -rf -- ""',                # ...behind the `--` terminator
])
def test_denies_unknown_operands(command):
    assert decide(command)[0] == "deny"


@pytest.mark.parametrize("command", [
    "rm -rf /",
    "rm -rf /*",
    "rm -rf /Applications",
    "rm -rf /Library/",
    "rm -rf /Users",
    "rm -rf ~",
    "rm -rf $HOME",
    "rm -rf ~/*",
])
def test_denies_protected_targets(command):
    assert decide(command)[0] == "deny"


def test_protection_is_the_top_level_itself_not_everything_under_it():
    # /usr is protected; /usr/local is merely far outside the roots.
    assert decide("rm -rf /usr")[0] == "deny"
    assert decide("rm -rf /usr/local")[0] == "ask"


def test_denies_single_segment_home_of_another_user():
    assert decide("rm -rf /Users/someone")[0] == "deny"


# --- DENY: bypasses of the runtime shim -------------------------------------

@pytest.mark.parametrize("command", [
    "sudo rm -rf build",
    "sudo -u marc rm -r build",
    "/bin/rm -rf build",
    "/usr/bin/rm -R build",
    "command -p rm -rf build",
    "env -i rm -rf build",
])
def test_denies_shim_bypasses(command):
    assert decide(command)[0] == "deny"


# --- DENY: nested contexts (operands invisible from here) -------------------

@pytest.mark.parametrize("command", [
    'bash -c "rm -rf $DIR"',
    "sh -c 'rm -rf /tmp/x'",
    "zsh -c 'rm -r build'",
    'eval "rm -rf $DIR"',
    "ls | xargs rm -rf",
    "find / -delete",
    "find /etc -exec rm -rf {} +",
    "echo $(rm -rf /)",
])
def test_denies_nested_destructive_contexts(command):
    assert decide(command)[0] == "deny"


def test_denies_a_recursive_rm_inside_a_function_body():
    # `f(){ rm -rf $1; }; f ""` is the incident's shape wrapped in a
    # definition: the segment's first token is `f(){`, so without reading
    # the body the rm is never seen at all.
    assert decide('f(){ rm -rf $1; }; f ""')[0] == "deny"
    assert decide("cleanup(){ rm -rf build; }; cleanup")[0] == "allow"


@pytest.mark.parametrize("command", ["busybox rm -rf /", "toybox rm -rf $x"])
def test_denies_rm_behind_an_applet_multiplexer(command):
    assert decide(command)[0] == "deny"


def test_denies_a_recursive_delete_behind_a_variable_command_word():
    assert decide("RM=rm; $RM -rf /")[0] == "deny"


def test_a_cd_earlier_in_the_line_makes_relative_operands_unknown():
    # The payload's cwd stops describing where the rm lands.
    assert decide("cd / && rm -rf *")[0] == "deny"
    assert decide("cd ~ && rm -rf .")[0] == "deny"
    assert decide("cd /elsewhere && rm -rf sub")[0] == "ask"
    # ...while a bare `*` in a cwd nobody moved out of stays allowed.
    assert decide("rm -rf *")[0] == "allow"


def test_denies_git_clean_with_an_exclude_pattern():
    assert decide("git clean -fdx -e keep")[0] == "deny"
    assert decide("git clean -fdx --exclude=keep")[0] == "deny"


# --- DENY: remote command strings (ledger item 11) --------------------------

@pytest.mark.parametrize("command", [
    "ssh root@mr3.comptec.de 'rm -rf $DIR'",
    "ssh mr3 rm -rf /etc/postfix",
    "docker exec web rm -rf /var/lib/app",
    "kubectl exec pod -- rm -rf /data",
])
def test_denies_recursive_rm_on_another_machine(command):
    # Layer B (the rm shim) lives on THIS machine only.
    decision, out = decide(command)
    assert decision == "deny"
    assert "remote" in out["permissionDecisionReason"].lower() or \
        "another machine" in out["permissionDecisionReason"]


def test_allows_a_harmless_remote_command():
    assert decide("ssh mr3 'systemctl reload postfix'")[0] == "allow"


# --- DENY: the non-rm destructive families ----------------------------------

@pytest.mark.parametrize("command", [
    "dd if=/dev/zero of=/dev/disk2 bs=1m",
    "mkfs.ext4 /dev/sda1",
    "diskutil eraseDisk JHFS+ blank disk2",
    "chmod -R 777 /usr",
    "chown -R marc /Library",
    'truncate -s 0 "$LOGFILE"',
    ": > $LOGFILE",
    "shred -u secret.txt",
    'git clean -fdx "$DIR"',
])
def test_denies_other_destructive_families(command):
    assert decide(command)[0] == "deny"


def test_an_append_redirect_to_a_variable_is_not_a_truncation():
    # `>>` adds; only a single `>` (or `:>`) empties the file.
    assert decide("echo x >> $LOG")[0] == "allow"
    assert decide("echo x > $LOG")[0] == "deny"


def test_denies_in_band_bypass_attempts():
    # The shim's dry-run switch belongs to the test suite; a command that
    # tries to set it (or a made-up SAFE_RM_BYPASS) is refused outright.
    assert decide("SAFE_RM_DRYRUN=1 rm -rf /tmp/x")[0] == "deny"
    assert decide("SAFE_RM_BYPASS=1 rm -rf build")[0] == "deny"


# --- ASK: recursive rm on a literal path outside the allowed roots ----------

def test_find_delete_inside_the_allowed_roots_asks_instead_of_denying():
    # An everyday cleanup inside .workflow is a confirm, not a refusal;
    # a walk that starts at a variable or a protected dir stays a refusal.
    assert decide("find .workflow/scratch -name '*.tmp' -delete")[0] == "ask"
    assert decide("find /tmp/old -delete")[0] == "ask"
    assert decide('find . -name "*.tmp" -exec rm -rf {} \\;')[0] == "ask"
    assert decide("find $DIR -delete")[0] == "deny"
    assert decide("find /Users -delete")[0] == "deny"


def test_asks_for_recursive_rm_outside_allowed_roots():
    decision, out = decide("rm -rf /Users/tester/Desktop/old-stuff")
    assert decision == "ask"
    reason = out["permissionDecisionReason"]
    assert reason.startswith("DESTRUCTIVE GUARD: ")
    assert "DESTRUCTIVE_GUARD=0" in reason


# --- ALLOW -----------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "rm -rf .workflow/scratch/x",      # under cwd
    "rm -rf ./build",
    "rm -rf /tmp/scratch-dir",         # allowed root
    "rm foo.txt",                      # not recursive
    "git status",
    "trash /Users/tester/Desktop/old-stuff",   # Marc's rule: trash over rm
    "echo hello && ls -la",
    "docker rm -f web",                # `rm` as a docker subcommand
    'grep -rn "rm -rf" docs/',
])
def test_allows_ordinary_work(command):
    assert decide(command)[0] == "allow"


# --- updatedInput: the PATH prepend ----------------------------------------

PREFIX = 'export PATH="$HOME/.claude/guard/bin:$PATH"; '


def test_rm_commands_are_rewritten_to_put_the_shim_first():
    decision, out = decide("rm -rf ./build")
    assert decision == "allow"
    assert out["updatedInput"]["command"] == PREFIX + "rm -rf ./build"
    # the rest of tool_input survives the rewrite
    assert out["updatedInput"]["description"] == "probe"


def test_ask_decisions_are_rewritten_too():
    decision, out = decide("rm -rf /Users/tester/Desktop/old-stuff")
    assert decision == "ask"
    assert out["updatedInput"]["command"].startswith(PREFIX)


def test_non_rm_commands_are_left_alone():
    # Rewriting everything would break `Bash(git *)`-style allow rules.
    assert decide("git status") == ("allow", None)
    assert decide("ls -la") == ("allow", None)


@pytest.mark.parametrize("command", [
    "git commit -m 'rm -rf cleanup'",   # `rm` inside a commit message
    'grep -r "rm -rf" docs/',           # ...inside a search pattern
    "docker rm c",                      # ...as another tool's subcommand
    "echo 'rm -rf /'",                  # ...as text
])
def test_the_word_rm_in_an_argument_is_not_an_rm_call(command):
    # The prefix comes off the PARSED tokens, not off the raw string.
    assert decide(command) == ("allow", None)


@pytest.mark.parametrize("command", ["rm foo.txt", "find . -exec rm {} +"])
def test_real_rm_calls_still_get_the_prefix(command):
    assert decide(command)[1]["updatedInput"]["command"] == PREFIX + command


def test_rewrite_is_idempotent():
    assert decide(PREFIX + "rm -rf ./build") == ("allow", None)


# --- disable switch + fail-open --------------------------------------------

def test_destructive_guard_0_disables_everything():
    assert run_hook(SCRIPT, payload(INCIDENT),
                    env_extra={"DESTRUCTIVE_GUARD": "0"}) is None


def test_malformed_stdin_never_blocks():
    assert run_hook(SCRIPT, raw="not json at all") is None
    assert run_hook(SCRIPT, raw="[1, 2, 3]") is None
    assert run_hook(SCRIPT, raw="") is None


def test_missing_command_is_a_noop():
    assert run_hook(SCRIPT, {"session_id": "s", "tool_name": "Bash",
                             "tool_input": {}}) is None


# --- metrics ---------------------------------------------------------------

def test_deny_and_ask_emit_metrics(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "USERPROFILE": str(home), "FABLE_ORCH_METRICS": "1"}
    run_hook(SCRIPT, payload(INCIDENT), env_extra=env)
    run_hook(SCRIPT, payload("rm -rf /Users/tester/Desktop/old"), env_extra=env)
    log = home / ".claude" / "fable-orch" / "metrics.jsonl"
    lines = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
    events = {l["event"]: l for l in lines}
    assert set(events) == {"destructive_deny", "destructive_ask"}
    assert events["destructive_deny"]["kind"]
    assert events["destructive_deny"]["cmd_head"] == INCIDENT[:60]
