"""المحرّك: مسح الأزواج بشكل دوري، توليد الإشارات، منع التكرار، الإرسال."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from config import Settings
from datafeed import DataFeed
from strategy import STRATEGIES, Signal

log = logging.getLogger("engine")


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
                    self.strategy.prepare(df)
                    return self.strategy.evaluate(sym, df)
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
