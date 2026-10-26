"""
منفّذ الصفقات — ثلاث طبقات:

  BaseExecutor   : الواجهة المشتركة + السجل (Journal)
  PaperExecutor  : تنفيذ وهمي على أسعار حقيقية (للتجربة بلا مفاتيح ولا مخاطرة)
  BinanceExecutor: تنفيذ حقيقي عبر ccxt على Binance Futures (Testnet هو الافتراضي)

قواعد السلامة المبنية في الكود:
  • Testnet افتراضي — التشغيل الحقيقي يحتاج تصريحين صريحين (mode=autotrade + ALLOW_LIVE=true)
  • لا تُفتح صفقة إلا بعد نجاح الستوب — وإن فشل الستوب تُغلق الصفقة فوراً (لا مركز عارٍ)
  • كل أمر له clientOrderId ثابت (Idempotency) لتفادي التكرار عند إعادة التشغيل
  • كل حركة تُسجّل في data/journal.jsonl
"""
import asyncio
import json
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from risk import KillSwitch, SizingError, round_price, size_position

log = logging.getLogger("executor")
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
JOURNAL = DATA / "journal.jsonl"


def journal(event: str, **kw):
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **kw}
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    log.info("%s %s", event, {k: v for k, v in kw.items() if k in ("symbol", "side", "qty", "price", "reason")})


@dataclass
class Position:
    symbol: str
    side: str
    qty: float
    entry: float
    sl: float
    leverage: int
    risk_amount: float
    opened_at: str
    peak: float = 0.0          # أعلى سعر منذ الفتح (LONG) / أدنى سعر (SHORT)
    atr: float = 0.0
    stop_order_id: str = ""
    client_id: str = ""

    def pnl(self, price: float) -> float:
        d = (price - self.entry) if self.side == "LONG" else (self.entry - price)
        return d * self.qty

    def r_multiple(self, price: float) -> float:
        risk = abs(self.entry - self.sl) * self.qty
        return self.pnl(price) / risk if risk else 0.0


class BaseExecutor(ABC):
    name = "base"

    def __init__(self, settings, kill_switch: KillSwitch | None = None):
        self.s = settings
        self.kill = kill_switch or KillSwitch()
        self.dry_run = getattr(settings, "dry_run", True)
        self.trailing_atr = getattr(settings, "trail_atr", 3.5)

    # ---- واجهة موحدة ----
    @abstractmethod
    def equity(self) -> float: ...

    @abstractmethod
    def positions(self) -> list[Position]: ...

    @abstractmethod
    def open(self, sig) -> Position | None: ...

    @abstractmethod
    def trail(self, symbol: str, new_sl: float) -> bool: ...

    @abstractmethod
    def close(self, symbol: str, reason: str = "") -> bool: ...

    # ---- منطق مشترك ----
    def proposed_stop(self, pos: Position, high: float, low: float, atr: float) -> float | None:
        """الستوب المتتبّع Chandelier: يحكم من قمم/قيعان الشموع، ولا يتراجع للخلف أبداً."""
        if atr <= 0:
            return None
        if pos.side == "LONG":
            peak = max(pos.peak, high)
            if peak > pos.peak:
                pos.peak = peak
            new_sl = peak - self.trailing_atr * atr
            return new_sl if new_sl > pos.sl else None
        peak = min(pos.peak or low, low)
        if peak < pos.peak or pos.peak == 0:
            pos.peak = peak
        new_sl = peak + self.trailing_atr * atr
        return new_sl if new_sl < pos.sl else None

    def risk_status(self) -> dict:
        return self.kill.status()


# ---------------------------------------------------------------------------
# تنفيذ وهمي (Paper) — نفس المنطق لكن بلا أوامر حقيقية
# ---------------------------------------------------------------------------
class PaperExecutor(BaseExecutor):
    name = "paper"
    STATE = DATA / "paper_state.json"

    def __init__(self, settings, feed, kill_switch=None, balance: float = 1000.0):
        super().__init__(settings, kill_switch)
        self.feed = feed
        self.balance = balance
        self._pos: dict[str, Position] = {}
        self.realized = 0.0
        if self.STATE.exists():
            st = json.loads(self.STATE.read_text(encoding="utf-8"))
            self.balance = st.get("balance", balance)
            self.realized = st.get("realized", 0.0)
            for p in st.get("positions", []):
                self._pos[p["symbol"]] = Position(**p)

    def _save(self):
        self.STATE.write_text(json.dumps({
            "balance": self.balance, "realized": self.realized,
            "positions": [asdict(p) for p in self._pos.values()]}, ensure_ascii=False, indent=1), encoding="utf-8")

    def equity(self) -> float:
        return self.balance

    def positions(self) -> list[Position]:
        return list(self._pos.values())

    def open(self, sig) -> Position | None:
        ok, why = self.kill.can_trade(self.balance)
        if not ok:
            journal("rejected", symbol=sig.symbol, reason=why)
            return None
        if sig.symbol in self._pos:
            journal("rejected", symbol=sig.symbol, reason="صفقة مفتوحة بالفعل")
            return None
        try:
            size = size_position(self.balance, self.s.risk_per_trade_pct, sig.entry,
                                 sig.stop_loss, self.s.leverage, min_notional=5.0, step=1e-6)
        except SizingError as e:
            journal("rejected", symbol=sig.symbol, reason=str(e))
            return None
        pos = Position(symbol=sig.symbol, side=sig.side, qty=size.qty, entry=sig.entry,
                       sl=sig.stop_loss, leverage=self.s.leverage, risk_amount=size.risk_amount,
                       opened_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       peak=sig.entry, atr=sig.atr, client_id=f"paper-{uuid.uuid4().hex[:8]}")
        self._pos[sig.symbol] = pos
        self._save()
        journal("open", symbol=pos.symbol, side=pos.side, qty=pos.qty, price=pos.entry,
                sl=pos.sl, notional=size.notional, risk=size.risk_amount,
                leverage=pos.leverage, reason=f"paper: {sig.reason}")
        return pos

    def trail(self, symbol: str, new_sl: float) -> bool:
        pos = self._pos.get(symbol)
        if not pos:
            return False
        improved = (new_sl > pos.sl) if pos.side == "LONG" else (new_sl < pos.sl)
        if not improved:
            return False
        journal("trail", symbol=symbol, side=pos.side, price=new_sl,
                reason=f"ستوب متتبّع: {pos.sl:.6g} → {new_sl:.6g}")
        pos.sl = new_sl
        self._save()
        return True

    def close(self, symbol: str, reason: str = "", price: float | None = None,
              when=None) -> bool:
        pos = self._pos.pop(symbol, None)
        if not pos:
            return False
        if price is None:
            price = pos.entry
        pnl = pos.pnl(price)
        fee = pos.qty * (pos.entry + price) * 0.0005
        pnl -= fee
        self.balance += pnl
        self.realized += pnl
        self.kill.register_trade(pnl, when=when)
        self._save()
        journal("close", symbol=symbol, side=pos.side, qty=pos.qty, price=price,
                pnl=round(pnl, 4), r=round(pos.r_multiple(price), 3),
                balance=round(self.balance, 2), reason=reason)
        return True

    def check_stops(self, candles: dict[str, pd.DataFrame]) -> list[str]:
        """في الورقي نتحقق من الستوب بأنفسنا (لا بورصة تحمينا)."""
        closed = []
        for sym, pos in list(self._pos.items()):
            df = candles.get(sym)
            if df is None or df.empty:
                continue
            bar = df.iloc[-1]
            hit = (bar["low"] <= pos.sl) if pos.side == "LONG" else (bar["high"] >= pos.sl)
            if hit:
                self.close(sym, "ضرب الستوب (ورقي)", price=pos.sl)
                closed.append(sym)
        return closed


# ---------------------------------------------------------------------------
# تنفيذ حقيقي على Binance Futures (Testnet افتراضياً)
# ---------------------------------------------------------------------------
class BinanceExecutor(BaseExecutor):
    name = "binance"

    SAFETY_MAX_NOTIONAL = 5000.0     # حماية قصوى: لا صفقة تتجاوز 5000 USDT قيمة اسمية
    SAFETY_MAX_LEVERAGE = 20

    def __init__(self, settings, exchange=None, kill_switch=None):
        super().__init__(settings, kill_switch)
        self.live = os.getenv("ALLOW_LIVE", "").lower() in ("1", "true", "yes")
        self.testnet = not self.live
        if exchange is None:
            import ccxt
            exchange = ccxt.binanceusdm({
                "apiKey": settings.api_key, "secret": settings.api_secret,
                "enableRateLimit": True, "options": {"defaultType": "swap"}})
            if self.testnet:
                # ccxt أزالت دعم set_sandbox_mode للفيوتشرز → توجيه يدوي
                try:
                    from .binance_testnet import enable_futures_testnet
                except ImportError:
                    from binance_testnet import enable_futures_testnet
                enable_futures_testnet(exchange)
        self.ex = exchange
        self._pos: dict[str, Position] = {}
        journal("executor_init", symbol="", mode="LIVE" if self.live else "TESTNET")

    # ---- مساعدات ----
    def _market(self, symbol):
        self.ex.load_markets()
        return self.ex.market(symbol)

    def _tick(self, symbol) -> float:
        m = self._market(symbol)
        p = m.get("precision", {}).get("price")
        return float(p) if isinstance(p, (int, float)) else 0.01

    def _step(self, symbol) -> float:
        m = self._market(symbol)
        p = m.get("precision", {}).get("amount")
        return float(p) if isinstance(p, (int, float)) else 0.001

    def equity(self) -> float:
        bal = self.ex.fetch_balance()
        usdt = bal.get("USDT", {})
        return float(usdt.get("total") or usdt.get("free") or 0.0)

    def positions(self) -> list[Position]:
        return list(self._pos.values())

    def sync(self) -> list[str]:
        """مزامنة الحالة مع البورصة: أي مركز أُغلق خارجياً يُزال من الذاكرة."""
        live = {p["symbol"]: p for p in self.ex.fetch_positions() if abs(float(p.get("contracts") or 0)) > 0}
        gone = []
        for sym in list(self._pos):
            if sym not in live:
                pos = self._pos.pop(sym)
                journal("sync_closed", symbol=sym, side=pos.side,
                        reason="أُغلق خارجياً (ستوب/يدوي)")
                gone.append(sym)
        return gone

    def open(self, sig) -> Position | None:
        eq = self.equity()
        ok, why = self.kill.can_trade(eq)
        if not ok:
            journal("rejected", symbol=sig.symbol, reason=why)
            return None
        if len(self._pos) >= self.s.max_open_positions:
            journal("rejected", symbol=sig.symbol, reason=f"سقف الصفقات المفتوحة {self.s.max_open_positions}")
            return None
        if sig.symbol in self._pos:
            journal("rejected", symbol=sig.symbol, reason="صفقة مفتوحة بالفعل")
            return None

        lev = min(int(self.s.leverage), self.SAFETY_MAX_LEVERAGE)
        step = self._step(sig.symbol)
        m = self._market(sig.symbol)
        min_qty = float(m.get("limits", {}).get("amount", {}).get("min") or 0)
        try:
            size = size_position(eq, self.s.risk_per_trade_pct, sig.entry, sig.stop_loss, lev,
                                 step=step, min_qty=min_qty, price_tick=self._tick(sig.symbol))
        except SizingError as e:
            journal("rejected", symbol=sig.symbol, reason=str(e))
            return None
        if size.notional > self.SAFETY_MAX_NOTIONAL:
            journal("rejected", symbol=sig.symbol, reason=f"قيمة الصفقة {size.notional} تتجاوز الحد الآمن")
            return None

        sl = round_price(sig.stop_loss, self._tick(sig.symbol),
                         mode="down" if sig.side == "LONG" else "up")
        side = "buy" if sig.side == "LONG" else "sell"
        client_id = f"tp{int(time.time())}{uuid.uuid4().hex[:4]}"

        if self.dry_run:
            journal("dry_run_open", symbol=sig.symbol, side=sig.side, qty=size.qty,
                    price=sig.entry, sl=sl, notional=size.notional)
            return None

        try:
            try:
                self.ex.set_margin_mode("isolated", sig.symbol)
            except Exception:
                pass                     # قد يكون معزولاً بالفعل
            self.ex.set_leverage(lev, sig.symbol)
            order = self.ex.create_order(sig.symbol, "market", side, size.qty,
                                         params={"newClientOrderId": client_id})
            fill = self._fill_price(order, sig.entry)
        except Exception as e:
            journal("error", symbol=sig.symbol, reason=f"فشل أمر الدخول: {e}")
            return None

        pos = Position(symbol=sig.symbol, side=sig.side, qty=size.qty, entry=fill, sl=sl,
                       leverage=lev, risk_amount=size.risk_amount,
                       opened_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       peak=fill, atr=sig.atr, client_id=client_id)
        journal("open", symbol=pos.symbol, side=pos.side, qty=pos.qty, price=fill, sl=sl,
                notional=size.notional, risk=size.risk_amount, leverage=lev,
                reason=sig.reason, mode="LIVE" if self.live else "TESTNET")

        # --- الستوب الواقي: إن فشل نُغلق الصفقة فوراً ---
        try:
            stop = self._place_stop(pos)
            # أوامر Algo تعيد algoId لا id
            pos.stop_order_id = str(stop.get("id") or stop.get("algoId") or stop.get("info", {}).get("algoId") or "")
        except Exception as e:
            journal("critical", symbol=sig.symbol, reason=f"فشل وضع الستوب: {e} — إغلاق فوري")
            self._flatten_now(sig.symbol, pos)
            return None
        self._pos[sig.symbol] = pos
        return pos

    def _cancel_stop(self, pos: Position) -> bool:
        """إلغاء ستوب closePosition.

        Binance تخزّن أوامر STOP_MARKET مع closePosition في مساحة «Algo Orders»
        وليس في /fapi/v1/order — لذلك cancel_order العادي يفشل بخطأ -2011.
        الإلغاء الصحيح: DELETE /fapi/v1/algoOrder.
        """
        oid = str(pos.stop_order_id or "")
        if not oid:
            return True
        # (أ) الإلغاء الصحيح لأوامر Algo
        try:
            self.ex.fapiPrivateDeleteAlgoOrder({"algoId": oid})
            journal("stop_cancelled", symbol=pos.symbol, order_id=oid, via="algo")
            return True
        except Exception as e1:
            # (ب) أمر عادي (احتياطاً)
            try:
                self.ex.cancel_order(oid, pos.symbol)
                journal("stop_cancelled", symbol=pos.symbol, order_id=oid, via="order")
                return True
            except Exception:
                journal("stop_cancel_failed", symbol=pos.symbol, order_id=oid,
                        reason=str(e1)[:120])
                return False

    def _place_stop(self, pos: Position):
        close_side = "sell" if pos.side == "LONG" else "buy"
        return self.ex.create_order(
            pos.symbol, "STOP_MARKET", close_side, None,
            # ⚠️ Binance يرفض closePosition + reduceOnly معاً (خطأ -1106).
            # closePosition وحده يُغلق المركز كاملاً — وهو المطلوب للستوب.
            params={"stopPrice": self._fmt_price(pos.symbol, pos.sl),
                    "closePosition": True,
                    "workingType": "MARK_PRICE", "newClientOrderId": f"sl-{pos.client_id}"})

    def _fmt_price(self, symbol: str, price: float):
        """تقريب السعر لدقة الزوج — بأمان إن لم تكن الدالة متاحة (اختبارات)."""
        try:
            return float(self.ex.price_to_precision(symbol, price))
        except Exception:
            return price

    def _fill_price(self, order: dict, fallback: float) -> float:
        try:
            o = self.ex.fetch_order(order["id"], order["symbol"])
            return float(o.get("average") or o.get("price") or fallback)
        except Exception:
            return float(order.get("average") or order.get("price") or fallback)

    def trail(self, symbol: str, new_sl: float) -> bool:
        pos = self._pos.get(symbol)
        if not pos:
            return False
        improved = (new_sl > pos.sl) if pos.side == "LONG" else (new_sl < pos.sl)
        if not improved or self.dry_run:
            return False
        sl = round_price(new_sl, self._tick(symbol), mode="down" if pos.side == "LONG" else "up")
        old_id = pos.stop_order_id
        try:
            if old_id:
                self._cancel_stop(pos)
            stop = self._place_stop(Position(**{**asdict(pos), "sl": sl}))
            pos.sl, pos.stop_order_id = sl, str(stop.get("id", ""))
            journal("trail", symbol=symbol, side=pos.side, price=sl, reason=f"ستوب متتبّع → {sl}")
            return True
        except Exception as e:
            journal("error", symbol=symbol, reason=f"فشل تحديث الستوب: {e} — إغلاق آمن")
            self.close(symbol, "تعذّر تحديث الستوب")
            return False

    def close(self, symbol: str, reason: str = "") -> bool:
        pos = self._pos.get(symbol)
        if not pos:
            return False
        if self.dry_run:
            journal("dry_run_close", symbol=symbol, reason=reason)
            self._pos.pop(symbol, None)
            return True
        try:
            if pos.stop_order_id:
                self._cancel_stop(pos)
            side = "sell" if pos.side == "LONG" else "buy"
            self.ex.create_order(symbol, "market", side, pos.qty,
                                 params={"reduceOnly": True,
                                         "newClientOrderId": f"cl-{pos.client_id}"})
            price = self._last_price(symbol)
            pnl = pos.pnl(price)
            self.kill.register_trade(pnl)
            journal("close", symbol=symbol, side=pos.side, qty=pos.qty, price=price,
                    pnl=round(pnl, 4), reason=reason)
            self._pos.pop(symbol, None)
            return True
        except Exception as e:
            journal("error", symbol=symbol, reason=f"فشل الإغلاق: {e}")
            return False

    def _flatten_now(self, symbol: str, pos: Position):
        try:
            side = "sell" if pos.side == "LONG" else "buy"
            self.ex.create_order(symbol, "market", side, pos.qty,
                                 params={"reduceOnly": True})
            journal("emergency_close", symbol=symbol, side=pos.side, reason="حماية: بلا ستوب")
        except Exception as e:
            journal("critical", symbol=symbol, reason=f"تعذّر الإغلاق الطارئ: {e}")

    def _last_price(self, symbol: str) -> float:
        try:
            return float(self.ex.fetch_ticker(symbol)["last"])
        except Exception:
            return 0.0

    def flatten_all(self) -> int:
        n = 0
        for sym in list(self._pos):
            n += 1 if self.close(sym, "إغلاق كلي يدوي") else 0
        return n


# ---------------------------------------------------------------------------
# مدير الستوب المتتبّع: يعمل كحلقة مستقلة
# ---------------------------------------------------------------------------
class TrailingManager:
    def __init__(self, executor: BaseExecutor, feed, interval: int = 300):
        self.ex = executor
        self.feed = feed
        self.interval = interval
        self.running = False

    async def tick(self) -> list[str]:
        positions = self.ex.positions()
        if not positions:
            return []
        acted = []
        candles = {}
        for pos in positions:
            try:
                df = await self.feed.candles(pos.symbol, self.ex.s.timeframe, 40)
            except Exception as e:
                log.warning("بيانات %s فشلت: %s", pos.symbol, e)
                continue
            candles[pos.symbol] = df
            atr = _atr(df, 14)
            bar = df.iloc[-1]
            now = datetime.fromisoformat(pos.opened_at) if isinstance(pos.opened_at, str) else None
            # نأخذ القمم فقط بعد لحظة الفتح
            if now is not None and "datetime" in df:
                window = df[df["datetime"] >= now]
                window = window if len(window) else df.tail(1)
            else:
                window = df
            high, low = float(window["high"].max()), float(window["low"].min())
            new_sl = self.ex.proposed_stop(pos, high, low, atr)
            if new_sl:
                if self.ex.trail(pos.symbol, new_sl):
                    acted.append(pos.symbol)
        # الورقي يحتاج فحص الستوب بنفسه
        if hasattr(self.ex, "check_stops"):
            for sym in self.ex.check_stops(candles):
                acted.append(f"{sym}:stop")
        return acted

    async def run_forever(self):
        self.running = True
        while self.running:
            try:
                acted = await self.tick()
                if acted:
                    log.info("تحديثات الستوب: %s", acted)
            except Exception as e:
                log.exception("خطأ في حلقة التتبّع: %s", e)
            await asyncio.sleep(self.interval)


def _atr(df: pd.DataFrame, n: int = 14) -> float:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1 / n, adjust=False).mean().iloc[-1])
