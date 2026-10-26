"""بوت تلجرام: أوامر + إرسال الإشارات + تشغيل المحرّك مع البوت في نفس الحلقة."""
import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram import ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config import Settings
from engine import Scanner
from executor import BinanceExecutor, PaperExecutor, TrailingManager
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
    await app.bot.send_message(settings.chat_id, fmt_signal(sig, settings), parse_mode=ParseMode.HTML)
    tracker.log_signal(sig, settings.timeframe)
    log.info("إشارة: %s %s", sig.side, sig.symbol)

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



# ═══════════════ لوحة الأزرار ═══════════════
MENU = ReplyKeyboardMarkup(
    [["📈 صفقات ممكنة", "🎯 أقرب الفرص"],
     ["📊 حالة السوق", "📉 حالة البوت"],
     ["📋 نتائج الإشارات", "❓ مساعدة"]],
    resize_keyboard=True, is_persistent=True,
    input_field_placeholder="اضغط زراً أو اكتب أمراً…")


_OPP_CACHE: dict = {"t": 0.0, "data": None, "scanned": 0}


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
async def btn_opportunities(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """الزر الأهم: الصفقات الممكنة الدخول فيها الآن."""
    import time
    # رد فوري حتى لا يبدو البوت معلقاً
    msg = await u.message.reply_text("⏳ أفحص السوق الآن… (ثوانٍ قليلة)")

    # نتيجة محفوظة حديثاً؟ استخدمها فوراً
    if _OPP_CACHE["data"] is not None and time.time() - _OPP_CACHE["t"] < 45:
        opps, scanned = _OPP_CACHE["data"], _OPP_CACHE["scanned"]
        note = "\n<i>(نتيجة محفوظة قبل أقل من دقيقة — اضغط مرة أخرى بعد قليل للتحديث)</i>"
    else:
        try:
            opps = await asyncio.wait_for(
                scanner.opportunities(min_readiness=66.0, limit=6), timeout=90)
            scanned = len(scanner._symbols)
            _OPP_CACHE.update({"t": time.time(), "data": opps, "scanned": scanned})
            note = ""
        except asyncio.TimeoutError:
            await msg.edit_text("⌛ الفحص أخذ وقتاً أطول من المتوقع (تحميل البورصة). جرّب مرة أخرى.")
            return
        except Exception as e:
            log.exception("فرص: %s", e)
            await msg.edit_text(
                f"⚠️ تعذّر الفحص: <code>{esc(type(e).__name__)}</code>\n{esc(str(e)[:150])}",
                parse_mode=ParseMode.HTML)
            return

    mood = opp_mod.market_mood(opps)
    text = fmt_opportunities(opps, mood, settings, scanned=scanned) + note
    try:
        # ملاحظة: edit_text لا يقبل لوحة أزرار عادية — نعدّل النص فقط
        await msg.edit_text(text, parse_mode=ParseMode.HTML)
    except Exception:
        # إن فشل التعديل (نص طويل/قديم) نرسل رسالة جديدة
        await u.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=MENU)


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
    asyncio.create_task(scanner.run_forever())
    asyncio.create_task(tracker_loop())
    if trailer is not None:
        asyncio.create_task(trailer.run_forever())
        log.info("حلقة الستوب المتتبّع بدأت (كل %ds)", settings.scan_interval)


app: Application | None = None


def main():
    global app, scanner
    if not settings.bot_token:
        raise SystemExit("ضع TELEGRAM_BOT_TOKEN في ملف .env")
    global executor, trailer
    scanner = Scanner(settings, STRATEGY_NAME, send_signal)
    if settings.mode == "autotrade":
        kill = KillSwitch(settings.max_daily_loss_pct, settings.max_trades_per_day,
                          settings.max_consecutive_losses)
        if settings.exchange == "binanceusdm" and settings.api_key:
            executor = BinanceExecutor(settings, kill_switch=kill)
            log.info("منفّذ Binance — %s", "LIVE" if executor.live else "TESTNET")
        else:
            executor = PaperExecutor(settings, scanner.feed, kill_switch=kill,
                                     balance=settings.paper_balance)
            log.warning("لا مفاتيح API — تشغيل ورقي (Paper) بمبلغ %.0f USDT", settings.paper_balance)
        trailer = TrailingManager(executor, scanner.feed, settings.scan_interval)
    app = Application.builder().token(settings.bot_token).post_init(post_init)\
        .concurrent_updates(True).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("scan", cmd_scan))
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))
    app.add_error_handler(on_error)
    log.info("البوت يعمل…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
