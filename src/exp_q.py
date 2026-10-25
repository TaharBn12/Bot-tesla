"""
تحليل ربع سنوي: هل الاستراتيجية تصمد في أنظمة سوق مختلفة أم أنها توفيق زائف؟
    python src/exp_q.py mean_rev 4h 4000
    python src/exp_q.py trend_pulse 4h 4000
"""
import asyncio
import sys

import numpy as np
import pandas as pd

from bt_core import Bars, fetch_history, simulate, summarize
from datafeed import DataFeed
from exp import SYMBOLS, EXITS
from strategy import STRATEGIES

PARAM_GRIDS = {
    "mean_rev": {
        "rsi_buy": [5, 10, 15],
        "rsi_sell": [90, 95],
        "sl_atr": [1.5, 2.0, 2.5],
    },
    "trend_pulse": {
        "adx_min": [20, 25],
        "use_breakout": [True, False],
        "sl_max_atr": [2.0, 3.0],
    },
}
EXIT_GRIDS = {
    "mean_rev": {
        "M1: EMA10+2R": dict(partials=(0.5, 0.3, 0.2), be_after_tp1=True),
        "M2: EMA10 فقط": dict(partials=(0.7, 0.3, 0.0), be_after_tp1=False),
        "M3: تتبّع 2ATR": dict(partials=None, be_after_tp1=False, trail_atr=2.0),
    },
    "trend_pulse": EXITS,
}


def collect(data, strat, exit_cfg, min_i=250):
    trades = []
    for sym, df in data.items():
        d = strat.prepare(df.copy())
        sigs = strat.signals(d)
        bars = Bars(d)
        side_arr = sigs["side"].to_numpy()
        nxt = min_i
        for i in range(min_i, len(d) - 2):
            if i < nxt or side_arr[i] == 0:
                continue
            row = sigs.iloc[i]
            side = "LONG" if row["side"] == 1 else "SHORT"
            t = simulate(bars, i, side, float(row["entry"]), float(row["sl"]),
                         [row["tp1"], row["tp2"], row["tp3"]], **exit_cfg)
            if t:
                t.symbol, t.setup = sym, row["setup"]
                trades.append(t)
                nxt = i + max(t.i1 - t.i0, 1) + 1
    trades.sort(key=lambda x: x.i0)
    return trades


def quarters(trades, n_bars, k=4):
    """تقسيم على عدد الشموع إلى k فترات متساوية."""
    step = n_bars // k
    out = []
    for q in range(k):
        lo, hi = q * step, (q + 1) * step
        sub = [t for t in trades if lo <= t.i0 < hi]
        s = summarize(sub)
        out.append(s)
    return out


async def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "mean_rev"
    tf = sys.argv[2] if len(sys.argv) > 2 else "4h"
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 4000
    feed = DataFeed("okx")
    data = {}
    for s in SYMBOLS:
        try:
            df = await fetch_history(feed, s, tf, n)
            if len(df) > 500:
                data[s] = df
        except Exception as e:
            print(s, str(e)[:60])
    await feed.close()
    nbars = min(len(v) for v in data.values())
    print(f"{name} | {tf} | {len(data)} زوج × {nbars} شمعة\n")

    grid = PARAM_GRIDS[name]
    exits = EXIT_GRIDS[name]
    keys = list(grid)
    import itertools
    rows = []
    for combo in itertools.product(*grid.values()):
        params = dict(zip(keys, combo))
        strat = STRATEGIES[name](params)
        for ex_name, ex_cfg in exits.items():
            tr = collect(data, strat, ex_cfg)
            if len(tr) < 80:
                continue
            s = summarize(tr)
            qs = quarters(tr, nbars, 4)
            rows.append({**params, "exit": ex_name, "n": s["n"], "win": s["win"],
                         "avg_r": s["avg_r"], "sum_r": s["sum_r"], "pf": s["pf"], "dd": s["dd"],
                         **{f"Q{i + 1}": q["sum_r"] for i, q in enumerate(qs)},
                         "pos_q": sum(1 for q in qs if q["sum_r"] > 0)})
    res = pd.DataFrame(rows)
    res["score"] = res["sum_r"] * (res["pos_q"] / 4)      # نكافئ الثبات بين الأرباع
    res = res.sort_values("score", ascending=False)
    res.to_csv(f"data/quarters_{name}_{tf}.csv", index=False)
    pd.set_option("display.width", 200)
    print(res.head(15).to_string(index=False))

    best = res.iloc[0]
    params = {k: (bool(best[k]) if isinstance(best[k], (bool, np.bool_)) else best[k]) for k in keys}
    strat = STRATEGIES[name](params)
    tr = collect(data, strat, exits[best["exit"]])
    print("\n" + "=" * 60)
    print(f"الأفضل: {params} | {best['exit']}")
    print(summarize(tr)["n"], "صفقة | أرباع رابحة:", int(best["pos_q"]), "/4")
    for i, q in enumerate(quarters(tr, nbars, 4)):
        print(f"  Q{i + 1}: {q['n']:3} صفقة | نجاح {q['win']:4.1f}% | {q['sum_r']:+7.1f}R | PF {q['pf']}")


if __name__ == "__main__":
    asyncio.run(main())
