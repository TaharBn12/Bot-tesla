"""
ماسح السوق الكامل — يعمل في الخلفية طوال حياة البوت.

الفكرة:
  1) يجلب تيكرات **كل** عقود الفيوتشرز (نداء واحد) كل دورة.
  2) يحسب «الضجة» لكل زوج: حجم التداول × قوة الحركة.
  3) يفحص بعمق دفعة من الأزواج: الأكثر ضجة + شريحة متغيّرة (دوران)
     حتى تُدرَس **كل** العملات على مدى حياة البوت، لا الأربعين الأعلى سيولة فقط.
  4) يخزّن النتائج في الذاكرة + data/hotscan.json.
  5) منع التكرار: مفتاح الفرصة = (الزوج، الاتجاه، وقت الشمعة).
     نفس الفرصة لا تُعرض مرتين أبداً؛ تظهر فقط حين تُغلق شمعة جديدة.

    hs = HotScanner(settings, feed, strategy)
    await hs.run_forever()          # في الخلفية
    stats = await hs.scan_now()     # زر «فحص»
    fresh = hs.take_fresh(8)        # زر «صفقات ممكنة» — الجديد فقط
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import opportunities as opp_mod

log = logging.getLogger("hotscan")
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE = DATA / "hotscan.json"
SEEN = DATA / "hotscan_seen.json"

SEEN_TTL_HOURS = 72          # نحتفظ بالمفاتيح 3 أيام ثم نحذفها


class HotScanner:
    def __init__(self, settings, feed, strategy):
        self.s = settings
        self.feed = feed
        self.strategy = strategy

        self.rows: dict[str, dict] = {}      # symbol → آخر قراءة
        self.seen: dict[str, float] = {}     # opp_key → آخر عرض (epoch)
        self.fresh: list = []                # فرص جديدة لم تُعرض بعد
        self._cursor = 0                     # مؤشر الدوران
        self._lock = asyncio.Lock()

        self.stats = {
            "cycles": 0,
            "symbols_total": 0,      # كل العقود المتاحة
            "symbols_studied": 0,    # عدد ما دُرِس حتى الآن (تراكمي)
            "last_cycle_at": 0.0,
            "last_cycle_sec": 0.0,
            "last_batch": 0,
            "fresh_count": 0,
        }
        self._load()

    # ─────────────────────────────── التخزين ───────────────────────────────
    def _load(self):
        try:
            DATA.mkdir(exist_ok=True)
            if STATE.exists():
                d = json.loads(STATE.read_text(encoding="utf-8"))
                self.rows = d.get("rows", {})
                self.stats["symbols_studied"] = d.get("symbols_studied", 0)
                self._cursor = d.get("cursor", 0)
            if SEEN.exists():
                cutoff = time.time() - SEEN_TTL_HOURS * 3600
                self.seen = {k: v for k, v in json.loads(
                    SEEN.read_text(encoding="utf-8")).items() if v >= cutoff}
        except Exception as e:
            log.warning("تعذّر تحميل حالة الماسح: %s", e)

    def _save(self):
        try:
            DATA.mkdir(exist_ok=True)
            tmp = STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "rows": self.rows,
                "symbols_studied": self.stats["symbols_studied"],
                "cursor": self._cursor,
                "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }, ensure_ascii=False), encoding="utf-8")
            tmp.replace(STATE)
            tmp2 = SEEN.with_suffix(".tmp")
            tmp2.write_text(json.dumps(self.seen), encoding="utf-8")
            tmp2.replace(SEEN)
        except Exception as e:
            log.warning("تعذّر حفظ حالة الماسح: %s", e)

    # ─────────────────────────────── الكون ───────────────────────────────
    async def universe(self) -> list[str]:
        """كل عقود الفيوتشرز القابلة للتداول (USDT) — لا حد أعلى."""
        ex = self.feed.ex
        markets = await ex.load_markets()
        out = []
        for sym, m in markets.items():
            if not m.get("swap") or m.get("quote") != "USDT":
                continue
            if not m.get("active", True):
                continue
            # بعض البورصات تُعيد الأزواج بحالة SETTLING/DELIVERING
            st = str((m.get("info") or {}).get("status", "TRADING")).upper()
            if st not in ("TRADING", "OPEN", ""):
                continue
            out.append(sym)
        return sorted(out)

    @staticmethod
    def qvol(t: dict) -> float:
        """حجم التداول بالدولار — OKX لا يرسل quoteVolume إطلاقاً."""
        qv = float(t.get("quoteVolume") or 0)
        if qv > 0:
            return qv
        last = float(t.get("last") or t.get("close") or 0)
        info = t.get("info") or {}
        # OKX: volCcy24h بالعملة الأساسية → نضربه بالسعر
        for k in ("volCcy24h", "volCcyQuote24h", "quoteVolume", "quoteVol"):
            try:
                v = float(info.get(k) or 0)
                if v > 0:
                    # volCcyQuote24h بالدولار أصلاً
                    return v if "Quote" in k or "quote" in k else v * last
            except (TypeError, ValueError):
                continue
        base = float(t.get("baseVolume") or 0)
        return base * last if base > 0 else 0.0

    @classmethod
    def heat(cls, t: dict) -> float:
        """درجة «الضجة»: حجم التداول × قوة الحركة السعرية."""
        qv = cls.qvol(t)
        pct = abs(float(t.get("percentage") or 0))
        if qv <= 0:
            return 0.0
        # اللوغاريتم يمنع الأزواج العملاقة من ابتلاع القائمة كلها
        return math.log10(qv + 10) * (1.0 + pct / 3.0)

    # ─────────────────────────────── دورة ───────────────────────────────
    async def cycle(self, source: str = "background") -> dict:
        log.info("بدء دورة (%s)", source)
        async with self._lock:
            t0 = time.time()
            syms = await self.universe()
            self.stats["symbols_total"] = len(syms)
            if not syms:
                return {"error": "لا أزواج"}

            try:
                tick = await self.feed.tickers()
            except Exception as e:
                log.warning("تعذّر جلب التيكرات: %s", e)
                tick = {}

            # ترتيب كل السوق بالضجة
            scored = []
            for sym in syms:
                t = tick.get(sym) or {}
                qv = self.qvol(t)
                if qv < float(getattr(self.s, "heat_min_volume", 1_000_000)):
                    continue
                scored.append((sym, self.heat(t), t))
            scored.sort(key=lambda r: r[1], reverse=True)
            if not scored:
                scored = [(s, 0.0, tick.get(s) or {}) for s in syms]

            batch_size = int(getattr(self.s, "hot_batch_size", 60))
            hot = [r[0] for r in scored[:batch_size]]
            # الدوران: بقية السوق تُفحص على دفعات عبر حياة البوت
            rest = [r[0] for r in scored[batch_size:]]
            rotate_n = int(getattr(self.s, "hot_rotate_size", 40))
            if rest:
                start = self._cursor % len(rest)
                rot = (rest + rest)[start:start + rotate_n]
                self._cursor += rotate_n
            else:
                rot = []
            batch = list(dict.fromkeys(hot + rot))

            sem = asyncio.Semaphore(int(getattr(self.s, "hot_concurrency", 8)))

            async def work(sym):
                async with sem:
                    try:
                        df = await self.feed.candles(sym, self.s.timeframe, 400)
                        if df is None or len(df) < 60:
                            return None
                        o = await asyncio.to_thread(
                            opp_mod.build, sym, df, self.strategy,
                            getattr(self.s, "paper_balance", 1000.0),
                            self.s.risk_per_trade_pct, self.s.leverage,
                            float(getattr(self.s, "hot_min_readiness", 60.0)))
                        candle_ts = int(df.iloc[-1]["ts"] // 1000)
                        t = tick.get(sym) or {}
                        row = {
                            "symbol": sym,
                            "price": float(df.iloc[-1]["close"]),
                            "chg": float(t.get("percentage") or 0),
                            "qvol": round(self.qvol(t) / 1e6, 2),   # بالمليون دولار
                            "heat": round(self.heat(t), 2),
                            "candle": candle_ts,
                            "readiness": (o.readiness if o else 0),
                            "grade": (o.grade if o else ""),
                            "side": (o.side if o else ""),
                            "ts": int(t0),
                        }
                        self.rows[sym] = row
                        if o:
                            o._key = f"{sym}|{o.side}|{candle_ts}"
                            o._candle = candle_ts
                            return o
                        return None
                    except Exception as e:
                        log.debug("%s: %s", sym, e)
                        return None

            found = [o for o in await asyncio.gather(*(work(s) for s in batch)) if o]

            # منع التكرار: الفرصة تُعرض مرة واحدة لكل شمعة/اتجاه
            new = []
            now = time.time()
            for o in found:
                if o._key in self.seen:
                    continue
                self.seen[o._key] = now
                new.append(o)

            self.fresh = new
            self.stats.update({
                "cycles": self.stats["cycles"] + 1,
                "last_cycle_at": now,
                "last_cycle_sec": round(time.time() - t0, 1),
                "last_batch": len(batch),
                "fresh_count": len(new),
                "symbols_studied": len(self.rows),
            })

            # ترتيب: الأقوى جودة أولاً
            order = {"A": 0, "B": 1, "C": 2}
            new.sort(key=lambda o: (order.get(o.grade, 9), -o.readiness))
            self.fresh = new
            self._save()
            log.info("دورة مسح: %d زوجاً في %.1fs — جديد: %d",
                     len(batch), time.time() - t0, len(new))
            return {
                "batch": len(batch),
                "total": int(self.stats.get("symbols_total") or 0),
                "seconds": round(time.time() - t0, 1),
                "fresh": len(new),
            }

    async def scan_now(self) -> dict:
        return await self.cycle(source="زر/يدوي")

    async def run_forever(self):
        await asyncio.sleep(5)          # لا نزاحم بدء التشغيل
        while True:
            try:
                await self.cycle()
            except Exception as e:
                log.exception("خطأ في الماسح: %s", e)
            wait = int(getattr(self.s, "hotscan_interval", 120))
            log.info("انتظار %ds قبل الدورة التالية", wait)
            await asyncio.sleep(wait)

    # ─────────────────────────────── العرض ───────────────────────────────
    def take_fresh(self, limit: int = 8) -> list:
        """يُعيد الفرص الجديدة ويُفرّغها (فلا تتكرر عند الضغط التالي)."""
        out = self.fresh[:limit]
        self.fresh = self.fresh[limit:]
        self.stats["fresh_count"] = len(self.fresh)
        return out

    def top_heat(self, n: int = 10) -> list[dict]:
        rows = sorted(self.rows.values(), key=lambda r: r.get("heat", 0), reverse=True)
        return rows[:n]

    def summary(self) -> dict:
        return dict(self.stats)
