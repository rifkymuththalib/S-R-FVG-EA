"""Break a Strategy Tester report down by exit type, side, hour and month: python3 tools/trades.py report.htm"""
import re, html, sys, collections

f = lambda s: float(s.replace(" ", "") or 0)
t = open(sys.argv[1], "rb").read().decode("utf-16", "ignore")
rows = [[html.unescape(re.sub(r"<[^>]+>", "", x)).strip() for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, flags=re.S | re.I)]
        for r in re.findall(r"<tr[^>]*>(.*?)</tr>", t, flags=re.S | re.I)]
deals = [r for r in rows if len(r) == 13 and re.match(r"\d{4}\.", r[0]) and r[2]]
open_, trades = {}, []
for d in deals:
    if d[4] == "in":
        open_ = d
    elif d[4] == "out" and open_:
        pl = f(d[10]) + f(d[8]) + f(d[9]) + f(open_[8])
        kind = d[12].split(" ")[0] if d[12] else "other"
        if kind == "sl" and abs(pl) < 300:
            kind = "be"
        trades.append(dict(t=open_[0], side=open_[3], hour=int(open_[0][11:13]), month=open_[0][:7], pl=pl, kind=kind))
        open_ = {}

def table(key):
    g = collections.OrderedDict()
    for tr in sorted(trades, key=lambda x: x[key]):
        g.setdefault(tr[key], []).append(tr["pl"])
    for k, v in g.items():
        w = sum(1 for x in v if x > 0)
        print(f"  {str(k):8s} n={len(v):4d} win={100*w/len(v):3.0f}% net={sum(v):+9.0f}")

print(f"{len(trades)} trades, net {sum(t['pl'] for t in trades):+.0f}")
for k in ("kind", "side", "hour", "month"):
    print(k); table(k)
