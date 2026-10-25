"""بوت تلجرام: أوامر + إرسال الإشارات + تشغيل المحرّك مع البوت في نفس الحلقة."""
import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from config import Settings
from engine import Scanner
from executor import BinanceExecutor, PaperExecutor, TrailingManager
from risk import KillSwitch
from strategy import Signal
from formatting import fmt_signal

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s | %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

settings = Settings.load()
STRATEGY_NAME = None   # من config.yaml
scanner: Scanner | None = None
executor = None            # BinanceExecutor أو PaperExecutor (حسب الوضع)
trailer: TrailingManager | None = None


async def send_signal(sig: Signal):
    await app.bot.send_message(settings.chat_id, fmt_signal(sig, settings), parse_mode=ParseMode.MARKDOWN)
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
            parse_mode=ParseMode.MARKDOWN)
    else:
        await app.bot.send_message(settings.chat_id, "⚙️ لم تُنفَّذ الصفقة — راجع `data/journal.jsonl`")


async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "أهلاً 👋\nأنا بوت إشارات العقود الآجلة.\n\n"
        "/status — حالة البوت والإحصاءات\n"
        "/scan — مسح فوري الآن\n"
        "/symbols — الأزواج المراقبة\n"
        "/positions — المراكز المفتوحة\n"
        "/close SYMBOL — إغلاق مركز\n"
        "/panic — إغلاق كل المراكز\n"
        "/risk — حالة إدارة المخاطر\n"
        "/id — معرف المحادثة (ضعه في .env)"
    )


async def cmd_id(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(f"chat_id = `{u.effective_chat.id}`", parse_mode=ParseMode.MARKDOWN)


async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE):
    st = scanner.stats
    await u.message.reply_text(
        f"الاستراتيجية: {STRATEGY_NAME}\n"
        f"البورصة: {settings.exchange} | الفريم: {settings.timeframe}\n"
        f"عدد الأزواج: {len(scanner._symbols)}\n"
        f"صفقات مفتوحة: {scanner.open_count()}/{settings.max_open_positions}\n"
        f"عمليات المسح: {st['scans']} | الإشارات: {st['signals']}\n"
        f"آخر مسح: {st['last_scan'] or '—'}"
    )


async def cmd_scan(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("جاري المسح…")
    n = await scanner.scan_once()
    await u.message.reply_text(f"تم. إشارات جديدة: {n}")


async def cmd_positions(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if executor is None:
        return await u.message.reply_text("التنفيذ الآلي غير مفعّل (mode: signals).")
    pos = executor.positions()
    if not pos:
        return await u.message.reply_text(f"لا مراكز مفتوحة.\nالرصيد: {executor.equity():.2f} USDT")
    lines = [f" الرصيد: {executor.equity():.2f} USDT | المراكز: {len(pos)}"]
    for p in pos:
        lines.append(f"• {p.symbol} {p.side} | {p.qty} @ {p.entry} | ستوب {p.sl:.6g}")
    st = executor.risk_status()
    lines.append(f"مفتاح الإيقاف: {st['موقوف']}")
    await u.message.reply_text("\n".join(lines))


async def cmd_close(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if executor is None or not c.args:
        return await u.message.reply_text("الاستخدام: /close BTC/USDT:USDT")
    ok = executor.close(c.args[0], "أمر من تلجرام")
    await u.message.reply_text("أُغلقت ✅" if ok else "لا مركز بهذا الزوج")


async def cmd_panic(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if executor is None:
        return await u.message.reply_text("التنفيذ غير مفعّل.")
    n = getattr(executor, "flatten_all", lambda: 0)()
    await u.message.reply_text(f"🛑 إغلاق كلي: {n} مركز")


async def cmd_risk(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if executor is None:
        return await u.message.reply_text("التنفيذ غير مفعّل.")
    st = executor.risk_status()
    await u.message.reply_text(
        "🛡️ إدارة المخاطر\n" + "\n".join(f"{k}: {v}" for k, v in st.items()) +
        f"\nحدود: خسارة يومية {settings.max_daily_loss_pct}% | صفقات/يوم {settings.max_trades_per_day}" +
        f" | خسائر متتالية {settings.max_consecutive_losses}")


async def cmd_symbols(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(", ".join(scanner._symbols[:60]))


async def post_init(application: Application):
    asyncio.create_task(scanner.run_forever())
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
    app = Application.builder().token(settings.bot_token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("symbols", cmd_symbols))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("panic", cmd_panic))
    app.add_handler(CommandHandler("risk", cmd_risk))
    log.info("البوت يعمل…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
