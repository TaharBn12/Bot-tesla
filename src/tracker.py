"""
تتبّع نتائج الإشارات — هذا ما يجعل البوت «قابلاً للقياس» بدل مجرد ادعاءات.

كل إشارة تُرسل تُسجَّل في data/signals.jsonl بحالة pending، ثم تُحدَّث تلقائياً
عند اكتمال الشموع: هل ضرب الستوب؟ أم حقق الهدف الأول/الثاني/الثالث؟

    python src/tracker.py update     # تحديث الحالات من بيانات السوق
    python src/tracker.py stats      # تقرير الأداء الحقيقي للإشارات المُرسَلة
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "data" / "signals.jsonl"
MAX_BARS = 250          # أقصى عمر للإشارة قبل اعتبارها منتهية


def log_signal(sig, timeframe: str, extra: dict | None = None) -> dict:
    """يسجّل إشارة جديدة بحالة pending (يُستدعى من bot.py عند الإرسال)."""
    row = {
        "id": f"{sig.symbol}|{sig.side}|{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol": sig.symbol, "side": sig.side, "entry": float(sig.entry),
        "sl": float(sig.stop_loss), "tp": [float(x) for x in sig.take_profits],
        "atr": float(getattr(sig, "atr", 0) or 0), "setup": getattr(sig, "setup", ""),
        "score": float(getattr(sig, "score", 0) or 0), "timeframe": timeframe,
        "status": "pending", "r": None, "closed_at": None, "bars": 0,
        **(extra or {}),
    }
    FILE.parent.mkdir(exist_ok=True)
    with FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def load() -> list[dict]:
    if not FILE.exists():
        return []
    out = []
    for line in FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def save(rows: list[dict]):
    FILE.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")


def _evaluate(row: dict, df: pd.DataFrame) -> dict | None:
    """يحدد نتيجة الإشارة من الشموع التي جاءت بعد وقتها."""
    from datetime import datetime as dt
    opened = dt.fromisoformat(row["ts"])
    df = df[df["datetime"] >= opened].reset_index(drop=True)
    if len(df) < 2:
        return None
    side, entry, sl, tps = row["side"], row["entry"], row["sl"], row["tp"]
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    for i, bar in df.iterrows():
        hi, lo = float(bar["high"]), float(bar["low"])
        hit_sl = (lo <= sl) if side == "LONG" else (hi >= sl)
        if hit_sl:
            return {"status": "stopped", "r": -1.0, "bars": int(i) + 1,
                    "closed_at": str(bar["datetime"])}
        for k, tp in enumerate(tps):
            hit = (hi >= tp) if side == "LONG" else (lo <= tp)
            if hit:
                # R المتحقق لو خرج عند هذا الهدف
                r = [1.0, 2.0, 3.0][k] if k < 3 else 0
                return {"status": f"tp{k + 1}", "r": r, "bars": int(i) + 1,
                        "closed_at": str(bar["datetime"])}
        if i + 1 >= MAX_BARS:
            px = float(bar["close"])
            r = ((px - entry) if side == "LONG" else (entry - px)) / risk
            return {"status": "expired", "r": round(r, 3), "bars": int(i) + 1,
                    "closed_at": str(bar["datetime"])}
    return None


async def update(feed, min_symbols: int | None = None) -> int:
    rows = load()
    pending = [r for r in rows if r["status"] == "pending"]
    if not pending:
        return 0
    updated = 0
    cache: dict[str, pd.DataFrame] = {}
    for row in pending:
        sym, tf = row["symbol"], row.get("timeframe", "4h")
        if sym not in cache:
            try:
                cache[sym] = await feed.candles(sym, tf, 300)
            except Exception:
                continue
        res = _evaluate(row, cache[sym])
        if res:
            row.update(res)
            updated += 1
    if updated:
        # إعادة بناء الملف: الأول pending كما هي، والباقي محدّث
        save(rows)
    return updated


def summary() -> dict:
    rows = load()
    closed = [r for r in rows if r["status"] != "pending" and r["r"] is not None]
    open_ = [r for r in rows if r["status"] == "pending"]
    if not closed:
        return {"total": len(rows), "closed": 0, "open": len(open_),
                "win_rate": 0, "avg_r": 0, "sum_r": 0, "best": 0, "worst": 0,
                "by_symbol": {}, "by_setup": {}}
    r = pd.Series([x["r"] for x in closed])
    wins = r[r > 0]
    def agg(key):
        out = {}
        for x in closed:
            out.setdefault(x.get(key) or "—", []).append(x["r"])
        return {k: (len(v), round(sum(1 for i in v if i > 0) / len(v) * 100, 1), round(sum(v), 1))
                for k, v in out.items()}
    return {
        "total": len(rows), "closed": len(closed), "open": len(open_),
        "win_rate": round(len(wins) / len(closed) * 100, 1),
        "avg_r": round(float(r.mean()), 3), "sum_r": round(float(r.sum()), 1),
        "best": round(float(r.max()), 2), "worst": round(float(r.min()), 2),
        "by_symbol": agg("symbol"), "by_setup": agg("setup"),
        "tp1": sum(1 for x in closed if x["status"] == "tp1"),
        "tp2": sum(1 for x in closed if x["status"] == "tp2"),
        "tp3": sum(1 for x in closed if x["status"] == "tp3"),
        "stopped": sum(1 for x in closed if x["status"] == "stopped"),
        "expired": sum(1 for x in closed if x["status"] == "expired"),
    }


if __name__ == "__main__":
    import asyncio
    from config import Settings
    from datafeed import DataFeed

    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "update":
        async def _run():
            s = Settings.load()
            feed = DataFeed(s.exchange)
            n = await update(feed)
            await feed.close()
            print(f"تم تحديث {n} إشارة")
        asyncio.run(_run())

    s = summary()
    print("═" * 52)
    print("أداء الإشارات المُرسَلة فعلياً")
    print("═" * 52)
    print(f"الإجمالي: {s['total']} | مغلقة: {s['closed']} | مفتوحة: {s['open']}")
    if s["closed"]:
        print(f"نسبة النجاح: {s['win_rate']}% | متوسط R: {s['avg_r']:+.3f} | مجموع R: {s['sum_r']:+.1f}")
        print(f"أهداف: TP1={s['tp1']} TP2={s['tp2']} TP3={s['tp3']} | ستوب={s['stopped']} | منتهية={s['expired']}")
        print(f"أفضل: {s['best']:+.2f}R | أسوأ: {s['worst']:+.2f}R")
