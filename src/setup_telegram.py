"""
أداة ربط البوت بتلجرام — تتحقق من كل شيء وتشخّص الأخطاء بالعربية.

    python src/setup_telegram.py check         # هل التوكن صالح؟ من هو البوت؟
    python src/setup_telegram.py discover      # اكتشاف chat_id تلقائياً (أرسل /start للبوت أولاً)
    python src/setup_telegram.py send "مرحبا"  # إرسال رسالة تجريبية
    python src/setup_telegram.py save          # حفظ التوكن/المعرّف في .env بأمان
    python src/setup_telegram.py doctor        # فحص شامل لكل الإعدادات

لا يحتاج أي مكتبة خارجية (urllib فقط).
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
API = "https://api.telegram.org/bot{token}/{method}"

OK, BAD, WARN = "✅", "❌", "⚠️"


def call(token: str, method: str, payload: dict | None = None, timeout: int = 30) -> dict:
    url = API.format(token=token, method=method)
    data = urllib.parse.urlencode(payload or {}).encode()
    req = urllib.request.Request(url, data=data if payload is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            return json.loads(body)
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}: {body[:200]}"}
    except Exception as e:
        return {"ok": False, "description": f"تعذّر الاتصال: {e}"}


def explain(err: dict) -> str:
    d = str(err.get("description", ""))
    code = err.get("error_code")
    if code == 401:
        return "التوكن غير صالح. تأكد أنك نسخته كاملاً من @BotFather (يحتوي على ':' بين رقمين)."
    if code == 404:
        return "طريقة غير معروفة — غالباً التوكن مشوّه (مسافة أو حرف ناقص)."
    if code == 400 and "chat not found" in d.lower():
        return ("المعرّف غير صحيح أو أنك لم تبدأ محادثة مع البوت بعد.\n"
                "   الحل: افتح البوت في تلجرام واضغط /start ثم أعد المحاولة.")
    if code == 403:
        return ("البوت لا يستطيع الكتابة في هذه المحادثة.\n"
                "   • قناة/مجموعة: أضف البوت كمشرف (Admin) مع صلاحية النشر.\n"
                "   • مستخدم حظر البوت؟ ارفع الحظر.")
    if code == 429:
        return "تجاوزت حدود الإرسال (429). انتظر قليلاً."
    return d or "خطأ غير معروف"


def load_env() -> dict:
    if not ENV.exists():
        return {}
    out = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def get_creds(cli_token: str | None = None, cli_chat: str | None = None):
    env = load_env()
    token = cli_token or os.getenv("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN", "")
    chat = cli_chat or os.getenv("TELEGRAM_CHAT_ID") or env.get("TELEGRAM_CHAT_ID", "")
    return token.strip(), str(chat).strip()


def mask(s: str) -> str:
    if not s:
        return "(فارغ)"
    if len(s) <= 12:
        return s[:3] + "…"
    return f"{s[:10]}…{s[-4:]} ({len(s)} حرفاً)"


def check(token: str | None = None) -> bool:
    token, _ = get_creds(token)
    print(f"التوكن: {mask(token)}")
    if not token:
        print(f"{BAD} لا يوجد توكن — ضعه في .env أو مرّره كوسيط")
        return False
    if not re.match(r"^\d{6,}:[A-Za-z0-9_-]{30,}$", token):
        print(f"{WARN} شكل التوكن غير معتاد (يجب أن يشبه 123456789:ABCdef...)")
    r = call(token, "getMe")
    if not r.get("ok"):
        print(f"{BAD} فشل: {explain(r)}")
        return False
    b = r["result"]
    print(f"{OK} البوت صالح: @{b.get('username')} — الاسم: {b.get('first_name')}")
    print(f"   يمكن إضافته من: https://t.me/{b.get('username')}")
    print(f"   يحق له الانضمام للمجموعات: {b.get('can_join_groups')}")
    return True


def discover(token: str | None = None, wait: int = 60) -> str | None:
    """يقرأ آخر التحديثات ليعرف من راسل البوت — أسهل طريقة للحصول على chat_id."""
    token, _ = get_creds(token)
    if not token:
        print(f"{BAD} لا يوجد توكن")
        return None
    print(f"أرسل /start للبوت الآن من تلجرام… (أستمع لمدة {wait} ثانية)")
    deadline, seen, last_id = time.time() + wait, {}, 0
    while time.time() < deadline:
        r = call(token, "getUpdates", {"offset": last_id + 1, "timeout": 10, "limit": 50}, timeout=20)
        if not r.get("ok"):
            print(f"{BAD} {explain(r)}")
            return None
        for u in r.get("result", []):
            last_id = max(last_id, u.get("update_id", 0))
            msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member", {}).get("chat") or {}
            chat = msg.get("chat") if isinstance(msg, dict) else None
            if not chat:
                continue
            cid = str(chat.get("id"))
            if cid not in seen:
                seen[cid] = (chat.get("type"), chat.get("title") or chat.get("username")
                             or chat.get("first_name") or "—")
                print(f"{OK} اكتشفت محادثة: id={cid} | النوع: {chat.get('type')} | الاسم: {seen[cid][1]}")
        if seen:
            break
    if not seen:
        print(f"{WARN} لم يستقبل أي رسالة. تأكد أنك ضغطت /start في البوت، ثم أعد المحاولة.")
        return None
    cid = list(seen)[0]
    print(f"\nالمعرّف المقترح: {cid}")
    print(f"للحفظ: python src/setup_telegram.py save")
    return cid


def send(token: str | None = None, chat: str | None = None, text: str = "🤖 تم ربط البوت بنجاح") -> bool:
    token, chat = get_creds(token, chat)
    if not token or not chat:
        print(f"{BAD} التوكن أو المعرّف ناقص")
        return False
    r = call(token, "sendMessage", {"chat_id": chat, "text": text})
    if not r.get("ok"):
        print(f"{BAD} فشل الإرسال: {explain(r)}")
        return False
    msg = r["result"]
    print(f"{OK} أُرسلت الرسالة إلى {msg['chat'].get('title') or msg['chat'].get('first_name')} "
          f"(id={msg['chat']['id']})")
    return True


def save(token: str | None = None, chat: str | None = None) -> bool:
    """يحفظ القيم في .env مع الحفاظ على بقية الأسطر والتعليقات."""
    token, chat = get_creds(token, chat)
    if not token:
        print(f"{BAD} لا يوجد توكن لحفظه")
        return False
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    if not lines and (ROOT / ".env.example").exists():
        lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    def upsert(key, val):
        for i, l in enumerate(lines):
            if l.strip().startswith(f"{key}="):
                lines[i] = f"{key}={val}"
                return
        lines.append(f"{key}={val}")
    upsert("TELEGRAM_BOT_TOKEN", token)
    if chat:
        upsert("TELEGRAM_CHAT_ID", chat)
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(ENV, 0o600)
    except Exception:
        pass
    print(f"{OK} حُفظ في {ENV} (صلاحيات 600)")
    print(f"   TELEGRAM_BOT_TOKEN={mask(token)}")
    print(f"   TELEGRAM_CHAT_ID={chat or '(لم يُحدَّد — استخدم discover)'}")
    return True


def doctor() -> bool:
    print("═" * 58)
    print("فحص شامل لإعدادات تلجرام")
    print("═" * 58)
    token, chat = get_creds()
    all_ok = True

    print(f"\n[1] ملف .env: {'موجود' if ENV.exists() else 'غير موجود'}")
    if ENV.exists():
        perms = oct(ENV.stat().st_mode)[-3:]
        print(f"    الصلاحيات: {perms} {'(سليم)' if perms in ('600', '640') else '⚠️ يُفضّل 600'}")
    else:
        print(f"    {WARN} انسخ .env.example إلى .env")
        all_ok = False

    print("\n[2] التوكن")
    if not check(token):
        return False

    print("\n[3] معرّف المحادثة")
    if not chat:
        print(f"{BAD} غير محدد — شغّل: python src/setup_telegram.py discover")
        return False
    print(f"{OK} محدد: {chat}")

    print("\n[4] رسالة تجريبية")
    if not send(token, chat, "🤖 فحص الاتصال: البوت يعمل ✅"):
        all_ok = False

    print("\n[5] الاتصال بالبورصة والاستراتيجية")
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from config import Settings
        s = Settings.load()
        print(f"{OK} الإعدادات: {s.exchange} | فريم {s.timeframe} | استراتيجية {s.strategy}")
    except Exception as e:
        print(f"{WARN} تعذّر تحميل الإعدادات: {e}")
        all_ok = False

    print("\n" + "═" * 58)
    print("النتيجة: " + ("جاهز للتشغيل ✅" if all_ok else "يوجد ما يجب إصلاحه ⚠️"))
    print("شغّل البوت:  python src/bot.py")
    print("═" * 58)
    return all_ok


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "doctor"
    args = sys.argv[2:]
    if cmd == "check":
        sys.exit(0 if check(args[0] if args else None) else 1)
    elif cmd == "discover":
        discover(args[0] if args else None, int(args[1]) if len(args) > 1 else 60)
    elif cmd == "send":
        sys.exit(0 if send(text=args[0] if args else "🤖 رسالة تجريبية") else 1)
    elif cmd == "save":
        save(args[0] if args else None, args[1] if len(args) > 1 else None)
    else:
        sys.exit(0 if doctor() else 1)
