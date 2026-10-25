"""
Walk-forward صادق: نختار المعاملات على النصف الأول فقط، ثم نقيس على النصف الثاني
(بيانات لم يرها الاختيار إطلاقاً). هذا أفضل اختبار متاح ضد التوفيق الزائف.

    python src/walkforward.py 4h 8000
"""
import asyncio
import itertools
import sys

import numpy as np
import pandas as pd

from bt_core import Bars, fetch_history, simulate, summarize
from datafeed import DataFeed
from exp import SYMBOLS, load
from strategy import TrendPulse

GRID = {
    "adx_min": [15, 20, 25],
    "use_breakout": [True, False],
    "sl_max_atr": [2.0, 3.0],
    "er_min": [0.0, 0.2, 0.3],
}
EXITS = {
    "C: تتبّع 3.5ATR": dict(partials=None, be_after_tp1=False, trail_atr=3.5),
    "C5: تتبّع 4.5ATR": dict(partials=None, be_after_tp1=False, trail_atr=4.5),
    "A: جزئي + BE": dict(partials=(0.5, 0.3, 0.2), be_after_tp1=True),
}


def collect(data, strat, exit_cfg, lo_frac, hi_frac, min_i=250):
    trades = []
    for sym, df in data.items():
        d = strat.prepare(df.copy())
        sigs = strat.signals(d)
        bars = Bars(d)
        sa = sigs["side"].to_numpy()
        lo = max(int(len(d) * lo_frac), min_i)
        hi = int(len(d) * hi_frac)
        nxt = lo
        for i in range(lo, hi - 2):
            if i < nxt or sa[i] == 0:
                continue
            r = sigs.iloc[i]
            t = simulate(bars, i, "LONG" if r["side"] == 1 else "SHORT",
                         float(r["entry"]), float(r["sl"]),
                         [r["tp1"], r["tp2"], r["tp3"]], **exit_cfg)
            if t:
                t.symbol, t.setup = sym, r["setup"]
                trades.append(t)
                nxt = i + max(t.i1 - t.i0, 1) + 1
    trades.sort(key=lambda x: x.i0)
    return trades


async def main():
    tf = sys.argv[1] if len(sys.argv) > 1 else "4h"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
    data = await load("okx", tf, n)
    keys = list(GRID)
    rows = []
    print(f"اختيار المعاملات على النصف الأول ({tf}, {n} شمعة, {len(data)} زوج)…")
    for combo in itertools.product(*GRID.values()):
        params = dict(zip(keys, combo))
        strat = TrendPulse(params)
        for ex, cfg in EXITS.items():
            tr = collect(data, strat, cfg, 0.0, 0.5)
            s = summarize(tr)
            if s["n"] < 80:
                continue
            rows.append({**params, "exit": ex, **{k: s[k] for k in ("n", "win", "avg_r", "sum_r", "pf", "dd")}})
    res = pd.DataFrame(rows).sort_values("sum_r", ascending=False)
    print("\n── أفضل 8 على النصف الأول ──")
    print(res.head(8).to_string(index=False))

    print("\n── النصف الثاني (خارج العينة تماماً) ──")
    out = []
    for _, r in res.head(8).iterrows():
        params = {k: (bool(r[k]) if k == "use_breakout" else float(r[k]) if k in ("er_min", "sl_max_atr") else int(r[k]))
                  for k in keys}
        strat = TrendPulse(params)
        tr = collect(data, strat, EXITS[r["exit"]], 0.5, 1.0)
        s = summarize(tr)
        out.append({"adx": params["adx_min"], "brk": params["use_breakout"],
                    "slmax": params["sl_max_atr"], "er": params["er_min"], "exit": r["exit"],
                    "train_sum_r": r["sum_r"], "test_n": s["n"], "test_win": s["win"],
                    "test_avg_r": s["avg_r"], "test_sum_r": s["sum_r"], "test_pf": s["pf"], "test_dd": s["dd"]})
    od = pd.DataFrame(out)
    print(od.to_string(index=False))
    od.to_csv("data/walkforward.csv", index=False)
    print(f"\nعدد التوليفات التي بقيت موجبة في النصف الثاني: {(od.test_sum_r > 0).sum()} / {len(od)}")
    print(f"متوسط أداء النصف الثاني: {od.test_avg_r.mean():+.3f}R للصفقة")


if __name__ == "__main__":
    asyncio.run(main())
