"""guard/rm: Layer B — the `rm` shim that sees the EXPANDED arguments.

EVERY case here runs with `SAFE_RM_DRYRUN=1`, which makes the shim
print its verdict and exit 0 BEFORE it would exec anything. No test in
this file can delete a file even if the shim were wrong, and no real
`rm` is ever invoked with a variable or empty operand — the 2026-09-09
incident happened because someone "just tried" the opposite.
"""
import subprocess

import pytest

from conftest import POSIX, REPO

SHIM = REPO / "guard" / "rm"

pytestmark = POSIX  # /bin/sh + cd -P + a POSIX $PATH; Git Bash is a rollout item


@pytest.fixture
def sandbox(tmp_path):
    """A fake HOME and a working directory inside it — the shim's
    `$PWD` allowed root then means this directory and nothing else."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    work = home / "Documents" / "git" / "project"
    work.mkdir(parents=True)
    (work / "file.txt").write_text("x", encoding="utf-8")
    (work / "build").mkdir()
    return home, work


def dry_run(sandbox, *args, **kwargs):
    home, work = sandbox
    tmpdir = kwargs.pop("tmpdir", None) or home / "tmp"
    assert not kwargs, kwargs
    proc = subprocess.run(
        ["/bin/sh", str(SHIM)] + list(args),
        cwd=str(work),
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin",
             "SAFE_RM_DRYRUN": "1", "TMPDIR": str(tmpdir)},
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr  # dry-run never execs, never fails
    return proc.stdout.strip()


def test_refuses_an_empty_operand(sandbox):
    # `rm -rf -- "$1"/*` with $1 unset arrives here as exactly this.
    assert dry_run(sandbox, "-rf", "").startswith("REFUSE")


def test_refuses_when_every_operand_vanished(sandbox):
    assert dry_run(sandbox, "-rf").startswith("REFUSE")


@pytest.mark.parametrize("target", ["/", "/Applications", "/Users", "/usr"])
def test_refuses_protected_paths(sandbox, target):
    assert dry_run(sandbox, "-rf", target).startswith("REFUSE")


def test_refuses_home_itself(sandbox):
    home, _ = sandbox
    assert dry_run(sandbox, "-rf", str(home)).startswith("REFUSE")


def test_refuses_a_recursive_delete_outside_the_allowed_roots(sandbox):
    # NOT a path under the fake HOME: pytest's tmp_path lives in the real
    # temp tree, which IS an allowed root once the roots are canonicalised.
    assert dry_run(sandbox, "-rf", "/Users/someone/Desktop/old") \
        .startswith("REFUSE")


def test_tmpdir_reached_through_a_symlink_is_an_allowed_root(tmp_path, sandbox):
    """macOS hands out `$TMPDIR=/var/folders/…` while `/var` is a symlink
    to `/private/var`, so an operand under it canonicalises into a path
    the un-canonicalised root never matched."""
    real = tmp_path / "real-tmp"
    (real / "build").mkdir(parents=True)
    link = tmp_path / "link-tmp"
    link.symlink_to(real)
    assert dry_run(sandbox, "-rf", str(link / "build"), tmpdir=link) == "ALLOW"


def test_deleting_a_symlink_itself_is_allowed(sandbox):
    """`rm -rf link` unlinks the LINK; only `rm -rf link/` walks into the
    protected tree behind it."""
    _, work = sandbox
    (work / "rootlink").symlink_to("/")
    assert dry_run(sandbox, "-rf", "rootlink") == "ALLOW"
    assert dry_run(sandbox, "-rf", "rootlink/").startswith("REFUSE")


def test_allows_a_file_inside_the_working_directory(sandbox):
    assert dry_run(sandbox, "file.txt") == "ALLOW"


def test_allows_a_recursive_delete_inside_the_working_directory(sandbox):
    assert dry_run(sandbox, "-rf", "build") == "ALLOW"
