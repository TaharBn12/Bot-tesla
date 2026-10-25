"""
تشغيل المسح بدون تلجرام — للاختبار السريع.
    python src/scan_cli.py            # مسح مرة واحدة
    python src/scan_cli.py --loop     # مسح متواصل
"""
import asyncio
import sys

from config import Settings
from engine import Scanner
from strategy import STRATEGIES


async def main():
    settings = Settings.load()
    name = sys.argv[2] if len(sys.argv) > 2 else "ema_cross_example"
    found = []

    async def collect(sig):
        found.append(sig)
        print(f"\n[{sig.side:5}] {sig.symbol} | entry={sig.entry} sl={sig.stop_loss} "
              f"tp={sig.take_profits} score={sig.score:.2f}")

    sc = Scanner(settings, name, collect)
    # إن كانت symbols فارغة في config.yaml → وضع مكثف: أعلى الأزواج سيولة
    await sc.ensure_universe()
    print(f"الأزواج: {len(sc._symbols)} | فريم: {settings.timeframe} | بورصة: {settings.exchange}")

    if "--loop" in sys.argv:
        await sc.run_forever()
    else:
        n = await sc.scan_once()
        print(f"\nانتهى المسح. إشارات: {n}")
    await sc.close()


if __name__ == "__main__":
    asyncio.run(main())
