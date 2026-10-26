# 🔑 دليل مفاتيح Binance Futures — TrendPulse

> **الخلاصة أولاً:** للإشارات **لا تحتاج أي مفتاح**. المفاتيح تُحتاج فقط للتنفيذ الآلي،
> والبداية الصحيحة **Testnet** (مال وهمي) قبل أي دولار حقيقي.

---

## ١) Testnet — ابدأ من هنا (لا يحتاج هوية ولا إيداع)

1. افتح **https://testnet.binancefuture.com**
2. سجّل الدخول بـ **GitHub** أو **بريد إلكتروني** (لا KYC، لا هاتف)
3. في الأسفل تجد **API Key** → اضغط **Generate**
4. انسخ الاثنين واحفظهما فوراً:
   - **API Key**
   - **Secret Key** ← يظهر **مرة واحدة فقط**، إن فقدته احذف المفتاح وأنشئ غيره
5. الرصيد الوهمي (عادة ~15,000 USDT) يظهر في **Wallet → Futures**
   - إن كان صفراً: **https://testnet.binancefuture.com/en/futures** ثم زر **Get Test Funds**

### ضعه في البوت
```bash
# في ملف .env
EXCHANGE_API_KEY=المفتاح_هنا
EXCHANGE_API_SECRET=السر_هنا
# ALLOW_LIVE يبقى فارغاً = Testnet
```

### تحقّق قبل التشغيل
```bash
python src/testnet_check.py --place-order
```
يجب أن تظهر كل الفحوصات ✅ — يفتح صفقة صغيرة ويغلقها ويتأكد من الستوب.

---

## ٢) Binance الحقيقي — لاحقاً فقط، بعد نجاح Testnet

1. ادخل **https://www.binance.com** → أيقونة الحساب → **API Management**
   (الرابط المباشر: https://www.binance.com/en/my/settings/api-management)
2. **Create API** → اختر **System generated** → اسم مثل `TrendPulse`
3. تحقّق: بريد إلكتروني + رمز Google Authenticator
4. **⚠️ لا تفعّل أبداً** خيار `Enable Withdrawals` — البوت لا يحتاجه إطلاقاً
5. فعّل فقط:
   - ✅ `Enable Futures`
   - ✅ `Enable Reading`
   - ❌ `Enable Spot & Margin Trading` (اتركه مغلقاً إن أمكن)
   - ❌ `Enable Withdrawals` ← **مغلق دائماً**
6. **Restrict access to trusted IPs only** → أضف IP السيرفر
   - IP Railway غير ثابت في الخطة المجانية → اتركه مفتوحاً مؤقتاً **مع** إغلاق السحب
7. أكّد بـ 2FA وانقر **Save** → انسخ المفتاح والسر

### فعّله في البوت (تصريحان معاً — لا واحد)
```yaml
# config.yaml
mode: autotrade
```
```bash
# .env
EXCHANGE_API_KEY=...
EXCHANGE_API_SECRET=...
ALLOW_LIVE=true
```
> بدون `ALLOW_LIVE=true` يبقى البوت على Testnet حتى لو كان `mode: autotrade`.

---

## ٣) 🔴 قاعدة الأمان الأولى

**صلاحية سحب = صفر.** مفتاح التداول وحده لا يستطيع تحويل أموالك.
لو سرق أحدهم المفتاح: أسوأ ما يحدث هو فتح صفقات خاسرة — لا سحب.

| الخيار | القيمة الصحيحة |
|---|---|
| Enable Reading | ✅ نعم |
| Enable Futures | ✅ نعم |
| Enable Withdrawals | ❌ **أبداً** |
| IP Whitelist | ✅ كلما أمكن |

---

## ٤) أين تُخزَّن المفاتيح؟

| البيئة | المكان | ملاحظة |
|---|---|---|
| جهازك | `.env` | مستبعد في `.gitignore` — لا يُرفع أبداً |
| GitHub Actions | Secrets → Actions | `EXCHANGE_API_KEY` / `EXCHANGE_API_SECRET` |
| Railway | Variables → الخدمة | يُدفع تلقائياً من GitHub عند كل نشر |

⚠️ **لا تكتب المفتاح في أي ملف داخل المستودع.** فحص الأسرار في `ci.yml` يرفض الدفعة إن وجد رمزاً.

---

## ٥) مشكلة الحجب (451) — اقرأها قبل أن تتعجب

Binance يحجب عناوين أمريكية وعدة دول. أثناء تطوير هذا المشروع أثبت الاختبار:

```
Binance: محجوب من هذه المنطقة (451)
OKX:     متاح
```

**ماذا يعني لك:**
- من **جهازك في الجزائر**: جرّب أولاً — قد يعمل مباشرة
- على **Railway** (خوادم أمريكية غالباً): سترى 451 أيضاً
- **الحل:** `EXCHANGE=okx` في متغيرات البيئة (البوت يدعم الاثنين)

```bash
EXCHANGE=okx python src/bot.py
```

> الإشارات تعمل على OKX بنفس الاستراتيجية. المفاتيح تبقى Binance فقط إن أردت تنفيذ Testnet.

---

## ٦) كيف تعرف أن المفاتيح تعمل؟

```bash
python src/testnet_check.py
```

| الفحص | معناه عند الفشل |
|---|---|
| المفاتيح صالحة | المفتاح من Binance الحقيقي لا Testnet (أو العكس) |
| وضع المركز One-way | فعّل One-way من تفضيلات Binance وإلا فشل `reduceOnly` |
| ضبط الرافعة | ارفع سقف الرافعة يدوياً في المنصة |
| أمر ستوب closePosition | المشكلة في صلاحية أو دقة الزوج |

---

## 🔒 ملاحظات أمان من هذه الجلسة

- توكن تلجرام ظهر في محادثة مفتوحة → **أعد توليده** عبر `@BotFather` → `/revoke` → ثم حدّث السر `TELEGRAM_BOT_TOKEN`
- رمز GitHub الكلاسيكي `ghp_…` ما زال صالحاً بصلاحيات `delete_repo` → **احذفه** من https://github.com/settings/tokens
- لا تشارك `Secret Key` مع أي شخص أو خدمة — لا حتى مع مساعد
