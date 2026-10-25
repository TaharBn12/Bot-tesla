"""
الاستراتيجيات المتاحة للبوت.
    StrategyBase  : الواجهة المشتركة
    TrendPulse    : استراتيجية "TrendPulse v1" — الاستراتيجية الأساسية (مبنية وتم اختبارها)
    EmaCrossExample: مثال تعليمي بسيط (غير موصى به للتشغيل الحقيقي)

كل استراتيجية تُرجع Signal أو None. لا إرسال لتلجرام من هنا.
"""
from dataclasses import dataclass
from typing import Optional
import numpy as np
import pandas as pd


@dataclass
class Signal:
    symbol: str
    side: str            # "LONG" / "SHORT"
    entry: float
    stop_loss: float
    take_profits: list   # [tp1, tp2, tp3]
    leverage: int
    reason: str = ""
    score: float = 0.0
    setup: str = ""      # pullback | breakout | meanrev
    atr: float = 0.0     # لتحديث الستوب المتتبَّع


class StrategyBase:
    name = "base"

    def __init__(self, params: dict | None = None):
        self.params = params or {}

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df

    def evaluate(self, symbol: str, df: pd.DataFrame) -> Optional[Signal]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# مؤشرات مساعدة
# ---------------------------------------------------------------------------
def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = _wilder(tr, n)
    pdi = 100 * _wilder(pd.Series(plus_dm, index=df.index), n) / atr.replace(0, np.nan)
    mdi = 100 * _wilder(pd.Series(minus_dm, index=df.index), n) / atr.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return _wilder(dx, n)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    gain = _wilder(d.clip(lower=0), n)
    loss = _wilder((-d).clip(lower=0), n)
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


# ---------------------------------------------------------------------------
# TrendPulse v1
# ---------------------------------------------------------------------------
class TrendPulse(StrategyBase):
    """
    استراتيجية تتبّع اتجاه مع دخول على التصحيح (Trend-following Pullback)
    + خروج تدريجي على 3 أهداف وتحرير الستوب.

    المنطق:
      1) فلتر نظام السوق : EMA200 صاعدة/هابطة + السعر فوقها/تحتها  → نتداول مع الاتجاه الكبير فقط
      2) فلتر القوة       : ADX > 20  → لا ندخل في سوق عرضي ميت
      3) الدخول           : تصحيح إلى منطقة EMA20 ثم شمعة انعكاس مؤكدة
                            أو اختراق Donchian20 في اتجاه الترند
      4) فلتر التطرف      : RSI بين 45–72 (شراء) / 28–55 (بيع) → لا نلحق قمة/قاع
      5) فلتر السيولة     : الحجم > 1.15 × متوسط 20
      6) فلتر التقلب      : ATR% بين 0.35% و 6% → لا سوق ميت ولا انفجار غير طبيعي
      7) الستوب           : أسفل أدنى قاع 5 شموع - 0.2×ATR (وبحد أقصى 2.5×ATR)
      8) الأهداف          : 1R / 2R / 3R مع إدارة تدريجية (50% / 30% / 20%)
    """

    name = "trend_pulse"

    def __init__(self, params=None):
        super().__init__(params)
        p = self.params
        self.ema_fast = p.get("ema_fast", 20)
        self.ema_mid = p.get("ema_mid", 50)
        self.ema_slow = p.get("ema_slow", 200)
        self.adx_min = p.get("adx_min", 20)
        self.vol_mult = p.get("volume_mult", 1.15)   # القيم النهائية = نتيجة Walk-forward
        self.atr_min_pct = p.get("atr_min_pct", 0.35)
        self.atr_max_pct = p.get("atr_max_pct", 6.0)
        self.sl_atr = p.get("sl_atr", 0.2)
        self.sl_max_atr = p.get("sl_max_atr", 2.5)
        self.rr = p.get("rr", [1.0, 2.0, 3.0])
        self.leverage = p.get("leverage", 5)
        self.use_breakout = p.get("use_breakout", True)
        self.rsi_long_lo = p.get("rsi_long_lo", 45)
        self.rsi_long_hi = p.get("rsi_long_hi", 72)
        self.rsi_short_lo = p.get("rsi_short_lo", 28)
        self.rsi_short_hi = p.get("rsi_short_hi", 55)
        self.er_min = p.get("er_min", 0.25)         # كفاءة الاتجاه (Kaufman) — 0 = معطّل
        self.er_len = p.get("er_len", 10)
        self.slope_len = p.get("slope_len", 10)

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        c = df["close"]
        df["ema_f"] = c.ewm(span=self.ema_fast, adjust=False).mean()
        df["ema_m"] = c.ewm(span=self.ema_mid, adjust=False).mean()
        df["ema_s"] = c.ewm(span=self.ema_slow, adjust=False).mean()
        slope_len = self.params.get("slope_len", 10)
        df["ema_s_slope"] = df["ema_s"].diff(slope_len) / df["ema_s"]
        df["rsi"] = rsi(c, 14)
        df["adx"] = adx(df, 14)
        df["vol_ma"] = df["volume"].rolling(20).mean()
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - c.shift()).abs(),
            (df["low"] - c.shift()).abs(),
        ], axis=1).max(axis=1)
        df["atr"] = _wilder(tr, 14)
        df["atr_pct"] = df["atr"] / c * 100
        # Kaufman Efficiency Ratio: 1 = اتجاه نظيف، 0 = ضوضاء
        change = (c - c.shift(self.er_len)).abs()
        path = c.diff().abs().rolling(self.er_len).sum()
        df["er"] = (change / path.replace(0, np.nan)).fillna(0)
        df["dc_hi"] = df["high"].rolling(20).max().shift(1)
        df["dc_lo"] = df["low"].rolling(20).min().shift(1)
        df["swing_lo"] = df["low"].rolling(5).min()
        df["swing_hi"] = df["high"].rolling(5).max()
        return df

    # ---- توليد الإشارات لكل الشموع دفعة واحدة (سريع جداً في الباكتست) ----
    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        c, o = df["close"], df["open"]
        rng = (df["high"] - df["low"]).replace(0, np.nan)
        body = c - o
        close_pos = (c - df["low"]) / rng
        vol_ok = df["volume"] > self.vol_mult * df["vol_ma"]
        atr_ok = df["atr_pct"].between(self.atr_min_pct, self.atr_max_pct)
        adx_ok = df["adx"] >= self.adx_min

        up_trend = (c > df["ema_s"]) & (df["ema_m"] > df["ema_s"]) & (df["ema_s_slope"] > 0)
        dn_trend = (c < df["ema_s"]) & (df["ema_m"] < df["ema_s"]) & (df["ema_s_slope"] < 0)

        low3 = df["low"].rolling(3).min()
        high3 = df["high"].rolling(3).max()
        pull_long = ((low3 <= df["ema_f"] * 1.006) & (c > df["ema_f"]) & (body > 0)
                     & (close_pos >= 0.55) & df["rsi"].between(self.rsi_long_lo, self.rsi_long_hi))
        pull_short = ((high3 >= df["ema_f"] * 0.994) & (c < df["ema_f"]) & (body < 0)
                      & (close_pos <= 0.45) & df["rsi"].between(self.rsi_short_lo, self.rsi_short_hi))
        brk_long = (c > df["dc_hi"]) & (c.shift(1) <= df["dc_hi"].shift(1)) & (df["rsi"] <= 78)
        brk_short = (c < df["dc_lo"]) & (c.shift(1) >= df["dc_lo"].shift(1)) & (df["rsi"] >= 22)

        base = atr_ok & adx_ok & vol_ok
        if self.er_min > 0:
            base &= df["er"] >= self.er_min
        side = pd.Series(0, index=df.index)
        setup = pd.Series("", index=df.index)
        m = base & up_trend & pull_long
        side[m], setup[m] = 1, "pullback"
        if self.use_breakout:
            m = base & up_trend & brk_long & (side == 0)
            side[m], setup[m] = 1, "breakout"
        m = base & dn_trend & pull_short
        side[m], setup[m] = -1, "pullback"
        if self.use_breakout:
            m = base & dn_trend & brk_short & (side == 0)
            side[m], setup[m] = -1, "breakout"

        entry = c
        atr = df["atr"]
        long_sl = np.maximum(df["swing_lo"] - self.sl_atr * atr, entry - self.sl_max_atr * atr)
        short_sl = np.minimum(df["swing_hi"] + self.sl_atr * atr, entry + self.sl_max_atr * atr)
        sl = np.where(side == 1, long_sl, np.where(side == -1, short_sl, np.nan))
        risk = np.abs(entry - sl)
        sl = np.where(side == 1, np.minimum(sl, entry - 1e-12), np.where(side == -1, np.maximum(sl, entry + 1e-12), sl))

        out = pd.DataFrame({"side": side, "setup": setup, "entry": entry, "sl": sl, "risk": risk,
                            "atr": atr, "adx": df["adx"],
                            "score": (df["adx"] / 25 + (df["volume"] / df["vol_ma"] - 1) * 0.5)})
        for k, r in enumerate(self.rr):
            out[f"tp{k + 1}"] = np.where(side == 1, entry + risk * r,
                                         np.where(side == -1, entry - risk * r, np.nan))
        # استبعاد الإشارات غير الصالحة (ستوب ضيق جداً / قيم ناقصة)
        bad = out[["entry", "sl", "tp1"]].isna().any(axis=1) | (out["risk"] / out["entry"] * 100 < 0.15)
        out.loc[bad, ["side"]] = 0
        return out

    def to_signal(self, symbol: str, row: pd.Series) -> Signal:
        side = "LONG" if row["side"] == 1 else "SHORT"
        tps = [row[f"tp{k + 1}"] for k in range(len(self.rr))]
        reason = (f"{'صعود' if side == 'LONG' else 'هبوط'} EMA{self.ema_slow} | "
                  f"{'تصحيح EMA' + str(self.ema_fast) if row['setup'] == 'pullback' else 'اختراق 20 شمعة'} | "
                  f"ADX {row['adx']:.0f}")
        return Signal(symbol=symbol, side=side, entry=float(row["entry"]),
                      stop_loss=float(row["sl"]), take_profits=[round(float(t), 8) for t in tps],
                      leverage=self.leverage, reason=reason,
                      score=float(row["score"]), setup=row["setup"],
                      atr=float(row["atr"]))

    def evaluate(self, symbol: str, df: pd.DataFrame) -> Optional[Signal]:
        sigs = self.signals(df)
        last = sigs.iloc[-1]
        if last["side"] == 0:
            return None
        return self.to_signal(symbol, last)


# ---------------------------------------------------------------------------
# MeanRevPulse — استراتيجية الارتداد من المتوسط (تكمّل TrendPulse)
# ---------------------------------------------------------------------------
class MeanRevPulse(StrategyBase):
    """
    فكرة مختلفة تماماً: بدل ملاحقة الاتجاه، نشتري التطرف في اتجاه عام صاعد
    ونبيع التطرف في اتجاه هابط (Connors RSI-2 المطوّر للكريبتو).

      1) الاتجاه العام : السعر فوق EMA200 (شراء) أو تحته (بيع)
      2) التطرف        : RSI(2) < 10 للشراء / > 90 للبيع
      3) فلتر خوف     : الشمعة ليست انهياراً حراً (ATR% < 8)
      4) الستوب        : 2×ATR
      5) الأهداف       : EMA10 ثم EMA20 ثم 2R (3R في الاتجاه القوي)
    """

    name = "mean_rev"

    def __init__(self, params=None):
        super().__init__(params)
        p = self.params
        self.rsi_len = p.get("rsi_len", 2)
        self.rsi_buy = p.get("rsi_buy", 10)
        self.rsi_sell = p.get("rsi_sell", 90)
        self.ema_slow = p.get("ema_slow", 200)
        self.sl_atr = p.get("sl_atr", 2.0)
        self.atr_max_pct = p.get("atr_max_pct", 8.0)
        self.leverage = p.get("leverage", 10)
        self.rr = p.get("rr", [1.0, 2.0, 3.0])

    def prepare(self, df):
        c = df["close"]
        df["ema_f"] = c.ewm(span=10, adjust=False).mean()
        df["ema_m"] = c.ewm(span=20, adjust=False).mean()
        df["ema_s"] = c.ewm(span=self.ema_slow, adjust=False).mean()
        df["rsi2"] = rsi(c, self.rsi_len)
        tr = pd.concat([df["high"] - df["low"],
                        (df["high"] - c.shift()).abs(),
                        (df["low"] - c.shift()).abs()], axis=1).max(axis=1)
        df["atr"] = _wilder(tr, 14)
        df["atr_pct"] = df["atr"] / c * 100
        df["vol_ma"] = df["volume"].rolling(20).mean()
        df["adx"] = adx(df, 14)
        return df

    def signals(self, df):
        c = df["close"]
        up = c > df["ema_s"]
        dn = c < df["ema_s"]
        atr_ok = df["atr_pct"].between(0.2, self.atr_max_pct)
        long_m = up & (df["rsi2"] < self.rsi_buy) & atr_ok
        short_m = dn & (df["rsi2"] > self.rsi_sell) & atr_ok

        side = pd.Series(0, index=df.index)
        side[long_m] = 1
        side[short_m & ~long_m] = -1
        setup = pd.Series("", index=df.index)
        setup[side != 0] = "meanrev"

        entry = c
        atr = df["atr"]
        sl = np.where(side == 1, entry - self.sl_atr * atr,
                      np.where(side == -1, entry + self.sl_atr * atr, np.nan))
        risk = np.abs(entry - sl)
        # الهدف الأول = EMA10 (عودة للمتوسط)، والبقية بمضاعفات R
        tp1 = np.where(side == 1, df["ema_f"], np.where(side == -1, df["ema_f"], np.nan))
        out = pd.DataFrame({"side": side, "setup": setup, "entry": entry, "sl": sl,
                            "risk": risk, "atr": atr, "adx": df["adx"],
                            "score": (100 - df["rsi2"]).abs() / 100})
        out["tp1"] = tp1
        out["tp2"] = np.where(side == 1, entry + risk * 2, np.where(side == -1, entry - risk * 2, np.nan))
        out["tp3"] = np.where(side == 1, entry + risk * 3, np.where(side == -1, entry - risk * 3, np.nan))
        # يجب أن يكون الهدف الأول في اتجاه الربح
        bad = out[["entry", "sl", "tp1", "tp2"]].isna().any(axis=1)
        bad |= np.where(side == 1, out["tp1"] <= entry, out["tp1"] >= entry)
        out.loc[bad, "side"] = 0
        return out

# ---------------------------------------------------------------------------
# مثال تعليمي بسيط (غير موصى به للتشغيل الحقيقي)
# ---------------------------------------------------------------------------
class EmaCrossExample(StrategyBase):
    name = "ema_cross_example"

    def __init__(self, params=None):
        super().__init__(params)
        p = self.params
        self.fast = p.get("ema_fast", 21)
        self.slow = p.get("ema_slow", 55)
        self.trend = p.get("ema_trend", 200)
        self.atr_len = p.get("atr_len", 14)
        self.vol_mult = p.get("volume_mult", 1.2)
        self.rr = p.get("rr", [1.0, 2.0, 3.0])
        self.leverage = p.get("leverage", 10)

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        c = df["close"]
        df["ema_fast"] = c.ewm(span=self.fast, adjust=False).mean()
        df["ema_slow"] = c.ewm(span=self.slow, adjust=False).mean()
        df["ema_trend"] = c.ewm(span=self.trend, adjust=False).mean()
        df["vol_ma"] = df["volume"].rolling(20).mean()
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - c.shift()).abs(),
            (df["low"] - c.shift()).abs(),
        ], axis=1).max(axis=1)
        df["atr"] = tr.rolling(self.atr_len).mean()
        return df

    def evaluate(self, symbol: str, df: pd.DataFrame) -> Optional[Signal]:
        if len(df) < max(self.trend, self.atr_len) + 5:
            return None
        a, b = df.iloc[-2], df.iloc[-3]
        if a[["ema_fast", "ema_slow", "ema_trend", "atr"]].isna().any():
            return None
        long_cross = b["ema_fast"] <= b["ema_slow"] and a["ema_fast"] > a["ema_slow"]
        short_cross = b["ema_fast"] >= b["ema_slow"] and a["ema_fast"] < a["ema_slow"]
        vol_ok = a["volume"] > self.vol_mult * a["vol_ma"]
        if long_cross and a["close"] > a["ema_trend"] and vol_ok:
            side = "LONG"
        elif short_cross and a["close"] < a["ema_trend"] and vol_ok:
            side = "SHORT"
        else:
            return None
        entry = float(a["close"])
        dist = 1.5 * float(a["atr"])
        sl = entry - dist if side == "LONG" else entry + dist
        tps = [entry + (dist * r if side == "LONG" else -dist * r) for r in self.rr]
        return Signal(symbol=symbol, side=side, entry=entry, stop_loss=sl,
                      take_profits=[round(t, 6) for t in tps], leverage=self.leverage,
                      reason="EMA cross + trend + volume",
                      score=float(abs(a["ema_fast"] - a["ema_slow"]) / a["atr"]) if a["atr"] else 0.0)


STRATEGIES = {
    "trend_pulse": TrendPulse,
    "mean_rev": MeanRevPulse,
    "ema_cross_example": EmaCrossExample,
}
DEFAULT_STRATEGY = "trend_pulse"
