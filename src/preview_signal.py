"""معاينة شكل رسالة الإشارة بدون تشغيل البوت:
    python src/preview_signal.py
"""
from config import Settings
from formatting import fmt_signal
from strategy import Signal

if __name__ == "__main__":
    s = Settings.load()
    demo = Signal(
        symbol="BTC/USDT:USDT", side="LONG", entry=110250.5,
        stop_loss=108900.0, take_profits=[111600.0, 112950.0, 114300.0],
        leverage=s.leverage, atr=1420.0, reason="EMA21 cross EMA55 + فوق EMA200 + حجم 1.6x", score=1.42,
    )
    print(fmt_signal(demo, s))
