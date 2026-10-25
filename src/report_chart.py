"""
تقرير مرئي: منحنى رأس المال + توزيع R + الأداء الربع سنوي (4h، 3.6 سنة)
    python src/report_chart.py
"""
import asyncio

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from bt_core import fetch_history, summarize
from datafeed import DataFeed
from verify import PARAMS, EXIT, SETS, run

plt.rcParams["font.family"] = "DejaVu Sans"


async def main():
    feed = DataFeed("okx")
    data = {}
    for s in SETS["10 أزواج رئيسية"]:
        data[s] = await fetch_history(feed, s, "4h", 8000)
    await feed.close()
    trades = run(data, EXIT, PARAMS)
    nb = min(len(v) for v in data.values())

    r = np.array([t.r_net for t in trades])
    eq = np.cumprod(1 + 0.01 * r)
    idx = np.array([t.i0 for t in trades])

    fig, ax = plt.subplots(2, 2, figsize=(14, 8.5))
    fig.suptitle("TrendPulse v1 — Binance/OKX Futures 4h — 10 major pairs — 2023-03 → 2026-10",
                 fontsize=12, fontweight="bold")

    ax[0][0].plot(idx, (eq - 1) * 100, color="#0b7285", lw=1.6)
    ax[0][0].axhline(0, color="gray", lw=0.8, ls="--")
    ax[0][0].set_title(f"Equity (risk 1% per trade) : {(eq[-1] - 1) * 100:+.1f}%")
    ax[0][0].set_ylabel("%")
    ax[0][0].grid(alpha=0.3)

    peak = np.maximum.accumulate(eq)
    ax[0][1].fill_between(idx, (eq - peak) / peak * 100, 0, color="#c92a2a", alpha=0.45)
    ax[0][1].set_title(f"Drawdown : {((eq - peak) / peak).min() * 100:.1f}%")
    ax[0][1].set_ylabel("%")
    ax[0][1].grid(alpha=0.3)

    ax[1][0].hist(r, bins=40, color="#5f3dc4", alpha=0.8)
    ax[1][0].axvline(0, color="k", lw=0.8)
    ax[1][0].axvline(r.mean(), color="#e8590c", lw=1.6, label=f"mean {r.mean():+.3f}R")
    ax[1][0].set_title(f"R distribution ({len(r)} trades, win rate {(r > 0).mean() * 100:.1f}%)")
    ax[1][0].legend()
    ax[1][0].grid(alpha=0.3)

    qs = []
    for q in range(4):
        sub = [t for t in trades if q * nb / 4 <= t.i0 < (q + 1) * nb / 4]
        qs.append(summarize(sub)["sum_r"])
    colors = ["#2f9e44" if v > 0 else "#c92a2a" for v in qs]
    ax[1][1].bar([f"Q{i + 1}" for i in range(4)], qs, color=colors)
    ax[1][1].axhline(0, color="k", lw=0.8)
    ax[1][1].set_title("Quarterly R (regime stability)")
    ax[1][1].set_ylabel("R")
    ax[1][1].grid(alpha=0.3, axis="y")

    plt.tight_layout()
    plt.savefig("data/equity_curve.png", dpi=130)
    s = summarize(trades)
    print(f"صفقات={s['n']} نجاح={s['win']}% متوسطR={s['avg_r']} مجموعR={s['sum_r']} PF={s['pf']} DD={s['dd']}%")
    print("أرباع:", [round(v, 1) for v in qs])


if __name__ == "__main__":
    asyncio.run(main())
