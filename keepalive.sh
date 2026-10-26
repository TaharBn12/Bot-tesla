#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════
#  مشرف دائم: يضمن نسخة واحدة فقط تعمل، ويعيد التشغيل عند أي توقف
#  الاستخدام:  setsid bash keepalive.sh > data/keepalive.log 2>&1 &
#  الإيقاف:    bash keepalive.sh stop
# ════════════════════════════════════════════════════════════
cd "$(dirname "$0")" || exit 1
mkdir -p data
PIDFILE=data/keepalive.pid
BOTLOG=data/bot.log

log() { echo "[$(date '+%F %T')] $*" | tee -a data/keepalive.log; }

if [ "$1" = "stop" ]; then
  for p in $(pgrep -f "keepalive.sh" ) $(pgrep -f "run.sh") $(pgrep -f "src/bot.py"); do
    [ "$p" != "$$" ] && kill -9 "$p" 2>/dev/null
  done
  rm -f "$PIDFILE"
  log "أُوقف كل شيء"
  exit 0
fi

# نسخة واحدة من المشرف
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
  log "المشرف يعمل بالفعل (pid $(cat "$PIDFILE")) — لا أشغّل نسخة ثانية"
  exit 0
fi
echo $$ > "$PIDFILE"

cleanup() {
  rm -f "$PIDFILE"
  for p in $(pgrep -f "src/bot.py"); do kill -9 "$p" 2>/dev/null; done
  log "المشرف توقف — أوقفت البوت"
}
trap cleanup EXIT INT TERM

log "بدء المشرف (pid $$)"
N=0
while true; do
  N=$((N+1))
  # لا تسمح بنسختين أبداً (409 Conflict)
  COUNT=$(pgrep -fc "python3 src/bot.py" || true)
  if [ "${COUNT:-0}" -gt 1 ]; then
    log "⚠️ $COUNT نسخ تعمل — أوقف الزائدة"
    for p in $(pgrep -f "python3 src/bot.py" | tail -n +2); do kill -9 "$p" 2>/dev/null; done
  fi

  if ! pgrep -f "python3 src/bot.py" > /dev/null 2>&1; then
    log "تشغيل البوت (دورة $N)"
    PYTHONUNBUFFERED=1 EXCHANGE="${EXCHANGE:-okx}" python3 src/bot.py >> "$BOTLOG" 2>&1 &
    BOTPID=$!
    echo "$BOTPID" > data/bot.pid
  fi
  sleep 20
done
