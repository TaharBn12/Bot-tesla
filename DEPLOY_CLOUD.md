# النشر السحابي — Railway / Fly.io

> **الهدف:** بوت يرد عليك 24/7 بلا توقف. المشكلة التي واجهتها (`التعليق`) سببها أن العملية توقفت — الحل هو استضافة تعيد التشغيل تلقائياً.

**ملفات جاهزة في الجذر:** `Dockerfile` • `Procfile` • `railway.json` • `fly.toml` • `.dockerignore`

---

## ⚠️ اقرأ هذا قبل الاختيار (بصدق)

| المنصة | مجاني؟ | ينام؟ | ملاحظة |
|---|---|---|---|
| **Railway** | رصيد تجريبي ~5$ ثم مدفوع (~5$/شهر) | لا ينام | **الأسهل والأنسب** — دعم Docker و CLI مباشر |
| **Fly.io** | رصيد مجاني محدود | لا ينام | يحتاج بطاقة للتحقق، ممتاز وأرخص |
| **Render (Free)** | نعم | **ينام بعد 15 دقيقة خمول** ⛔ | **غير مناسب** لبوت تلجرام — سيتوقف عن الرد |
| **VPS (Hetzner)** | ~4€/شهر | لا ينام | الأرخص على المدى الطويل والأكثر تحكماً |

**الخلاصة:** إن أردت مجاناً تماماً وبلا انقطاع → لا يوجد حل موثوق. الأقرب: Fly.io برصيد مجاني، أو VPS بـ 4 يورو.

---

## الطريقة أ — Railway عبر CLI (بلا GitHub — الأسرع)

لا تحتاج إصلاح صلاحية GitHub. نفّذ من **جهازك**:

```bash
# 1) ثبّت الأداة
npm install -g @railway/cli        # أو: brew install railway
# بديل بلا npm:
bash <(curl -fsSL cli.new)

# 2) سجّل الدخول (يفتح المتصفح)
railway login

# 3) داخل مجلد المشروع
cd futures-signal-bot
railway init            # أنشئ مشروعاً جديداً وسمّه trendpulse

# 4) أضف الأسرار (المتغيّرات) — نفّذها واحدة واحدة
railway variables --set "TELEGRAM_BOT_TOKEN=توكنك"
railway variables --set "TELEGRAM_CHAT_ID=5265226396"
railway variables --set "ALLOWED_CHAT_IDS=5265226396"
railway variables --set "EXCHANGE=binanceusdm"
railway variables --set "ALLOW_LIVE="

# 5) انشر
railway up

# 6) راقب السجل مباشرة
railway logs
```

عند نجاح النشر سترى في السجل:
```
البوت يعمل…
Application started
تم تحديث القائمة: 40 زوج
```

---

## الطريقة ب — Fly.io

```bash
# 1) ثبّت flyctl
curl -L https://fly.io/install.sh | sh

# 2) سجّل (يحتاج بطاقة للتحقق، لكن الرصيد المجاني يغطي بوتاً صغيراً)
fly auth signup      # أو fly auth login

# 3) عدّل الاسم في fly.toml (سطر app) ثم:
fly apps create trendpulse-tahar     # استخدم نفس الاسم

# 4) الأسرار
fly secrets set TELEGRAM_BOT_TOKEN="توكنك" \
                TELEGRAM_CHAT_ID="5265226396" \
                ALLOWED_CHAT_IDS="5265226396" \
                EXCHANGE="binanceusdm"

# 5) انشر (نسخة واحدة فقط — مهم لمنع تعارض 409)
fly deploy --ha=false

# 6) راقب
fly logs
```

---

## الطريقة ج — GitHub + Railway (إن أصلحت الصلاحية)

1. على https://github.com/settings/tokens → الرمز → **Edit** → **Contents = Read and write** → **Save**.
2. أنشئ المستودع `Boy-telegram` من https://github.com/new (بلا README).
3. ارفع المشروع:
```bash
git remote add origin https://github.com/TaharBn12/Boy-telegram.git
git push -u origin main
```
4. في Railway: **New Project → Deploy from GitHub repo** → اختر المستودع → أضف المتغيّرات → Deploy.

---

## المتغيّرات المطلوبة (كل المنصات)

| المتغيّر | إلزامي | القيمة |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | توكن BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | `5265226396` |
| `ALLOWED_CHAT_IDS` | ✅ | `5265226396` (أو `*` للجميع — غير مستحسن) |
| `EXCHANGE` | ⬜ | `binanceusdm` (افتراضي) أو `okx` إن كانت Binance محجوبة |
| `ALLOW_LIVE` | ⬜ | **اتركه فارغاً** — فارغ = Testnet فقط |
| `EXCHANGE_API_KEY` / `EXCHANGE_API_SECRET` | ⬜ | فقط للتنفيذ الآلي |

> **لا ترفع `.env` إلى GitHub أبداً** — المنصات تأخذ المتغيّرات من إعداداتها.

---

## قواعد مهمة بعد النشر

1. **نسخة واحدة فقط** من البوت. نسختان على نفس التوكن = `409 Conflict` وأوامر تضيع. (لذلك `numReplicas: 1` و`--ha=false`).
2. **التخزين مؤقت**: على Railway الحاوية تُعاد عند كل نشر فتضيع `data/signals.jsonl` (سجل الإشارات). على Fly عالجتها بـ `[mounts]` (وحدة تخزين دائمة). على Railway: أضف **Volume** من لوحة المشروع واربطه بـ `/app/data`.
3. **المنطقة**: اختر أقرب منطقة لأوروبا (`cdg` باريس / `fra` فرانكفورت). Binance تحجب بعض المناطق، وأوروبا تعمل.
4. **راقب السجل** أول 10 دقائق بعد النشر للتأكد من عدم وجود `409` أو `451`.

## التحقق بعد النشر
1. افتح البوت واضغط `/ping` → يجب أن يرد فوراً.
2. اضغط **📈 صفقات ممكنة** → خطة صفقة كاملة.
3. إن لم يرد: `railway logs` أو `fly logs` وأرسل لي آخر الأسطر.
