"""Print the best optimizer passes from a Strategy Tester XML report: python3 tools/opt.py report.xml [N]"""
import re, html, sys

t = open(sys.argv[1], "rb").read()
t = t.decode("utf-16" if t[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8", "ignore")
rows = [[html.unescape(c) for c in re.findall(r"<Data[^>]*>(.*?)</Data>", r, flags=re.S)]
        for r in re.findall(r"<Row[^>]*>(.*?)</Row>", t, flags=re.S)]
head, rows = rows[0], [r for r in rows[1:] if len(r) == len(rows[0])]
col = {h: i for i, h in enumerate(head)}
rows.sort(key=lambda r: -float(r[col["Result"]]))
keep = ["Result", "Profit", "Profit Factor", "Equity DD %", "Trades"] + head[head.index("Trades") + 1:]
print(" | ".join(keep))
for r in rows[: int(sys.argv[2]) if len(sys.argv) > 2 else 15]:
    print(" | ".join(r[col[k]][:8] for k in keep))
print(f"{len(rows)} passes, {sum(float(r[col['Profit']]) > 0 for r in rows)} profitable")
