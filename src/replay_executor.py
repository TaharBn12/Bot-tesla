"""
إعادة تشغيل كاملة لطبقة التنفيذ على بيانات حقيقية (بدون بورصة وبدون مفاتيح).

هذه ليست باكتست — بل اختبار لدورة حياة المنفّذ نفسه (PaperExecutor):
  إشارة → فتح → ستوب متتبّع → ضرب ستوب → محاسبة → مفتاح إيقاف.

    python src/replay_executor.py
"""
import asyncio
import json
from pathlib import Path

import pandas as pd

from bt_core import fetch_history
from datafeed import DataFeed
from executor import JOURNAL, PaperExecutor
from risk import KillSwitch
from strategy import TrendPulse
from verify import PARAMS

ROOT = Path(__file__).resolve().parent.parent
SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
           "XRP/USDT:USDT", "DOGE/USDT:USDT", "ADA/USDT:USDT", "LINK/USDT:USDT"]


class Sett:
    risk_per_trade_pct = 1.0
    leverage = 5
    max_open_positions = 3
    timeframe = "4h"
    dry_run = False
    trail_atr = 3.5
    exchange = "okx"


def atr_at(df, i, n=14):
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1 / n, adjust=False).mean().iloc[i])


async def main():
    JOURNAL.unlink(missing_ok=True)
    PaperExecutor.STATE.unlink(missing_ok=True)

    feed = DataFeed("okx")
    raw = {}
    for s in SYMBOLS:
        df = await fetch_history(feed, s, "4h", 8000)
        raw[s] = df.tail(2200).reset_index(drop=True)     # ~سنة: 2025-10 → 2026-10
    await feed.close()

    strat = TrendPulse(PARAMS)
    data = {s: strat.prepare(d.copy()) for s, d in raw.items()}
    sigs = {s: strat.signals(d) for s, d in data.items()}

    n = min(len(d) for d in data.values())
    start = 260
    ex = PaperExecutor(Sett(), feed=None, balance=1000.0,
                       kill_switch=KillSwitch(max_daily_loss_pct=6.0, max_trades_per_day=50,
                                              max_consecutive_losses=8))
    opened_at_idx: dict[str, int] = {}
    equity_curve, concurrent_max = [], 0
    trades = []

    for i in range(start, n - 1):
        # 1) فحص الستوبات داخل الشمعة (الأسوأ أولاً)
        for pos in list(ex.positions()):
            sym = pos.symbol
            bar = data[sym].iloc[i]
            if (bar["low"] <= pos.sl) if pos.side == "LONG" else (bar["high"] >= pos.sl):
                before = ex.equity()
                when = pd.Timestamp(data[sym]["datetime"].iloc[i]).date()
                ex.close(sym, "ضرب الستوب المتتبّع", price=pos.sl, when=when)
                trades.append(ex.equity() - before)
                opened_at_idx.pop(sym, None)
                continue
            # 2) تحديث الستوب المتتبّع (من بيانات ما بعد الفتح فقط)
            k = opened_at_idx.get(sym, i)
            window = data[sym].iloc[max(k, i - 60): i + 1]
            new_sl = ex.proposed_stop(pos, float(window["high"].max()),
                                      float(window["low"].min()), atr_at(data[sym], i))
            if new_sl:
                ex.trail(sym, new_sl)

        # 3) إشارات جديدة
        if len(ex.positions()) < Sett.max_open_positions:
            for sym in SYMBOLS:
                if len(ex.positions()) >= Sett.max_open_positions:
                    break
                if sym in [p.symbol for p in ex.positions()] or sigs[sym]["side"].iloc[i] == 0:
                    continue
                if sigs[sym]["side"].iloc[i - 1] != 0:     # نتجاهل تكرار نفس الإشارة
                    continue
                row = sigs[sym].iloc[i]
                from strategy import Signal
                sig = Signal(symbol=sym, side="LONG" if row["side"] == 1 else "SHORT",
                             entry=float(row["entry"]), stop_loss=float(row["sl"]),
                             take_profits=[float(row["tp1"]), float(row["tp2"]), float(row["tp3"])],
                             leverage=5, reason="replay", atr=float(row["atr"]), setup=row["setup"])
                if ex.open(sig):
                    opened_at_idx[sym] = i
                    concurrent_max = max(concurrent_max, len(ex.positions()))

        equity_curve.append(ex.equity())

    # إغلاق ما تبقّى بسعر السوق
    for pos in list(ex.positions()):
        px = float(data[pos.symbol]["close"].iloc[n - 1])
        before = ex.equity()
        ex.close(pos.symbol, "انتهاء فترة الاختبار", price=px,
                 when=pd.Timestamp(data[pos.symbol]["datetime"].iloc[n - 1]).date())
        trades.append(ex.equity() - before)

    eq = pd.Series(equity_curve)
    dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
    wins = [t for t in trades if t > 0]
    print("\n" + "=" * 62)
    print("نتيجة إعادة تشغيل طبقة التنفيذ على بيانات 4h حقيقية")
    print("=" * 62)
    print(f"الفترة            : {data[SYMBOLS[0]]['datetime'].iloc[start]} → {data[SYMBOLS[0]]['datetime'].iloc[n - 1]}")
    print(f"الرصيد الابتدائي  : 1000.00 USDT")
    print(f"الرصيد النهائي    : {ex.equity():.2f} USDT ({(ex.equity() / 1000 - 1) * 100:+.1f}%)")
    print(f"عدد الصفقات       : {len(trades)}")
    print(f"نسبة النجاح       : {len(wins) / max(len(trades), 1) * 100:.1f}%")
    print(f"أقصى تراجع        : {dd:.1f}%")
    print(f"أقصى صفقات متزامنة: {concurrent_max} (السقف {Sett.max_open_positions})")
    print(f"حالة مفتاح الإيقاف: {ex.risk_status()}")
    print(f"ملف السجل         : data/journal.jsonl")

    rows = [json.loads(line) for line in JOURNAL.read_text(encoding="utf-8").splitlines()]
    ev = pd.Series([r["event"] for r in rows]).value_counts()
    print("\nتفصيل الأحداث في السجل:")
    for k, v in ev.items():
        print(f"  {k:16} {v}")


if __name__ == "__main__":
    asyncio.run(main())
