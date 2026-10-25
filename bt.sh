#!/bin/bash
# Compile SR_FVG_EA and backtest it in the portable MT5 under ../MT5-trading (bundled Wine + FTMO history).
# Usage: ./bt.sh [SYMBOL] [FROM] [TO] [Input=Value ...]
#   ./bt.sh XAUUSD 2024.01.01 2026.10.01 InpUseSRFilter=false
# An input given as Input=start||step||stop runs the optimizer over it instead (OPT=1 full grid, default 2 genetic).
set -e
SYM=${1:-XAUUSD}; FROM=${2:-2024.01.01}; TO=${3:-2026.10.01}; shift $(( $# < 3 ? $# : 3 ))
HERE="$(cd "$(dirname "$0")" && pwd)"
BASE="${MT5_BASE:-$HERE/../MT5-trading}"
T="$BASE/prefix/drive_c/${MT5_DIR:-MT5-tester}"   # MT5_DIR=MT5-w1.. for parallel runs (tools/grid.sh)
export WINEPREFIX="$BASE/prefix" WINEDEBUG=-all PATH="$BASE/wine/bin:$PATH"
# Don't use the optimizer (Input=a||b||c) on long ranges: MT5 starts an agent per CPU, each loading all ticks,
# and 20 of them exhaust 30 GB RAM. tools/grid.sh runs single tests in parallel worker folders instead.

cp "$HERE/SR_FVG_EA.mq5" "$T/MQL5/Experts/"
(cd "$T" && wine MetaEditor64.exe /portable /compile:"MQL5\\Experts\\SR_FVG_EA.mq5" /log:"logs\\srfvg_compile.log") || true
LOG=$(iconv -f utf-16 -t utf-8 "$T/logs/srfvg_compile.log" | tr -d '\r')
echo "$LOG" | grep -E 'error|warning|Result' || true
echo "$LOG" | grep -q ' 0 errors' || { echo "compile failed"; exit 1; }

NAME="srfvg_${SYM}_${FROM//./}_${TO//./}_$(echo "$*" | md5sum | cut -c1-6)"
# Stale report, remembered inputs (unlisted inputs would inherit the last run's values) and cached results.
rm -f "$T/reports/$NAME"* "$T/MQL5/Profiles/Tester/SR_FVG_EA.set" "$T/Tester/cache/SR_FVG_EA."*
cat > "$T/$NAME.ini" <<EOF
[Experts]
Enabled=0
AllowLiveTrading=0
AllowDllImport=0
[Tester]
Expert=SR_FVG_EA.ex5
Symbol=$SYM
Period=M15
Optimization=$( [[ "$*" == *"||"* ]] && echo "${OPT:-2}" || echo 0)
OptimizationCriterion=6
Model=4
FromDate=$FROM
ToDate=$TO
ForwardMode=0
Deposit=100000
Currency=USD
Leverage=100
ExecutionMode=0
Visual=0
Report=reports\\$NAME
ReplaceReport=1
ShutdownTerminal=1
[TesterInputs]
InpDraw=false
$(for a in "$@"; do [[ $a == *"||"* ]] && echo "${a%%=*}=0||${a#*=}||Y" || echo "$a"; done)
EOF

echo "backtesting $SYM $FROM-$TO $*"
# Own network namespace: each worker gets a private localhost, so parallel agents can all bind port 3000.
# Tests run offline from the local history in Bases/.
(cd "$T" && if [ -n "$MT5_DIR" ]; then unshare -rn sh -c 'ip link set lo up && exec wine terminal64.exe /portable /config:"$0.ini"' "$NAME"; else wine terminal64.exe /portable /config:"$NAME.ini"; fi)
if [ -f "$T/reports/$NAME.xml" ]; then python3 "$HERE/tools/opt.py" "$T/reports/$NAME.xml"; exit; fi
python3 "$BASE/prefix/drive_c/MT5-tester/summary.py" "$T/reports/$NAME.htm"
echo "report: $T/reports/$NAME.htm"
