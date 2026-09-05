"""Shared fixtures for codex-orchestrator's own tests (not core/'s tests —
those are untouched fable5-opus5-orchestrator tests, run separately against
core/, see the CI workflow and README "Pull-core workflow").
"""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CODEX_SYNC_PATH = REPO / "bin" / "codex-sync"


def _load_codex_sync():
    # bin/codex-sync has no .py extension (it's an executable), so it can't
    # be `import`-ed normally — load it as a module by path instead.
    loader = SourceFileLoader("codex_sync", str(CODEX_SYNC_PATH))
    spec = importlib.util.spec_from_loader("codex_sync", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def codex_sync():
    return _load_codex_sync()


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".claude" / "skills").mkdir(parents=True)
    (h / ".claude.json").write_text("{}", encoding="utf-8")
    return h


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / ".claude" / "agents").mkdir(parents=True)
    (r / ".git").mkdir()
    return r


@pytest.fixture
def write_agent_md():
    def _write(repo_path, name, body, *, model="sonnet", effort=None, extra_frontmatter=""):
        fm = f'name: {name}\ndescription: "{name} test agent"\nmodel: {model}\n'
        if effort:
            fm += f"effort: {effort}\n"
        fm += extra_frontmatter
        text = f"---\n{fm}---\n\n{body}\n"
        path = repo_path / ".claude" / "agents" / f"{name}.md"
        path.write_text(text, encoding="utf-8")
        return path

    return _write
