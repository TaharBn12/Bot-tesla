"""
محرّك «الفرص الممكنة» — يعطي خطة صفقة كاملة لكل زوج قريب من شروط الدخول،
حتى لو لم تكتمل الإشارة الرسمية بعد.

الفرق عن الإشارة الرسمية:
  • إشارة رسمية (A)  : كل الشروط تحققت على شمعة مغلقة → يُرسلها البوت تلقائياً.
  • فرصة (B / C)     : الاتجاه والبنية جاهزون لكن شرط ناقص (سيولة، كفاءة، تقلب)
                       → تُعرض هنا مع خطة دخول وزون سعري وتقييم صريح للجودة.

كل فرصة تعطي: منطقة دخول، ستوب، 3 أهداف، نسبة الخطر، حجم الصفقة، سبب، وما ينقص.
"""
from dataclasses import dataclass, field

import pandas as pd

GRADES = {
    "A": ("🅰️ إشارة مكتملة", "كل الشروط تحققت — أعلى جودة"),
    "B": ("🅱️ قوية جداً", "اتجاه واضح، ينقص شرط واحد فقط"),
    "C": ("🆑 ممكنة", "البنية جيدة لكن ينقص أكثر من شرط — راقبها"),
}


@dataclass
class Opportunity:
    symbol: str
    side: str
    grade: str
    entry: float
    entry_zone: tuple
    stop: float
    targets: list
    risk_pct: float
    qty: float
    notional: float
    leverage: int
    readiness: float
    missing: list
    reason: str
    rr: float = 0.0
    atr: float = 0.0
    price: float = 0.0
    extra: dict = field(default_factory=dict)


def build(symbol: str, df: pd.DataFrame, strat, balance: float = 1000.0,
          risk_pct: float = 1.0, leverage: int = 5, min_readiness: float = 66.0) -> Opportunity | None:
    """يبني خطة صفقة من بيانات زوج واحد، أو None إن لم تكن هناك فرصة."""
    d = strat.prepare(df.copy())
    r = strat.readiness(d)
    if r.get("bias") not in ("LONG", "SHORT"):
        return None
    if r.get("readiness", 0) < min_readiness:
        return None

    last = d.iloc[-1]
    side = r["bias"]
    price = float(last["close"])
    atr = float(last["atr"])
    if atr <= 0:
        return None
    ema_f = float(last["ema_f"])
    sl_atr = getattr(strat, "sl_atr", 0.2)
    sl_max = getattr(strat, "sl_max_atr", 2.5)
    min_stop_atr = 1.5          # ⭐ أرضية الستوب: لا يقل عن 1.5×ATR (يمنع ستوبات وهمية ضيقة)

    # ---- 1) الدخول الفوري = سعر الإغلاق الحالي ----
    entry = price

    # ---- 2) أفضل منطقة دخول (أمر Limit عند التصحيح إلى EMA) ----
    if side == "LONG":
        lo, hi = ema_f - 0.20 * atr, ema_f + 0.35 * atr
        hi = min(hi, price)                      # لا نشتري فوق السوق
        lo = min(lo, hi - 0.05 * atr)
    else:
        lo, hi = ema_f - 0.35 * atr, ema_f + 0.20 * atr
        lo = max(lo, price)                      # لا نبيع تحت السوق
        hi = max(hi, lo + 0.05 * atr)
    zone = (round(min(lo, hi), 8), round(max(lo, hi), 8))

    # ---- 3) الستوب: قاع/قمة 5 شموع مع أرضية وسقف ATR ----
    if side == "LONG":
        swing = float(d["low"].iloc[-6:-1].min()) - sl_atr * atr
        stop = min(swing, entry - min_stop_atr * atr)      # لا أقرب من 1.5×ATR
        stop = max(stop, entry - sl_max * atr)             # ولا أبعد من 2.5×ATR
    else:
        swing = float(d["high"].iloc[-6:-1].max()) + sl_atr * atr
        stop = max(swing, entry + min_stop_atr * atr)
        stop = min(stop, entry + sl_max * atr)

    risk = abs(entry - stop)
    if risk <= 0 or risk / entry > 0.35:
        return None
    targets = [round(entry + risk * k, 8) if side == "LONG" else round(entry - risk * k, 8)
               for k in (1.0, 2.0, 3.0)]

    # ---- 4) حجم الصفقة من المخاطرة (1% افتراضياً) ----
    risk_amount = balance * risk_pct / 100
    qty = risk_amount / risk
    notional = qty * entry
    max_notional = balance * leverage * 0.85
    if notional > max_notional:
        qty = max_notional / entry
        notional = qty * entry
    qty = round(qty, 6)

    pct = r["readiness"]
    missing = r.get("missing", [])
    grade = "A" if pct >= 100 else ("B" if pct >= 85 or len(missing) <= 1 else "C")

    reasons = [f"{'صعود' if side == 'LONG' else 'هبوط'} EMA{strat.ema_slow}",
               f"ADX {r['adx']}", f"ER {r['er']}"]
    if last["volume"] > 0 and last["vol_ma"]:
        reasons.append(f"حجم {last['volume'] / last['vol_ma']:.1f}x")
    reasons.append(f"RSI {r['rsi']}")

    return Opportunity(
        symbol=symbol, side=side, grade=grade, entry=entry, entry_zone=zone, stop=round(stop, 8),
        targets=targets, risk_pct=round(risk / entry * 100, 2), qty=qty,
        notional=round(notional, 2), leverage=leverage, readiness=pct, missing=missing,
        reason=" | ".join(reasons), rr=3.0, atr=atr, price=price,
        extra={"adx": r["adx"], "er": r["er"], "rsi": r["rsi"], "atr_pct": r["atr_pct"]},
    )


def market_mood(opps: list[Opportunity]) -> dict:
    longs = sum(1 for o in opps if o.side == "LONG")
    shorts = sum(1 for o in opps if o.side == "SHORT")
    mood = "صاعد" if longs > shorts else ("هابط" if shorts > longs else "متوازن")
    return {"LONG": longs, "SHORT": shorts, "mood": mood,
            "grade_a": sum(1 for o in opps if o.grade == "A"),
            "grade_b": sum(1 for o in opps if o.grade == "B"),
            "grade_c": sum(1 for o in opps if o.grade == "C")}
