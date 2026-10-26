"""المحرّك: مسح الأزواج بشكل دوري، توليد الإشارات، منع التكرار، الإرسال."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from config import Settings
from datafeed import DataFeed
from strategy import STRATEGIES, Signal

log = logging.getLogger("engine")


def _eval(strategy, sym, df):
    """حساب المؤشرات — يُنفّذ في thread حتى لا يجمّد حلقة الأحداث."""
    strategy.prepare(df)
    return strategy.evaluate(sym, df)


class Scanner:
    def __init__(self, settings: Settings, strategy_name: str | None, on_signal):
        self.s = settings
        name = strategy_name or settings.strategy
        params = {"leverage": settings.leverage, **(settings.strategy_params or {})}
        self.strategy_name = name
        self.strategy = STRATEGIES[name](params)
        self.feed = DataFeed(settings.exchange, settings.api_key, settings.api_secret)
        self.on_signal = on_signal          # دالة async تُرسل الإشارة
        self._cooldown: dict[tuple, datetime] = {}
        self._open: list[dict] = []      # إشارات مفتوحة (للسقف المتزامن)
        self._symbols: list = list(settings.symbols)
        self.stats = {"scans": 0, "signals": 0, "last_scan": None}

    async def refresh_universe(self, top_n: int | None = None):
        """وضع 'مكثف': يأخذ أعلى الأزواج سيولة تلقائياً."""
        try:
            self._symbols = await self.feed.top_volume_symbols(n=top_n or self.s.universe_size)
            log.info("تم تحديث القائمة: %d زوج", len(self._symbols))
        except Exception as e:
            log.warning("فشل تحديث القائمة، إبقاء القائمة الحالية: %s", e)

    def trailing_stop(self, sig: Signal) -> float | None:
        """الستوب المتتبَّع (Chandelier): أعلى قمة − 3.5×ATR للشراء، والعكس للبيع."""
        if not sig.atr:
            return None
        k = 3.5
        return sig.entry - k * sig.atr if sig.side == "LONG" else sig.entry + k * sig.atr

    def _prune_open(self):
        now = datetime.now(timezone.utc)
        self._open = [p for p in self._open if p["expires"] > now]

    def open_count(self) -> int:
        self._prune_open()
        return len(self._open)

    def _register_open(self, sig: Signal):
        now = datetime.now(timezone.utc)
        self._open.append({"symbol": sig.symbol, "side": sig.side, "at": now,
                           "expires": now + timedelta(hours=self.s.signal_expiry_hours)})

    def _allowed(self, sig: Signal) -> bool:
        key = (sig.symbol, sig.side)
        last = self._cooldown.get(key)
        now = datetime.now(timezone.utc)
        if last and now - last < timedelta(minutes=self.s.cooldown_minutes):
            return False
        self._cooldown[key] = now
        return True

    async def scan_once(self):
        self.stats["scans"] += 1
        sem = asyncio.Semaphore(8)

        async def work(sym):
            async with sem:
                try:
                    df = await self.feed.candles(sym, self.s.timeframe, 320)
                    return await asyncio.to_thread(_eval, self.strategy, sym, df)
                except Exception as e:
                    log.debug("%s: %s", sym, e)
                    return None

        results = await asyncio.gather(*(work(s) for s in self._symbols))
        found = [r for r in results if r]
        found.sort(key=lambda x: x.score, reverse=True)

        self._prune_open()
        sent = 0
        for sig in found:
            if sent >= self.s.max_signals_per_scan:
                break
            if len(self._open) >= self.s.max_open_positions:
                log.info("تم بلوغ سقف %d صفقات متزامنة — تأجيل بقية الإشارات",
                         self.s.max_open_positions)
                break
            if self._allowed(sig):
                await self.on_signal(sig)
                self._register_open(sig)
                sent += 1
        self.stats["signals"] += sent
        self.stats["last_scan"] = datetime.now(timezone.utc)
        return sent

    async def ensure_universe(self):
        if not self._symbols:
            await self.refresh_universe()

    async def nearest(self, n: int = 6) -> list[dict]:
        """أقرب الأزواج إلى إشارة صالحة + السبب الناقص (لأمر /near)."""
        sem = asyncio.Semaphore(8)

        async def work(sym):
            async with sem:
                try:
                    df = await self.feed.candles(sym, self.s.timeframe, 400)
                    self.strategy.prepare(df)
                    r = self.strategy.readiness(df)
                    r["symbol"] = sym
                    return r
                except Exception:
                    return None

        rows = [r for r in await asyncio.gather(*(work(x) for x in self._symbols)) if r]
        rows.sort(key=lambda x: x.get("readiness", 0), reverse=True)
        return rows[:n]

    async def opportunities(self, min_readiness: float = 66.0, limit: int = 8,
                            min_quote_volume: float = 8_000_000, max_symbols: int = 25) -> list:
        # ملاحظة: max_symbols يحدّ الفحص اليدوي لأسرع 25 زوجاً سيولة (استجابة سريعة)
        """يفحص كل الأزواج ويُعيد الفرص القابلة للدخول الآن (مرتبة بالجودة)."""
        import opportunities as opp_mod
        sem = asyncio.Semaphore(6)
        # فلتر السيولة: يستبعد أزواج الأسهم المُرمّزة والأزواج الميتة على البورصات غير المتوقعة
        try:
            tick = await self.feed.tickers()
        except Exception:
            tick = {}
        liquid = set()
        for sym, t in tick.items():
            if (t.get("quoteVolume") or 0) >= min_quote_volume:
                liquid.add(sym)
        universe = [s for s in self._symbols if s in liquid] or self._symbols
        if max_symbols and len(universe) > max_symbols:
            # نختار الأعلى سيولة
            ranked = sorted(universe, key=lambda x: (tick.get(x, {}).get("quoteVolume") or 0), reverse=True)
            universe = ranked[:max_symbols]

        async def work(sym):
            async with sem:
                try:
                    df = await self.feed.candles(sym, self.s.timeframe, 400)
                    return await asyncio.to_thread(
                        opp_mod.build, sym, df, self.strategy,
                        getattr(self.s, "paper_balance", 1000.0),
                        self.s.risk_per_trade_pct, self.s.leverage, min_readiness)
                except Exception:
                    return None

        found = [o for o in await asyncio.gather(*(work(s) for s in universe)) if o]
        order = {"A": 0, "B": 1, "C": 2}
        found.sort(key=lambda o: (order.get(o.grade, 9), -o.readiness))
        return found[:limit]

    async def run_forever(self):
        await self.ensure_universe()
        while True:
            try:
                await self.scan_once()
            except Exception as e:
                log.exception("خطأ في المسح: %s", e)
            await asyncio.sleep(self.s.scan_interval)

    async def close(self):
        await self.feed.close()
