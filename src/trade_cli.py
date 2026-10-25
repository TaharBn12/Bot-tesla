"""
واجهة التنفيذ الآلي من الطرفية.

    python src/trade_cli.py info              # الرصيد والمراكز وحالة الإيقاف
    python src/trade_cli.py paper 5           # جلسة تداول ورقية (5 دورات) بأسعار حقيقية
    python src/trade_cli.py open BTC/USDT:USDT LONG
    python src/trade_cli.py close BTC/USDT:USDT
    python src/trade_cli.py trail             # تحديث الستوب المتتبّع مرة واحدة
    python src/trade_cli.py flatten           # إغلاق كل المراكز

⚠️ التشغيل الحقيقي يحتاج: mode=autotrade في config.yaml + ALLOW_LIVE=true في .env + مفاتيح API.
   بدون ALLOW_LIVE يعمل على Testnet فقط.
"""
import asyncio
import logging
import sys

from config import Settings
from datafeed import DataFeed
from executor import BinanceExecutor, PaperExecutor, TrailingManager
from strategy import STRATEGIES

logging.basicConfig(format="%(asctime)s %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("trade")


def banner(ex):
    mode = "LIVE 🔴" if getattr(ex, "live", False) else ("TESTNET 🟡" if ex.name == "binance" else "PAPER 🟢")
    print(f"\n╭─ الوضع: {mode} | البورصة: {ex.s.exchange} | الفريم: {ex.s.timeframe}")
    print(f"│ الرصيد: {ex.equity():.2f} USDT | الرافعة: {ex.s.leverage}x | مخاطرة: {ex.s.risk_per_trade_pct}%")
    st = ex.risk_status()
    print(f"│ مفتاح الإيقاف: {st['موقوف']} | صفقات اليوم: {st['صفقات اليوم']} | "
          f"نتيجة اليوم: {st['ربح/خسارة اليوم']}")
    pos = ex.positions()
    if not pos:
        print("│ لا مراكز مفتوحة")
    for p in pos:
        print(f"│ • {p.symbol} {p.side} | كمية {p.qty} | دخول {p.entry} | ستوب {p.sl:.6g} "
              f"| مخاطرة {p.risk_amount} USDT")
    print("╰" + "─" * 55)


async def build_paper(settings, feed):
    return PaperExecutor(settings, feed, balance=float(getattr(settings, "paper_balance", 1000)))


async def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
    settings = Settings.load()
    feed = DataFeed(settings.exchange if settings.exchange else "okx",
                    settings.api_key, settings.api_secret)
    if settings.mode == "signals" and cmd in ("open", "close", "flatten", "trail"):
        print("⚠️ mode=signals في config.yaml — لن أرسل أوامر حقيقية. اضبط mode: autotrade")
        return

    if settings.exchange == "binanceusdm" and settings.api_key and cmd in ("open", "close", "flatten", "trail", "info"):
        ex = BinanceExecutor(settings)
    else:
        ex = await build_paper(settings, feed)

    try:
        if cmd == "info":
            banner(ex)

        elif cmd == "open":
            symbol, side = sys.argv[2], sys.argv[3].upper()
            strat = STRATEGIES[settings.strategy]({"leverage": settings.leverage, **settings.strategy_params})
            df = strat.prepare(await feed.candles(symbol, settings.timeframe, 400))
            sig = strat.evaluate(symbol, df)
            if not sig:
                print(f"لا إشارة حالياً على {symbol}. لن أفتح صفقة بدون إشارة الاستراتيجية.")
                return
            if sig.side != side:
                print(f"⚠️ الاستراتيجية تقول {sig.side} لا {side} — ألغيت الأمر حمايةً لك.")
                return
            pos = ex.open(sig)
            banner(ex)
            print("تم الفتح ✅" if pos else "لم تُفتح الصفقة (راجع السجل)")

        elif cmd == "close":
            print("أُغلقت ✅" if ex.close(sys.argv[2], "أمر يدوي") else "لا مركز بهذا الزوج")

        elif cmd == "flatten":
            print(f"عدد المراكز المغلقة: {ex.flatten_all() if hasattr(ex, 'flatten_all') else 0}")

        elif cmd == "trail":
            tm = TrailingManager(ex, feed, settings.scan_interval)
            acted = await tm.tick()
            print("تحديثات:", acted or "لا جديد")
            banner(ex)

        elif cmd == "paper":
            cycles = int(sys.argv[2]) if len(sys.argv) > 2 else 3
            strat = STRATEGIES[settings.strategy]({"leverage": settings.leverage, **settings.strategy_params})
            tm = TrailingManager(ex, feed, settings.scan_interval)
            for c in range(cycles):
                print(f"\n── دورة {c + 1}/{cycles} ──")
                acted = await tm.tick()
                if acted:
                    print("  ستوب متتبّع:", acted)
                if len(ex.positions()) < settings.max_open_positions:
                    syms = settings.symbols or await feed.top_volume_symbols(n=settings.universe_size)
                    for sym in syms:
                        if len(ex.positions()) >= settings.max_open_positions:
                            break
                        try:
                            df = strat.prepare(await feed.candles(sym, settings.timeframe, 400))
                            sig = strat.evaluate(sym, df)
                            if sig:
                                ex.open(sig)
                        except Exception as e:
                            log.debug("%s: %s", sym, e)
                banner(ex)
                await asyncio.sleep(settings.scan_interval)
        else:
            print(__doc__)
    finally:
        await feed.close()


if __name__ == "__main__":
    asyncio.run(main())
