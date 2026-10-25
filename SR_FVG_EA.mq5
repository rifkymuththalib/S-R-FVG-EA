//+------------------------------------------------------------------+
//| SR_FVG_EA.mq5                                                    |
//| Trades fresh Fair Value Gaps that sit on higher-timeframe        |
//| support/resistance zones, with fixed-percent risk per trade.     |
//+------------------------------------------------------------------+
#property copyright "RIfky"
#property version   "1.00"
#property description "Support/Resistance + Fair Value Gap Expert Advisor"

#include <Trade/Trade.mqh>

enum ENUM_ENTRY_LEVEL { LEVEL_NEAR, LEVEL_MID, LEVEL_FAR };   // where in the gap a limit order sits
enum ENUM_CONFIRM     { CONFIRM_LIMIT, CONFIRM_CANDLE };      // limit at gap level, or market after a confirming candle

input group "General"
input ulong            InpMagic           = 260101;
input ENUM_TIMEFRAMES  InpEntryTF         = PERIOD_M15;  // Entry timeframe (FVGs)
input ENUM_TIMEFRAMES  InpHTF             = PERIOD_H4;   // Higher timeframe (S/R zones, trend)
input int              InpATRPeriod       = 14;

input group "Support / Resistance"
input int              InpPivotStrength   = 3;      // Bars each side of a swing point
input int              InpSRLookback      = 300;    // HTF bars scanned for zones
input double           InpClusterATR      = 0.25;   // Merge pivots within this x HTF ATR
input int              InpMinTouches      = 2;      // Minimum pivots for a valid zone

input group "Fair Value Gap"
input double           InpFVGMinATR       = 0.3;    // Minimum gap size x ATR
input double           InpDisplaceATR     = 1.0;    // Middle candle body >= this x ATR (or >= 60% of its range)
input int              InpFVGMaxAge       = 50;     // Gap expires after this many bars
input ENUM_ENTRY_LEVEL InpEntryLevel      = LEVEL_MID;
input double           InpZoneDistATR     = 0.5;    // Max distance gap to zone x ATR

input group "Entry"
input bool             InpUseSRFilter     = true;   // false = pure FVG baseline for comparison
input ENUM_CONFIRM     InpConfirm         = CONFIRM_LIMIT;
input bool             InpUseTrendFilter  = true;   // Trade with HTF EMA only
input int              InpTrendEMA        = 200;
input int              InpPendingExpiry   = 8;      // Delete unfilled limit orders after N entry bars
input int              InpSide            = 0;      // 0 = both, 1 = longs only, -1 = shorts only

input group "Risk"
input double           InpRiskPct         = 1.0;    // Risk per trade, % of equity
input double           InpSLBufferATR     = 0.2;    // SL beyond gap/zone by this x ATR
input double           InpRR              = 2.0;    // Take profit in R
input bool             InpTPToNextZone    = true;   // Cap TP at the next opposing zone
input double           InpMinRR           = 1.5;    // Skip if the next opposing zone is closer than this R

input group "Management"
input double           InpBEAtR           = 1.0;    // Move SL to break-even at this R (0 = off)
input double           InpPartialPct      = 0;      // % closed at 1R (0 = off)
input bool             InpTrail           = false;  // Trail SL behind entry-TF swing points
input double           InpTrailStartR     = 1.5;

input group "Limits"
input double           InpDailyLossPct    = 3.0;    // No new trades after this daily loss %
input int              InpMaxTradesSymbol = 1;
input int              InpMaxTradesTotal  = 3;
input double           InpMaxSpreadATR    = 0.15;   // Skip if spread > this x entry ATR (tracks price level; 0 = off)

input group "Session (server time)"
input int              InpStartHour       = 8;
input int              InpEndHour         = 20;

input group "Visual"
input bool             InpDraw            = true;   // Draw zones and gaps

struct Zone { double top; double bot; int touches; };
struct Gap  { bool bull; double top; double bot; datetime time; };

CTrade   trade;
Zone     zones[];
Gap      gaps[];
int      hAtrEntry = INVALID_HANDLE, hAtrHTF = INVALID_HANDLE, hEma = INVALID_HANDLE;
datetime lastEntryBar = 0, lastHTFBar = 0, dayStart = 0;
double   dayStartEquity = 0;
bool     warmedUp = false;

//+------------------------------------------------------------------+
int OnInit()
{
   if(PeriodSeconds(InpHTF) <= PeriodSeconds(InpEntryTF))
   {
      Print("Higher timeframe must be above the entry timeframe");
      return INIT_PARAMETERS_INCORRECT;
   }
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(20);
   trade.SetTypeFillingBySymbol(_Symbol);

   hAtrEntry = iATR(_Symbol, InpEntryTF, InpATRPeriod);
   hAtrHTF   = iATR(_Symbol, InpHTF, InpATRPeriod);
   hEma      = iMA(_Symbol, InpHTF, InpTrendEMA, 0, MODE_EMA, PRICE_CLOSE);
   if(hAtrEntry == INVALID_HANDLE || hAtrHTF == INVALID_HANDLE || hEma == INVALID_HANDLE)
      return INIT_FAILED;
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   ObjectsDeleteAll(0, "SRFVG_");
   IndicatorRelease(hAtrEntry);
   IndicatorRelease(hAtrHTF);
   IndicatorRelease(hEma);
}

// Optimizer score: net profit per unit of equity drawdown; too few trades is not evidence.
double OnTester()
{
   if(TesterStatistics(STAT_TRADES) < 30)
      return 0;
   return TesterStatistics(STAT_PROFIT) / MathMax(1, TesterStatistics(STAT_EQUITY_DD));
}

void OnTick()
{
   UpdateDay();

   datetime htf = iTime(_Symbol, InpHTF, 0);
   if(htf != 0 && htf != lastHTFBar && BuildZones())
      lastHTFBar = htf;

   datetime bar = iTime(_Symbol, InpEntryTF, 0);
   bool newBar = bar != 0 && bar != lastEntryBar;
   ManagePositions(newBar && InpTrail);
   if(!newBar)
      return;

   if(!warmedUp)
   {
      if(!WarmUp())
         return;          // history not ready yet, retry next tick
      warmedUp = true;
   }
   else
      OnNewBar();
   lastEntryBar = bar;
}

//+------------------------------------------------------------------+
//| Support / resistance zones                                       |
//+------------------------------------------------------------------+
bool IsPivot(const MqlRates &r[], int i, int n, bool high)
{
   // r is series-ordered: i-k is newer, i+k is older
   for(int k = 1; k <= n; k++)
   {
      if(high)
      {
         if(r[i - k].high >= r[i].high || r[i + k].high > r[i].high)
            return false;
      }
      else if(r[i - k].low <= r[i].low || r[i + k].low < r[i].low)
         return false;
   }
   return true;
}

bool BuildZones()
{
   int n = InpPivotStrength;
   MqlRates r[];
   ArraySetAsSeries(r, true);
   int got = CopyRates(_Symbol, InpHTF, 1, InpSRLookback, r);
   double atr = Buf(hAtrHTF, 1);
   if(got < 2 * n + 1 || atr <= 0)
      return false;

   double p[];
   int cnt = 0;
   for(int i = n; i < got - n; i++)
   {
      if(IsPivot(r, i, n, true))  { ArrayResize(p, cnt + 1, 64); p[cnt++] = r[i].high; }
      if(IsPivot(r, i, n, false)) { ArrayResize(p, cnt + 1, 64); p[cnt++] = r[i].low; }
   }
   ArraySort(p);

   // Greedy clustering: pivots within tolerance of the cluster's lowest price form one zone.
   // A zone below price acts as support, above as resistance, so role reversal is automatic.
   ArrayResize(zones, 0);
   double tol = InpClusterATR * atr, minW = 0.1 * atr;
   for(int i = 0; i < cnt;)
   {
      int j = i;
      while(j + 1 < cnt && p[j + 1] - p[i] <= tol)
         j++;
      if(j - i + 1 >= InpMinTouches)
      {
         Zone z;
         z.bot = p[i];
         z.top = p[j];
         z.touches = j - i + 1;
         if(z.top - z.bot < minW)
         {
            double mid = (z.top + z.bot) / 2;
            z.top = mid + minW / 2;
            z.bot = mid - minW / 2;
         }
         int k = ArraySize(zones);
         ArrayResize(zones, k + 1);
         zones[k] = z;
      }
      i = j + 1;
   }

   if(CanDraw())
   {
      ObjectsDeleteAll(0, "SRFVG_Z");
      datetime t1 = r[got - 1].time, t2 = TimeCurrent() + 20 * PeriodSeconds(InpHTF);
      for(int i = 0; i < ArraySize(zones); i++)
         DrawRect("SRFVG_Z" + IntegerToString(i), t1, zones[i].top, t2, zones[i].bot, clrSlateGray);
   }
   return true;
}

// Strongest zone overlapping the gap (within dist) on the correct side: support for buys, resistance for sells.
int FindZone(const Gap &g, double dist)
{
   int best = -1;
   for(int i = 0; i < ArraySize(zones); i++)
   {
      if(zones[i].bot > g.top + dist || zones[i].top < g.bot - dist)
         continue;
      double mid = (zones[i].top + zones[i].bot) / 2;
      if(g.bull ? mid > g.top : mid < g.bot)
         continue;
      if(best < 0 || zones[i].touches > zones[best].touches)
         best = i;
   }
   return best;
}

// Nearest zone edge in the trade direction, 0 if none.
double NextOpposingZone(bool bull, double entry)
{
   double best = 0;
   for(int i = 0; i < ArraySize(zones); i++)
   {
      if(bull && zones[i].bot > entry && (best == 0 || zones[i].bot < best))
         best = zones[i].bot;
      if(!bull && zones[i].top < entry && (best == 0 || zones[i].top > best))
         best = zones[i].top;
   }
   return best;
}

//+------------------------------------------------------------------+
//| Fair value gaps                                                  |
//+------------------------------------------------------------------+
// r is series-ordered; r[s] = candle 3 (newest), r[s+1] = middle, r[s+2] = candle 1.
bool CheckGap(const MqlRates &r[], int s, double atr, Gap &g)
{
   MqlRates c3 = r[s], c2 = r[s + 1], c1 = r[s + 2];
   double body = MathAbs(c2.close - c2.open), range = c2.high - c2.low;
   if(body < InpDisplaceATR * atr && (range <= 0 || body < 0.6 * range))
      return false;

   if(c2.close > c2.open && c3.low - c1.high >= InpFVGMinATR * atr)
   {
      g.bull = true;
      g.bot = c1.high;
      g.top = c3.low;
   }
   else if(c2.close < c2.open && c1.low - c3.high >= InpFVGMinATR * atr)
   {
      g.bull = false;
      g.top = c1.low;
      g.bot = c3.high;
   }
   else
      return false;
   g.time = c3.time;
   return true;
}

void AddGap(const Gap &g)
{
   int k = ArraySize(gaps);
   ArrayResize(gaps, k + 1, 32);
   gaps[k] = g;
   if(CanDraw())
      DrawRect("SRFVG_G" + IntegerToString((long)g.time), g.time, g.top,
               g.time + InpFVGMaxAge * PeriodSeconds(InpEntryTF), g.bot, g.bull ? clrSeaGreen : clrIndianRed);
}

// Feed one closed bar to the live gaps: first touch ends a gap (only the first retest is traded),
// and in candle mode that touch is where a confirmed entry can fire.
void ProcessBar(const MqlRates &bar, const MqlRates &prev, bool live)
{
   long maxAge = (long)InpFVGMaxAge * PeriodSeconds(InpEntryTF);
   for(int i = ArraySize(gaps) - 1; i >= 0; i--)
   {
      Gap g = gaps[i];
      bool touched = g.bull ? bar.low <= g.top : bar.high >= g.bot;
      if(touched && live && InpConfirm == CONFIRM_CANDLE && Confirms(g, bar, prev))
         TrySignal(g, true);
      if(touched || bar.time - g.time > maxAge)
         ArrayRemove(gaps, i, 1);
   }
}

// Pin bar (rejection wick >= 2x body) or engulfing candle, closing back on the right side of the gap.
bool Confirms(const Gap &g, const MqlRates &bar, const MqlRates &prev)
{
   double body = MathAbs(bar.close - bar.open);
   if(g.bull)
   {
      if(bar.close <= bar.open || bar.close < g.bot)
         return false;
      bool pin = bar.open - bar.low >= 2 * body;
      bool engulf = prev.close < prev.open && bar.close >= prev.open && bar.open <= prev.close;
      return pin || engulf;
   }
   if(bar.close >= bar.open || bar.close > g.top)
      return false;
   bool pin = bar.high - bar.open >= 2 * body;
   bool engulf = prev.close > prev.open && bar.close <= prev.open && bar.open >= prev.close;
   return pin || engulf;
}

// Replay recent history so gaps that formed before the EA started are known (no trades placed).
bool WarmUp()
{
   int n = InpFVGMaxAge + 3;
   MqlRates r[];
   double atr[];
   ArraySetAsSeries(r, true);
   ArraySetAsSeries(atr, true);
   if(CopyRates(_Symbol, InpEntryTF, 1, n, r) < n || CopyBuffer(hAtrEntry, 0, 1, n, atr) < n)
      return false;

   ArrayResize(gaps, 0);
   for(int s = n - 3; s >= 0; s--)
   {
      ProcessBar(r[s], r[s + 1], false);
      Gap g;
      if(CheckGap(r, s, atr[s], g))
         AddGap(g);
   }
   return true;
}

void OnNewBar()
{
   CancelExpiredOrders();

   MqlRates r[];
   ArraySetAsSeries(r, true);
   if(CopyRates(_Symbol, InpEntryTF, 1, 3, r) < 3)
      return;
   double atr = Buf(hAtrEntry, 1);
   if(atr <= 0)
      return;

   ProcessBar(r[0], r[1], true);   // existing gaps first, so the new gap's own candle doesn't count as a touch
   Gap g;
   if(CheckGap(r, 0, atr, g))
   {
      AddGap(g);
      if(InpConfirm == CONFIRM_LIMIT)
         TrySignal(g, false);
   }
}

//+------------------------------------------------------------------+
//| Entries                                                          |
//+------------------------------------------------------------------+
void TrySignal(const Gap &g, bool market)
{
   if((InpSide != 0 && (g.bull ? 1 : -1) != InpSide) || !FiltersOK() || (InpUseTrendFilter && !TrendOK(g.bull)))
      return;
   double atr = Buf(hAtrEntry, 1);
   if(atr <= 0)
      return;

   int z = -1;
   if(InpUseSRFilter)
   {
      z = FindZone(g, InpZoneDistATR * atr);
      if(z < 0)
         return;
   }

   double entry;
   if(market)
      entry = g.bull ? AskP() : BidP();
   else if(InpEntryLevel == LEVEL_MID)
      entry = (g.top + g.bot) / 2;
   else
      entry = (g.bull == (InpEntryLevel == LEVEL_NEAR)) ? g.top : g.bot;

   // Stop beyond whichever is further: the gap's far edge or the zone.
   double far = g.bull ? g.bot : g.top;
   if(z >= 0)
      far = g.bull ? MathMin(far, zones[z].bot) : MathMax(far, zones[z].top);
   double sl = g.bull ? far - InpSLBufferATR * atr : far + InpSLBufferATR * atr;
   double risk = MathAbs(entry - sl);
   double stops = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
   if(risk <= stops)
      return;

   double tp = g.bull ? entry + InpRR * risk : entry - InpRR * risk;
   if(InpTPToNextZone)
   {
      double next = NextOpposingZone(g.bull, entry);
      if(next > 0)
      {
         double reward = MathAbs(next - entry);
         if(reward < InpMinRR * risk)
            return;
         if(reward < InpRR * risk)
            tp = next;
      }
   }

   entry = Norm(entry);
   sl = Norm(sl);
   tp = Norm(tp);
   if(market ? (g.bull ? sl >= BidP() - stops : sl <= AskP() + stops)
             : (g.bull ? entry >= AskP() - stops : entry <= BidP() + stops))
      return;

   double lots = LotsForRisk(g.bull, entry, sl);
   if(lots <= 0)
      return;

   bool ok;
   if(market)
      ok = g.bull ? trade.Buy(lots, _Symbol, 0, sl, tp, "SRFVG")
                  : trade.Sell(lots, _Symbol, 0, sl, tp, "SRFVG");
   else
      ok = g.bull ? trade.BuyLimit(lots, entry, _Symbol, sl, tp, ORDER_TIME_GTC, 0, "SRFVG")
                  : trade.SellLimit(lots, entry, _Symbol, sl, tp, ORDER_TIME_GTC, 0, "SRFVG");
   PrintFormat("%s %s %.2f lots entry=%s sl=%s tp=%s zone=%d -> %s (%u %s)",
               g.bull ? "BUY" : "SELL", market ? "market" : "limit", lots,
               DoubleToString(entry, _Digits), DoubleToString(sl, _Digits), DoubleToString(tp, _Digits),
               z, ok ? "ok" : "FAILED", trade.ResultRetcode(), trade.ResultRetcodeDescription());
}

bool TrendOK(bool bull)
{
   double ema = Buf(hEma, 1), c = iClose(_Symbol, InpHTF, 1);
   if(ema <= 0 || c <= 0)
      return false;
   return bull ? c > ema : c < ema;
}

bool FiltersOK()
{
   double atr = Buf(hAtrEntry, 1);
   if(InpMaxSpreadATR > 0 && (atr <= 0 || (AskP() - BidP()) > InpMaxSpreadATR * atr))
      return false;

   MqlDateTime t;
   TimeCurrent(t);
   bool inSession = InpStartHour < InpEndHour ? (t.hour >= InpStartHour && t.hour < InpEndHour)
                                              : (t.hour >= InpStartHour || t.hour < InpEndHour);  // overnight session
   if(!inSession || DailyLossHit())
      return false;

   int sym = 0, total = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
      if(PositionGetTicket(i) > 0 && PositionGetInteger(POSITION_MAGIC) == (long)InpMagic)
      {
         total++;
         if(PositionGetString(POSITION_SYMBOL) == _Symbol)
            sym++;
      }
   for(int i = OrdersTotal() - 1; i >= 0; i--)
      if(OrderGetTicket(i) > 0 && OrderGetInteger(ORDER_MAGIC) == (long)InpMagic)
      {
         total++;
         if(OrderGetString(ORDER_SYMBOL) == _Symbol)
            sym++;
      }
   return sym < InpMaxTradesSymbol && total < InpMaxTradesTotal;
}

void CancelExpiredOrders()
{
   long maxAge = (long)InpPendingExpiry * PeriodSeconds(InpEntryTF);
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket > 0 && OrderGetInteger(ORDER_MAGIC) == (long)InpMagic && OrderGetString(ORDER_SYMBOL) == _Symbol
         && TimeCurrent() - (datetime)OrderGetInteger(ORDER_TIME_SETUP) >= maxAge)
         trade.OrderDelete(ticket);
   }
}

//+------------------------------------------------------------------+
//| Risk                                                             |
//+------------------------------------------------------------------+
// OrderCalcProfit handles forex, metals, indices and cross-currency conversion.
double LotsForRisk(bool bull, double entry, double sl)
{
   double loss;
   if(!OrderCalcProfit(bull ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, _Symbol, 1.0, entry, sl, loss) || loss >= 0)
      return 0;
   double riskMoney = AccountInfoDouble(ACCOUNT_EQUITY) * InpRiskPct / 100.0;
   double lots = NormLots(riskMoney / -loss);
   if(lots < SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN))
   {
      Print("Skipped: minimum lot would risk more than ", InpRiskPct, "%");
      return 0;
   }
   return MathMin(lots, SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX));
}

void UpdateDay()
{
   datetime d = iTime(_Symbol, PERIOD_D1, 0);
   if(d != 0 && d != dayStart)
   {
      dayStart = d;
      dayStartEquity = AccountInfoDouble(ACCOUNT_EQUITY);   // ponytail: resets if the EA restarts mid-day
   }
}

bool DailyLossHit()
{
   return dayStartEquity > 0
          && (dayStartEquity - AccountInfoDouble(ACCOUNT_EQUITY)) / dayStartEquity * 100.0 >= InpDailyLossPct;
}

//+------------------------------------------------------------------+
//| Trade management                                                 |
//+------------------------------------------------------------------+
// Initial risk (1R) per position is kept in a terminal global variable so it survives
// break-even moves and EA restarts.
void ManagePositions(bool trailNow)
{
   double atr = trailNow ? Buf(hAtrEntry, 1) : 0;
   double swingLow = trailNow ? LastSwing(false) : 0, swingHigh = trailNow ? LastSwing(true) : 0;
   double stops = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
   bool hedging = AccountInfoInteger(ACCOUNT_MARGIN_MODE) == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || PositionGetInteger(POSITION_MAGIC) != (long)InpMagic || PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;
      bool bull = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY;
      double open = PositionGetDouble(POSITION_PRICE_OPEN), sl = PositionGetDouble(POSITION_SL);
      double tp = PositionGetDouble(POSITION_TP), vol = PositionGetDouble(POSITION_VOLUME);

      string id = IntegerToString((long)ticket);
      if(!GlobalVariableCheck("SRFVG_R_" + id))
      {
         if(sl == 0)
            continue;
         GlobalVariableSet("SRFVG_R_" + id, MathAbs(open - sl));
      }
      double R = GlobalVariableGet("SRFVG_R_" + id);
      if(R <= 0)
         continue;
      double bid = BidP(), ask = AskP();
      double gained = bull ? bid - open : open - ask;

      if(InpPartialPct > 0 && gained >= R && !GlobalVariableCheck("SRFVG_P_" + id))
      {
         double closeVol = NormLots(vol * InpPartialPct / 100.0), vmin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
         bool done = closeVol < vmin || vol - closeVol < vmin   // too small to split: give up, don't retry every tick
                     || (hedging ? trade.PositionClosePartial(ticket, closeVol) : trade.PositionClosePartial(_Symbol, closeVol));
         if(done)
            GlobalVariableSet("SRFVG_P_" + id, 1);
      }

      double newSL = sl;
      if(InpBEAtR > 0 && gained >= InpBEAtR * R)
         newSL = Better(bull, newSL, Norm(bull ? open + (ask - bid) : open - (ask - bid)));
      if(trailNow && gained >= InpTrailStartR * R)
      {
         if(bull && swingLow > 0)
            newSL = Better(bull, newSL, Norm(swingLow - InpSLBufferATR * atr));
         if(!bull && swingHigh > 0)
            newSL = Better(bull, newSL, Norm(swingHigh + InpSLBufferATR * atr));
      }
      if(newSL != sl && (bull ? newSL < bid - stops : newSL > ask + stops))
         trade.PositionModify(ticket, newSL, tp);
   }
}

// The tighter of two stops (higher for buys, lower for sells); 0 means no stop.
double Better(bool bull, double current, double candidate)
{
   if(current == 0)
      return candidate;
   return bull ? MathMax(current, candidate) : MathMin(current, candidate);
}

// Most recent confirmed swing high/low on the entry timeframe, 0 if none.
double LastSwing(bool high)
{
   int n = InpPivotStrength;
   MqlRates r[];
   ArraySetAsSeries(r, true);
   int got = CopyRates(_Symbol, InpEntryTF, 1, 100, r);
   for(int i = n; i < got - n; i++)
      if(IsPivot(r, i, n, high))
         return high ? r[i].high : r[i].low;
   return 0;
}

//+------------------------------------------------------------------+
//| Helpers                                                          |
//+------------------------------------------------------------------+
double AskP() { return SymbolInfoDouble(_Symbol, SYMBOL_ASK); }
double BidP() { return SymbolInfoDouble(_Symbol, SYMBOL_BID); }

double Buf(int handle, int shift)
{
   double b[1];
   if(CopyBuffer(handle, 0, shift, 1, b) != 1)
      return 0;
   return b[0];
}

double Norm(double price)
{
   double ts = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   return NormalizeDouble(MathRound(price / ts) * ts, _Digits);
}

double NormLots(double v)
{
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   int digits = (int)MathMax(0, MathCeil(-MathLog10(step)));
   return NormalizeDouble(MathFloor(v / step + 1e-9) * step, digits);
}

bool CanDraw()
{
   return InpDraw && (!MQLInfoInteger(MQL_TESTER) || MQLInfoInteger(MQL_VISUAL_MODE));
}

void DrawRect(string name, datetime t1, double p1, datetime t2, double p2, color c)
{
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_RECTANGLE, 0, t1, p1, t2, p2);
   else
   {
      ObjectMove(0, name, 0, t1, p1);
      ObjectMove(0, name, 1, t2, p2);
   }
   ObjectSetInteger(0, name, OBJPROP_COLOR, c);
   ObjectSetInteger(0, name, OBJPROP_FILL, true);
   ObjectSetInteger(0, name, OBJPROP_BACK, true);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
}
//+------------------------------------------------------------------+
