#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Keep the GitHub organisation variables that workflows read for runner OS versions equal to
# versions.toml [ci].
#
# Usage:
#   scripts/sync-org-vars.sh            DRY RUN (default): read each organisation's current value
#                                       and print the `gh variable set` command that would make it
#                                       equal versions.toml, marked `same` or `DIFFERS`.
#   scripts/sync-org-vars.sh --apply    run the commands for the variables that differ.
#
# Needs the GitHub CLI authenticated with permission to read (and, for --apply, write)
# organisation variables. Exit: 0 all equal (or applied), 1 a variable differs in a dry run or
# could not be read, 2 bad arguments.
set -euo pipefail

apply=0
case "${1:-}" in
  "") ;;
  --apply) apply=1 ;;
  -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
  *) echo "unknown argument: $1" >&2; exit 2 ;;
esac

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# "org name value" lines from versions.toml (python3 >= 3.11 for tomllib). Plain loops, no
# mapfile, so the stock macOS bash 3.2 runs this too.
plan=$(python3 - "$root/versions.toml" <<'PY'
import sys, tomllib
ci = tomllib.load(open(sys.argv[1], "rb"))["ci"]
for org in ci["orgs"]:
    print(org, ci["ubuntu_var"], ci["ubuntu"])
    print(org, ci["macos_var"], ci["macos"])
PY
)

status=0
last_org=""; current=""
while read -r org name want; do
  if [ "$org" != "$last_org" ]; then
    last_org=$org
    current=$(gh variable list --org "$org" 2>/dev/null) || {
      echo "# $org: cannot read variables" >&2; status=1; current=""; }
  fi
  have=$(awk -v n="$name" '$1 == n {print $2}' <<<"$current")
  cmd="gh variable set $name --org $org --visibility all --body $want"
  if [ "$have" = "$want" ]; then
    echo "same     $cmd    # current: $have"
  else
    echo "DIFFERS  $cmd    # current: ${have:-<unset>}"
    if [ $apply = 1 ]; then
      $cmd || status=1
    else
      status=1
    fi
  fi
done <<<"$plan"
[ $apply = 1 ] || echo "# dry run: nothing changed (pass --apply to run the DIFFERS lines)"
exit $status
