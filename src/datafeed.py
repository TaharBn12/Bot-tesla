"""جلب بيانات العقود الآجلة (شموع OHLCV) من البورصة عبر ccxt."""
import asyncio
import ccxt.async_support as ccxt
import pandas as pd


class DataFeed:
    def __init__(self, exchange_id: str = "binanceusdm", api_key: str = "", api_secret: str = ""):
        if not hasattr(ccxt, exchange_id):
            raise ValueError(f"بورصة غير مدعومة: {exchange_id}")
        self.ex = getattr(ccxt, exchange_id)({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
        self._sem = asyncio.Semaphore(5)   # حد التوازي لتفادي حظر البورصة

    async def candles(self, symbol: str, timeframe: str = "4h", limit: int = 300) -> pd.DataFrame:
        async with self._sem:
            ohlcv = await self.ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
        df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        # نستبعد الشمعة الجارية (غير المغلقة) لمنع إعادة الرسم (Repainting)
        tf_ms = self.ex.parse_timeframe(timeframe) * 1000
        now_ms = self.ex.milliseconds()
        df = df[df["ts"] + tf_ms <= now_ms + 1000].reset_index(drop=True)
        return df

    async def tickers(self) -> dict:
        return await self.ex.fetch_tickers()

    async def top_volume_symbols(self, quote: str = "USDT", n: int = 50) -> list:
        """أزواج العقود الآجلة الأعلى سيولة — تُستخدم لوضع 'مكثف'."""
        markets = await self.ex.load_markets()
        tickers = await self.ex.fetch_tickers()
        rows = []
        for sym, t in tickers.items():
            m = markets.get(sym)
            if not m or not m.get("swap") or m.get("quote") != quote:
                continue
            qv = t.get("quoteVolume") or 0
            rows.append((sym, qv))
        rows.sort(key=lambda r: r[1], reverse=True)
        return [r[0] for r in rows[:n]]

    async def close(self):
        await self.ex.close()
