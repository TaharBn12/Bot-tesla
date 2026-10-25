"""
فحص انحياز المحاكي: دخول عشوائي 100% بنفس الستوب/الأهداف.
إذا كان متوسط R للدخول العشوائي ≈ 0 (ناقص العمولات) فالمحاكي سليم.
إذا كان سالباً بشكل كبير، فالمنطق نفسه فيه انحياز ويجب إصلاحه قبل أي استنتاج.
"""
import asyncio
import random
import sys

import pandas as pd

from bt_core import Bars, fetch_history, simulate, summarize, fmt_stats
from datafeed import DataFeed
from strategy import TrendPulse, Signal

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT", "DOGE/USDT:USDT"]
EXITS = {
    "C: تتبّع 3.5ATR": dict(partials=None, be_after_tp1=False, trail_atr=3.5),
    "A: أهداف 1/2/3R": dict(partials=(0.5, 0.3, 0.2), be_after_tp1=True, trail_atr=0.0),
    "D: هدف وحيد 3R": dict(partials=None, be_after_tp1=False, trail_atr=0.0),
}


async def main():
    tf = sys.argv[1] if len(sys.argv) > 1 else "1h"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 4000
    feed = DataFeed("okx")
    prep = TrendPulse({})
    rnd = random.Random(7)
    per_exit = {k: [] for k in EXITS}
    for sym in SYMBOLS:
        d = prep.prepare(await fetch_history(feed, sym, tf, n))
        df = d
        bars = Bars(d)
        for _ in range(300):
            i = rnd.randrange(300, len(df) - 30)
            bar = df.iloc[i]
            atr = float(bar["atr"])
            if not (atr > 0):
                continue
            side = rnd.choice(["LONG", "SHORT"])
            entry = float(bar["close"])
            sl = entry - 1.5 * atr if side == "LONG" else entry + 1.5 * atr
            risk = abs(entry - sl)
            tps = [entry + risk * r if side == "LONG" else entry - risk * r for r in (1, 2, 3)]
            sig = Signal(sym, side, entry, sl, tps, 10, "random", 0, "random")
            for name, cfg in EXITS.items():
                t = simulate(bars, i, side, entry, sl, tps, **cfg)
                if t:
                    per_exit[name].append(t)
    await feed.close()
    print(f"دخول عشوائي — {tf} — {n} شمعة — {len(SYMBOLS)} أزواج\n")
    for name, tr in per_exit.items():
        print(fmt_stats(tr, f"عشوائي | {name}"))
        print()


if __name__ == "__main__":
    asyncio.run(main())
