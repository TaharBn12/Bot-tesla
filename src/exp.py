"""
منصة تجارب: تقييم استراتيجية/معاملات مع فصل تدريب (70%) وتحقق (30%).
    python src/exp.py 4h 4000
"""
import asyncio
import itertools
import sys

import numpy as np
import pandas as pd

from bt_core import Bars, fetch_history, simulate, summarize
from datafeed import DataFeed
from strategy import TrendPulse

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
           "XRP/USDT:USDT", "DOGE/USDT:USDT", "ADA/USDT:USDT", "AVAX/USDT:USDT",
           "LINK/USDT:USDT", "LTC/USDT:USDT"]

EXITS = {
    "A: جزئي 1/2/3R": dict(partials=(0.5, 0.3, 0.2), be_after_tp1=True),
    "C: تتبّع 3.5ATR": dict(partials=None, be_after_tp1=False, trail_atr=3.5),
    "E: تتبّع 2.5ATR": dict(partials=(0.4, 0.3, 0.3), be_after_tp1=False, trail_atr=2.5),
    "F: تتبّع 1.5ATR": dict(partials=None, be_after_tp1=False, trail_atr=1.5),
}


async def load(exch, tf, n, symbols=SYMBOLS):
    feed = DataFeed(exch)
    out = {}
    for s in symbols:
        try:
            df = await fetch_history(feed, s, tf, n)
            if len(df) > 500:
                out[s] = df
        except Exception as e:
            print(f"  {s}: {e}")
    await feed.close()
    return out


def collect(data, strat, exit_cfg, lo_frac=0.0, hi_frac=1.0, min_i=250):
    trades = []
    for sym, df in data.items():
        d = strat.prepare(df.copy())
        sigs = strat.signals(d)
        bars = Bars(d)
        lo = max(int(len(d) * lo_frac), min_i)
        hi = int(len(d) * hi_frac)
        nxt = lo
        side_arr = sigs["side"].to_numpy()
        for i in range(lo, hi - 2):
            if i < nxt or side_arr[i] == 0:
                continue
            row = sigs.iloc[i]
            side = "LONG" if row["side"] == 1 else "SHORT"
            tps = [row["tp1"], row["tp2"], row["tp3"]]
            t = simulate(bars, i, side, float(row["entry"]), float(row["sl"]), tps, **exit_cfg)
            if t:
                t.symbol, t.setup = sym, row["setup"]
                trades.append(t)
                nxt = i + max(t.i1 - t.i0, 1) + 1
    trades.sort(key=lambda x: x.i0)
    return trades


async def main():
    tf = sys.argv[1] if len(sys.argv) > 1 else "4h"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 4000
    data = await load("okx", tf, n)
    print(f"بيانات: {len(data)} زوج × ~{np.mean([len(v) for v in data.values()]):.0f} شمعة {tf}\n")

    grid = {
        "adx_min": [15, 20, 25],
        "use_breakout": [True, False],
        "sl_max_atr": [2.0, 3.0],
    }
    base_params = {}
    rows = []
    keys = list(grid)
    combos = list(itertools.product(*grid.values()))
    print(f"{len(combos)} توليفة دخول × {len(EXITS)} خروج\n")
    for combo in combos:
        params = {**base_params, **dict(zip(keys, combo))}
        strat = TrendPulse(params)
        for ex_name, ex_cfg in EXITS.items():
            tr = collect(data, strat, ex_cfg, 0.0, 0.7)
            s = summarize(tr)
            if s["n"] < 50:
                continue
            rows.append({**dict(zip(keys, combo)), "exit": ex_name,
                         **{k: s[k] for k in ("n", "win", "avg_r", "sum_r", "pf", "dd")}})
    res = pd.DataFrame(rows).sort_values("sum_r", ascending=False)
    res.to_csv(f"data/exp_{tf}.csv", index=False)
    print("── أفضل 12 على التدريب (70% الأولى) ──")
    print(res.head(12).to_string(index=False))

    print("\n── تحقق على 30% الأخيرة (لم تُستخدم في الاختيار) ──")
    val_rows = []
    for _, r in res.head(8).iterrows():
        params = {k: (bool(r[k]) if k == "use_breakout" else int(r[k])) for k in keys}
        strat = TrendPulse(params)
        tr = collect(data, strat, EXITS[r["exit"]], 0.7, 1.0)
        s = summarize(tr)
        val_rows.append({**params, "exit": r["exit"], "train_sum_r": r["sum_r"],
                         **{k: s[k] for k in ("n", "win", "avg_r", "sum_r", "pf", "dd")}})
    print(pd.DataFrame(val_rows).to_string(index=False))


if __name__ == "__main__":
    asyncio.run(main())
