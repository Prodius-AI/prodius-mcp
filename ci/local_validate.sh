#!/usr/bin/env bash
# Local end-to-end pipeline rehearsal for prodius-mcp.
#
# Runs every step the release pipeline runs EXCEPT the network upload.
# Lets you prove the whole publish flow works without ever touching PyPI
# or even pushing a branch. Use this before bumping the pyproject version
# for a real release.
#
# Steps:
#   0. inject_version — mutate pyproject.toml exactly the way CI would,
#                       then restore the original on exit
#   1. version_guard  — confirm the injected version isn't on PyPI already
#   2. pytest         — run the package test suite in the current Python
#   3. python -m build — produce a wheel + sdist in dist/
#   4. twine check    — strict metadata validation on the built artifacts
#   5. smoke test     — install the wheel in a clean throwaway venv,
#                       assert the three console scripts exist + run cleanly
#   6. sha256 manifest — same SHA256SUMS file the CI build job would emit
#
# Output mirrors what the CI job prints, so a green run here is a strong
# signal the CI run will be green too. No tokens needed.
#
# Override the synthetic pipeline id and branch via env vars:
#   PIPELINE_ID=12345 BRANCH=beta ./ci/local_validate.sh
#
# Defaults: BRANCH = current git branch, PIPELINE_ID = 99999999 (high enough
# that local rehearsals don't collide with any real CI version).
#
# Requires: python3, pip, internet access (for version_guard's PyPI query
# and for pip to install build/twine into a side venv).
set -euo pipefail

PACKAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON:-python3}"

cd "${PACKAGE_DIR}"

echo "==> [0/6] inject_version (local rehearsal)"
# Snapshot the unmodified pyproject so the developer's working tree is
# restored even if a later step fails. Mirrors the CI artifact contract
# without committing the patched version.
PYPROJECT_BACKUP="$(mktemp -t pyproject-XXXXXX.toml)"
cp pyproject.toml "${PYPROJECT_BACKUP}"

# All EXIT-time cleanup goes through this one function. Bash only honours
# ONE EXIT trap per shell — declaring `trap '...' EXIT` a second time
# silently REPLACES the first. The original implementation lost the
# pyproject-restore step when a later trap was added for the tools venv,
# so on any failure (or Ctrl-C, or normal exit) the developer's working
# tree was left with the injected version stamped in. Keep all cleanups
# inside this function and register the trap exactly once.
TOOLS_VENV=""
_local_validate_cleanup() {
    # Capture exit status first so steps below don't clobber it.
    local rc=$?
    if [[ -f "${PYPROJECT_BACKUP}" ]]; then
        mv "${PYPROJECT_BACKUP}" pyproject.toml || true
    fi
    if [[ -n "${TOOLS_VENV}" && -d "${TOOLS_VENV}" ]]; then
        rm -rf "${TOOLS_VENV}" || true
    fi
    return "${rc}"
}
trap _local_validate_cleanup EXIT

LOCAL_BRANCH="${BRANCH:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "feature/local")}"
LOCAL_PIPELINE_ID="${PIPELINE_ID:-99999999}"
bash ci/inject_pypi_version.sh --pipeline-id "${LOCAL_PIPELINE_ID}" --branch "${LOCAL_BRANCH}"

echo
echo "==> [1/6] version_guard"
"${PYTHON_BIN}" ci/version_guard.py

echo
echo "==> [2/6] pytest"
# Use the user's own pip so the suite picks up whatever Python they're on.
# The conftest puts src/ on sys.path so no editable install is required.
"${PYTHON_BIN}" -m pytest tests -q

# Build + twine + smoke test need `build` and `twine`. Install them into a
# side venv so they never touch the user's global Python install.
TOOLS_VENV="$(mktemp -d -t prodius-mcp-tools-XXXXXX)"
"${PYTHON_BIN}" -m venv "${TOOLS_VENV}"
TOOLS_PY="${TOOLS_VENV}/bin/python"
TOOLS_PIP="${TOOLS_VENV}/bin/pip"
if [[ ! -x "${TOOLS_PY}" ]]; then
    TOOLS_PY="${TOOLS_VENV}/Scripts/python.exe"
    TOOLS_PIP="${TOOLS_VENV}/Scripts/pip.exe"
fi
"${TOOLS_PIP}" install --quiet --upgrade pip build twine

echo
echo "==> [3/6] python -m build"
# Wipe any prior dist/ so we don't accidentally ship stale wheels.
rm -rf dist/
"${TOOLS_PY}" -m build --outdir dist/

echo
echo "==> [4/6] twine check --strict"
"${TOOLS_PY}" -m twine check --strict dist/*

echo
echo "==> [5/6] wheel smoke test"
PYTHON="${PYTHON_BIN}" bash ci/smoke_test.sh

echo
echo "==> [6/6] sha256 manifest"
( cd dist && sha256sum * | tee SHA256SUMS )

echo
echo "==============================================================="
echo "  local_validate: OK — pipeline rehearsed end-to-end."
echo "  Built artifacts in: ${PACKAGE_DIR}/dist/"
echo "  No upload was performed."
echo "==============================================================="
