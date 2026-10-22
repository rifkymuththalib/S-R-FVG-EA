# S/R + FVG Expert Advisor (MT5)

MQL5 Expert Advisor that trades fresh Fair Value Gaps sitting on higher-timeframe support/resistance zones.

## How it trades

1. **S/R zones (H4):** swing highs/lows from the last 300 bars, clustered into zones (min 2 touches).
2. **FVG (M15):** three-candle gap of at least 0.3 x ATR with a displacement middle candle.
3. **Confluence:** the gap must overlap a zone (support for buys, resistance for sells), with the H4 200 EMA trend.
4. **Entry:** limit order at the gap's 50% level (or a market order after a pin bar / engulfing candle on the retest).
5. **Risk:** 1% of equity per trade, SL beyond the gap/zone + 0.2 x ATR, TP at 2R or the next opposing zone, 3% daily loss limit.

Every number above is an input.

## Install

1. Copy `SR_FVG_EA.mq5` to `MQL5/Experts/` (MetaTrader: File > Open Data Folder).
2. Open it in MetaEditor and press F7 to compile.
3. Attach to a chart, allow Algo Trading.

## Backtest

Strategy Tester (Ctrl+R): pick the EA and symbol, model "Every tick based on real ticks", 2-3 years.
Run once in visual mode first to check the zones (grey) and gaps (green/red) look right.
Compare against `InpUseSRFilter = false` to see if the S/R filter adds edge.
