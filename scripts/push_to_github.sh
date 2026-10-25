#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  رفع المشروع إلى GitHub — نفّذه من جهازك أنت (لا يحتاج أي أسرار هنا)
#
#  الاستخدام:
#     1) حمّل مجلد المشروع من مساحة العمل إلى جهازك
#     2) cd futures-signal-bot
#     3) bash scripts/push_to_github.sh
#
#  سيسألك عن اسم المستخدم و«رمز الوصول» (PAT) — لا تكتب كلمة مرور GitHub،
#  فGitHub لم يعد يقبلها. أنشئ رمزاً من:
#     https://github.com/settings/tokens  →  Fine-grained token
#     Repository access: Only select repositories → Boy-telegram
#     Permissions: Contents = Read and write
#     Expiration: 7 أيام (ثم احذفه)
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

REPO_URL="${1:-https://github.com/TaharBn12/Boy-telegram.git}"
BRANCH="${2:-main}"

echo "▶ التحقق من الأسرار قبل الرفع…"
if [ -f .env ]; then
  echo "⛔ يوجد ملف .env في المجلد — سيُستبعد تلقائياً بفضل .gitignore، لكن تأكد أنه غير مُتتبَّع:"
  git ls-files | grep -x ".env" && { echo "⛔ .env مُتتبَّع! أوقفت العملية."; exit 1; } || echo "✅ .env غير متتبَّع"
fi
if [ -d .git ] && git ls-files | grep -q "^data/cache/"; then
  echo "⛔ بيانات الكاش متتبَّعة — أوقفت العملية (ملفات ثقيلة)."; exit 1
fi

if [ ! -d .git ]; then
  echo "▶ تهيئة المستودع…"
  git init -b "$BRANCH"
  git add -A
  git commit -m "TrendPulse v1: بوت إشارات تلجرام + تنفيذ آلي مُختبر"
fi

if ! git remote | grep -qx origin; then
  git remote add origin "$REPO_URL"
else
  git remote set-url origin "$REPO_URL"
fi

echo "▶ الدفع إلى $REPO_URL (فرع $BRANCH)…"
echo "   سيُطلب منك: Username = TaharBn12  |  Password = رمز PAT (يظهر فارغاً أثناء الكتابة، وهذا طبيعي)"
git push -u origin "$BRANCH"

echo
echo "✅ تم الرفع: ${REPO_URL%.git}"
echo "   تذكير: احذف رمز PAT من إعدادات GitHub بعد الانتهاء إن لم تكن بحاجة إليه."
