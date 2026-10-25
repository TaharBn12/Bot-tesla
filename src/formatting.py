"""تنسيق رسالة الإشارة (منفصل عن تلجرام ليمكن اختباره بدون توكن)."""
from strategy import Signal


def fmt_signal(sig: Signal, settings) -> str:
    arrow = "🟢 LONG" if sig.side == "LONG" else "🔴 SHORT"
    tps = " / ".join(f"`{p}`" for p in sig.take_profits)
    rr = ""
    risk = abs(sig.entry - sig.stop_loss)
    if risk > 0 and sig.take_profits:
        rr = f" (R:R ≈ 1:{abs(sig.take_profits[-1] - sig.entry) / risk:.1f})"
    return (
        f"{arrow}  *{sig.symbol}*\n"
        f"──────────────\n"
        f"الدخول: `{sig.entry}`\n"
        f"الستوب: `{sig.stop_loss}`\n"
        f"الأهداف: {tps}{rr}\n"
        f"الرافعة المقترحة: {sig.leverage}x\n"
        f"الفريم: {settings.timeframe}\n"
        + (f"ATR: `{sig.atr:.6g}` | الستوب المتتبَّع يتحرك مع "
           f"{'أعلى قمة − 3.5×ATR' if sig.side == 'LONG' else 'أدنى قاع + 3.5×ATR'}\n" if sig.atr else "")
        + f"السبب: {sig.reason}\n"
        f"⚠️ إدارة رأس المال: مخاطرة {settings.risk_per_trade_pct}% | لا تُدخل أكثر من صفقتين في نفس الوقت"
    )
