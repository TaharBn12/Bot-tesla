"""
فحص صحة البوت من الخارج — يعمل بلا مفاتيح بورصة (يحتاج التوكن فقط).

    python src/healthcheck.py              # فحص محلي
    python src/healthcheck.py --notify     # يرسل تنبيهاً إلى تلجرام إن وُجدت مشكلة

يُستخدم في GitHub Actions كل ساعة، وأيضاً يدوياً للتشخيص.

الفحوصات:
  1) التوكن صالح؟ (getMe)
  2) هل يوجد تراكم تحديثات؟ (pending_update_count > 0 = البوت لا يقرأ الرسائل → متوقف)
  3) هل تصل البورصة؟ (ping)
  4) هل ملف الإعدادات سليم؟

رمز الخروج: 0 = سليم، 1 = مشكلة.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS: list[tuple[str, bool, str]] = []


def add(name: str, ok: bool, detail: str = "", fatal: bool = False):
    RESULTS.append((name, ok, detail))
    mark = "✅" if ok else ("❌" if fatal else "⚠️ ")
    print(f"{mark} {name}" + (f" — {detail}" if detail else ""))


def env(key: str) -> str:
    """يقرأ من متغيّرات البيئة أولاً (GitHub Actions) ثم من .env."""
    v = os.getenv(key, "").strip()
    if v:
        return v
    envfile = ROOT / ".env"
    if envfile.exists():
        for line in envfile.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def tg(token: str, method: str) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "description": f"تعذّر الاتصال: {e}"}


def check_telegram(token: str) -> bool:
    if not token:
        add("التوكن", False, "TELEGRAM_BOT_TOKEN غير موجود", fatal=True)
        return False
    r = tg(token, "getMe")
    if not r.get("ok"):
        add("التوكن", False, r.get("description", "غير صالح"), fatal=True)
        return False
    add("التوكن", True, f"@{r['result'].get('username')}")
    return True


def check_queue(token: str) -> bool:
    """أهم فحص: تراكم التحديثات يعني أن البوت لا يقرأ الرسائل (متوقف)."""
    r = tg(token, "getWebhookInfo")
    if not r.get("ok"):
        add("طابور التحديثات", False, r.get("description", "?"))
        return False
    info = r.get("result", {})
    pending = int(info.get("pending_update_count") or 0)
    has_hook = bool(info.get("url"))
    if pending == 0:
        add("طابور التحديثات", True, "لا تراكم — البوت يقرأ الرسائل" if not has_hook else "وضع webhook")
        return True
    if pending <= 3:
        add("طابور التحديثات", True, f"{pending} رسالة معلّقة (طبيعي أثناء الإرسال)")
        return True
    add("طابور التحديثات", False,
        f"{pending} رسالة معلّقة — البوت على الأرجح متوقف أو نسختان تتعارضان",
        fatal=pending > 10)
    return False


def check_exchange() -> bool:
    """ping للبورصة — يكشف الحجب (451) وانقطاع الشبكة."""
    targets = [
        ("Binance", "https://fapi.binance.com/fapi/v1/ping"),
        ("OKX", "https://www.okx.com/api/v5/public/time"),
    ]
    reachable = []
    for name, url in targets:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "trendpulse-healthcheck"})
            with urllib.request.urlopen(req, timeout=12) as r:
                if r.status == 200:
                    reachable.append(name)
        except urllib.error.HTTPError as e:
            if e.code == 451:
                print(f"   ℹ️ {name}: محجوب من هذه المنطقة (451)")
        except Exception:
            pass
    if reachable:
        add("البورصة", True, "متاحة: " + ", ".join(reachable))
        return True
    add("البورصة", False, "لا تصل أي بورصة — انقطاع شبكة أو حجب كامل", fatal=True)
    return False


def check_config() -> bool:
    try:
        cfg = (ROOT / "config.yaml").read_text(encoding="utf-8")
        import yaml
        d = yaml.safe_load(cfg) or {}
        tf = d.get("timeframe")
        mode = d.get("mode")
        ok = tf in ("5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d")
        add("الإعدادات", ok, f"فريم {tf} | وضع {mode} | استراتيجية {d.get('strategy')}")
        return ok
    except Exception as e:
        add("الإعدادات", False, str(e)[:80])
        return False


def notify(token: str, chat: str, problems: list[str]):
    text = ("🚨 <b>تنبيه: البوت يحتاج انتباهك</b>\n"
            "──────────────\n" + "\n".join(f"• {p}" for p in problems) +
            "\n\n<i>من فحص الصحة التلقائي</i>")
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=20)
        print("📨 أُرسل تنبيه إلى تلجرام")
    except Exception as e:
        print(f"تعذّر إرسال التنبيه: {e}")


def main() -> int:
    notify_flag = "--notify" in sys.argv
    print("═" * 54)
    print("فحص صحة البوت")
    print("═" * 54)

    token = env("TELEGRAM_BOT_TOKEN")
    chat = env("TELEGRAM_CHAT_ID")

    if check_telegram(token):
        check_queue(token)
    check_exchange()
    check_config()

    problems = [f"{n}: {d}" for n, ok, d in RESULTS if not ok]
    fatal = [p for p in problems if any(r[0] in p and not r[1] and "❌" for r in RESULTS)]

    print("─" * 54)
    if not problems:
        print("النتيجة: ✅ كل الفحوصات سليمة")
        return 0

    print(f"النتيجة: ⚠️ {len(problems)} مشكلة")
    for p in problems:
        print(f"  • {p}")

    if notify_flag and token and chat:
        notify(token, chat, problems)
    return 1


if __name__ == "__main__":
    sys.exit(main())
