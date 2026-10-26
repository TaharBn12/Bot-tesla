"""تحميل الإعدادات من .env و config.yaml"""
import os
from dataclasses import dataclass, field
from pathlib import Path
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass
class Settings:
    bot_token: str
    chat_id: str
    allowed_chats: list = field(default_factory=list)
    exchange: str = "binanceusdm"      # binanceusdm | bybit | okx
    timeframe: str = "15m"
    scan_interval: int = 60            # كل كم ثانية يعيد المسح
    cooldown_minutes: int = 120        # منع تكرار نفس الإشارة لنفس الزوج
    max_signals_per_scan: int = 5
    max_open_positions: int = 3        # سقف الصفقات المتزامنة (أثبت أنه يقلل التراجع)
    signal_expiry_hours: int = 72      # بعدها تُعتبر الإشارة منتهية
    universe_size: int = 40       # عدد الأزواج في الوضع المكثف (عند ترك symbols فارغة)
    symbols: list = field(default_factory=list)
    leverage: int = 10
    risk_per_trade_pct: float = 1.0
    mode: str = "signals"              # signals | autotrade
    strategy: str = "trend_pulse"
    strategy_params: dict = field(default_factory=dict)
    # ---- التنفيذ الآلي ----
    dry_run: bool = True
    paper_balance: float = 1000.0
    trail_atr: float = 3.5
    max_daily_loss_pct: float = 3.0
    max_trades_per_day: int = 6
    max_consecutive_losses: int = 4
    api_key: str = ""
    api_secret: str = ""

    @classmethod
    def load(cls) -> "Settings":
        cfg_path = ROOT / "config.yaml"
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        s = cls(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", raw.get("bot_token", "")),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", str(raw.get("chat_id", ""))),
        )
        for k in ("exchange", "timeframe", "scan_interval", "cooldown_minutes",
                  "max_signals_per_scan", "max_open_positions", "signal_expiry_hours", "universe_size",
                  "dry_run", "paper_balance", "trail_atr", "max_daily_loss_pct",
                  "max_trades_per_day", "max_consecutive_losses", "symbols", "leverage", "strategy", "strategy_params",
                  "risk_per_trade_pct", "mode"):
            if k in raw and raw[k] is not None:
                setattr(s, k, raw[k])
        if os.getenv("EXCHANGE"):        # تجاوز سريع للاختبار
            s.exchange = os.getenv("EXCHANGE")
        raw_allowed = os.getenv("ALLOWED_CHAT_IDS", "")
        s.allowed_chats = [x.strip() for x in raw_allowed.split(",") if x.strip()] or [str(s.chat_id)]
        s.api_key = os.getenv("EXCHANGE_API_KEY", "")
        s.api_secret = os.getenv("EXCHANGE_API_SECRET", "")
        s.symbols = list(s.symbols or [])   # فارغة = وضع ديناميكي (أعلى سيولة)
        return s
