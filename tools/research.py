# /// script
# dependencies = ["numpy", "pandas"]
# ///
"""Fast Python replica of SR_FVG_EA for searching ideas; MT5 real-tick runs (bt.sh) stay the final judge.
Signals on closed entry-TF bars, fills and exits simulated on FTMO M1 bars with real per-bar spread.
Pessimistic: SL wins when SL and TP are in the same M1 bar, and TP never counts on the fill bar.
Results are in R (1R = initial risk), net of spread and commission.  uv run tools/research.py"""
import glob, os, sys, numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.environ.get("MT5_HIST", os.path.join(HERE, "../../MT5-trading/prefix/drive_c/MT5-tester/Bases/FTMO-Server/history"))
CACHE = os.path.join(HERE, "../data")
# symbol: (point, commission round trip in price units). FTMO: metals 2.86 USD/lot, FX ~5 USD/lot, indices none.
SPEC = {"XAUUSD": (0.01, 0.029), "EURUSD": (1e-5, 5e-5), "GBPUSD": (1e-5, 5e-5), "USDCHF": (1e-5, 5e-5),
        "USDJPY": (1e-3, 7e-3), "US100.cash": (0.01, 0.0)}
SPREAD_X = 1.1   # M1 history stores the bar's minimum spread; real ticks run ~10% wider
REC = np.dtype([("t", "<u8"), ("o", "<f8"), ("h", "<f8"), ("l", "<f8"), ("c", "<f8"), ("tv", "<u8"), ("sp", "<u4"), ("rv", "<u8")])


def load_m1(sym="XAUUSD"):
    """Decode MT5 .hcc M1 history (60-byte records, not globally aligned) -> dict of arrays, cached as npz."""
    out = f"{CACHE}/{sym}_m1.npz"
    if os.path.exists(out):
        return dict(np.load(out))
    parts = []
    for f in sorted(glob.glob(f"{HIST}/{sym}/*.hcc")):
        buf = np.fromfile(f, np.uint8)
        cand = []
        for off in range(4):
            u = buf[off:off + (len(buf) - off) // 4 * 4].view("<u4")
            ok = (u[:-1] > 1_400_000_000) & (u[:-1] < 1_900_000_000) & (u[:-1] % 60 == 0) & (u[1:] == 0)
            cand.append(np.flatnonzero(ok) * 4 + off)
        pos = np.concatenate(cand)
        pos = pos[pos + 60 <= len(buf)]
        r = np.ascontiguousarray(buf[pos[:, None] + np.arange(60)]).view(REC).ravel()
        with np.errstate(all="ignore"):
            good = (r["l"] > 0.1) & (r["h"] < 1e5) & (r["l"] <= r["o"]) & (r["l"] <= r["c"]) & (r["h"] >= r["o"]) \
                   & (r["h"] >= r["c"]) & (r["h"] / r["l"] < 1.05) & (r["sp"] < 5000) & (r["tv"] > 0) & (r["tv"] < 10**6)
        r = r[good]
        # The files also hold summary records (one per day at 00:00, some hourly) carrying a whole day's range.
        # They are not M1 bars; left in, every 00:00 H4 bar spans the day and S/R zones are wrong.
        parts.append(r[r["tv"] <= 30 * np.median(r["tv"])])
    r = np.concatenate(parts)
    r = r[np.unique(r["t"], return_index=True)[1]]
    d = {k: r[k] for k in ("t", "o", "h", "l", "c", "sp")}
    d["t"] = d["t"].astype("int64")
    os.makedirs(CACHE, exist_ok=True)
    np.savez_compressed(out, **d)
    return d


def bars(m1, minutes):
    """Aggregate M1 -> N-minute bars (aligned to server midnight like MT5), with SMA(14) ATR like iATR."""
    k = m1["t"] // (60 * minutes)
    first = np.r_[0, np.flatnonzero(np.diff(k)) + 1]
    last = np.r_[first[1:], len(k)] - 1
    b = pd.DataFrame({"t": k[first] * 60 * minutes, "o": m1["o"][first], "c": m1["c"][last],
                      "h": np.maximum.reduceat(m1["h"], first), "l": np.minimum.reduceat(m1["l"], first),
                      "i0": first, "i1": last + 1})
    pc = b.c.shift()
    tr = np.maximum(b.h - b.l, np.maximum((b.h - pc).abs(), (b.l - pc).abs()))
    b["atr"] = tr.rolling(14).mean()
    return b


def zones_at(H, j, p):
    """S/R zones from the 300 HTF bars closed before HTF bar j (EA BuildZones)."""
    n, lo = p["piv"], max(0, j - 300)
    hi_, lo_ = H.h.values[lo:j], H.l.values[lo:j]
    atr = H.atr.values[j - 1] if j > 0 else np.nan
    if len(hi_) < 2 * n + 1 or not atr > 0:
        return None
    piv = []
    for i in range(n, len(hi_) - n):   # chronological: i+k newer (strict), i-k older (ties allowed)
        if all(hi_[i + k] < hi_[i] and hi_[i - k] <= hi_[i] for k in range(1, n + 1)):
            piv.append(hi_[i])
        if all(lo_[i + k] > lo_[i] and lo_[i - k] >= lo_[i] for k in range(1, n + 1)):
            piv.append(lo_[i])
    piv.sort()
    tol, minw, z, i = p["cluster"] * atr, 0.1 * atr, [], 0
    while i < len(piv):
        j2 = i
        while j2 + 1 < len(piv) and piv[j2 + 1] - piv[i] <= tol:
            j2 += 1
        if j2 - i + 1 >= p["touches"]:
            bot, top = piv[i], piv[j2]
            if top - bot < minw:
                mid = (top + bot) / 2
                bot, top = mid - minw / 2, mid + minw / 2
            z.append((top, bot, j2 - i + 1))
        i = j2 + 1
    return z


DEFAULT = dict(tf=15, htf=240, piv=3, cluster=0.25, touches=2, fvg_min=0.3, displace=1.0, max_age=50, level="mid",
               zone_dist=0.5, sr=True, confirm="limit", trend=True, ema=200, expiry=8, sl_buf=0.2, rr=2.0,
               tp_zone=True, min_rr=1.5, be=1.0, daily_r=3.0, spread_atr=0.15, h0=8, h1=20, days=(0, 1, 2, 3, 4),
               max_hold=0, side=0, sym="XAUUSD", start=None)   # start: no trades before (to mirror an MT5 run)


def run(p, m1, cache={}):
    """Replay the EA over all history. Returns a DataFrame of trades (t_in, dir, R, why)."""
    p = {**DEFAULT, **p}
    key = (p["sym"], p["tf"], p["htf"])
    if key not in cache:
        cache[key] = (bars(m1, p["tf"]), bars(m1, p["htf"]))
    E, H = cache[key]
    zkey = ("z", p["sym"], p["htf"], p["piv"], p["cluster"], p["touches"])
    if zkey not in cache:
        cache[zkey] = [zones_at(H, j, p) for j in range(len(H))]
    Z = cache[zkey]
    ekey = ("ema", p["sym"], p["htf"], p["ema"])
    if ekey not in cache:
        cache[ekey] = H.c.ewm(span=p["ema"], adjust=False).mean().values
    ema = cache[ekey]

    POINT, COMM = SPEC[p["sym"]]
    t1, o1, h1, l1, c1, sp1 = m1["t"], m1["o"], m1["h"], m1["l"], m1["c"], m1["sp"] * POINT * SPREAD_X
    et, eo, eh, el, ec, ea, ei0, ei1 = (E[k].values for k in ("t", "o", "h", "l", "c", "atr", "i0", "i1"))
    hend = H.t.values + p["htf"] * 60          # HTF bar close times
    hc = H.c.values
    sec = p["tf"] * 60
    t0 = pd.Timestamp(p["start"]).value // 10**9 if p["start"] else 0
    trades, gaps, orders = [], [], []
    pend = pos = None
    day, day_r = None, 0.0

    def close(why, px, j):
        nonlocal pos, day_r
        d, entry, sl0 = pos["d"], pos["entry"], pos["sl0"]
        r = (d * (px - entry) - COMM) / abs(entry - sl0)
        trades.append((pos["t"], t1[j], d, r, why))
        day_r += r
        pos = None

    def step_m1(a, b):
        """Advance pending order and open position through M1 bars [a, b)."""
        nonlocal pend, pos
        for j in range(a, b):
            s = sp1[j]
            if pend is not None:
                d, entry = pend["d"], pend["entry"]
                if (d > 0 and l1[j] + s <= entry) or (d < 0 and h1[j] >= entry):
                    fill = min(entry, o1[j] + s) if d > 0 else max(entry, o1[j])
                    pos = dict(d=d, entry=fill, sl=pend["sl"], sl0=pend["sl"], tp=pend["tp"], t=t1[j], j0=j)
                    pend = None
                    # fill bar: only the stop can count
                    if (d > 0 and l1[j] <= pos["sl"]) or (d < 0 and h1[j] + s >= pos["sl"]):
                        close("sl", pos["sl"], j)
                    continue
            if pos is not None:
                d, entry, sl, tp = pos["d"], pos["entry"], pos["sl"], pos["tp"]
                if d > 0:
                    if l1[j] <= sl:   close("be" if sl >= entry else "sl", sl, j); continue
                    if h1[j] >= tp:   close("tp", tp, j); continue
                    gained = h1[j] - entry
                else:
                    if h1[j] + s >= sl: close("be" if sl <= entry else "sl", sl, j); continue
                    if l1[j] + s <= tp: close("tp", tp, j); continue
                    gained = entry - (l1[j] + s)
                R = abs(entry - pos["sl0"])
                if p["be"] > 0 and gained >= p["be"] * R:
                    be = entry + d * s
                    pos["sl"] = max(sl, be) if d > 0 else min(sl, be)
                if p["max_hold"] and (t1[j] - pos["t"]) >= p["max_hold"] * sec:
                    close("time", c1[j] - (s if d > 0 else 0) + (s if d < 0 else 0) * 0, j)

    def trace(now, why):
        if now in p.get("trace", ()):
            print("trace", pd.to_datetime(now, unit="s"), "rejected:", why)

    def signal(g, market, k, now, j_now):
        nonlocal pend, pos
        if pend is not None or pos is not None or now < t0:
            return trace(now, 'if pend is not None or pos is not None or now < t0')
        hj = np.searchsorted(hend, now, side="right") - 1      # last closed HTF bar
        if hj < 0:
            return trace(now, 'if hj < 0')
        atr = ea[k]
        if not atr > 0:
            return trace(now, 'if not atr > 0')
        bull = g[0]
        if p["side"] and (1 if bull else -1) != p["side"]:
            return trace(now, 'if p["side"] and (1 if bull else -1) != p["side"]')
        if p["trend"] and not (hc[hj] > ema[hj] if bull else hc[hj] < ema[hj]):
            return trace(now, 'if p["trend"] and not (hc[hj] > ema[hj] if bull else hc[hj] < ema[hj])')
        zs = Z[hj + 1] if hj + 1 < len(Z) else None
        if zs is None:
            return trace(now, 'if zs is None')
        top, bot = g[1], g[2]
        z = None
        if p["sr"]:
            dist = p["zone_dist"] * atr
            for zz in zs:
                if zz[1] > top + dist or zz[0] < bot - dist:
                    continue
                mid = (zz[0] + zz[1]) / 2
                if (mid > top) if bull else (mid < bot):
                    continue
                if z is None or zz[2] > z[2]:
                    z = zz
            if z is None:
                return trace(now, 'if z is None')
        s = sp1[j_now]
        bid, ask = o1[j_now], o1[j_now] + s
        if market:
            entry = ask if bull else bid
        elif p["level"] == "mid":
            entry = (top + bot) / 2
        else:
            entry = top if bull == (p["level"] == "near") else bot
        far = bot if bull else top
        if z is not None:
            far = min(far, z[1]) if bull else max(far, z[0])
        sl = far - p["sl_buf"] * atr if bull else far + p["sl_buf"] * atr
        risk = abs(entry - sl)
        if risk <= 0:
            return trace(now, 'if risk <= 0')
        tp = entry + p["rr"] * risk if bull else entry - p["rr"] * risk
        if p["tp_zone"]:
            nxt = None
            for zz in zs:
                if bull and zz[1] > entry and (nxt is None or zz[1] < nxt): nxt = zz[1]
                if not bull and zz[0] < entry and (nxt is None or zz[0] > nxt): nxt = zz[0]
            if nxt is not None:
                reward = abs(nxt - entry)
                if reward < p["min_rr"] * risk:
                    return trace(now, 'if reward < p["min_rr"] * risk')
                if reward < p["rr"] * risk:
                    tp = nxt
        d = 1 if bull else -1
        if market:
            if (bull and sl >= bid) or (not bull and sl <= ask):
                return trace(now, 'if (bull and sl >= bid) or (not bull and sl <= ask)')
            pos = dict(d=d, entry=entry, sl=sl, sl0=sl, tp=tp, t=now, j0=j_now)
        else:
            if (bull and entry >= ask) or (not bull and entry <= bid):
                return trace(now, 'if (bull and entry >= ask) or (not bull and entry <= bid)')
            pend = dict(d=d, entry=entry, sl=sl, tp=tp, t=now)
            orders.append((now, d, entry, sl, tp))

    for k in range(len(E)):
        step_m1(ei0[k], ei1[k])
        now = et[k] + sec
        j_now = ei1[k]
        if j_now >= len(t1) or k < 3:
            continue
        if t1[j_now] - now > 3 * 3600:           # weekend/holiday gap: flat before it
            if pos is not None: close("gap", c1[j_now - 1] - (sp1[j_now - 1] if pos["d"] < 0 else 0), j_now - 1)
            pend, gaps = None, []
            continue
        dd = now // 86400
        if dd != day:
            day, day_r = dd, 0.0
        if pend is not None and now - pend["t"] >= p["expiry"] * sec:
            pend = None
        # filters that gate any new entry this bar (EA FiltersOK)
        hour = (now % 86400) // 3600
        wd = (now // 86400 + 3) % 7               # 1970-01-01 was a Thursday
        ok = (p["h0"] <= hour < p["h1"]) and wd in p["days"] and day_r > -p["daily_r"] \
             and (p["spread_atr"] <= 0 or sp1[j_now] <= p["spread_atr"] * ea[k])
        atr = ea[k]
        # existing gaps first (first touch ends a gap; candle mode can enter on it)
        live = []
        for g in gaps:
            touched = el[k] <= g[1] if g[0] else eh[k] >= g[2]
            if touched and ok and p["confirm"] == "candle" and confirms(g, k, eo, eh, el, ec):
                signal(g, True, k, now, j_now)
            if not touched and et[k] - g[3] <= p["max_age"] * sec:
                live.append(g)
        gaps = live
        # new gap on bars k-2, k-1, k
        if not atr > 0:
            continue
        body, rng = abs(ec[k - 1] - eo[k - 1]), eh[k - 1] - el[k - 1]
        if body < p["displace"] * atr and (rng <= 0 or body < 0.6 * rng):
            continue
        g = None
        if ec[k - 1] > eo[k - 1] and el[k] - eh[k - 2] >= p["fvg_min"] * atr:
            g = (True, el[k], eh[k - 2], et[k])
        elif ec[k - 1] < eo[k - 1] and el[k - 2] - eh[k] >= p["fvg_min"] * atr:
            g = (False, el[k - 2], eh[k], et[k])
        if g:
            gaps.append(g)
            if ok and p["confirm"] == "limit":
                signal(g, False, k, now, j_now)

    df = pd.DataFrame(trades, columns=["t_in", "t_out", "dir", "R", "why"])
    df["t_in"] = pd.to_datetime(df.t_in, unit="s")
    df.attrs["orders"] = orders
    return df


def confirms(g, k, o, h, l, c):
    """Pin bar (wick >= 2x body) or engulfing, closing back on the right side of the gap (EA Confirms)."""
    body = abs(c[k] - o[k])
    if g[0]:
        if c[k] <= o[k] or c[k] < g[2]:
            return False
        return o[k] - l[k] >= 2 * body or (c[k - 1] < o[k - 1] and c[k] >= o[k - 1] and o[k] <= c[k - 1])
    if c[k] >= o[k] or c[k] > g[1]:
        return False
    return h[k] - o[k] >= 2 * body or (c[k - 1] > o[k - 1] and c[k] <= o[k - 1] and o[k] >= c[k - 1])


HOLD = pd.Timestamp("2025-10-01")   # last 12 months: never used for choosing


def summary(tr, tag=""):
    """Per-year R, plus development (before HOLD) vs holdout totals."""
    if len(tr) == 0:
        return dict(tag=tag, n=0)
    yr = tr.groupby(tr.t_in.dt.year).R.sum().round(1).to_dict()
    dev, hold = tr[tr.t_in < HOLD], tr[tr.t_in >= HOLD]
    def pf(x): g, l = x[x > 0].sum(), -x[x < 0].sum(); return round(g / l, 2) if l else 9.99
    def dd(x): e = np.cumsum(x); return round(float((np.maximum.accumulate(np.r_[0, e]) - np.r_[0, e]).max()), 1)
    return dict(tag=tag, n=len(tr), dev_n=len(dev), dev_R=round(dev.R.sum(), 1), dev_pf=pf(dev.R), dev_dd=dd(dev.R.values),
                dev_min_yr=min(v for k, v in yr.items() if k < 2025) if any(k < 2025 for k in yr) else 0,
                hold_n=len(hold), hold_R=round(hold.R.sum(), 1), hold_pf=pf(hold.R), hold_dd=dd(hold.R.values),
                **{f"y{k}": v for k, v in yr.items()})


_M1 = {}
def _one(p):
    sym = p.get("sym", "XAUUSD")
    if sym not in _M1:
        _M1[sym] = load_m1(sym)
    return {**p, **summary(run(p, _M1[sym]))}


def grid(ps, procs=8):
    """Run many configs in parallel (each worker ~0.5 GB per symbol). Returns a DataFrame, one row per config."""
    from multiprocessing import Pool
    for sym in {p.get("sym", "XAUUSD") for p in ps}:
        load_m1(sym)
    with Pool(procs) as pool:
        return pd.DataFrame(pool.map(_one, ps, chunksize=1))


if __name__ == "__main__":
    m1 = load_m1()
    print("M1 bars", len(m1["t"]), pd.to_datetime(m1["t"][0], unit="s"), "->", pd.to_datetime(m1["t"][-1], unit="s"))
    tr = run({}, m1)
    print(summary(tr, "default"))
    print(tr[tr.t_in >= HOLD].why.value_counts().to_dict())
