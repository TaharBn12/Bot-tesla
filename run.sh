#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════
#  مشرف التشغيل: يعيد تشغيل البوت تلقائياً إذا مات
#  الاستخدام:  bash run.sh
#  السجل:      data/bot.log   (تابع بـ: tail -f data/bot.log)
# ════════════════════════════════════════════════════════════
cd "$(dirname "$0")" || exit 1
mkdir -p data
export PYTHONUNBUFFERED=1
export EXCHANGE="${EXCHANGE:-okx}"

# منع تشغيل نسختين على نفس التوكن (يسبب 409 Conflict وتضيع الأوامر)
if pgrep -f "src/bot.py" > /dev/null 2>&1; then
  echo "⚠️ نسخة أخرى من البوت تعمل — أوقفها أولاً:  pkill -f src/bot.py"
  exit 1
fi

ATTEMPT=0
while true; do
  ATTEMPT=$((ATTEMPT+1))
  echo "───── [$(date '+%F %T')] تشغيل البوت (محاولة $ATTEMPT) ─────" | tee -a data/bot.log
  python3 src/bot.py >> data/bot.log 2>&1
  CODE=$?
  echo "[$(date '+%F %T')] توقف البوت (رمز $CODE) — إعادة التشغيل بعد 5 ثوانٍ" | tee -a data/bot.log
  sleep 5
done
