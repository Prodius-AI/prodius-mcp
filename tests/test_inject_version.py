"""Tests for `ci/inject_pypi_version.sh`.

The injector is the only piece of the publish flow that turns the
partial-version-in-git into the final string PyPI sees. A silent regression
here would either block a real-PyPI release at the publish step or — worse —
let a malformed version slip through. These tests run the actual bash
script against a throwaway pyproject and assert the rewritten version.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "ci" / "inject_pypi_version.sh"
MINIMAL_PYPROJECT = textwrap.dedent(
    """\
    [build-system]
    requires = ["setuptools>=68.0"]
    build-backend = "setuptools.build_meta"

    [project]
    name = "prodius-mcp"
    version = "0.2.0"
    description = "Test fixture"
    """
)


def _bash() -> str:
    """Locate a usable bash. Required on Windows runners where it may not be on PATH."""
    found = shutil.which("bash")
    if not found:
        pytest.skip("bash not available on this runner")
    return found


def _make_project(tmp_path: Path, pyproject_body: str = MINIMAL_PYPROJECT) -> Path:
    """Lay out a fixture package directory with the injector available.

    The script resolves PACKAGE_DIR as the parent of its own ci/ folder, so we
    have to copy it into a fake `ci/` rather than invoking the source path —
    otherwise it would mutate the real repo's pyproject.toml.
    """
    project = tmp_path / "fake-pkg"
    (project / "ci").mkdir(parents=True)
    shutil.copy(SCRIPT, project / "ci" / SCRIPT.name)
    (project / "pyproject.toml").write_text(pyproject_body, encoding="utf-8")
    return project


def _run(project: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_bash(), str(project / "ci" / SCRIPT.name), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _injected_version(project: Path) -> str:
    for line in (project / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("version"):
            return stripped.split("=", 1)[1].strip().strip('"')
    raise AssertionError("no version line found in pyproject.toml")


# ── Channel mapping ────────────────────────────────────────────────


def test_stable_branch_produces_bare_release(tmp_path: Path):
    project = _make_project(tmp_path)
    result = _run(project, "--pipeline-id", "12547", "--branch", "stable")
    assert result.returncode == 0, result.stderr
    assert _injected_version(project) == "0.2.12547"
    assert result.stdout.strip().splitlines()[-1] == "0.2.12547"


def test_beta_branch_produces_pep440_prerelease(tmp_path: Path):
    project = _make_project(tmp_path)
    result = _run(project, "--pipeline-id", "12547", "--branch", "beta")
    assert result.returncode == 0, result.stderr
    assert _injected_version(project) == "0.2.12547b1"


def test_feature_branch_produces_dev_release(tmp_path: Path):
    project = _make_project(tmp_path)
    result = _run(project, "--pipeline-id", "12547", "--branch", "feature/foo")
    assert result.returncode == 0, result.stderr
    assert _injected_version(project) == "0.2.12547.dev1"


def test_unknown_branch_falls_through_to_alpha(tmp_path: Path):
    """Anything that isn't stable/beta is alpha — that's the whole point of
    the catch-all in the case statement.
    """
    project = _make_project(tmp_path)
    result = _run(project, "--pipeline-id", "1", "--branch", "hotfix/urgent")
    assert result.returncode == 0, result.stderr
    assert _injected_version(project) == "0.2.1.dev1"


# ── X.Y extraction ─────────────────────────────────────────────────


def test_three_segment_version_in_git_is_truncated_to_xy(tmp_path: Path):
    """Patch segment in git is meaningless — only X.Y is read."""
    pyproject = MINIMAL_PYPROJECT.replace('version = "0.2.0"', 'version = "1.5.99"')
    project = _make_project(tmp_path, pyproject)
    result = _run(project, "--pipeline-id", "42", "--branch", "stable")
    assert result.returncode == 0, result.stderr
    assert _injected_version(project) == "1.5.42"


def test_partial_version_in_git_works(tmp_path: Path):
    """X.Y without a patch is also valid — we only need the first two segments."""
    pyproject = MINIMAL_PYPROJECT.replace('version = "0.2.0"', 'version = "3.7"')
    project = _make_project(tmp_path, pyproject)
    result = _run(project, "--pipeline-id", "10", "--branch", "beta")
    assert result.returncode == 0, result.stderr
    assert _injected_version(project) == "3.7.10b1"


# ── Failure modes ─────────────────────────────────────────────────


def test_missing_pipeline_id_fails(tmp_path: Path):
    project = _make_project(tmp_path)
    # Make sure CI_PIPELINE_ID isn't leaking in from the test runner's env.
    result = _run(project, "--branch", "stable", env={"PATH": _path_env()})
    assert result.returncode == 1
    assert "CI_PIPELINE_ID" in result.stderr


def test_non_numeric_pipeline_id_fails(tmp_path: Path):
    project = _make_project(tmp_path)
    result = _run(project, "--pipeline-id", "abc", "--branch", "stable")
    assert result.returncode == 1
    assert "non-negative integer" in result.stderr


def test_missing_branch_fails(tmp_path: Path):
    project = _make_project(tmp_path)
    # Make sure CI_COMMIT_BRANCH isn't leaking in from the test runner's env.
    result = _run(project, "--pipeline-id", "1", env={"PATH": _path_env()})
    assert result.returncode == 1
    assert "CI_COMMIT_BRANCH" in result.stderr


def test_malformed_pyproject_version_fails(tmp_path: Path):
    pyproject = MINIMAL_PYPROJECT.replace('version = "0.2.0"', 'version = "garbage"')
    project = _make_project(tmp_path, pyproject)
    result = _run(project, "--pipeline-id", "1", "--branch", "stable")
    assert result.returncode == 1
    assert "not X.Y" in result.stderr


def test_dry_run_does_not_mutate_pyproject(tmp_path: Path):
    project = _make_project(tmp_path)
    before = (project / "pyproject.toml").read_text(encoding="utf-8")
    result = _run(project, "--dry-run", "--pipeline-id", "5", "--branch", "beta")
    assert result.returncode == 0, result.stderr
    after = (project / "pyproject.toml").read_text(encoding="utf-8")
    assert before == after
    assert result.stdout.strip().splitlines()[-1] == "0.2.5b1"


# ── Helpers ────────────────────────────────────────────────────────


def _path_env() -> str:
    """Pass through PATH so bash/sed/grep are still resolvable in the subprocess.

    Used when we need to scrub CI_COMMIT_BRANCH out of the inherited env.
    """
    import os

    return os.environ.get("PATH", "")
