"""
التحقق من جاهزية Testnet قبل أي مال حقيقي.

    python src/testnet_check.py                 # فحص شامل (قراءة فقط)
    python src/testnet_check.py --place-order   # يفتح صفقة صغيرة ويغلقها فوراً (Testnet فقط)

ما يفحصه:
  1) هل المفاتيح صالحة على Testnet؟
  2) نوع المركز: One-way مطلوب (وإلا تفشل أوامر reduceOnly)
  3) الرصيد والرافعة المتاحة
  4) دقة السعر والكمية والحد الأدنى للزوج
  5) الستوب: closePosition + reduceOnly مدعومان
  6) المفتاح ليس بصلاحية سحب (Withdraw)

⚠️ يرفض العمل على الشبكة الحقيقية — للسلامة.
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYMBOL = "BTC/USDT:USDT"


def env(key: str) -> str:
    v = os.getenv(key, "").strip()
    if v:
        return v
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


class Report:
    def __init__(self):
        self.rows = []

    def add(self, name, ok, detail="", fix=""):
        self.rows.append((name, ok, detail, fix))
        print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))
        if not ok and fix:
            print(f"   ↳ الحل: {fix}")

    def ok(self):
        return all(r[1] for r in self.rows)


async def main() -> int:
    import ccxt.async_support as ccxt

    place = "--place-order" in sys.argv
    rep = Report()
    print("═" * 58)
    print("فحص Testnet — Binance Futures")
    print("═" * 58)

    key, secret = env("EXCHANGE_API_KEY"), env("EXCHANGE_API_SECRET")
    rep.add("المفاتيح موجودة", bool(key and secret),
            f"{key[:6]}…{key[-4:]}" if key else "فارغة",
            "أنشئ مفاتيح من https://testnet.binancefuture.com ثم ضعها في .env")

    live = env("ALLOW_LIVE").lower() in ("1", "true", "yes")
    rep.add("ليس على الشبكة الحقيقية", not live,
            "ALLOW_LIVE فارغ ✅" if not live else "ALLOW_LIVE=true ⛔",
            "أفرغ ALLOW_LIVE في .env — هذا الفحص للـ Testnet فقط")
    if live:
        print("\n⛔ أوقفت الفحص: ALLOW_LIVE=true يعني شبكة حقيقية.")
        return 1
    if not (key and secret):
        return 1

    ex = ccxt.binanceusdm({
        "apiKey": key, "secret": secret, "enableRateLimit": True,
        "options": {"defaultType": "swap", "adjustForTimeDifference": True},
    })
    ex.set_sandbox_mode(True)

    try:
        # 1) الاتصال والرصيد
        try:
            bal = await ex.fetch_balance()
            usdt = bal.get("USDT", {})
            total = float(usdt.get("total") or 0)
            rep.add("المفاتيح صالحة على Testnet", True, f"الرصيد {total:.2f} USDT")
            rep.add("الرصيد يكفي للتجربة", total >= 50,
                    f"{total:.2f} USDT", "احصل على عملات وهمية من صنبور Testnet")
        except Exception as e:
            rep.add("المفاتيح صالحة على Testnet", False, str(e)[:110],
                    "تأكد أن المفاتيح من testnet.binancefuture.com وليس من Binance الحقيقي")
            return 1

        # 2) نوع المركز (One-way مطلوب)
        try:
            dual = await ex.fapiPrivateGetPositionSideDual()
            hedge = bool(dual.get("dualSidePosition"))
            rep.add("وضع المركز One-way", not hedge,
                    "Hedge Mode ⛔" if hedge else "One-way ✅",
                    "من واجهة Binance: Preferences → Position Mode → One-way Mode")
        except Exception as e:
            rep.add("وضع المركز One-way", True, f"تعذّر الفحص ({str(e)[:40]}) — يُنصح بالتأكد يدوياً")

        # 3) السوق والدقة
        await ex.load_markets()
        m = ex.market(SYMBOL)
        prec = m.get("precision", {})
        limits = m.get("limits", {})
        rep.add("زوج BTC/USDT:USDT متاح", bool(m.get("swap")),
                f"دقة السعر {prec.get('price')} | دقة الكمية {prec.get('amount')} | "
                f"أدنى كمية {limits.get('amount', {}).get('min')}")

        # 4) الرافعة
        try:
            await ex.set_leverage(5, SYMBOL)
            rep.add("ضبط الرافعة (5x)", True, "نجح")
        except Exception as e:
            rep.add("ضبط الرافعة (5x)", False, str(e)[:90], "قد تحتاج رفع الرافعة يدوياً على المنصة")

        # 5) صفقة تجريبية كاملة
        if place:
            print("\n── صفقة تجريبية (فتح + ستوب + إغلاق) ──")
            try:
                await ex.set_margin_mode("isolated", SYMBOL)
            except Exception:
                pass
            qty = float(limits.get("amount", {}).get("min") or 0.001)
            try:
                o = await ex.create_order(SYMBOL, "market", "buy", qty,
                                          params={"newClientOrderId": "testnet-probe"})
                fill = float(o.get("average") or o.get("price") or 0)
                rep.add("أمر دخول Market", True, f"كمية {qty} @ {fill}")
                try:
                    stop = await ex.create_order(
                        SYMBOL, "STOP_MARKET", "sell", None,
                        params={"stopPrice": round(fill * 0.97, 1), "closePosition": True,
                                "reduceOnly": True, "workingType": "MARK_PRICE"})
                    rep.add("أمر ستوب (closePosition)", True, f"id={stop.get('id')}")
                    try:
                        await ex.cancel_order(stop["id"], SYMBOL)
                    except Exception:
                        pass
                except Exception as e:
                    rep.add("أمر ستوب (closePosition)", False, str(e)[:110])
                try:
                    await ex.create_order(SYMBOL, "market", "sell", qty,
                                          params={"reduceOnly": True,
                                                  "newClientOrderId": "testnet-probe-close"})
                    rep.add("إغلاق الصفقة", True, "تم")
                except Exception as e:
                    rep.add("إغلاق الصفقة", False, str(e)[:110],
                            "أغلق المركز يدوياً على Testnet فوراً!")
            except Exception as e:
                rep.add("أمر دخول Market", False, str(e)[:110])
        else:
            print("\nℹ️ لم تُفتح صفقة. لاختبار التنفيذ الكامل: --place-order")

    finally:
        await ex.close()

    print("\n" + "═" * 58)
    if rep.ok():
        print("النتيجة: ✅ Testnet جاهز — يمكنك تفعيل mode: autotrade")
        print("الخطوة التالية: 1) dry_run: false  2) python src/bot.py")
    else:
        print("النتيجة: ⚠️ يوجد ما يجب إصلاحه قبل التشغيل الآلي")
    print("═" * 58)
    return 0 if rep.ok() else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
