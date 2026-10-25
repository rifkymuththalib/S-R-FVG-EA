# /// script
# dependencies = ["numpy", "pandas"]
# ///
"""H1 support/resistance + trend-following research sim for gold (no FVG).
Signals on closed H1 bars, market entry at the next H1 open, exits on FTMO M1 bars with real per-bar spread,
commission and swap; flat before the weekend. Pessimistic: SL wins when SL and TP fall in the same M1 bar.
Results in R (1R = initial risk).  uv run tools/srtrend.py"""
import os, sys, numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from research import load_m1, bars, SPEC, SPREAD_X

SWAP = {"XAUUSD": (-0.5785, -0.042)}   # price units per night (FTMO -57.85 / -4.2 points), x3 Wednesday night
DEFAULT = dict(trend="h4_ema", entry="bounce", exit="trail", trail=3.0, rr=2.0, piv=3, look=300, cluster=0.25,
               touches=2, sl_buf=0.3, sl_atr=2.0, retest_bars=24, adx_min=0, spread_atr=0.1, side=0, sym="XAUUSD")
WINDOWS = [("pre", "2017-07-01", "2021-06-01"), ("dev", "2021-06-01", "2025-10-01"), ("hold", "2025-10-01", "2030")]


def ema(x, n):
    return pd.Series(x).ewm(span=n, adjust=False).mean().values


def adx(b, n=14):
    """Wilder ADX (MT5 iADXWilder)."""
    up, dn = b.h.diff().values, -b.l.diff().values
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    pc = b.c.shift()
    tr = np.maximum(b.h - b.l, np.maximum((b.h - pc).abs(), (b.l - pc).abs())).values
    w = lambda x: pd.Series(x).ewm(alpha=1 / n, adjust=False).mean().values
    a = w(tr)
    pdi, mdi = 100 * w(pdm) / a, 100 * w(mdm) / a
    return w(100 * np.abs(pdi - mdi) / np.maximum(pdi + mdi, 1e-9))


def trend(H, H4, D, kind):
    """+1 / -1 / 0 per H1 bar, known at that bar's close."""
    tc = H.t.values + 3600
    def at(X, sec, v):
        j = np.searchsorted(X.t.values + sec, tc, side="right") - 1
        return np.where(j >= 0, v[np.maximum(j, 0)], 0)
    if kind == "h4_ema":
        return at(H4, 14400, np.sign(H4.c.values - ema(H4.c.values, 200)))
    if kind == "h4_cross":
        return at(H4, 14400, np.sign(ema(H4.c.values, 50) - ema(H4.c.values, 200)))
    if kind == "h1_cross":
        return np.sign(ema(H.c.values, 50) - ema(H.c.values, 200))
    if kind == "d1_ema":
        return at(D, 86400, np.sign(D.c.values - ema(D.c.values, 50)))
    raise ValueError(kind)


def zones(H, p):
    """S/R zones per H1 bar k from swing pivots among the last `look` closed bars (EA BuildZones rule)."""
    n, look, h, l, atr = p["piv"], p["look"], H.h.values, H.l.values, H.atr.values
    N, s = len(h), slice(p["piv"], len(h) - p["piv"])
    ph, pl = np.zeros(N, bool), np.zeros(N, bool)
    ph[s] = pl[s] = True
    for k in range(1, n + 1):   # strictly beyond the n newer bars, at least the n older ones
        ph[s] &= (h[s] > h[n + k:N - n + k]) & (h[s] >= h[n - k:N - n - k])
        pl[s] &= (l[s] < l[n + k:N - n + k]) & (l[s] <= l[n - k:N - n - k])
    idx = np.r_[np.flatnonzero(ph), np.flatnonzero(pl)]
    px = np.r_[h[ph], l[pl]]
    o = np.argsort(idx, kind="stable")
    idx, px = idx[o], px[o]
    Z = [()] * N
    for k in range(look, N):
        a, b = np.searchsorted(idx, [k - look + 1 + n, k - n + 1])
        pv = np.sort(px[a:b])
        tol, minw, z, i = p["cluster"] * atr[k], 0.1 * atr[k], [], 0
        while i < len(pv):
            j = i
            while j + 1 < len(pv) and pv[j + 1] - pv[i] <= tol:
                j += 1
            if j - i + 1 >= p["touches"]:
                bot, top = pv[i], pv[j]
                if top - bot < minw:
                    mid = (top + bot) / 2
                    bot, top = mid - minw / 2, mid + minw / 2
                z.append((top, bot))
            i = j + 1
        Z[k] = z
    return Z


def run(p, m1, cache={}):
    """Replay the strategy over all history. Returns a DataFrame of trades (t_in, t_out, dir, R, why, nights)."""
    p = {**DEFAULT, **p}
    sym = p["sym"]
    if sym not in cache:
        cache[sym] = (bars(m1, 60), bars(m1, 240), bars(m1, 1440))
    H, H4, D = cache[sym]
    zk = ("z", sym, p["piv"], p["look"], p["cluster"], p["touches"])
    if zk not in cache:
        cache[zk] = zones(H, p)
    tk = ("t", sym, p["trend"])
    if tk not in cache:
        cache[tk] = trend(H, H4, D, p["trend"])
    if ("adx", sym) not in cache:
        cache[("adx", sym)] = adx(H)
    Z, T, ADX = cache[zk], cache[tk], cache[("adx", sym)]

    POINT, COMM = SPEC[sym]
    SW_L, SW_S = SWAP.get(sym, (0, 0))
    t1, o1, h1, l1, c1, sp1 = m1["t"], m1["o"], m1["h"], m1["l"], m1["c"], m1["sp"] * POINT * SPREAD_X
    ht, ho, hh, hl, hc, ha, i0, i1 = (H[k].values for k in ("t", "o", "h", "l", "c", "atr", "i0", "i1"))
    N = len(ht)
    entry_kind = p["entry"]
    donch = int(entry_kind[5:]) if entry_kind.startswith("donch") else 0
    hmax = H.h.rolling(donch).max().values if donch else None
    lmin = H.l.rolling(donch).min().values if donch else None
    trades, pos, brk = [], None, None

    def close(why, px, j):
        nonlocal pos
        d, entry = pos["d"], pos["entry"]
        day0, day1 = pos["t"] // 86400, t1[j] // 86400
        nights = sum(3 if (dd + 3) % 7 == 2 else 1 for dd in range(day0, day1))   # (day+3)%7: Mon=0, Wed=2
        swap = (SW_L if d > 0 else SW_S) * nights
        trades.append((pos["t"], t1[j], d, (d * (px - entry) - COMM + swap) / pos["risk"], why, nights))
        pos = None

    def play(a, b):
        """Run the open position through M1 bars [a, b)."""
        d, sl, tp = pos["d"], pos["sl"], pos["tp"]
        if d > 0:
            hs, ht_ = l1[a:b] <= sl, h1[a:b] >= tp
        else:
            hs, ht_ = h1[a:b] + sp1[a:b] >= sl, l1[a:b] + sp1[a:b] <= tp
        js = np.argmax(hs) if hs.any() else b
        jt = np.argmax(ht_) if ht_.any() else b
        if js == b and jt == b:
            return
        if js <= jt:
            j = a + js
            close("sl" if (sl - pos["entry"]) * d < 0 else "trail", min(sl, o1[j]) if d > 0 else max(sl, o1[j] + sp1[j]), j)
        else:
            j = a + jt
            close("tp", max(tp, o1[j]) if d > 0 else min(tp, o1[j] + sp1[j]), j)

    for k in range(p["look"], N - 1):
        if pos is not None:
            play(i0[k], i1[k])
        atr, wd, hour = ha[k], (ht[k] // 86400 + 3) % 7, (ht[k] % 86400) // 3600
        gap = ht[k + 1] - (ht[k] + 3600) > 3 * 3600
        if pos is not None:
            if gap or (wd == 4 and hour >= 22):        # flat before the weekend / holidays
                j = i1[k] - 1
                close("weekend", c1[j] if pos["d"] > 0 else c1[j] + sp1[j], j)
            elif p["exit"] == "trail":
                d = pos["d"]
                pos["ext"] = max(pos["ext"], hh[k]) if d > 0 else min(pos["ext"], hl[k])
                new = pos["ext"] - d * p["trail"] * atr
                pos["sl"] = max(pos["sl"], new) if d > 0 else min(pos["sl"], new)
        if not atr > 0 or gap or (wd == 4 and hour >= 20):
            continue
        # signal at the close of bar k
        sig = None
        dirs = (T[k],) if p["side"] == 0 else ((p["side"],) if T[k] == p["side"] else ())
        for d in dirs:
            if d == 0 or ADX[k] < p["adx_min"]:
                continue
            z = Z[k]
            if entry_kind == "bounce":   # dipped into a support zone below and closed back above it, with a candle in trend direction
                c = [zz for zz in z if ((zz[0] < hc[k] and hl[k] <= zz[0] + 0.1 * atr and hc[k] > ho[k]) if d > 0
                                        else (zz[1] > hc[k] and hh[k] >= zz[1] - 0.1 * atr and hc[k] < ho[k]))]
                if c:
                    zz = max(c) if d > 0 else min(c, key=lambda x: x[1])
                    sig = (d, (min(zz[1], hl[k]) - p["sl_buf"] * atr) if d > 0 else (max(zz[0], hh[k]) + p["sl_buf"] * atr))
            elif entry_kind in ("break", "retest"):
                c = [zz for zz in z if ((hc[k - 1] <= zz[0] < hc[k]) if d > 0 else (hc[k - 1] >= zz[1] > hc[k]))]
                if c:
                    zz = max(c) if d > 0 else min(c, key=lambda x: x[1])
                    brk = (d, zz[0], zz[1], k)
                    if entry_kind == "break":
                        sig = (d, (zz[1] - p["sl_buf"] * atr) if d > 0 else (zz[0] + p["sl_buf"] * atr))
                elif entry_kind == "retest" and brk and brk[0] == d and k - brk[3] <= p["retest_bars"]:
                    _, top, bot, _ = brk
                    if (hc[k] < bot) if d > 0 else (hc[k] > top):
                        brk = None                         # break failed
                    elif (hl[k] <= top + 0.1 * atr and hc[k] > top) if d > 0 else (hh[k] >= bot - 0.1 * atr and hc[k] < bot):
                        sig = (d, (min(bot, hl[k]) - p["sl_buf"] * atr) if d > 0 else (max(top, hh[k]) + p["sl_buf"] * atr))
                        brk = None
            elif donch:   # close through the prior N-bar high / low (fresh)
                if (hc[k] > hmax[k - 1] and hc[k - 1] <= hmax[k - 2]) if d > 0 else (hc[k] < lmin[k - 1] and hc[k - 1] >= lmin[k - 2]):
                    sig = (d, None)
        if sig is None or pos is not None:
            continue
        d, sl = sig
        j = i0[k + 1]
        if p["spread_atr"] and sp1[j] > p["spread_atr"] * atr:
            continue
        entry = o1[j] + sp1[j] if d > 0 else o1[j]
        if sl is None:
            sl = entry - d * p["sl_atr"] * atr
        risk = d * (entry - sl)
        if risk <= 0.1 * atr:
            continue
        tp = entry + d * p["rr"] * risk if p["exit"] == "rr" else (np.inf if d > 0 else -np.inf)
        pos = dict(d=d, entry=entry, sl=sl, tp=tp, risk=risk, t=t1[j], ext=entry)
    df = pd.DataFrame(trades, columns=["t_in", "t_out", "dir", "R", "why", "nights"])
    df["t_in"] = pd.to_datetime(df.t_in, unit="s")
    df["t_out"] = pd.to_datetime(df.t_out, unit="s")
    return df


def pf(x):
    g, l = x[x > 0].sum(), -x[x < 0].sum()
    return round(g / l, 2) if l else 9.99


def summary(tr):
    out = {}
    for w, a, b in WINDOWS:
        x = tr[(tr.t_in >= a) & (tr.t_in < b)].R
        e = np.r_[0, np.cumsum(x.values)]
        out.update({f"{w}_n": len(x), f"{w}_R": round(x.sum(), 1), f"{w}_pf": pf(x),
                    f"{w}_dd": round(float((np.maximum.accumulate(e) - e).max()), 1)})
    yr = tr[tr.t_in >= WINDOWS[0][1]].groupby(tr.t_in.dt.year).R.sum()
    out["yrs+"] = f"{(yr > 0).sum()}/{len(yr)}"
    out["by_year"] = " ".join(f"{v:+.0f}" for v in yr.values)
    return out


_M1 = {}
def _one(p):
    sym = p.get("sym", "XAUUSD")
    if sym not in _M1:
        _M1[sym] = load_m1(sym)
    return {**p, **summary(run(p, _M1[sym]))}


def grid(ps, procs=6):
    """Run many configs in parallel (<= 6 workers: more runs this 30 GB box out of memory)."""
    from multiprocessing import Pool
    with Pool(procs) as pool:
        return pd.DataFrame(pool.map(_one, ps, chunksize=1))


if __name__ == "__main__":
    m1 = load_m1()
    tr = run({}, m1)
    print(summary(tr))
    print(tr.why.value_counts().to_dict(), "avg nights", round(tr.nights.mean(), 2))
