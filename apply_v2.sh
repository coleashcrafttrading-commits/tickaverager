#!/usr/bin/env bash
# Apply the ladder-v2 profile to a ticker, through the audited path.
#
#   ./apply_v2.sh RAM              # long AND short, the trend picks the side,
#                                  # a confirmed reversal flips the position
#   ./apply_v2.sh MSTX flatten     # same gate and sizing, but a confirmed
#                                  # reversal only closes -- no other side
#
# What it sets, and why, is in engine.LADDER_V2 (side_mode=both is part of
# it). Backtested (research/ladder_v2): on the one window every gate lost --
# MSTX Feb-Sep -- the cap turned -$17k / -$50k of open drawdown into +$1.2k /
# -$5.4k (11% of equity). Nothing here changes a ticker's per-lot take-profit;
# that stays owned by /tune.
set -euo pipefail
cd "$(dirname "$0")"
SYM="${1:?usage: apply_v2.sh SYMBOL [off|flatten|reverse]}"
MODE="${2:-reverse}"
ARGS="$(.venv/Scripts/python -c 'import engine; print(engine.v2_args())')"
.venv/Scripts/python agentctl.py set "$SYM" $ARGS reversal_mode="$MODE" --actor ladder-v2
echo
echo "$SYM is on ladder v2 (reversal_mode=$MODE). Verify:"
echo "  .venv/Scripts/python agentctl.py health"
