"""
محرك باكتست واقعي (بإنصاف ومتحفظ) لاستراتيجيات البوت.

    python src/backtest.py                        # الافتراضي: trend_pulse, okx, 1h
    python src/backtest.py trend_pulse 1h 3000    # استراتيجية / فريم / عدد الشموع
    python src/backtest.py trend_pulse 1h 3000 --exchange binanceusdm

الافتراضات المتحفظة:
  • الدخول عند إغلاق الشمعة التي تحققت فيها الشروط (بدون Look-ahead).
  • إذا لامست الشمعة الستوب والهدف معاً → نعتبر الستوب أولاً (الأسوأ).
  • هدف واحد فقط لكل شمعة، ومراكز جزئية 50% / 30% / 20% على 1R/2R/3R.
  • بعد تحقق الهدف الأول يُنقل الستوب إلى الدخول (Breakeven) للجزء المتبقي.
  • عمولات Taker 0.05% لكل جهة + انزلاق 0.02%.
  • مخاطرة 1% من رأس المال لكل صفقة، صفقة واحدة مفتوحة لكل زوج.
"""
import asyncio
import sys
from dataclasses import asdict, dataclass, field

import pandas as pd

from datafeed import DataFeed
from strategy import STRATEGIES, DEFAULT_STRATEGY

FEE_RATE = 0.0005      # عمولة Taker
SLIPPAGE = 0.0002      # انزلاق تقديري لكل جهة
RISK_PCT = 0.01        # 1% لكل صفقة
SIDE_MODE = "one"      # صفقة واحدة لكل زوج في نفس الوقت


@dataclass
class Trade:
    symbol: str
    side: str
    setup: str
    entry_time: str
    exit_time: str
    entry: float
    stop: float
    r_gross: float
    fee_r: float
    r_net: float
    bars: int


async def fetch_history(feed: DataFeed, symbol: str, timeframe: str, total: int) -> pd.DataFrame:
    """جلب عدد كبير من الشموع عبر الترقيم (pagination)."""
    tf_ms = feed.ex.parse_timeframe(timeframe) * 1000   # ccxt يعيد الثواني
    since = feed.ex.milliseconds() - tf_ms * total
    out = []
    while len(out) < total:
        chunk = await feed.ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=300)
        if not chunk:
            break
        out += chunk
        since = chunk[-1][0] + tf_ms
        if len(chunk) < 300:
            break
    df = pd.DataFrame(out, columns=["ts", "open", "high", "low", "close", "volume"]).drop_duplicates("ts")
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.reset_index(drop=True)


def simulate(df: pd.DataFrame, i0: int, sig, side_mode=SIDE_MODE) -> Trade | None:
    """محاكاة صفقة تبدأ من الشمعة i0+1 مع الإدارة التدريجية."""
    side = sig.side
    entry, sl = sig.entry, sig.stop_loss
    tps = sig.take_profits
    risk = abs(entry - sl)
    weights = [0.5, 0.3, 0.2]
    remaining = 1.0
    realized = 0.0
    be = False
    bars = 0
    for j in range(i0 + 1, len(df)):
        bars += 1
        if bars > 200:       # حماية من صفقات معلّقة للأبد
            t = df.iloc[j - 1]
            px = float(t["close"])
            realized += remaining * ((px - entry) if side == "LONG" else (entry - px)) / risk
            return _mk(sig, df, i0, j - 1, realized, risk)

        bar = df.iloc[j]
        hi, lo = float(bar["high"]), float(bar["low"])
        cur_sl = entry if be else sl

        # --- فحص الستوب أولاً (الأسوأ) ---
        hit_sl = (lo <= cur_sl) if side == "LONG" else (hi >= cur_sl)
        if hit_sl:
            realized += remaining * (((cur_sl - entry) if side == "LONG" else (entry - cur_sl)) / risk)
            return _mk(sig, df, i0, j, realized, risk)

        # --- فحص الأهداف بالترتيب ---
        for k, tp in enumerate(tps):
            if weights[k] <= 0:
                continue
            hit_tp = (hi >= tp) if side == "LONG" else (lo <= tp)
            if hit_tp:
                realized += weights[k] * (((tp - entry) if side == "LONG" else (entry - tp)) / risk)
                remaining -= weights[k]
                weights[k] = 0.0
                if k == 0:
                    be = True          # تحرير الستوب بعد الهدف الأول
        if remaining <= 1e-9:
            return _mk(sig, df, i0, j, realized, risk)
    return None


def _mk(sig, df, i0, j, r_gross, risk) -> Trade:
    fee_r = 2 * (FEE_RATE + SLIPPAGE) * sig.entry / risk
    return Trade(
        symbol=sig.symbol, side=sig.side, setup=sig.setup,
        entry_time=str(df.iloc[i0]["datetime"]), exit_time=str(df.iloc[j]["datetime"]),
        entry=sig.entry, stop=sig.stop_loss,
        r_gross=round(r_gross, 4), fee_r=round(fee_r, 4),
        r_net=round(r_gross - fee_r, 4), bars=j - i0,
    )


def stats(trades: list[Trade], subtitle: str = "") -> str:
    if not trades:
        return "لا توجد صفقات."
    r = pd.Series([t.r_net for t in trades])
    eq = (1 + RISK_PCT * r).cumprod()
    peak = eq.cummax()
    dd = ((eq - peak) / peak).min() * 100
    wins = r[r > 0]
    losses = r[r <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() != 0 else float("inf")
    days = (pd.to_datetime(trades[-1].exit_time) - pd.to_datetime(trades[0].entry_time)).days or 1
    lines = [
        f"═══ {subtitle} ═══",
        f"عدد الصفقات        : {len(trades)}  (~{len(trades) / days:.2f} صفقة/يوم)",
        f"نسبة النجاح        : {len(wins) / len(trades) * 100:.1f}%",
        f"متوسط R للصفقة     : {r.mean():+.3f}",
        f"مجموع R            : {r.sum():+.1f}R",
        f"Profit Factor      : {pf:.2f}",
        f"أقصى تراجع (1% risk): {dd:.1f}%",
        f"نمو رأس المال      : {(eq.iloc[-1] - 1) * 100:+.1f}%",
        f"متوسط مدة الصفقة   : {pd.Series([t.bars for t in trades]).mean():.0f} شمعة",
    ]
    for side in ("LONG", "SHORT"):
        sub = [t for t in trades if t.side == side]
        if sub:
            sr = pd.Series([t.r_net for t in sub])
            lines.append(f"  {side:5}: {len(sub):3} صفقة | نجاح {len(sr[sr > 0]) / len(sub) * 100:4.1f}% | مجموع {sr.sum():+.1f}R")
    for st in ("pullback", "breakout"):
        sub = [t for t in trades if t.setup == st]
        if sub:
            sr = pd.Series([t.r_net for t in sub])
            lines.append(f"  {st:8}: {len(sub):3} صفقة | نجاح {len(sr[sr > 0]) / len(sub) * 100:4.1f}% | مجموع {sr.sum():+.1f}R")
    return "\n".join(lines)


async def run(strategy_name: str, timeframe: str, candles: int, exchange: str,
              symbols: list[str], max_bars_hold: int = 200):
    strat = STRATEGIES[strategy_name]({"leverage": 10})
    feed = DataFeed(exchange)
    all_trades: list[Trade] = []
    print(f"الاستراتيجية: {strategy_name} | البورصة: {exchange} | الفريم: {timeframe} | "
          f"{candles} شمعة/زوج | {len(symbols)} زوج\n")
    for sym in symbols:
        try:
            df = await fetch_history(feed, sym, timeframe, candles)
            if len(df) < 400:
                print(f"  {sym}: بيانات غير كافية ({len(df)})")
                continue
            strat.prepare(df)
            tr = []
            nxt = 300
            for i in range(300, len(df) - 5):
                if i < nxt:
                    continue
                sig = strat.signal_at(sym, df, i)
                if not sig:
                    continue
                t = simulate(df, i, sig)
                if t:
                    tr.append(t)
                    nxt = i + max(t.bars, 1) + 1     # لا تداخل: ننتظر إغلاق الصفقة
            all_trades += tr
            r = pd.Series([x.r_net for x in tr])
            tot = f"{r.sum():+.1f}R" if len(r) else "—"
            print(f"  {sym:16} شموع={len(df):5} صفقات={len(tr):3} مجموع={tot:>8}")
        except Exception as e:
            print(f"  {sym}: خطأ {str(e)[:80]}")
    await feed.close()
    all_trades.sort(key=lambda t: t.entry_time)
    print()
    print(stats(all_trades, f"{strategy_name} — {exchange} {timeframe}"))
    return all_trades


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    name = args[0] if len(args) > 0 else DEFAULT_STRATEGY
    tf = args[1] if len(args) > 1 else "1h"
    candles = int(args[2]) if len(args) > 2 else 3000
    exch = "okx"
    if "--exchange" in sys.argv:
        exch = sys.argv[sys.argv.index("--exchange") + 1]
    syms = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
            "XRP/USDT:USDT", "DOGE/USDT:USDT", "ADA/USDT:USDT", "AVAX/USDT:USDT",
            "LINK/USDT:USDT", "LTC/USDT:USDT"]
    asyncio.run(run(name, tf, candles, exch, syms))
