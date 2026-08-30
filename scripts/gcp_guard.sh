#!/usr/bin/env bash
# DRIFTZERO cloud guard — sourced before every mutating gcloud command.
#
# The legacy project driftzero-agentic-2026 is quarantined. It is not enough to pass
# --project explicitly: gcloud also routes billing and quota through
# billing/quota_project, which is how a read of the runtime project's budgets was
# observed being attributed to the legacy project. Both must be checked.

set -euo pipefail

DZ_PROJECT="driftzero-runtime-2026"
DZ_LEGACY="driftzero-agentic-2026"
DZ_REGION="us-central1"          # quickstart MS-7: the single selected region
# The billing account is an account identifier, not a secret, but it has no place in
# a public repository. Supply it from the environment when a guard actually needs it.
DZ_BILLING="${DZ_BILLING:-}"

dz_guard() {
  local core quota
  core="$(gcloud config get-value core/project 2>/dev/null || true)"
  quota="$(gcloud config get-value billing/quota_project 2>/dev/null || true)"

  if [ "$core" = "$DZ_LEGACY" ]; then
    echo "GUARD: core/project is the quarantined legacy project" >&2; return 1
  fi
  if [ "$quota" = "$DZ_LEGACY" ]; then
    echo "GUARD: billing/quota_project is the quarantined legacy project" >&2; return 1
  fi
  if [ "$core" != "$DZ_PROJECT" ]; then
    echo "GUARD: core/project is '$core', expected '$DZ_PROJECT'" >&2; return 1
  fi
  return 0
}

# Every mutation goes through this wrapper: it re-checks immediately before the call
# and pins --project, so a stale shell can never mutate the wrong project.
dzg() {
  dz_guard || { echo "GUARD: refusing to run: gcloud $*" >&2; return 1; }
  for arg in "$@"; do
    if [ "$arg" = "--project=$DZ_LEGACY" ]; then
      echo "GUARD: explicit legacy --project in argv" >&2; return 1
    fi
  done
  gcloud "$@" --project="$DZ_PROJECT"
}
