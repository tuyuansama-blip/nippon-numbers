#!/usr/bin/env bash
# The data boundary of docs/DESIGN_SITE.md 2.6, enforced on every push.
#
# 2.6 is the one rule in this project whose violation cannot be undone by a
# later commit: once a raw The Odds API snapshot is pushed to a public
# repository it is in the history, in every clone, and in GitHub's own
# forks/caches. `.gitignore` is the first line of defence and this is the
# second -- it asks git what is actually *tracked*, which `.gitignore` has no
# say over once a file has been added with `git add -f`.
set -euo pipefail

fail=0

check() {
    local pattern="$1" why="$2"
    local hits
    hits=$(git ls-files -- "$pattern" || true)
    if [ -n "$hits" ]; then
        echo "::error::tracked file(s) matching '$pattern' -- $why"
        echo "$hits" | sed 's/^/    /'
        fail=1
    fi
}

# The Odds API forbids redistributing its feed as a standalone data product
# (DESIGN_SITE.md 0.10, 2.6). Raw snapshots live in R2, never here.
check 'data/odds_snapshots/*' 'raw odds feed must never enter the public repo (DESIGN_SITE.md 2.6)'
# football-data.co.uk publishes no redistribution terms; 2.6 declines the risk.
check 'data/raw/*' 're-fetchable source CSVs are deliberately not redistributed (DESIGN_SITE.md 2.6)'
# The live API key.
check '.env' 'holds a live third-party API key'
check '.env.*' 'holds a live third-party API key'

if [ "$fail" -ne 0 ]; then
    echo
    echo "Nothing above is fixed by deleting the file in a new commit: the bytes"
    echo "stay in the history. Rewrite the history before the branch is pushed"
    echo "anywhere, and rotate any key that was exposed."
    exit 1
fi

echo "data boundary ok: no raw feed, source CSV or .env is tracked"
