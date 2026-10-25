"""نواة الباكتست v2: محاكاة بأسرّة numpy (أسرع ~20x) + إحصاءات."""
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cache"
CACHE.mkdir(parents=True, exist_ok=True)

FEE = 0.0005       # Taker لكل جهة
SLIP = 0.0002      # انزلاق لكل جهة
RISK = 0.01        # 1% من رأس المال لكل صفقة
FEE_MAKER = 0.0002


async def fetch_history(feed, symbol: str, timeframe: str, total: int) -> pd.DataFrame:
    key = CACHE / f"{feed.ex.id}_{timeframe}_{symbol.replace('/', '_').replace(':', '-')}_{total}.pkl"
    if key.exists():
        return pickle.loads(key.read_bytes())
    tf_ms = feed.ex.parse_timeframe(timeframe) * 1000
    now = feed.ex.milliseconds()
    since = now - tf_ms * total
    limit = 100 if tf_ms >= 86_400_000 else 300     # OKX يحدد 100 شمعة كحد أقصى للفريم اليومي
    out = []
    guard = 0
    while len(out) < total and since < now and guard < 200:
        guard += 1
        chunk = await feed.ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)
        if not chunk:
            break
        out += chunk
        since = chunk[-1][0] + tf_ms
        if len(chunk) < limit and len(out) >= total:
            break
    out = [c for c in out if c[0] <= now]
    df = (pd.DataFrame(out, columns=["ts", "open", "high", "low", "close", "volume"])
          .drop_duplicates("ts").reset_index(drop=True))
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    key.write_bytes(pickle.dumps(df))
    return df


class Bars:
    """تمثيل سريع بالـ numpy لشموع + ATR."""

    def __init__(self, df: pd.DataFrame):
        self.h = df["high"].to_numpy(float)
        self.l = df["low"].to_numpy(float)
        self.c = df["close"].to_numpy(float)
        self.o = df["open"].to_numpy(float)
        self.v = df["volume"].to_numpy(float)
        self.atr = df["atr"].to_numpy(float) if "atr" in df else np.zeros(len(df))
        self.dt = df["datetime"].astype(str).to_numpy() if "datetime" in df else None
        self.n = len(self.c)


@dataclass
class Trade:
    symbol: str
    side: str
    setup: str
    i0: int
    i1: int
    entry: float
    r_gross: float
    r_net: float


def simulate(bars: Bars, i0: int, side: str, entry: float, sl0: float, tps: list,
             *, partials=(0.5, 0.3, 0.2), be_after_tp1=True, trail_atr=0.0,
             max_bars=250, fee_side=FEE, slip=SLIP) -> Optional[Trade]:
    risk = abs(entry - sl0)
    if risk <= 0:
        return None
    w = list(partials) if partials else None
    remaining, realized, be = 1.0, 0.0, False
    best, sl = entry, sl0
    n = min(bars.n, i0 + 1 + max_bars)
    H, L, C, A = bars.h, bars.l, bars.c, bars.atr
    for j in range(i0 + 1, n):
        hi, lo = H[j], L[j]
        # التتبّع يُحسب من قمم/قيعان الشموع *السابقة* فقط (بدون Look-ahead داخل الشمعة)
        if trail_atr > 0:
            if side == "LONG":
                sl = max(sl, best - trail_atr * A[j])
            else:
                sl = min(sl, best + trail_atr * A[j])
        cur = entry if (be and trail_atr == 0) else sl
        if trail_atr > 0:
            best = max(best, hi) if side == "LONG" else min(best, lo)
        if (lo <= cur) if side == "LONG" else (hi >= cur):
            realized += remaining * (((cur - entry) if side == "LONG" else (entry - cur)) / risk)
            return _mk(side, i0, j, realized, risk, fee_side, slip, entry)
        if w:
            for k, tp in enumerate(tps):
                if w[k] <= 0:
                    continue
                if (hi >= tp) if side == "LONG" else (lo <= tp):
                    realized += w[k] * (((tp - entry) if side == "LONG" else (entry - tp)) / risk)
                    remaining -= w[k]
                    w[k] = 0.0
                    if k == 0 and be_after_tp1:
                        be = True
            if remaining <= 1e-9:
                return _mk(side, i0, j, realized, risk, fee_side, slip, entry)
        else:
            last = tps[-1]
            if (hi >= last) if side == "LONG" else (lo <= last):
                realized += ((last - entry) if side == "LONG" else (entry - last)) / risk
                return _mk(side, i0, j, realized, risk, fee_side, slip, entry)
    return None


def _mk(side, i0, j, r_gross, risk, fee_side, slip, entry) -> Trade:
    fee_r = 2 * (fee_side + slip) * entry / risk
    return Trade("", side, "", i0, j, entry, round(r_gross, 4), round(r_gross - fee_r, 4))


def summarize(trades: list, label: str = "") -> dict:
    if not trades:
        return {"label": label, "n": 0, "win": 0, "avg_r": 0, "sum_r": 0, "pf": 0, "dd": 0, "freq": 0}
    r = np.array([t.r_net for t in trades])
    eq = np.cumprod(1 + RISK * r)
    peak = np.maximum.accumulate(eq)
    dd = float(((eq - peak) / peak).min() * 100)
    wins, losses = r[r > 0], r[r <= 0]
    pf = float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else 99.0
    span_days = max((trades[-1].i1 - trades[0].i0) / 6, 1)   # 6 شموع 4h ≈ يوم
    return {"label": label, "n": len(trades), "win": round(len(wins) / len(trades) * 100, 1),
            "avg_r": round(float(r.mean()), 3), "sum_r": round(float(r.sum()), 1),
            "pf": round(pf, 2), "dd": round(dd, 1), "freq": round(len(trades) / span_days, 2)}


def fmt_stats(trades: list, label: str = "") -> str:
    s = summarize(trades, label)
    if not s["n"]:
        return f"═══ {label} ═══\nلا توجد صفقات."
    out = [f"═══ {label} ═══",
           f"الصفقات       : {s['n']} (~{s['freq']} / يوم لكل المحفظة)",
           f"نسبة النجاح   : {s['win']}%",
           f"متوسط R       : {s['avg_r']:+.3f}",
           f"مجموع R       : {s['sum_r']:+.1f}R",
           f"Profit Factor : {s['pf']}",
           f"أقصى تراجع    : {s['dd']}% (مخاطرة 1%/صفقة)"]
    for side in ("LONG", "SHORT"):
        sub = [t for t in trades if t.side == side]
        if sub:
            sr = np.array([t.r_net for t in sub])
            out.append(f"  {side:5}: {len(sub):3} | نجاح {len(sr[sr > 0]) / len(sub) * 100:4.1f}% | {sr.sum():+.1f}R")
    for st in ("pullback", "breakout", "meanrev"):
        sub = [t for t in trades if t.setup == st]
        if sub:
            sr = np.array([t.r_net for t in sub])
            out.append(f"  {st:8}: {len(sub):3} | نجاح {len(sr[sr > 0]) / len(sub) * 100:4.1f}% | {sr.sum():+.1f}R")
    return "\n".join(out)
