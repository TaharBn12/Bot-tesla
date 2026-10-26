"""
تفعيل Binance Futures Testnet على ccxt يدوياً.

السبب: ccxt أزال دعم `set_sandbox_mode(True)` لـ binanceusdm في الإصدارات الحديثة
ورفع الخطأ: "binanceusdm testnet/sandbox mode is not supported for futures anymore".

الحل: نعيد توجيه عناوين fapi إلى testnet.binancefuture.com بأنفسنا.
هذا يعمل مع أي إصدار من ccxt (قديم أو جديد).

    import ccxt
    from binance_testnet import enable_futures_testnet
    ex = ccxt.binanceusdm({...})
    enable_futures_testnet(ex)     # بدل ex.set_sandbox_mode(True)
"""
from __future__ import annotations

TESTNET_HOST = "https://testnet.binancefuture.com"

# عناوين Futures على الشبكة التجريبية
_TEST_URLS = {
    "fapiPublic": f"{TESTNET_HOST}/fapi/v1",
    "fapiPublicV2": f"{TESTNET_HOST}/fapi/v2",
    "fapiPublicV3": f"{TESTNET_HOST}/fapi/v3",
    "fapiPrivate": f"{TESTNET_HOST}/fapi/v1",
    "fapiPrivateV2": f"{TESTNET_HOST}/fapi/v2",
    "fapiPrivateV3": f"{TESTNET_HOST}/fapi/v3",
    "fapiData": f"{TESTNET_HOST}/futures/data",
}


def enable_futures_testnet(ex):
    """يوجّه كائن ccxt إلى Binance Futures Testnet ويعيده."""
    # المفتاح الحقيقي: ccxt ترفض Testnet للفيوتشرز إلا إن أُغلق هذا التحذير.
    # (ccxt >= 4.x: binance.py يرفع NotSupported عند رؤية testnet.binancefuture.com)
    ex.options["disableFuturesSandboxWarning"] = True

    # 1) إن كان الإصدار يدعم sandbox رسمياً، استخدمه (الأنظف)
    try:
        ex.set_sandbox_mode(True)
        ex.options["disableFuturesSandboxWarning"] = True
        return ex
    except Exception:
        pass

    # 2) وإلا: توجيه يدوي للعناوين
    api = ex.urls.setdefault("api", {})
    for key, url in _TEST_URLS.items():
        api[key] = url

    # 3) علامات داخلية يفترضها ccxt عند العمل على testnet
    ex.options["sandbox"] = True
    ex.options.setdefault("defaultType", "swap")
    ex.options["adjustForTimeDifference"] = True

    # بعض الإصدارات تقرأ من urls['test'] عند sandbox
    ex.urls.setdefault("test", {}).update(_TEST_URLS)

    return ex


def is_testnet(ex) -> bool:
    """هل الكائن موجّه إلى الشبكة التجريبية؟"""
    api = getattr(ex, "urls", {}).get("api", {})
    return "testnet.binancefuture.com" in str(api.get("fapiPrivate", ""))
