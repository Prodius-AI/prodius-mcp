#!/usr/bin/env bash
# Wheel smoke test for prodius-mcp.
#
# Builds a clean venv, installs a wheel from dist/, and asserts that:
#   1. The package and its three submodules import cleanly.
#   2. The three console scripts are on PATH after install.
#   3. `prodius-mcp` exits cleanly when given EOF on stdin
#      (i.e. the JSON-RPC NDJSON loop terminates without raising).
#
# This catches the #1 wheel packaging regression class — console scripts
# that point at the wrong import path. The smoke test runs against the
# wheel bytes that will be uploaded, not an editable install.
#
# Invocation:
#   ./ci/smoke_test.sh [WHEEL_PATH]
#
#   WHEEL_PATH defaults to dist/*.whl (the first match).
#
# Exit codes:
#   0 — all checks passed.
#   non-zero — first failure aborts the script.
set -euo pipefail

PACKAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WHEEL_PATH="${1:-}"

if [[ -z "${WHEEL_PATH}" ]]; then
    # Pick the first wheel in dist/. Fail if zero or more than one — the
    # caller should disambiguate; a wheel-build glob silently picking the
    # wrong one is a footgun.
    shopt -s nullglob
    WHEEL_CANDIDATES=("${PACKAGE_DIR}/dist/"*.whl)
    shopt -u nullglob
    if (( ${#WHEEL_CANDIDATES[@]} == 0 )); then
        echo "smoke_test: ERROR — no wheels found in ${PACKAGE_DIR}/dist/" >&2
        exit 2
    fi
    if (( ${#WHEEL_CANDIDATES[@]} > 1 )); then
        echo "smoke_test: ERROR — multiple wheels in ${PACKAGE_DIR}/dist/, pass explicit path:" >&2
        printf '  %s\n' "${WHEEL_CANDIDATES[@]}" >&2
        exit 2
    fi
    WHEEL_PATH="${WHEEL_CANDIDATES[0]}"
fi

if [[ ! -f "${WHEEL_PATH}" ]]; then
    echo "smoke_test: ERROR — wheel not found: ${WHEEL_PATH}" >&2
    exit 2
fi

# mktemp -d gives us a venv path that's unique per run and easy to nuke.
VENV_DIR="$(mktemp -d -t prodius-mcp-smoke-XXXXXX)"
trap 'rm -rf "${VENV_DIR}"' EXIT

PYTHON_BIN="${PYTHON:-python3}"

echo "smoke_test: building clean venv at ${VENV_DIR}"
"${PYTHON_BIN}" -m venv "${VENV_DIR}"

VENV_PY="${VENV_DIR}/bin/python"
VENV_PIP="${VENV_DIR}/bin/pip"
if [[ ! -x "${VENV_PY}" ]]; then
    # Windows layout (bash on Windows / Git Bash). Not used by Debian CI
    # runners but lets the script double as a local-validate helper.
    VENV_PY="${VENV_DIR}/Scripts/python.exe"
    VENV_PIP="${VENV_DIR}/Scripts/pip.exe"
fi

echo "smoke_test: installing ${WHEEL_PATH}"
"${VENV_PIP}" install --quiet --upgrade pip
"${VENV_PIP}" install --quiet "${WHEEL_PATH}"

echo "smoke_test: importing the package and its submodules"
# Includes ``prodius_mcp.__main__`` so a wheel missing __main__.py (which the
# test suite hits via ``python -m prodius_mcp``) fails here instead of in
# next release's pytest matrix.
"${VENV_PY}" -c "import prodius_mcp, prodius_mcp.__main__, prodius_mcp.reset, prodius_mcp.permission_hook, prodius_mcp._config; print('imports OK:', prodius_mcp.__version__)"

# Walk through each console script: assert it exists in the venv's bin dir
# (not just on PATH — we want the wheel's entry point, not an outer install).
VENV_BIN_DIR="$(dirname "${VENV_PY}")"
for script in prodius-mcp prodius-mcp-reset prodius-permission-hook; do
    if [[ -x "${VENV_BIN_DIR}/${script}" ]] || [[ -x "${VENV_BIN_DIR}/${script}.exe" ]]; then
        echo "smoke_test: console script present: ${script}"
    else
        echo "smoke_test: ERROR — console script missing: ${VENV_BIN_DIR}/${script}" >&2
        ls -la "${VENV_BIN_DIR}" >&2
        exit 1
    fi
done

# `prodius-mcp` runs an NDJSON stdio loop. Feeding it EOF (no stdin) should
# end the loop and exit 0. Anything else means the shim is broken — either
# the entry point doesn't resolve, or an import-time error escaped.
#
# Timeout guard: if the loop hangs (e.g. trying to dial a desktop app it
# was never given a token for), kill it after 10 s so the CI job doesn't
# stall. `timeout` returns 124 on kill; we surface whatever rc came back.
#
# Capture rc with `|| rc=$?` rather than `if ! ...; then rc=$?` — the
# latter captures the negated rc (always 0 inside the `then` branch on
# some shells), losing the real exit code in the error message.
echo "smoke_test: running prodius-mcp with EOF stdin (should exit cleanly)"
rc=0
timeout 10s "${VENV_BIN_DIR}/prodius-mcp" </dev/null || rc=$?
if [[ ${rc} -ne 0 ]]; then
    echo "smoke_test: ERROR — prodius-mcp exited non-zero (rc=${rc}) on EOF stdin" >&2
    exit 1
fi

echo "smoke_test: all checks passed"
