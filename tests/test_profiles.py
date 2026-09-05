"""Profile schema tests — independent of bin/codex-sync so a broken sync
script can never mask a broken profile file. Uses the stdlib tomllib
directly."""
import tomllib
from pathlib import Path

import pytest

PROFILES_DIR = Path(__file__).resolve().parents[1] / "profiles"
PROFILE_NAMES = ["openai", "openrouter-free", "anthropic-api"]


def load(name):
    with (PROFILES_DIR / f"{name}.toml").open("rb") as f:
        return tomllib.load(f)


@pytest.mark.parametrize("name", PROFILE_NAMES)
def test_profile_parses_and_has_provider(name):
    data = load(name)
    assert data.get("provider") == name


def test_openai_profile_tier_values():
    data = load("openai")
    tiers = data["tiers"]
    expected = {
        "chair": ("gpt-6-astra", "high"),
        "fable": ("gpt-6-astra", "high"),
        "opus": ("gpt-5.6-sol", "high"),
        "sonnet": ("gpt-5.6-luna", "xhigh"),
        "cheap": ("gpt-5.6-luna", "low"),
    }
    for tier, (model, effort) in expected.items():
        assert tiers[tier]["model"] == model, tier
        assert tiers[tier]["model_reasoning_effort"] == effort, tier
    assert tiers["chair"]["chair_fallback"] == "gpt-5.6-sol"
    # Terra is deliberately unused anywhere in this profile.
    assert "gpt-5.6-terra" not in str(data)


def test_openai_model_ids_match_api_docs():
    """Model ids verified 2026-09-05 against developers.openai.com/api/docs/models/*
    — these are the exact `model=` API strings, not marketing names."""
    data = load("openai")
    ids = {t["model"] for t in data["tiers"].values()}
    assert ids == {"gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-luna"}


def test_openrouter_free_profile_shape():
    data = load("openrouter-free")
    assert "single" in data, "openrouter-free must use the [single] shortcut, not per-tier blocks"
    single = data["single"]
    assert single["model"] == "openrouter/openrouter/omen-alpha"
    assert single.get("model_reasoning_effort")
    assert "tiers" not in data
    assert "customer" in data.get("note", "") or "credential" in data.get("note", "")


def test_anthropic_api_profile_values():
    data = load("anthropic-api")
    tiers = data["tiers"]
    expected = {
        "chair": "claude-fable-5-1",
        "fable": "claude-fable-5-1",
        "opus": "claude-opus-5",
        "sonnet": "claude-sonnet-5",
        "cheap": "claude-haiku-4-5",
    }
    for tier, model in expected.items():
        assert tiers[tier]["model"] == model, tier
        # No reasoning-effort concept for this profile (see the file's own comment).
        assert "model_reasoning_effort" not in tiers[tier]


@pytest.mark.parametrize("name", PROFILE_NAMES)
def test_every_profile_resolves_all_five_tiers(name, codex_sync):
    data = load(name)
    for tier in ("chair", "fable", "opus", "sonnet", "cheap"):
        model, _effort = codex_sync.resolve_tier(data, tier)
        assert model, f"{name} profile has no model for tier {tier!r}"
