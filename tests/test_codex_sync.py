"""Unit tests for bin/codex-sync: frontmatter translation, idempotency,
dry-run, no-overwrite-AGENTS.md, secret redaction. Uses the real hub +
bulwark agent files as frontmatter fixtures where useful, and synthetic
ones (via the `repo`/`write_agent_md` conftest helpers) for edge cases.
"""
import json
from pathlib import Path

import pytest

HUB_AGENTS = Path("/Users/marc/Documents/git/claude/.claude/agents")
BULWARK_AGENTS = Path("/Users/marc/Documents/git/bulwark/.claude/agents")


# --------------------------------------------------------------------------
# Frontmatter parsing against the real files this tool has to handle
# --------------------------------------------------------------------------


@pytest.mark.skipif(not HUB_AGENTS.is_dir(), reason="hub repo not present in this checkout")
def test_parses_hub_agent_without_mcpservers(codex_sync):
    text = (HUB_AGENTS / "ha-collector.md").read_text(encoding="utf-8")
    fields, body = codex_sync.parse_frontmatter(text)
    assert fields["name"] == "ha-collector"
    assert fields["model"] == "sonnet"
    assert "mcpServers" not in fields
    assert body.startswith("You are the Home Assistant")


@pytest.mark.skipif(not HUB_AGENTS.is_dir(), reason="hub repo not present in this checkout")
def test_parses_hub_agent_folded_multiline_description_and_mcpservers(codex_sync):
    text = (HUB_AGENTS / "blender-artist.md").read_text(encoding="utf-8")
    fields, body = codex_sync.parse_frontmatter(text)
    assert fields["name"] == "blender-artist"
    assert fields["model"] == "opus"
    # The description folds across several indented lines in the source file.
    assert "Modelling only" in fields["description"]
    assert "\n" not in fields["description"]
    assert fields["mcpServers"]["blender"]["command"] == "uvx"
    assert fields["mcpServers"]["blender"]["env"]["DISABLE_TELEMETRY"] == "true"


@pytest.mark.skipif(not BULWARK_AGENTS.is_dir(), reason="bulwark repo not present in this checkout")
def test_parses_bulwark_agent_with_effort_field(codex_sync):
    text = (BULWARK_AGENTS / "sim-engineer.md").read_text(encoding="utf-8")
    fields, body = codex_sync.parse_frontmatter(text)
    assert fields["model"] == "sonnet"
    assert fields["effort"] == "medium"
    assert not body.startswith("\n"), "leading blank line after frontmatter must be stripped"


def test_parse_frontmatter_rejects_missing_fence(codex_sync):
    with pytest.raises(ValueError):
        codex_sync.parse_frontmatter("no frontmatter here\n")


# --------------------------------------------------------------------------
# sandbox_mode derivation
# --------------------------------------------------------------------------


def test_sandbox_mode_defaults_to_workspace_write_when_tools_absent(codex_sync):
    assert codex_sync.sandbox_mode_for(None) == "workspace-write"


def test_sandbox_mode_read_only_when_every_tool_is_read_only(codex_sync):
    assert codex_sync.sandbox_mode_for("Read, Grep, Glob") == "read-only"
    assert codex_sync.sandbox_mode_for(["Read", "WebFetch"]) == "read-only"


def test_sandbox_mode_workspace_write_when_any_write_tool_present(codex_sync):
    assert codex_sync.sandbox_mode_for("Read, Write") == "workspace-write"
    assert codex_sync.sandbox_mode_for("Bash") == "workspace-write"


# --------------------------------------------------------------------------
# Tier agent rendering — golden match against the committed openai output
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["sonnet", "opus", "fable"])
def test_render_tier_agent_matches_committed_openai_output(codex_sync, tier):
    repo_root = Path(__file__).resolve().parents[1]
    profile_data = codex_sync.load_profile("openai")
    generated = codex_sync.render_tier_agent("openai", tier, profile_data)
    committed = (repo_root / "agents" / f"{tier}.toml").read_text(encoding="utf-8")
    assert generated == committed


def test_render_tier_agent_openrouter_free_uses_single_model(codex_sync):
    profile_data = codex_sync.load_profile("openrouter-free")
    generated = codex_sync.render_tier_agent("openrouter-free", "opus", profile_data)
    assert 'model = "openrouter/free"' in generated
    assert "not committed" in generated


# --------------------------------------------------------------------------
# Project agent generation + idempotency + AGENTS.md never-overwrite
# --------------------------------------------------------------------------


def test_sync_project_agents_idempotent(codex_sync, repo, write_agent_md):
    write_agent_md(repo, "foo", "You are foo.\n")
    profile_data = codex_sync.load_profile("openai")

    first = codex_sync.sync_project_agents(repo, profile_data, dry_run=False)
    assert dict(first)[".codex/agents/foo.toml"] == "created"

    second = codex_sync.sync_project_agents(repo, profile_data, dry_run=False)
    assert dict(second)[".codex/agents/foo.toml"] == "unchanged"

    out = (repo / ".codex" / "agents" / "foo.toml").read_text(encoding="utf-8")
    assert 'name = "foo"' in out
    assert 'model = "gpt-5.6-luna"' in out  # model: sonnet -> openai profile's sonnet model
    assert "You are foo." in out


def test_sync_project_agents_dry_run_writes_nothing(codex_sync, repo, write_agent_md):
    write_agent_md(repo, "foo", "You are foo.\n")
    profile_data = codex_sync.load_profile("openai")
    results = codex_sync.sync_project_agents(repo, profile_data, dry_run=True)
    assert dict(results)[".codex/agents/foo.toml"] == "would-create"
    assert not (repo / ".codex").exists()


def test_effort_frontmatter_overrides_profile_default(codex_sync, repo, write_agent_md):
    write_agent_md(repo, "foo", "Body.\n", model="sonnet", effort="low")
    profile_data = codex_sync.load_profile("openai")
    codex_sync.sync_project_agents(repo, profile_data, dry_run=False)
    out = (repo / ".codex" / "agents" / "foo.toml").read_text(encoding="utf-8")
    # profile default for sonnet is xhigh; the agent's own `effort: low` wins.
    assert 'model_reasoning_effort = "low"' in out


def test_agents_md_stub_created_once_then_never_overwritten(codex_sync, repo):
    status1 = codex_sync.ensure_agents_md_stub(repo, dry_run=False)
    assert status1 == "created"
    assert "CLAUDE.md" in (repo / "AGENTS.md").read_text(encoding="utf-8")

    (repo / "AGENTS.md").write_text("Marc's own custom AGENTS.md content.\n", encoding="utf-8")
    status2 = codex_sync.ensure_agents_md_stub(repo, dry_run=False)
    assert "never overwritten" in status2
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == "Marc's own custom AGENTS.md content.\n"


def test_agents_md_stub_dry_run_does_not_create(codex_sync, repo):
    status = codex_sync.ensure_agents_md_stub(repo, dry_run=True)
    assert status == "would-create"
    assert not (repo / "AGENTS.md").exists()


# --------------------------------------------------------------------------
# Skills symlink farm
# --------------------------------------------------------------------------


def test_sync_skills_farm_creates_symlinks_and_is_idempotent(codex_sync, home):
    (home / ".claude" / "skills" / "some-skill").mkdir(parents=True)
    first = codex_sync.sync_skills_farm(home, dry_run=False)
    assert dict(first)["~/.agents/skills/some-skill"] == "created"
    link = home / ".agents" / "skills" / "some-skill"
    assert link.is_symlink()

    second = codex_sync.sync_skills_farm(home, dry_run=False)
    assert dict(second)["~/.agents/skills/some-skill"] == "unchanged"


def test_sync_skills_farm_dry_run_writes_nothing(codex_sync, home):
    (home / ".claude" / "skills" / "some-skill").mkdir(parents=True)
    results = codex_sync.sync_skills_farm(home, dry_run=True)
    assert dict(results)["~/.agents/skills/some-skill"] == "would-create"
    assert not (home / ".agents").exists()


# --------------------------------------------------------------------------
# MCP server merge — never touches secrets, never re-adds an existing server
# --------------------------------------------------------------------------


def test_redact_env_withholds_only_secret_looking_keys(codex_sync):
    env = {
        "HOMEASSISTANT_URL": "http://192.168.6.13:8123",
        "HOMEASSISTANT_TOKEN": "eyJhbGciOi...",
        "UNIFI_USERNAME": "mcp",
        "UNIFI_PASSWORD": "derqUf-kadkad-6pofba",
    }
    out = codex_sync._redact_env(env)
    assert out["HOMEASSISTANT_URL"] == "http://192.168.6.13:8123"
    assert out["UNIFI_USERNAME"] == "mcp"
    assert "REPLACE_ME" in out["HOMEASSISTANT_TOKEN"]
    assert "REPLACE_ME" in out["UNIFI_PASSWORD"]
    assert "derqUf" not in out["UNIFI_PASSWORD"]


def test_redact_env_keeps_reference_style_secret_values(codex_sync):
    env = {"API_KEY": "$MY_SECRET_ENV_VAR", "TOKEN_PATH": "~/.claude/secrets/some-token"}
    out = codex_sync._redact_env(env)
    assert out["API_KEY"] == "$MY_SECRET_ENV_VAR"
    assert out["TOKEN_PATH"] == "~/.claude/secrets/some-token"


def test_sync_mcp_servers_appends_new_and_never_writes_secret(codex_sync, home):
    home.joinpath(".claude.json").write_text(
        json.dumps({"mcpServers": {"home-assistant": {"command": "uvx", "args": ["ha-mcp"], "env": {"HOMEASSISTANT_TOKEN": "supersecret"}}}}),
        encoding="utf-8",
    )
    results = codex_sync.sync_mcp_servers(home, repo=None, dry_run=False)
    assert dict(results)["mcp_servers.home-assistant"] == "appended"
    content = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "supersecret" not in content
    assert "REPLACE_ME" in content
    assert "[mcp_servers.home-assistant]" in content


def test_sync_mcp_servers_skips_server_already_in_config(codex_sync, home):
    codex_dir = home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(
        '[mcp_servers.home-assistant]\ncommand = "uvx"\n', encoding="utf-8"
    )
    home.joinpath(".claude.json").write_text(
        json.dumps({"mcpServers": {"home-assistant": {"command": "uvx", "args": []}}}),
        encoding="utf-8",
    )
    before = (codex_dir / "config.toml").read_text(encoding="utf-8")
    results = codex_sync.sync_mcp_servers(home, repo=None, dry_run=False)
    assert dict(results)["mcp_servers.home-assistant"].startswith("unchanged")
    after = (codex_dir / "config.toml").read_text(encoding="utf-8")
    assert before == after, "an already-configured server must never be touched"


def test_collect_mcp_servers_merges_home_and_repo(codex_sync, home, repo):
    home.joinpath(".claude.json").write_text(
        json.dumps({"mcpServers": {"home-assistant": {"command": "uvx"}}}), encoding="utf-8"
    )
    repo.joinpath(".mcp.json").write_text(
        json.dumps({"mcpServers": {"unifi-network": {"command": "uvx"}}}), encoding="utf-8"
    )
    servers = codex_sync.collect_mcp_servers(home, repo)
    assert set(servers) == {"home-assistant", "unifi-network"}


# --------------------------------------------------------------------------
# End-to-end CLI: dry-run must never write, exit 0
# --------------------------------------------------------------------------


def test_main_dry_run_end_to_end_writes_nothing(codex_sync, home, repo, capsys, write_agent_md):
    write_agent_md(repo, "foo", "Body.\n")
    rc = codex_sync.main(
        ["--profile", "openai", "--repo", str(repo), "--home", str(home), "--global", "--dry-run"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert not (home / ".codex").exists()
    assert not (repo / ".codex").exists()
    assert not (repo / "AGENTS.md").exists()


def test_main_real_run_is_idempotent_on_second_invocation(codex_sync, home, repo, capsys, write_agent_md):
    write_agent_md(repo, "foo", "Body.\n")
    args = ["--profile", "openai", "--repo", str(repo), "--home", str(home), "--global"]
    assert codex_sync.main(args) == 0
    capsys.readouterr()
    assert codex_sync.main(args) == 0
    out = capsys.readouterr().out
    assert "0 of" in out.splitlines()[-1] or "\n0 of" in out


def test_frontmatter_double_quoted_scalar_is_unescaped(codex_sync):
    fm, _ = codex_sync.parse_frontmatter(
        '---\nname: x\ndescription: "Answer any \\"what does X say\\" question"\n---\nbody\n'
    )
    assert fm["description"] == 'Answer any "what does X say" question'
    # and the TOML emitter round-trips it as a plain quote, not \\"
    assert codex_sync._toml_str(fm["description"]) == '"Answer any \\"what does X say\\" question"'
