"""
التحقق النهائي من المعاملات المختارة على فريمات ومحافظ أزواج مختلفة.
    python src/verify.py
"""
import asyncio

import numpy as np
import pandas as pd

from bt_core import Bars, fetch_history, simulate, summarize
from datafeed import DataFeed
from strategy import TrendPulse

# المعاملات النهائية (مركز الكتلة، لا أفضل نقطة)
PARAMS = dict(ema_fast=20, ema_mid=50, ema_slow=200, adx_min=20, volume_mult=1.15,
              er_min=0.25, use_breakout=True, sl_max_atr=2.5, sl_atr=0.2,
              atr_min_pct=0.35, atr_max_pct=6.0, rr=[1.0, 2.0, 3.0])
EXIT = dict(partials=None, be_after_tp1=False, trail_atr=3.5)

SETS = {
    "10 أزواج رئيسية": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
                        "XRP/USDT:USDT", "DOGE/USDT:USDT", "ADA/USDT:USDT", "AVAX/USDT:USDT",
                        "LINK/USDT:USDT", "LTC/USDT:USDT"],
    "الثلاثة الكبرى فقط": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"],
    "أزواج أخرى (خارج الاختيار)": ["ATOM/USDT:USDT", "DOT/USDT:USDT", "NEAR/USDT:USDT",
                                   "FIL/USDT:USDT", "TRX/USDT:USDT", "ETC/USDT:USDT",
                                   "AAVE/USDT:USDT", "UNI/USDT:USDT"],
}


def run(data, exit_cfg=EXIT, params=PARAMS, min_i=250):
    strat = TrendPulse(params)
    trades = []
    for sym, df in data.items():
        d = strat.prepare(df.copy())
        sigs = strat.signals(d)
        bars = Bars(d)
        sa = sigs["side"].to_numpy()
        nxt = min_i
        for i in range(min_i, len(d) - 2):
            if i < nxt or sa[i] == 0:
                continue
            r = sigs.iloc[i]
            t = simulate(bars, i, "LONG" if r["side"] == 1 else "SHORT",
                         float(r["entry"]), float(r["sl"]), [r["tp1"], r["tp2"], r["tp3"]], **exit_cfg)
            if t:
                t.symbol, t.setup = sym, r["setup"]
                trades.append(t)
                nxt = i + max(t.i1 - t.i0, 1) + 1
    trades.sort(key=lambda x: x.i0)
    return trades


async def main():
    feed = DataFeed("okx")
    report = []
    for tf, n in (("4h", 8000), ("1h", 8000), ("1d", 2500)):
        for set_name, syms in SETS.items():
            data = {}
            for s in syms:
                try:
                    df = await fetch_history(feed, s, tf, n)
                    if len(df) > 500:
                        data[s] = df
                except Exception as e:
                    print(f"  {s}: {str(e)[:50]}")
            if not data:
                continue
            tr = run(data)
            s = summarize(tr)
            nb = min(len(v) for v in data.values())
            q = len(tr) and [summarize([t for t in tr if q0 * nb / 4 <= t.i0 < (q0 + 1) * nb / 4]) for q0 in range(4)]
            pos = sum(1 for x in (q or []) if x["n"] and x["sum_r"] > 0)
            tot = sum(1 for x in (q or []) if x["n"])
            report.append({"فريم": tf, "المجموعة": set_name, "صفقات": s["n"], "نجاح%": s["win"],
                           "متوسط R": s["avg_r"], "مجموع R": s["sum_r"], "PF": s["pf"],
                           "تراجع%": s["dd"], "أرباع رابحة": f"{pos}/{tot}"})
            print(f"{tf:3} | {set_name:26} | {s['n']:4} صفقة | {s['sum_r']:+7.1f}R | PF {s['pf']} | أرباع {pos}/{tot}")
    await feed.close()
    df = pd.DataFrame(report)
    df.to_csv("data/verify_report.csv", index=False)
    print("\n" + df.to_string(index=False))


if __name__ == "__main__":
    asyncio.run(main())
