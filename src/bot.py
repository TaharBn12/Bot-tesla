"""بوت تلجرام: أوامر + إرسال الإشارات + تشغيل المحرّك مع البوت في نفس الحلقة."""
import asyncio
import logging
import time
import uuid

from telegram import Update
from telegram.constants import ParseMode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from config import Settings
from engine import Scanner
from executor import BinanceExecutor, PaperExecutor, TrailingManager, journal
from hotscan import HotScanner
from risk import KillSwitch
from strategy import Signal
import tracker
import opportunities as opp_mod
from formatting import esc, fmt_opportunities, fmt_positions, fmt_signal, fmt_status

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s | %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

settings = Settings.load()
STRATEGY_NAME = None   # من config.yaml
scanner: Scanner | None = None
executor = None            # BinanceExecutor أو PaperExecutor (حسب الوضع)
trailer: TrailingManager | None = None
hotscanner: HotScanner | None = None

# ═══ الموافقة اليدوية: كل صفقة رسالة مستقلة بزر تنفيذ ═══
PENDING: dict[str, dict] = {}      # token → {sig, exp, chat, msg_id}
APPROVAL_TTL = 3600                 # صلاحية زر الموافقة: ساعة


def _kb(tok: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تنفيذ الصفقة", callback_data=f"ap:{tok}"),
        InlineKeyboardButton("❌ تجاهل", callback_data=f"rj:{tok}"),
    ]])


def allowed(chat_id) -> bool:
    """يقصر البوت على المحادثات المصرّح لها فقط (ALLOWED_CHAT_IDS في .env)."""
    ids = [str(x) for x in (settings.allowed_chats or [settings.chat_id])]
    return "*" in ids or str(chat_id) in ids


def deny(u: Update, c: ContextTypes.DEFAULT_TYPE) -> bool:
    """إن كانت المحادثة غير مصرّح لها: يرد مرة واحدة ويوقف."""
    if allowed(u.effective_chat.id):
        return False
    log.warning("محادثة غير مصرّح لها: %s", u.effective_chat.id)
    return True


def safe(fn):
    """يمنع أي خطأ من أن يترك البوت يبدو «معلقاً»: يرد دائماً بشيء."""
    async def wrapper(u: Update, c: ContextTypes.DEFAULT_TYPE):
        try:
            if deny(u, c):
                return await u.message.reply_text(
                    f"⛔ هذا البوت خاص.\nمعرّف محادثتك: <code>{u.effective_chat.id}</code>\n"
                    "أضفه إلى ALLOWED_CHAT_IDS في ملف .env ليصرّح لك.",
                    parse_mode=ParseMode.HTML)
            return await fn(u, c)
        except Exception as e:
            log.exception("خطأ في %s: %s", fn.__name__, e)
            try:
                await u.message.reply_text(
                    f"⚠️ خطأ في <code>{fn.__name__}</code>:\n<code>{esc(type(e).__name__)}: "
                    f"{esc(str(e)[:160])}</code>",
                    parse_mode=ParseMode.HTML)
            except Exception:
                pass
    wrapper.__name__ = fn.__name__
    return wrapper




async def send_signal(sig: Signal):
    text = fmt_signal(sig, settings)
    tracker.log_signal(sig, settings.timeframe)
    log.info("إشارة: %s %s", sig.side, sig.symbol)

    # ═══ وضع الموافقة: رسالة مستقلة لكل صفقة + زر تنفيذ ═══
    if executor is not None and settings.mode == "approve":
        tok = uuid.uuid4().hex[:10]
        PENDING[tok] = {"sig": sig, "exp": time.time() + APPROVAL_TTL,
                        "chat": settings.chat_id, "msg_id": None}

        # إشارات المسح من okx — قد لا يكون الزوج موجوداً على Binance
        can_exec = await _tradable(sig.symbol)
        if can_exec:
            kb = _kb(tok)
            note = "\n\n⏳ <b>بانتظار موافقتك</b> — اضغط ✅ للتنفيذ على البورصة"
        else:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ تجاهل", callback_data=f"rj:{tok}")]])
            note = ("\n\n🚫 <b>غير متاح على Binance Futures</b>\n"
                    "الزوج موجود على بورصة المسح فقط — لا يمكن تنفيذه.")
        try:
            m = await app.bot.send_message(
                settings.chat_id, text + note,
                parse_mode=ParseMode.HTML, reply_markup=kb)
            PENDING[tok]["msg_id"] = m.message_id
        except Exception as e:
            log.error("تعذّر إرسال رسالة الموافقة: %s", e)
            PENDING.pop(tok, None)
        return

    await app.bot.send_message(settings.chat_id, text, parse_mode=ParseMode.HTML)

    if executor is None:
        return
    pos = executor.open(sig)
    if pos:
        mode = "حقيقية 🔴" if getattr(executor, "live", False) else (
            "Testnet 🟡" if executor.name == "binance" else "ورقية 🟢")
        await app.bot.send_message(
            settings.chat_id,
            f"⚙️ نُفِّذت صفقة {mode}\n"
            f"{pos.side} {pos.symbol} | كمية `{pos.qty}` @ `{pos.entry}`\n"
            f"الستوب على البورصة: `{pos.sl}` | المخاطرة: {pos.risk_amount} USDT",
            parse_mode=ParseMode.HTML)
    else:
        await app.bot.send_message(settings.chat_id, "⚙️ لم تُنفَّذ الصفقة — راجع <code>data/journal.jsonl</code>", parse_mode=ParseMode.HTML)



@safe
async def cmd_demo(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """إشارة تجريبية بزر تنفيذ — لاختبار الموافقة يدوياً بأمان."""
    if executor is None:
        return await u.message.reply_text(
            "المحرّك غير مفعّل. اضبط <code>mode: approve</code> في config.yaml.",
            parse_mode=ParseMode.HTML)
    await u.message.reply_text("🔧 أجهّز إشارة تجريبية…")
    try:
        ex = executor.ex
        sym = "BTC/USDT:USDT"
        await asyncio.to_thread(ex.load_markets)
        t = await asyncio.to_thread(ex.fetch_ticker, sym)
        price = float(t["last"])
        atr = price * 0.01
        sig = Signal(symbol=sym, side="LONG", entry=price,
                     stop_loss=round(price - 1.5 * atr, 1),
                     take_profits=[round(price + 2 * atr, 1),
                                   round(price + 3.5 * atr, 1)],
                     leverage=settings.leverage, atr=atr,
                     reason="إشارة تجريبية — ليست من الاستراتيجية", setup="test")
    except Exception as e:
        return await u.message.reply_text(
            f"⚠️ تعذّر جلب السعر: <code>{esc(str(e)[:150])}</code>", parse_mode=ParseMode.HTML)

    await send_signal(sig)


_BINANCE_OK: dict[str, bool] = {}


async def _tradable(symbol: str) -> bool:
    """هل الزوج متاح للتداول على Binance Futures؟ (إشارات المسح تأتي من okx)"""
    if executor is None:
        return False
    if symbol in _BINANCE_OK:
        return _BINANCE_OK[symbol]
    ok = False
    try:
        ex = executor.ex
        await asyncio.to_thread(ex.load_markets)
        m = ex.market(symbol) if symbol in ex.markets else None
        info = (m or {}).get("info") or {}
        status = str(info.get("status", "")).upper()
        # بعض الأزواج موجودة لكن حالتها SETTLING/DELIVERING → Binance يرفض
        # فتح صفقة عليها بخطأ -4140 «Invalid symbol status»
        ok = (bool(m) and m.get("swap", False)
              and status == "TRADING" and m.get("active", True))
    except Exception as e:
        log.warning("تعذّر التحقق من %s على Binance: %s", symbol, e)
        ok = False
    _BINANCE_OK[symbol] = ok
    return ok


# ═══════════════ الموافقة اليدوية على الصفقات ═══════════════
def _exec_mode_tag() -> str:
    if executor is None:
        return "ورقية"
    if getattr(executor, "live", False):
        return "حقيقية 🔴"
    return "Testnet 🟡" if executor.name == "binance" else "ورقية 🟢"


async def on_approval(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """زر ✅ تنفيذ / ❌ تجاهل تحت كل صفقة."""
    q = u.callback_query
    try:
        await q.answer()                     # أوقف دوران الساعة فوراً
    except Exception:
        pass

    chat = q.message.chat_id
    if not allowed(chat):
        try:
            await q.answer("⛔ غير مصرّح", show_alert=True)
        except Exception:
            pass
        return

    data = q.data or ""
    action, _, tok = data.partition(":")
    rec = PENDING.get(tok)

    async def edit(txt: str):
        try:
            # نمرّر لوحة فارغة لإزالة الأزرار بعد الضغط (وإلا تبقى قابلة للنقر)
            await q.edit_message_text(txt, parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup([]))
        except Exception:
            try:
                await q.message.reply_text(txt, parse_mode=ParseMode.HTML)
            except Exception:
                pass

    # زر قديم أو مُستخدَم
    if not rec:
        return await edit("⌛ <b>انتهت صلاحية هذه الصفقة</b>\n"
                          "أُرسلت منذ أكثر من ساعة أو نُفِّذت سابقاً.\n"
                          "استخدم <code>/scan</code> لمسح جديد.")

    sig = rec["sig"]

    # ❌ تجاهل
    if action == "rj":
        PENDING.pop(tok, None)
        return await edit(fmt_signal(sig, settings) + "\n\n❌ <b>تم التجاهل — لم تُنفَّذ</b>")

    # انتهت المدة
    if time.time() > rec["exp"]:
        PENDING.pop(tok, None)
        return await edit(fmt_signal(sig, settings) + "\n\n⌛ <b>انتهت الصلاحية — لم تُنفَّذ</b>")

    if executor is None:
        PENDING.pop(tok, None)
        return await edit("⚠️ المحرّك غير مفعّل — شغّل البوت بوضع <code>approve</code>")

    # ✅ تنفيذ
    await edit(fmt_signal(sig, settings) + "\n\n⏳ <b>جاري التنفيذ على البورصة…</b>")
    try:
        pos = await asyncio.to_thread(executor.open, sig)
    except Exception as e:
        log.exception("فشل التنفيذ من زر الموافقة")
        PENDING.pop(tok, None)
        return await edit(fmt_signal(sig, settings) +
                          f"\n\n❌ <b>فشل التنفيذ</b>\n<code>{esc(type(e).__name__)}: {esc(str(e)[:180])}</code>")

    PENDING.pop(tok, None)

    if pos:
        return await edit(
            fmt_signal(sig, settings) +
            f"\n\n✅ <b>نُفِّذت الصفقة — {_exec_mode_tag()}</b>\n"
            f"<code>{esc(pos.side)} {esc(pos.symbol)}</code>\n"
            f"الكمية: <code>{esc(pos.qty)}</code> @ <code>{esc(pos.entry)}</code>\n"
            f"الستوب على البورصة: <code>{esc(pos.sl)}</code>\n"
            f"المخاطرة: {esc(pos.risk_amount)} USDT")

    return await edit(fmt_signal(sig, settings) +
                      "\n\n⚠️ <b>لم تُفتح الصفقة</b> — راجع "
                      "<code>data/journal.jsonl</code> (غالباً: سقف الصفقات أو مفتاح الإيقاف)")


# ═══════════════ لوحة الأزرار ═══════════════
MENU = ReplyKeyboardMarkup(
    [["🔍 فحص السوق", "📈 صفقات ممكنة"],
     ["🎯 أقرب الفرص"],
     ["📊 حالة السوق", "📉 حالة البوت"],
     ["📋 نتائج الإشارات", "❓ مساعدة"]],
    resize_keyboard=True, is_persistent=True,
    input_field_placeholder="اضغط زراً أو اكتب أمراً…")


_OPP_CACHE: dict = {"t": 0.0, "data": None, "scanned": 0}


def _split(text: str, limit: int = 3800) -> list[str]:
    """يقسّم نصاً طويلاً على حدود الأسطر (تلغرام يرفض أكثر من 4096 حرفاً)."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for ln in text.split("\n"):
        if len(cur) + len(ln) + 1 > limit:
            parts.append(cur); cur = ln
        else:
            cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        parts.append(cur)
    return parts


async def cmd_menu(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("اختَر من الأزرار 👇", reply_markup=MENU)


async def cmd_ping(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """اختبار سرعة الاستجابة — يجب أن يرد فوراً."""
    import time
    await u.message.reply_text(
        f"🏓 البوت حي — {time.strftime('%H:%M:%S')}\n"
        f"الأزواج: {len(scanner._symbols) if scanner else 0} | "
        f"المسحات: {scanner.stats['scans'] if scanner else 0}",
        reply_markup=MENU)


@safe
async def cmd_hotscan(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """🔍 فحص السوق — يمسح كل عقود الفيوتشرز ويعيد «تم»."""
    if hotscanner is None:
        return await u.message.reply_text("الماسح غير مهيّأ — أعد تشغيل البوت.")
    msg = await u.message.reply_text("⏳ <b>بدأ الفحص</b>\nأمسح كل عقود الفيوتشرز…",
                                    parse_mode=ParseMode.HTML)
    try:
        r = await asyncio.wait_for(hotscanner.scan_now(), timeout=300)
    except asyncio.TimeoutError:
        return await msg.edit_text("⌛ الفحص تجاوز 5 دقائق — جرّب بعد قليل.")
    except Exception as e:
        log.exception("فحص السوق: %s", e)
        return await msg.edit_text(f"⚠️ تعذّر الفحص: <code>{esc(str(e)[:160])}</code>",
                                   parse_mode=ParseMode.HTML)
    st = hotscanner.summary()
    fresh = st.get("fresh_count", 0)
    lines = [
        "✅ <b>تم الفحص</b>",
        "──────────────",
        f"فُحص الآن: <b>{r.get('batch', 0)}</b> زوجاً في {r.get('seconds', 0)} ثانية",
        f"إجمالي عقود الفيوتشرز: <b>{st.get('symbols_total', 0)}</b>",
        f"دُرِس حتى الآن: <b>{st.get('symbols_studied', 0)}</b> زوجاً",
        f"صفقات جديدة: <b>{fresh}</b>" + ("  ← اضغط «📈 صفقات ممكنة»" if fresh else ""),
        "",
        "<i>الماسح يعمل في الخلفية كل دقيقتين: الأكثر ضجة + شريحة جديدة من بقية السوق.</i>",
    ]
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


@safe
async def btn_opportunities(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """📈 صفقات ممكنة — الجديدة فقط، لا تتكرر."""
    if hotscanner is None:
        return await u.message.reply_text("الماسح غير مهيّأ — أعد تشغيل البوت.")
    st0 = hotscanner.summary()
    if not st0.get("cycles"):
        msg = await u.message.reply_text("⏳ أول فحص للسوق… انتظر قليلاً")
        try:
            await asyncio.wait_for(hotscanner.scan_now(), timeout=300)
        except Exception as e:
            return await msg.edit_text(f"⚠️ تعذّر الفحص: <code>{esc(str(e)[:150])}</code>",
                                       parse_mode=ParseMode.HTML)
    opps = hotscanner.take_fresh(6)
    st = hotscanner.summary()
    if not opps:
        last = st.get("last_cycle_at") or 0
        when = time.strftime("%H:%M", time.localtime(last)) if last else "—"
        return await u.message.reply_text(
            "🆕 <b>لا صفقات جديدة</b>\n──────────────\n"
            "كل ما وُجد عُرض عليك سابقاً ولم تُغلق شمعة جديدة.\n"
            f"آخر فحص: <b>{when}</b> | دُرِس <b>{st.get('symbols_studied', 0)}</b> "
            f"من <b>{st.get('symbols_total', 0)}</b> زوجاً\n\n"
            "<i>اضغط «🔍 فحص السوق» لفحص فوري.</i>", parse_mode=ParseMode.HTML)
    mood = opp_mod.market_mood(opps)
    head = (f"🆕 <b>صفقات جديدة</b> — لم تُعرض سابقاً\n"
            f"<i>دُرِس {st.get('symbols_studied', 0)} من {st.get('symbols_total', 0)} زوجاً "
            f"· الدورة {st.get('last_cycle_sec', 0)}s</i>\n\n")
    text = head + fmt_opportunities(opps, mood, settings, scanned=st.get("last_batch", 0))
    ok = False
    for chunk in _split(text, 3800):
        try:
            await u.message.reply_text(chunk, parse_mode=ParseMode.HTML, reply_markup=MENU)
            ok = True
        except Exception:
            try:
                await u.message.reply_text(chunk, reply_markup=MENU); ok = True
            except Exception as e2:
                log.warning("تعذّر الإرسال: %s", e2)
    if not ok:
        await u.message.reply_text("⚠️ تعذّر الإرسال — جرّب مرة أخرى.")


async def btn_market(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await cmd_brief(u, c)


def _normalize(t: str) -> str:
    """يحذف الإيموجي والمسافات الزائدة ليصبح المطابق مرناً."""
    import re
    t = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200d]", " ", t or "")
    return " ".join(t.replace("ـ", "").split()).strip().lower()


async def menu_router(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """يحوّل أي نص/زر إلى الإجراء المناسب — ولا يترك رسالة بلا رد أبداً."""
    raw = (u.message.text or "").strip()
    t = _normalize(raw)
    log.info("رسالة من %s: %r", u.effective_chat.id, raw)

    # 1) مطابقة دقيقة لعناوين الأزرار أولاً (يمنع تداخل «أقرب الفرص» مع «صفقات ممكنة»)
    exact = {
        "فحص السوق": cmd_hotscan,
        "صفقات ممكنة": btn_opportunities,
        "اقرب الفرص": cmd_near,
        "حالة السوق": btn_market,
        "حالة البوت": cmd_status,
        "نتائج الاشارات": cmd_stats,
        "مساعدة": cmd_start,
    }
    for key, fn in exact.items():
        if t == key or t.startswith(key):
            await fn(u, c)
            return

    # 2) ثم الكلمات المفتاحية — الأكثر تخصيصاً أولاً
    rules = [
        (("اقرب", "قرب", "near", "جاهز"), cmd_near),
        (("فحص السوق", "فحص", "امسح", "hotscan"), cmd_hotscan),
        (("صفقات", "فرص", "opportun", "توصيات", "دخول"), btn_opportunities),
        (("سوق", "brief", "ملخص", "ميل"), btn_market),
        (("حالة البوت", "status", "احصائ", "إحصائ", "stats2"), cmd_status),
        (("نتائج", "stats", "اداء", "أداء"), cmd_stats),
        (("مساعدة", "help", "start", "بداية", "اوامر", "أوامر"), cmd_start),
        (("ping", "بينغ", "اختبار"), cmd_ping),
    ]
    for keys, fn in rules:
        if any(k in t for k in keys):
            await fn(u, c)
            return

    # لم نتعرف على النص: نرد دائماً بدل الصمت
    await u.message.reply_text(
        f"لم أفهم «{raw[:40]}».\n"
        "اضغط أحد الأزرار 👇 أو استخدم /near أو /stats",
        reply_markup=MENU)


async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "أهلاً 👋\nأنا بوت إشارات العقود الآجلة.\n\n"
        "/status — حالة البوت والإحصاءات\n"
        "/scan — مسح فوري الآن\n"
        "/near — أقرب الفرص ونسبة الجاهزية\n"
        "/stats — أداء الإشارات المُرسَلة فعلياً\n"
        "/brief — ملخص حالة السوق\n"
        "/symbols — الأزواج المراقبة\n"
        "/positions — المراكز المفتوحة\n"
        "/close SYMBOL — إغلاق مركز\n"
        "/panic — إغلاق كل المراكز\n"
        "/risk — حالة إدارة المخاطر\n"
        "/id — معرف المحادثة (ضعه في .env)",
        reply_markup=MENU,
    )


async def cmd_id(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(f"chat_id = <code>{u.effective_chat.id}</code>", parse_mode=ParseMode.HTML)


@safe
async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE):
    log.info("أمر /status من %s", u.effective_chat.id)
    st = scanner.stats
    extra = {"symbols": len(scanner._symbols)}
    if executor is not None:
        extra["open"] = executor.open_count() if hasattr(executor, "open_count") else len(executor.positions())
        mode = "حقيقية 🔴" if getattr(executor, "live", False) else (
            "Testnet 🟡" if executor.name == "binance" else "ورقية 🟢")
        extra["mode"] = f"تنفيذ {mode}"
    else:
        extra["mode"] = "إشارات فقط"
    await u.message.reply_text(fmt_status(st, settings, extra), parse_mode=ParseMode.HTML)


@safe
async def cmd_scan(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("جاري المسح…")
    n = await scanner.scan_once()
    await u.message.reply_text(f"تم. إشارات جديدة: {n}")


@safe
async def cmd_positions(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if executor is None:
        return await u.message.reply_text("التنفيذ الآلي غير مفعّل (mode: signals).")
    txt = fmt_positions(executor.positions(), executor.equity(), executor.risk_status())
    await u.message.reply_text(txt, parse_mode=ParseMode.HTML)


async def cmd_close(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if executor is None or not c.args:
        return await u.message.reply_text("الاستخدام: /close BTC/USDT:USDT")
    ok = executor.close(c.args[0], "أمر من تلجرام")
    await u.message.reply_text("أُغلقت ✅" if ok else "لا مركز بهذا الزوج")


@safe
async def cmd_panic(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if executor is None:
        return await u.message.reply_text("التنفيذ غير مفعّل.")
    n = getattr(executor, "flatten_all", lambda: 0)()
    await u.message.reply_text(f"🛑 إغلاق كلي: {n} مركز")


@safe
async def cmd_risk(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if executor is None:
        return await u.message.reply_text("التنفيذ غير مفعّل.")
    st = executor.risk_status()
    body = "\n".join(f"{esc(k)}: <b>{esc(v)}</b>" for k, v in st.items())
    await u.message.reply_text(
        f"🛡️ <b>إدارة المخاطر</b>\n{body}\n\n"
        f"الحدود: خسارة يومية {settings.max_daily_loss_pct}% | صفقات/يوم {settings.max_trades_per_day}"
        f" | خسائر متتالية {settings.max_consecutive_losses}",
        parse_mode=ParseMode.HTML)



@safe
async def cmd_near(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("أفحص أقرب الفرص…")
    rows = await scanner.nearest(6)
    if not rows:
        return await u.message.reply_text("تعذّر جلب البيانات.")
    lines = ["<b>أقرب الفرص الآن</b>", "──────────────"]
    for r in rows:
        bias = {"LONG": "🟢 صعود", "SHORT": "🔴 هبوط"}.get(r["bias"], "⚪ محايد")
        miss = "، ".join(r["missing"][:2]) if r["missing"] else "لا شيء — إشارة جاهزة"
        lines.append(f"<b>{esc(r['symbol'])}</b> — جاهزية {r['readiness']:.0f}% | {bias}")
        lines.append(f"   ADX {r['adx']} | ER {r['er']} | RSI {r['rsi']} | ATR% {r['atr_pct']}")
        lines.append(f"   ينقص: {esc(miss)}")
    lines += ["", "الجاهزية = نسبة الشروط المتحققة. 100% تعني إشارة صالحة على الشمعة المغلقة."]
    await u.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@safe
async def cmd_stats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("أحدّث نتائج الإشارات…")
    try:
        await tracker.update(scanner.feed)
    except Exception as e:
        log.warning("تحديث المتتبّع: %s", e)
    s = tracker.summary()
    if not s["total"]:
        return await u.message.reply_text(
            "لم تُرسل أي إشارة بعد — لا توجد نتائج للقياس.\n"
            "استخدم /near لمعرفة أقرب الفرص.")
    lines = ["<b>أداء الإشارات المُرسَلة</b>", "──────────────",
             f"الإجمالي: {s['total']} | مغلقة: {s['closed']} | مفتوحة: {s['open']}"]
    if s["closed"]:
        lines += [
            f"نسبة النجاح: <b>{s['win_rate']}%</b>",
            f"متوسط R: <b>{s['avg_r']:+.3f}</b> | مجموع R: {s['sum_r']:+.1f}",
            f"TP1={s['tp1']} TP2={s['tp2']} TP3={s['tp3']} | ستوب={s['stopped']} | منتهية={s['expired']}",
            f"أفضل {s['best']:+.2f}R | أسوأ {s['worst']:+.2f}R",
        ]
        if s["by_setup"]:
            lines += ["", "<b>حسب نوع الدخول</b>"]
            for k, (n, wr, tot) in s["by_setup"].items():
                lines.append(f"  {esc(k)}: {n} صفقة | نجاح {wr}% | {tot:+.1f}R")
    lines += ["", "هذه أرقام إشارات حقيقية أُرسلت من البوت — لا باكتست."]
    await u.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@safe
async def cmd_brief(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("أجهّز ملخص السوق…")
    rows = await scanner.nearest(8)
    longs = sum(1 for r in rows if r["bias"] == "LONG")
    shorts = sum(1 for r in rows if r["bias"] == "SHORT")
    mood = "صاعد" if longs > shorts else ("هابط" if shorts > longs else "محايد")
    lines = [f"<b>ملخص السوق — فريم {esc(settings.timeframe)}</b>", "──────────────",
             f"الميل العام: <b>{mood}</b> ({longs} صعود / {shorts} هبوط)",
             f"الأزواج المراقبة: {len(scanner._symbols)} | آخر مسح: {esc(str(scanner.stats.get('last_scan') or '—'))}",
             "", "<b>الأقرب للدخول</b>"]
    for r in rows[:5]:
        bias = {"LONG": "🟢", "SHORT": "🔴"}.get(r["bias"], "⚪")
        lines.append(f"{bias} <b>{esc(r['symbol'])}</b> {r['readiness']:.0f}%")
    lines.append("\n<i>البوت يرسل الإشارة تلقائياً عند تحقق 100% من الشروط على شمعة مغلقة.</i>")
    await u.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@safe
async def cmd_symbols(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(", ".join(scanner._symbols[:60]))


async def on_error(u: object, c: ContextTypes.DEFAULT_TYPE):
    log.exception("خطأ في معالجة التحديث: %s", c.error)
    try:
        if isinstance(u, Update) and u.effective_chat:
            await c.bot.send_message(u.effective_chat.id,
                                     f"⚠️ حدث خطأ: <code>{esc(type(c.error).__name__)}</code>",
                                     parse_mode=ParseMode.HTML)
    except Exception:
        pass


async def tracker_loop():
    """يحدّث نتائج الإشارات كل 30 دقيقة تلقائياً (بلا تدخل منك)."""
    while True:
        try:
            n = await tracker.update(scanner.feed)
            if n:
                log.info("تحديث نتائج %d إشارة", n)
        except Exception as e:
            log.warning("متتبّع الإشارات: %s", e)
        await asyncio.sleep(1800)


async def post_shutdown(application: Application):
    """إغلاق نظيف: يوقف المهام ويغلق اتصال البورصة (يمنع أخطاء الإيقاف)."""
    for task in asyncio.all_tasks():
        if task is not asyncio.current_task() and not task.done():
            task.cancel()
    try:
        await scanner.close()
    except Exception:
        pass
    log.info("تم الإيقاف النظيف")


async def post_init(application: Application):
    global STARTUP_WARNING
    if STARTUP_WARNING:
        try:
            await application.bot.send_message(settings.chat_id, STARTUP_WARNING,
                                               parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning("تعذّر إرسال تنبيه البدء: %s", e)
        STARTUP_WARNING = None
    asyncio.create_task(scanner.run_forever())
    if hotscanner is not None:
        asyncio.create_task(hotscanner.run_forever())
        log.info("بدأ الماسح في الخلفية")
    asyncio.create_task(tracker_loop())
    if trailer is not None:
        asyncio.create_task(trailer.run_forever())
        log.info("حلقة الستوب المتتبّع بدأت (كل %ds)", settings.scan_interval)


app: Application | None = None
STARTUP_WARNING: str | None = None   # يُرسل بعد بدء التطبيق (app غير جاهز قبله)


def main():
    global app, scanner
    if not settings.bot_token:
        raise SystemExit("ضع TELEGRAM_BOT_TOKEN في ملف .env")
    global executor, trailer, hotscanner
    scanner = Scanner(settings, STRATEGY_NAME, send_signal)
    hotscanner = HotScanner(settings, scanner.feed, scanner.strategy)
    log.info("ماسح السوق الكامل جاهز — %s", scanner.s.exchange)
    if settings.mode in ("autotrade", "approve"):
        kill = KillSwitch(settings.max_daily_loss_pct, settings.max_trades_per_day,
                          settings.max_consecutive_losses)
        # التنفيذ دائماً على Binance Futures — منفصل عن بورصة المسح (قد تكون okx بسبب الحجب)
        if settings.api_key:
            executor = BinanceExecutor(settings, kill_switch=kill)
            log.info("منفّذ Binance — %s | المسح من %s",
                     "LIVE" if executor.live else "TESTNET", settings.exchange)
        else:
            executor = PaperExecutor(settings, scanner.feed, kill_switch=kill,
                                     balance=settings.paper_balance)
            # ⚠️ فخّ شائع: وضع autotrade + بورصة ليست binanceusdm (مثل EXCHANGE=okx)
            # يعني تنفيذاً ورقياً وليس أوامر حقيقية على Binance — يجب أن يكون صريحاً.
            if not settings.api_key:
                msg = ("⚠️ <b>تنفيذ ورقي — لا مفاتيح API</b>\n"
                       "أضف <code>EXCHANGE_API_KEY</code> و<code>EXCHANGE_API_SECRET</code> في .env")
                log.warning("لا مفاتيح API — تشغيل ورقي (Paper) بمبلغ %.0f USDT", settings.paper_balance)
            else:
                msg = None
            if msg:
                global STARTUP_WARNING
                STARTUP_WARNING = msg
        trailer = TrailingManager(executor, scanner.feed, settings.scan_interval)
    app = Application.builder().token(settings.bot_token).post_init(post_init)\
        .concurrent_updates(True).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("scan", cmd_hotscan))
    app.add_handler(CommandHandler("symbols", cmd_symbols))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("panic", cmd_panic))
    app.add_handler(CommandHandler("risk", cmd_risk))
    app.add_handler(CommandHandler("near", cmd_near))
    app.add_handler(CommandHandler("best", cmd_near))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("brief", cmd_brief))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("demo", cmd_demo))
    app.add_handler(CallbackQueryHandler(on_approval, pattern=r"^(ap|rj):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))
    app.add_error_handler(on_error)
    log.info("البوت يعمل…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
