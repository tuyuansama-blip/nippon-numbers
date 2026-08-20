#!/bin/sh
# J1 オッズ前向き収集: The Odds API から h2h オッズを取得し
# タイムスタンプ付き JSON で data/odds_snapshots/ に保存する。
# cron から呼ばれる前提(2回/日)。1回の実行で 1 クレジット消費(月500枠)。
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/.env"

SPORT="soccer_japan_j_league"
OUTDIR="$ROOT/data/odds_snapshots"
LOG="$OUTDIR/collect.log"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$OUTDIR/j1_h2h_eu_$TS.json"
HDR="$OUTDIR/.last_headers.txt"

mkdir -p "$OUTDIR"

HTTP_CODE=$(curl -s -w "%{http_code}" -D "$HDR" -o "$OUT" \
  "https://api.the-odds-api.com/v4/sports/$SPORT/odds/?apiKey=$ODDS_API_KEY&regions=eu&markets=h2h&oddsFormat=decimal")

REMAIN=$(grep -i '^x-requests-remaining' "$HDR" | tr -d '\r' | awk '{print $2}')

if [ "$HTTP_CODE" != "200" ]; then
  echo "$TS ERROR http=$HTTP_CODE remaining=${REMAIN:-?}" >> "$LOG"
  rm -f "$OUT"
  exit 1
fi

N=$(python3 -c "import json;print(len(json.load(open('$OUT'))))" 2>/dev/null || echo "?")
echo "$TS OK matches=$N remaining=${REMAIN:-?}" >> "$LOG"
