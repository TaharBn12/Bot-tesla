"""
إدارة المخاطر وحجم الصفقة — القلب الحقيقي للتنفيذ الآلي.
لا يوجد أي استدعاء لشبكة هنا، لذلك يمكن اختبارها بدقة.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_DOWN, Decimal, ROUND_HALF_UP


class SizingError(Exception):
    pass


@dataclass
class Sizing:
    qty: float
    notional: float
    margin: float
    risk_amount: float
    risk_pct_actual: float
    leverage: int
    reason: str = ""


def size_position(equity: float, risk_pct: float, entry: float, sl: float, leverage: int,
                  *, max_margin_frac: float = 0.85, min_notional: float = 5.0,
                  step: float = 0.001, min_qty: float = 0.0, price_tick: float = 0.01) -> Sizing:
    """
    حجم الصفقة من المخاطرة (وليس من الرصيد):
        الكمية = (الرصيد × نسبة المخاطرة) ÷ (المسافة بين الدخول والستوب)

    ثم نضع سقفين:
      1) الهامش المستخدم ≤ الرصيد × max_margin_frac (لا نحرق الحساب بالرافعة)
      2) الكمية لا تقل عن الحد الأدنى للبورصة (وإلا تُرفض الصفقة)
    """
    if equity <= 0:
        raise SizingError("رصيد غير كافٍ")
    if not (0 < risk_pct <= 10):
        raise SizingError(f"نسبة مخاطرة غير منطقية: {risk_pct}")
    if entry <= 0 or sl <= 0:
        raise SizingError("سعر غير صالح")
    if leverage < 1:
        raise SizingError("رافعة غير صالحة")

    risk_per_unit = abs(entry - sl)
    if risk_per_unit <= 0:
        raise SizingError("الستوب يساوي الدخول — صفقة بلا مخاطرة معرّفة")
    if risk_per_unit / entry > 0.35:
        raise SizingError(f"الستوب بعيد جداً ({risk_per_unit / entry * 100:.1f}% من السعر)")

    risk_amount = equity * risk_pct / 100.0
    qty = risk_amount / risk_per_unit

    # سقف الهامش
    max_notional = equity * leverage * max_margin_frac
    if qty * entry > max_notional:
        qty = max_notional / entry
        reason = "حُدَّت الكمية بسقف الهامش"
    else:
        reason = ""

    # تقريب لأسفل حسب خطوة البورصة (وليس الأعلى — لا نتجاوز المخاطرة أبداً)
    qty = _round_down(qty, step)
    if qty < max(min_qty, 0):
        raise SizingError(f"الكمية {qty} أقل من الحد الأدنى {min_qty}")
    notional = qty * entry
    if notional < min_notional:
        raise SizingError(f"قيمة الصفقة {notional:.2f} أقل من الحد الأدنى {min_notional:.2f} USDT")

    margin = notional / leverage
    return Sizing(qty=qty, notional=round(notional, 2), margin=round(margin, 2),
                  risk_amount=round(risk_amount, 2),
                  risk_pct_actual=round(qty * risk_per_unit / equity * 100, 3),
                  leverage=leverage, reason=reason)


def _round_down(value: float, step: float) -> float:
    if step <= 0:
        return value
    d = Decimal(str(value))
    s = Decimal(str(step))
    return float((d / s).to_integral_value(rounding=ROUND_DOWN) * s)


def round_price(price: float, tick: float, mode: str = "nearest") -> float:
    if tick <= 0:
        return price
    d, t = Decimal(str(price)), Decimal(str(tick))
    if mode == "down":
        return float((d / t).to_integral_value(rounding=ROUND_DOWN) * t)
    if mode == "up":
        return float((d / t).to_integral_value(rounding="ROUND_UP") * t)
    return float((d / t).to_integral_value(rounding=ROUND_HALF_UP) * t)


class KillSwitch:
    """
    مفتاح إيقاف تلقائي:
      • خسارة يومية تتجاوز الحد → توقف حتى اليوم التالي
      • عدد صفقات اليوم يتجاوز الحد → توقف
      • خسارة متتالية → توقف (حماية نفسية ورياضية)
    """

    def __init__(self, max_daily_loss_pct: float = 3.0, max_trades_per_day: int = 10,
                 max_consecutive_losses: int = 5):
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_trades_per_day = max_trades_per_day
        self.max_consecutive_losses = max_consecutive_losses
        self.day = date.today()
        self.day_pnl = 0.0
        self.trades_today = 0
        self.consecutive_losses = 0
        self.halted_reason = ""

    def _roll_day(self, today: date | None = None):
        today = today or date.today()
        if today != self.day:
            self.day, self.day_pnl, self.trades_today = today, 0.0, 0
            if self.halted_reason.startswith("خسارة يومية") or self.halted_reason.startswith("عدد الصفقات"):
                self.halted_reason = ""

    def can_trade(self, equity: float) -> tuple[bool, str]:
        self._roll_day()
        if self.halted_reason:
            return False, self.halted_reason
        if equity > 0 and self.day_pnl < -abs(equity * self.max_daily_loss_pct / 100):
            self.halted_reason = f"خسارة يومية {self.day_pnl:.2f} تجاوزت {self.max_daily_loss_pct}% — توقف لليوم"
            return False, self.halted_reason
        if self.trades_today >= self.max_trades_per_day:
            self.halted_reason = f"عدد الصفقات اليوم {self.trades_today} بلغ الحد"
            return False, self.halted_reason
        if self.consecutive_losses >= self.max_consecutive_losses:
            self.halted_reason = f"{self.consecutive_losses} خسائر متتالية — توقف"
            return False, self.halted_reason
        return True, ""

    def register_trade(self, pnl: float, when: date | None = None):
        """when = تاريخ السوق (مهم في إعادة التشغيل التاريخي، وإلا حُسبت سنة كيوم واحد)."""
        self._roll_day(when)
        self.trades_today += 1
        self.day_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
        elif pnl > 0:
            self.consecutive_losses = 0

    def status(self) -> dict:
        self._roll_day()
        return {"اليوم": str(self.day), "ربح/خسارة اليوم": round(self.day_pnl, 2),
                "صفقات اليوم": self.trades_today, "خسائر متتالية": self.consecutive_losses,
                "موقوف": self.halted_reason or "لا"}
