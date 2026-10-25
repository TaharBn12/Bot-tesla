"""تنسيق رسائل تلجرام بصيغة HTML — آمنة مع الرموز الخاصة (_ * ` [ ])."""
from html import escape

from strategy import Signal


def esc(x) -> str:
    return escape(str(x), quote=False)


def fmt_signal(sig: Signal, settings) -> str:
    arrow = "🟢 <b>LONG</b>" if sig.side == "LONG" else "🔴 <b>SHORT</b>"
    tps = " / ".join(f"<code>{esc(p)}</code>" for p in sig.take_profits)
    risk = abs(sig.entry - sig.stop_loss)
    rr = ""
    if risk > 0 and sig.take_profits:
        rr = f" (R:R ≈ 1:{abs(sig.take_profits[-1] - sig.entry) / risk:.1f})"

    risk_pct = risk / sig.entry * 100 if sig.entry else 0
    trail = "أعلى قمة − 3.5×ATR" if sig.side == "LONG" else "أدنى قاع + 3.5×ATR"

    lines = [
        f"{arrow}  <b>{esc(sig.symbol)}</b>",
        "──────────────",
        f"الدخول: <code>{esc(sig.entry)}</code>",
        f"الستوب: <code>{esc(sig.stop_loss)}</code>  ({risk_pct:.2f}%)",
        f"الأهداف: {tps}{rr}",
        f"الرافعة المقترحة: {sig.leverage}x | الفريم: {esc(settings.timeframe)}",
    ]
    if sig.atr:
        lines.append(f"ATR: <code>{sig.atr:.6g}</code> | الستوب المتتبّع: {trail}")
    if sig.reason:
        lines.append(f"السبب: {esc(sig.reason)}")
    lines.append(f"⚠️ مخاطرة {settings.risk_per_trade_pct}% من الحساب | أقصى {settings.max_open_positions} صفقات متزامنة")
    return "\n".join(lines)


def fmt_positions(pos: list, equity: float, kill: dict) -> str:
    if not pos:
        return f"لا مراكز مفتوحة.\nالرصيد: <code>{equity:.2f}</code> USDT"
    lines = [f"الرصيد: <code>{equity:.2f}</code> USDT | المراكز: {len(pos)}", ""]
    for p in pos:
        tag = "🟢" if p.side == "LONG" else "🔴"
        lines.append(f"{tag} <b>{esc(p.symbol)}</b> {esc(p.side)}")
        lines.append(f"   كمية <code>{esc(p.qty)}</code> @ <code>{esc(p.entry)}</code>")
        lines.append(f"   ستوب <code>{p.sl:.6g}</code> | مخاطرة {p.risk_amount} USDT")
    lines += ["", f"🛡️ مفتاح الإيقاف: {esc(kill.get('موقوف', '—'))}"]
    return "\n".join(lines)


def fmt_status(stats: dict, settings, extra: dict | None = None) -> str:
    extra = extra or {}
    lines = [
        "<b>حالة البوت</b>",
        "──────────────",
        f"الاستراتيجية: <code>{esc(settings.strategy)}</code>",
        f"البورصة: <code>{esc(settings.exchange)}</code> | الفريم: <code>{esc(settings.timeframe)}</code>",
        f"عدد الأزواج: {extra.get('symbols', 0)}",
        f"عمليات المسح: {stats.get('scans', 0)} | الإشارات: {stats.get('signals', 0)}",
        f"آخر مسح: {esc(stats.get('last_scan') or '—')}",
    ]
    if "open" in extra:
        lines.append(f"صفقات مفتوحة: {extra['open']}/{settings.max_open_positions}")
    if "mode" in extra:
        lines.append(f"الوضع: {esc(extra['mode'])}")
    return "\n".join(lines)
