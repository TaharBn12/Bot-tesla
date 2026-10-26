"""
اختبارات التنفيذ الآلي — تعمل بلا إنترنت وبلا مفاتيح (بورصة وهمية).
    python src/tests_executor.py
"""
import sys
from dataclasses import dataclass, field

from risk import KillSwitch, SizingError, round_price, size_position
from executor import BinanceExecutor, Position, TrailingManager

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


# ---------------------------------------------------------------------------
# بورصة وهمية تسجّل كل أمر
# ---------------------------------------------------------------------------
class FakeExchange:
    def __init__(self, fail_stop=False, fail_entry=False):
        self.orders = []
        self.cancelled = []
        self.fail_stop = fail_stop
        self.fail_entry = fail_entry
        self.price = 100.0
        self.balance = 1000.0

    def set_sandbox_mode(self, v): self.sandbox = v

    def load_markets(self): return {}

    def market(self, symbol):
        return {"symbol": symbol, "precision": {"price": 0.01, "amount": 0.001},
                "limits": {"amount": {"min": 0.001}, "cost": {"min": 5.0}}}

    def set_leverage(self, lev, symbol): self.lev = (lev, symbol)

    def set_margin_mode(self, mode, symbol): self.margin = (mode, symbol)

    def fetch_balance(self): return {"USDT": {"total": self.balance, "free": self.balance}}

    def fetch_positions(self):
        return [{"symbol": p, "contracts": 1} for p in getattr(self, "_open", [])]

    def fetch_ticker(self, symbol): return {"last": self.price}

    def create_order(self, symbol, type_, side, amount, params=None, **kw):
        params = params or {}
        if type_ == "STOP_MARKET" and self.fail_stop:
            raise RuntimeError("stop rejected")
        if type_ == "market" and self.fail_entry:
            raise RuntimeError("entry rejected")
        oid = f"o{len(self.orders) + 1}"
        self.orders.append({"id": oid, "symbol": symbol, "type": type_, "side": side,
                            "amount": amount, "params": params})
        if type_ == "market" and not params.get("reduceOnly"):
            self._open = [symbol]
        return {"id": oid, "symbol": symbol, "price": self.price, "average": self.price}

    def fetch_order(self, oid, symbol): return {"id": oid, "average": self.price}

    def cancel_order(self, oid, symbol): self.cancelled.append(oid)

    # Binance تخزّن ستوب closePosition في مساحة Algo Orders
    def fapiPrivateDeleteAlgoOrder(self, params):
        self.algo_cancelled = getattr(self, "algo_cancelled", [])
        self.algo_cancelled.append(params.get("algoId"))
        return {"algoId": params.get("algoId"), "code": "200"}


@dataclass
class Sig:
    symbol: str = "BTC/USDT:USDT"
    side: str = "LONG"
    entry: float = 100.0
    stop_loss: float = 95.0
    take_profits: list = field(default_factory=lambda: [105, 110, 115])
    leverage: int = 5
    reason: str = "test"
    atr: float = 2.0
    score: float = 1.0
    setup: str = "breakout"


@dataclass
class Sett:
    risk_per_trade_pct: float = 1.0
    leverage: int = 5
    max_open_positions: int = 3
    timeframe: str = "4h"
    dry_run: bool = False
    trail_atr: float = 3.5
    api_key: str = ""
    api_secret: str = ""


def test_sizing():
    print("\n[1] حساب حجم الصفقة")
    s = size_position(1000, 1.0, 100.0, 95.0, 5)
    check("مخاطرة 1% من 1000 = 10 USDT", abs(s.risk_amount - 10) < 1e-9, s.risk_amount)
    check("الكمية = 10/5 = 2", abs(s.qty - 2) < 1e-9, s.qty)
    check("قيمة الصفقة = 200", abs(s.notional - 200) < 0.01, s.notional)
    check("الهامش = 40 (رافعة 5)", abs(s.margin - 40) < 0.01, s.margin)

    s2 = size_position(1000, 1.0, 100.0, 99.9, 20)
    check("ستوب ضيق + رافعة عالية → الكمية محدودة بالهامش", s2.qty * 100 <= 1000 * 20 * 0.85 + 1e-6,
          s2.notional)
    check("المخاطرة الفعلية لا تتجاوز المطلوبة", s2.risk_pct_actual <= 1.0, s2.risk_pct_actual)

    for bad, why in [((0, 1, 100, 95, 5), "رصيد صفر"),
                     ((1000, 1, 100, 100, 5), "ستوب = دخول"),
                     ((1000, 1, 100, 40, 5), "ستوب بعيد 60%"),
                     ((1000, 20, 100, 95, 5), "مخاطرة 20%")]:
        try:
            size_position(*bad)
            check(f"رفض {why}", False)
        except SizingError:
            check(f"رفض {why}", True)

    r = size_position(1000, 1.0, 100.0, 70.0, 5, min_notional=5)
    check("ستوب 30% يُقبل بكيمة صغيرة", r.qty > 0, round(r.qty, 3))
    try:
        size_position(1000, 1.0, 100.0, 40.0, 5)
        check("رفض ستوب 60% (غير منطقي)", False)
    except SizingError:
        check("رفض ستوب 60% (غير منطقي)", True)


def test_rounding():
    print("\n[2] تقريب الأسعار")
    check("تقريب لأسفل 100.017 → 100.01", round_price(100.017, 0.01, "down") == 100.01)
    check("تقريب لأعلى 100.011 → 100.02", round_price(100.011, 0.01, "up") == 100.02)
    check("تقريب لأقرب 100.016 → 100.02", round_price(100.016, 0.01) == 100.02)


def test_open_flow():
    print("\n[3] دورة فتح صفقة (بورصة وهمية)")
    ex = FakeExchange()
    exf = BinanceExecutor(Sett(), exchange=ex)
    pos = exf.open(Sig())
    check("تم فتح الصفقة", pos is not None)
    check("أمر السوق أُرسل بالجهة الصحيحة", ex.orders[0]["type"] == "market" and ex.orders[0]["side"] == "buy")
    check("الرافعة ضُبطت = 5", getattr(ex, "lev", None) == (5, "BTC/USDT:USDT"))
    check("الهامش معزول", getattr(ex, "margin", None) == ("isolated", "BTC/USDT:USDT"))
    stop = ex.orders[1]
    check("أمر ستوب أُرسل بعد الدخول", stop["type"] == "STOP_MARKET" and stop["side"] == "sell")
    check("الستوب closePosition=True", stop["params"].get("closePosition") is True)
    # Binance يرفض إرسال closePosition و reduceOnly معاً (خطأ -1106)
    check("الستوب closePosition=True", stop["params"].get("closePosition") is True)
    check("الستوب بلا reduceOnly (قاعدة Binance -1106)",
          stop["params"].get("reduceOnly") is not True)
    check("الستوب بسعر صالح > 0", float(stop["params"].get("stopPrice") or 0) > 0)
    check("سعر الستوب مُقرَّب صحيحاً", stop["params"].get("stopPrice") == 95.0, stop["params"])


def test_stop_failure_closes():
    print("\n[4] فشل الستوب → إغلاق فوري (لا مركز عارٍ)")
    ex = FakeExchange(fail_stop=True)
    exf = BinanceExecutor(Sett(), exchange=ex)
    pos = exf.open(Sig())
    check("الصفقة لم تُعتمد", pos is None)
    closes = [o for o in ex.orders if o["type"] == "market" and o["side"] == "sell"
              and o["params"].get("reduceOnly")]
    check("أُرسل أمر إغلاق طارئ", len(closes) == 1, [o["side"] for o in ex.orders])


def test_entry_failure():
    print("\n[5] فشل أمر الدخول → لا ستوب ولا صفقة")
    ex = FakeExchange(fail_entry=True)
    exf = BinanceExecutor(Sett(), exchange=ex)
    check("لا صفقة", exf.open(Sig()) is None)
    check("لا أوامر إضافية", len(ex.orders) == 0, ex.orders)


def test_trailing():
    print("\n[6] الستوب المتتبّع")
    ex = FakeExchange()
    exf = BinanceExecutor(Sett(), exchange=ex)
    pos = exf.open(Sig())
    old_sl, old_id = pos.sl, pos.stop_order_id

    p = Position("BTC/USDT:USDT", "LONG", 2, 100, 95, 5, 10, "2026-01-01T00:00:00+00:00", peak=100)
    new = exf.proposed_stop(p, high=110, low=104, atr=2.0)
    check("ستوب جديد = 110 − 3.5×2 = 103", new == 103.0, new)
    p.sl = 103.0            # بعد تطبيق الستوب الجديد
    check("الستوب لا يتراجع للخلف", exf.proposed_stop(p, high=104, low=100, atr=2.0) is None)
    check("القمة محفوظة بعد هبوط السعر", p.peak == 110.0, p.peak)
    check("صعود جديد يرفع الستوب", exf.proposed_stop(p, high=120, low=110, atr=2.0) == 113.0)

    check("تحديث الستوب نُفّذ", exf.trail("BTC/USDT:USDT", 103.0) is True)
    # الستوب يُلغى الآن عبر Algo Orders لا cancel_order العادي
    check("الستوب القديم أُلغي (Algo)",
          old_id in getattr(ex, "algo_cancelled", []) or old_id in ex.cancelled,
          getattr(ex, "algo_cancelled", None))
    stop2 = ex.orders[-1]
    check("ستوب جديد بسعر 103", stop2["params"].get("stopPrice") == 103.0, stop2["params"])
    check("الستوب في الذاكرة تحدّث", exf.positions()[0].sl == 103.0)

    p2 = Position("ETH/USDT:USDT", "SHORT", 1, 100, 105, 5, 10, "2026-01-01T00:00:00+00:00", peak=100)
    check("SHORT: ستوب = أدنى قاع + 3.5×ATR", exf.proposed_stop(p2, high=96, low=90, atr=2.0) == 97.0)


def test_kill_switch():
    print("\n[7] مفتاح الإيقاف")
    k = KillSwitch(max_daily_loss_pct=3.0, max_trades_per_day=3, max_consecutive_losses=2)
    ok, _ = k.can_trade(1000)
    check("البداية: التداول مسموح", ok)
    k.register_trade(-15)   # 1.5%
    ok, _ = k.can_trade(1000)
    check("خسارة 1.5%: لا يزال مسموحاً", ok)
    k.register_trade(-20)   # المجموع 3.5% > 3%
    ok, why = k.can_trade(1000)
    check("خسارة يومية 3.5% → توقف", not ok, why)

    k2 = KillSwitch(max_consecutive_losses=2)
    k2.register_trade(-5); k2.register_trade(-5)
    ok, why = k2.can_trade(1000)
    check("خسارتان متتاليتان → توقف", not ok, why)

    k3 = KillSwitch(max_consecutive_losses=3)
    k3.register_trade(-5); k3.register_trade(20); k3.register_trade(-5)
    ok, _ = k3.can_trade(1000)
    check("ربح في المنتصف يُصفّر العدّاد", ok)


def test_dry_run_and_safety():
    print("\n[8] وضع المحاكاة والحدود الآمنة")
    ex = FakeExchange()
    exf = BinanceExecutor(Sett(dry_run=True), exchange=ex)
    check("dry-run لا يرسل أوامر", exf.open(Sig()) is None and len(ex.orders) == 0)

    ex2 = FakeExchange()
    ex2.balance = 1_000_000
    exf2 = BinanceExecutor(Sett(risk_per_trade_pct=1.0, leverage=20), exchange=ex2)
    r = exf2.open(Sig())     # رصيد مليون: الكمية ستتجاوز الحد الآمن
    check("رفض صفقة تتجاوز الحد الآمن", r is None, r)


def test_paper_lifecycle():
    print("\n[9] دورة حياة كاملة (ورقي)")
    import pandas as pd
    from executor import PaperExecutor

    class Feed:
        def __init__(self):
            self.rows = []

    pe = PaperExecutor(Sett(), Feed(), balance=1000)
    pe.STATE.unlink(missing_ok=True)
    pe = PaperExecutor(Sett(), Feed(), balance=1000)
    pos = pe.open(Sig())
    check("فتح صفقة ورقية", pos is not None)
    check("الرصيد لم يتغيّر عند الفتح", pe.equity() == 1000)
    check("تحديث الستوب لأعلى", pe.trail("BTC/USDT:USDT", 98.0) is True)
    check("رفض تراجع الستوب", pe.trail("BTC/USDT:USDT", 96.0) is False)
    pe.close("BTC/USDT:USDT", "هدف", price=110.0)
    check("الرصيد ارتفع بعد الربح", pe.equity() > 1000, round(pe.equity(), 2))
    check("لا صفقات مفتوحة بعد الإغلاق", pe.positions() == [])




def test_algo_stop_cancel():
    print("\n[10] إلغاء ستوب Algo (Binance -2011)")
    ex = FakeExchange()
    exf = BinanceExecutor(Sett(), exchange=ex)
    pos = exf.open(Sig())
    check("فُتحت صفقة", pos is not None)
    check("حُفظ معرّف الستوب", bool(pos.stop_order_id), pos.stop_order_id)
    ok = exf._cancel_stop(pos)
    check("الإلغاء عبر Algo نجح", ok is True)
    check("استُخدم algoId لا orderId",
          getattr(ex, "algo_cancelled", []) == [pos.stop_order_id],
          getattr(ex, "algo_cancelled", None))
    check("لم يُستدعَ cancel_order العادي", pos.stop_order_id not in ex.cancelled)

    # إن فشل الإلغاء أيضاً: لا يرمي استثناء
    class BadAlgo(FakeExchange):
        def fapiPrivateDeleteAlgoOrder(self, params):
            raise Exception("-2011 Unknown order sent.")
    ex2 = BadAlgo()
    exf2 = BinanceExecutor(Sett(), exchange=ex2)
    p2 = exf2.open(Sig())
    check("فشل الإلغاء لا يرمي استثناء", exf2._cancel_stop(p2) in (True, False))


if __name__ == "__main__":
    print("=" * 60)
    print("اختبارات التنفيذ الآلي (بلا إنترنت / بلا مفاتيح)")
    print("=" * 60)
    test_sizing()
    test_rounding()
    test_open_flow()
    test_stop_failure_closes()
    test_entry_failure()
    test_trailing()
    test_kill_switch()
    test_dry_run_and_safety()
    test_paper_lifecycle()
    test_algo_stop_cancel()
    print("\n" + "=" * 60)
    print(f"النتيجة: {PASS} نجح | {FAIL} فشل")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
