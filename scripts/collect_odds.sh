#!/bin/sh
# J1 オッズ前向き収集: キックオフ起点スケジュール(DESIGN_PHASE2.md 8.4)。
#
# 旧版(1日2回・固定時刻)は Fri 19:00 JST 開始の試合に対して T-26.6h/T-50h
# にしかならず、"クローズ"と呼べる粒度ではなかった(DESIGN_PHASE2.md 0.18)。
# 本版は footy/pipeline/odds_schedule.py に実装されたクラスタ単位
# T-72h/T-24h/T-6h/T-2h/T-25min の5点スケジュールを1回分だけ実行する。
#
# 冪等: 状態は data/odds_snapshots/.schedule_state.json に持ち、既に取得
# 済みの点は再実行しても再取得しない(=再度クレジットを消費しない)。その
# ため cron の実行頻度を上げても安全(推奨: 15分毎。下記コマンドは実行す
# るだけで、実際の crontab は変更しない)。
#
#   */15 * * * * cd /path/to/footy-ev && ./scripts/collect_odds.sh >> data/odds_snapshots/cron.log 2>&1
#
# 月間クレジット見積り(DESIGN_PHASE2.md 8.4): 5点 x 4.5クラスタ x 4.3節/月
# ≈ 97 クレジット/月(カップ戦込みで150〜200)。月500枠に対して余裕あり。
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/.env"

export ODDS_API_KEY

exec "$ROOT/bin/footy" odds schedule
