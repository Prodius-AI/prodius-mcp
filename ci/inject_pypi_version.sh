#!/usr/bin/env bash
# Inject the final PyPI version into pyproject.toml before build.
#
# The version line in `pyproject.toml` is a partial
# source-of-truth: only the MAJOR.MINOR segment is meaningful in git. PATCH
# is always replaced by ${CI_PIPELINE_ID} at build time, and a PEP 440
# channel suffix is appended based on the branch:
#
#   stable branch → X.Y.${CI_PIPELINE_ID}
#   beta branch   → X.Y.${CI_PIPELINE_ID}b1
#   any other     → X.Y.${CI_PIPELINE_ID}.dev1   (alpha — feature/, hotfix/, etc.)
#
# This means CI_PIPELINE_ID monotonically owns the PATCH dimension and PyPI
# can never reject an upload as "version already exists" — the next pipeline
# always produces a higher version. `ci/version_guard.py` stays in place as
# defense-in-depth against an injector bug.
#
# Invocation:
#   ./ci/inject_pypi_version.sh                         (CI: reads env)
#   ./ci/inject_pypi_version.sh --pipeline-id N         (local rehearsal)
#   ./ci/inject_pypi_version.sh --pipeline-id N --branch beta
#   ./ci/inject_pypi_version.sh --dry-run --pipeline-id N --branch beta
#
# Required (from env or flags):
#   CI_PIPELINE_ID   — CI pipeline ID (integer, monotonic per project)
#   CI_COMMIT_BRANCH — current branch name (used for channel derivation)
#
# On success, prints the final version to stdout (last line) so the calling
# job can capture it via `$(...)` if needed.
#
# Exit codes:
#   0 — pyproject.toml updated (or dry-run completed)
#   1 — invalid arguments, missing env, or malformed pyproject.toml
#   2 — file I/O or sed failure (pyproject.toml restored from backup)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYPROJECT="${PACKAGE_DIR}/pyproject.toml"

# Parsed args
DRY_RUN=false
PIPELINE_ID="${CI_PIPELINE_ID:-}"
BRANCH="${CI_COMMIT_BRANCH:-}"

err() { echo "inject_pypi_version: ERROR — $*" >&2; }
info() { echo "inject_pypi_version: $*"; }

usage() {
    sed -n '2,/^set -euo pipefail$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
    exit 1
}

# ── Argument parsing ────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)      DRY_RUN=true; shift ;;
        --pipeline-id)  PIPELINE_ID="${2:?--pipeline-id requires a value}"; shift 2 ;;
        --branch)       BRANCH="${2:?--branch requires a value}"; shift 2 ;;
        -h|--help)      usage ;;
        *)              err "unknown argument: $1"; usage ;;
    esac
done

# ── Validation ──────────────────────────────────────────────────────
if [[ -z "${PIPELINE_ID}" ]]; then
    err "CI_PIPELINE_ID env var not set and --pipeline-id not provided"
    exit 1
fi
if ! [[ "${PIPELINE_ID}" =~ ^[0-9]+$ ]]; then
    err "pipeline id must be a non-negative integer, got: '${PIPELINE_ID}'"
    exit 1
fi
if [[ -z "${BRANCH}" ]]; then
    err "CI_COMMIT_BRANCH env var not set and --branch not provided"
    exit 1
fi
if [[ ! -f "${PYPROJECT}" ]]; then
    err "pyproject.toml not found at ${PYPROJECT}"
    exit 2
fi

# ── Read MAJOR.MINOR from pyproject.toml ────────────────────────────
# Extract the version line, then strip everything past the second dot. We
# deliberately ignore the patch segment in git — only MAJOR.MINOR matters.
CURRENT_VERSION="$(grep -E '^[[:space:]]*version[[:space:]]*=' "${PYPROJECT}" | head -n1 | sed -E 's/.*"([^"]+)".*/\1/')"
if [[ -z "${CURRENT_VERSION}" ]]; then
    err "no version = \"...\" line in ${PYPROJECT}"
    exit 1
fi
if ! [[ "${CURRENT_VERSION}" =~ ^([0-9]+)\.([0-9]+)(\.|$) ]]; then
    err "version '${CURRENT_VERSION}' in pyproject.toml is not X.Y[.Z...]"
    exit 1
fi
MAJOR="${BASH_REMATCH[1]}"
MINOR="${BASH_REMATCH[2]}"

# ── Derive channel from branch ──────────────────────────────────────
case "${BRANCH}" in
    stable) CHANNEL="stable"; SUFFIX="" ;;
    beta)   CHANNEL="beta";   SUFFIX="b1" ;;
    *)      CHANNEL="alpha";  SUFFIX=".dev1" ;;
esac

FINAL_VERSION="${MAJOR}.${MINOR}.${PIPELINE_ID}${SUFFIX}"

info "branch=${BRANCH} → channel=${CHANNEL}"
info "pyproject X.Y=${MAJOR}.${MINOR} (ignoring patch in git: '${CURRENT_VERSION}')"
info "final version: ${FINAL_VERSION}"

if "${DRY_RUN}"; then
    info "DRY RUN — pyproject.toml not modified"
    echo "${FINAL_VERSION}"
    exit 0
fi

# ── Rewrite pyproject.toml ──────────────────────────────────────────
# Cross-platform sed: BSD sed (macOS) requires '' after -i; GNU sed (Linux)
# rejects it. Detect once.
SED_INPLACE=(sed -i)
if ! sed --version >/dev/null 2>&1; then
    SED_INPLACE=(sed -i '')
fi

# Back up so we can restore on verify failure. The CI runner's workspace is
# ephemeral anyway, but local rehearsals share the dev tree.
BACKUP="${PYPROJECT}.inject-backup"
cp "${PYPROJECT}" "${BACKUP}"
trap 'if [[ -f "${BACKUP}" ]]; then mv "${BACKUP}" "${PYPROJECT}"; fi' ERR

# Anchor on the project-table version line (not, e.g., a dependency
# specifier that happens to contain "version ="). The check is conservative:
# we match the same regex grep did above.
"${SED_INPLACE[@]}" -E "s|^([[:space:]]*version[[:space:]]*=[[:space:]]*\")[^\"]+(\".*)\$|\1${FINAL_VERSION}\2|" "${PYPROJECT}"

# ── Verify ──────────────────────────────────────────────────────────
WRITTEN="$(grep -E '^[[:space:]]*version[[:space:]]*=' "${PYPROJECT}" | head -n1 | sed -E 's/.*"([^"]+)".*/\1/')"
if [[ "${WRITTEN}" != "${FINAL_VERSION}" ]]; then
    err "verify failed: pyproject now reads '${WRITTEN}', expected '${FINAL_VERSION}'"
    mv "${BACKUP}" "${PYPROJECT}"
    exit 2
fi
rm -f "${BACKUP}"
trap - ERR

info "pyproject.toml updated → ${FINAL_VERSION}"
echo "${FINAL_VERSION}"
